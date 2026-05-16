"""Layer-3 Kani formal-verification runner for Anchor programs — isolated.

Same isolation pattern as :mod:`audit_pipeline.anchor_litesvm_runner`:
nothing is ever written to the audited repository. The harness lives
under ``<cycle>/kani/<finding>/`` in its own sidecar Cargo workspace,
``cargo kani`` runs there, and the only contact with the target repo
is a read-only copy of the program crate (done via
:func:`audit_pipeline.anchor_builder.build_anchor_program` semantics —
but here we keep the source for Kani to compile against rather than
producing a BPF .so).

Kani for Anchor is honest about its limits. The LLM author is told to
emit a real, runnable Kani harness when the bug class admits one
(arithmetic / state-machine / close-without-zero / monotonicity /
cast-truncation), and to emit the literal sentinel
``// CANNOT_VERIFY: <reason>`` when the bug class would require
modelling the Solana runtime or Anchor's framework macros (the
account-validation bug classes — Signer vs AccountInfo, has_one,
seeds+bump, owner-check absent). Findings that hit CANNOT_VERIFY are
recorded so the cycle narrative can cite the reason; they are not
silently dropped.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit_pipeline.anchor_builder import _solana_augmented_path


# Sidecar Cargo workspace for one Kani harness. The proofs crate is
# self-contained (no path deps on the audit target) so isolation is
# total — the LLM author embeds whatever program-fragment it needs to
# reason about as plain Rust inside the harness file.
_KANI_SIDECAR_CARGO_TOML = """\
# Synthesised by audit_pipeline.anchor_kani_runner — Kani sandbox crate.
# Lives entirely under <cycle>/kani/<finding>/. Target repo is read-only.
[package]
name = "anchor-kani-proofs-{slug}"
version = "0.0.1"
edition = "2021"

[lib]
path = "src/lib.rs"

[dependencies]
"""

_KANI_SIDECAR_LIB_RS = """\
//! Sidecar crate root for one L3 Kani proof. The proof body lives in
//! `proofs.rs` and is conditionally compiled under `cfg(kani)`.

#[cfg(kani)]
pub mod proofs;
"""


@dataclass
class KaniProofOutcome:
    """Outcome of one L3 Kani proof attempt for one hypothesis."""

    hyp_id: str
    harness_path: Path | None
    sidecar_dir: Path
    compile_rc: int | None
    kani_rc: int | None
    proved: bool
    counterexample: bool
    cannot_verify: bool
    reason: str = ""
    log_path: Path | None = None
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def outcome_label(self) -> str:
        if self.cannot_verify:
            return "cannot_verify"
        if self.compile_rc is not None and self.compile_rc != 0:
            return "compile_error"
        if self.proved:
            return "verification_successful"
        if self.counterexample:
            return "verification_failed_counterexample"
        if self.kani_rc is None:
            return "not_run"
        return "indeterminate"


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower()
    return s or "anon"


def write_kani_sidecar(
    *,
    sidecar_dir: Path,
    slug: str,
    harness_body: str,
) -> Path:
    """Write the sidecar Cargo workspace with a Kani harness inside.

    Returns the path to the proofs file.
    """
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    src_dir = sidecar_dir / "src"
    src_dir.mkdir(exist_ok=True)
    (sidecar_dir / "Cargo.toml").write_text(
        _KANI_SIDECAR_CARGO_TOML.format(slug=slug), encoding="utf-8",
    )
    (src_dir / "lib.rs").write_text(_KANI_SIDECAR_LIB_RS, encoding="utf-8")
    proofs_path = src_dir / "proofs.rs"
    proofs_path.write_text(harness_body, encoding="utf-8")
    return proofs_path


def run_kani_proof(
    *,
    sidecar_dir: Path,
    harness_name: str,
    timeout_s: int = 1800,
) -> tuple[int, str]:
    """Compile + verify one harness in the sidecar workspace.

    Returns ``(returncode, combined_log)``. ``cargo kani`` can take
    minutes; default timeout is 30m per harness.
    """
    env = os.environ.copy()
    env["PATH"] = _solana_augmented_path()
    cargo_bin = shutil.which("cargo", path=env["PATH"]) or "cargo"
    proc = subprocess.run(
        [cargo_bin, "kani", "--harness", harness_name],
        cwd=str(sidecar_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    combined = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    return proc.returncode, combined


# ---------------------------------------------------------------------------
# Kani output classifier
# ---------------------------------------------------------------------------


_KANI_SUCCESS_RE = re.compile(r"VERIFICATION:- SUCCESSFUL", re.MULTILINE)
_KANI_FAILED_RE = re.compile(r"VERIFICATION:- FAILED", re.MULTILINE)
_KANI_COUNTER_RE = re.compile(r"Failed Checks:\s+(.+)", re.MULTILINE)


def parse_kani_outcome(log: str) -> tuple[bool, bool, str]:
    """Classify the cargo kani log.

    Returns ``(proved, counterexample, reason)``.
    """
    if re.search(r"^error: could not compile", log, re.MULTILINE):
        return False, False, "kani harness failed to compile"
    if re.search(r"^error\[E\d+\]", log, re.MULTILINE) and "VERIFICATION:" not in log:
        return False, False, "rustc error in harness before verification"
    if _KANI_SUCCESS_RE.search(log):
        return True, False, "Kani proved the post-patch invariant"
    if _KANI_FAILED_RE.search(log):
        m = _KANI_COUNTER_RE.search(log)
        cex = m.group(1).strip() if m else "Kani found a counterexample"
        return False, True, cex[:280]
    return False, False, "kani produced no terminal verdict (timeout / infra?)"


# ---------------------------------------------------------------------------
# LLM author — Anchor Kani prompt + compile-iterate prompt
# ---------------------------------------------------------------------------


def build_anchor_l3_prompt(
    *,
    hyp_id: str,
    claim: str,
    bug_class: str,
    target_file: str,
    program_name: str,
    program_source: str,
    harness_name: str,
) -> str:
    """Construct the L3 Kani authoring prompt for one Anchor hypothesis.

    The prompt explicitly enumerates the bug classes Kani CAN prove vs
    the ones that legitimately require ``CANNOT_VERIFY``. We do not want
    the LLM hallucinating Solana-runtime stubs.
    """
    return f"""\
# Layer-3 Kani formal-verification author (Anchor mode)

You author ONE Kani harness that formally proves (via the cargo-kani
model checker) the post-patch invariant for the bug described below.
The harness is symbolic — Kani explores all reachable inputs that
satisfy the harness's `kani::any()` constraints. On the buggy code the
proof must FAIL (a counterexample input violates the invariant). On
the patched code the proof must SUCCEED (the invariant holds for every
input).

## Hypothesis under test

- Hyp ID:    {hyp_id}
- Bug class: {bug_class}
- Claim:     {claim}
- Source:    {target_file}

## Program source (read every line)

```rust
{program_source}
```

## Sidecar Cargo workspace (already wired)

The harness ships in a self-contained sidecar crate. NO external
crates are available beyond Rust std and the implicit `kani` runtime
when `--cfg kani` is set. You CANNOT `use anchor_lang::...` or
`use {program_name}::...` — those are not in the dep tree (by design,
to keep the harness isolated from the audited workspace).

## Authoring rules

1. **Output only the contents of `src/proofs.rs`** — start with module-
   level docs (`//! ...`) and end with the final closing brace. No
   markdown fences, no commentary.

2. The harness fn MUST be `pub fn {harness_name}()` annotated with
   `#[kani::proof]`. Optionally `#[kani::unwind(N)]` if loops need
   bounding.

3. Use `let x: T = kani::any();` to introduce symbolic inputs. Bound
   them with `kani::assume(...)` when the program's preconditions
   constrain them.

4. **Bug-class strategies — Kani CAN verify all of these. No
   handwaves, no CANNOT_VERIFY for account-validation or auth-gate
   bugs. You MUST build the minimal stubs needed.**

   a) ARITHMETIC / OVERFLOW / TRUNCATION (e.g. unchecked add,
      saturating_sub silently returning 0, u128→u64 cast loss):
      reconstruct the relevant arithmetic in plain Rust inside the
      harness, introduce symbolic inputs, and assert the post-patch
      invariant (e.g. `assert!(amount <= balance, ...)` for the
      subtraction case). Kani enumerates all u64 pairs and either
      proves the property holds or returns a counterexample.

   b) STATE-MACHINE / CLOSE-WITHOUT-ZERO / CLOCK-MONOTONICITY:
      reconstruct the state-transition function in plain Rust,
      introduce symbolic pre-state, call the function, assert the
      consistency invariant on post-state.

   c) ACCOUNT-VALIDATION (Signer vs AccountInfo) — Kani-verifiable
      via STUB MODEL. Define this small model at the top of the
      harness:

      ```rust
      #[derive(Clone, Copy)]
      struct AccountInfoStub {{
          key: [u8; 32],
          is_signer: bool,
          owner: [u8; 32],
      }}
      // Anchor's Signer<'info> is just an AccountInfo with the
      // RUNTIME-enforced invariant is_signer == true. Wrap that as
      // a constructor that aborts if is_signer is false:
      struct SignerStub(AccountInfoStub);
      impl SignerStub {{
          fn try_from(a: AccountInfoStub) -> Result<SignerStub, ()> {{
              if a.is_signer {{ Ok(SignerStub(a)) }} else {{ Err(()) }}
          }}
      }}
      ```

      Then reconstruct the relevant Accounts struct (mirroring the
      program's `#[derive(Accounts)]` block) using `AccountInfoStub`
      for the buggy field and `SignerStub` for the patched field.
      Drive symbolic inputs (`let is_signer: bool = kani::any();`),
      run the same body the program runs (the pubkey-equality check
      from `withdraw` etc.), and assert the post-patch invariant.

      Example for SOL34 (vault admin AccountInfo not Signer) — this
      harness models the BUGGY CODE (no signer gate) and asserts the
      post-patch invariant. Kani must find a counterexample on the
      current code; on the patched code (signer gate present) the
      same invariant would hold.

      ```rust
      #[kani::proof]
      fn proof_sol34_admin_must_sign() {{
          // Symbolic inputs the runtime can pass
          let admin_is_signer: bool = kani::any();
          let admin_key: [u8; 32] = kani::any();
          let stored_admin: [u8; 32] = kani::any();

          // The program's Withdraw body runs once accounts are loaded.
          // BUGGY CODE: admin is AccountInfo<'info>. No is_signer gate
          // exists. The only constraint on the path is the pubkey
          // equality check inside the body:
          kani::assume(admin_key == stored_admin);

          // Buggy body executes — there is no signer enforcement.
          let body_ran = true;

          // Post-patch invariant: withdraw body MUST only run when the
          // admin actually signed the tx. Asserting that no reachable
          // path exists where the body runs without the signer flag.
          if body_ran {{
              assert!(
                  admin_is_signer,
                  "post-patch invariant: withdraw body executed but admin \
                   did not sign — SOL34, programs/vault/src/lib.rs:113 \
                   (admin declared as AccountInfo<'info> instead of \
                   Signer<'info>)."
              );
          }}
      }}
      ```

      Kani enumerates `admin_is_signer` over {{true, false}} and finds
      the input `admin_is_signer = false` which satisfies the
      assumption (`admin_key == stored_admin`) and reaches the assert
      with the assertion FALSE → counterexample = bug demonstrated.
      After the patch (`admin: Signer<'info>`), the runtime gate
      removes `admin_is_signer = false` from the reachable input set,
      and the same harness re-run on the patched program would prove
      the invariant.

      Same shape applies for missing has_one, missing seeds+bump,
      owner-check absent — stub the relevant Anchor constraint as a
      function/check, model the BUGGY code (omit the check), and
      assert the post-patch invariant.

   d) AUTHORISATION / GATE-MISSING (e.g. function admits unsigned
      caller because the gate is absent): same modelling as (c).
      Build a tiny stub of the missing gate (`fn check_admin_sig(
      info: AccountInfoStub) -> Result<(), ()>`), have the harness
      call it on a symbolic AccountInfoStub, and assert the
      post-patch invariant. No CANNOT_VERIFY.

5. The assertion in your harness MUST encode the *post-patch*
   invariant — the property that holds once the bug is fixed. On the
   current buggy code, Kani should find a counterexample
   (`VERIFICATION:- FAILED` with a `Failed Checks:` line). On the
   patched code, Kani should prove the property (`VERIFICATION:-
   SUCCESSFUL`).

6. Do NOT fabricate APIs (no `use anchor_lang::...`, no `use
   solana_program::...`). Build stubs as plain Rust structs/fns
   inside the harness file as shown above. If your first attempt
   uses fabricated imports, the compile-iterate prompt will tell you
   to replace them with stubs.

7. Keep the harness short (under 250 lines including stub
   definitions). Use `kani::assume` early and `assert!` once or
   twice. Multi-loop harnesses blow up verification time and rarely
   produce useful results.

## Output

Begin immediately with the first character of `src/proofs.rs`. End
with the closing brace of the harness function. No extra prose.
"""


def build_kani_compile_fix_prompt(
    *,
    original_prompt: str,
    previous_attempt: str,
    compile_log: str,
) -> str:
    """Ask the LLM to fix Kani harness compile errors."""
    error_lines: list[str] = []
    capture = 0
    for line in compile_log.splitlines():
        s = line.rstrip()
        if s.startswith("error[") or s.startswith("error:") or s.startswith("  --> "):
            capture = 4
        if capture > 0:
            error_lines.append(s)
            capture -= 1
    trimmed = "\n".join(error_lines[:120])

    return f"""\
Your previous Kani harness did not compile. Fix the compile errors and
re-emit the FULL file.

## Original task

{original_prompt}

## Your previous attempt

```rust
{previous_attempt}
```

## Cargo kani / rustc errors

```
{trimmed}
```

## Fix rules

1. Output the COMPLETE corrected file (not a diff). Same output rules
   as the original — no markdown fences, no prose.
2. The sidecar dep tree is intentionally minimal: ONLY Rust std and
   the implicit `kani::` runtime are available. Do NOT add `use`
   statements pulling in `anchor_lang`, `solana_program`, the program
   crate, or anything else — they are not in scope.
3. The most common fix is removing fabricated imports. Replace them
   with inline Rust definitions of the structs / functions you need.
4. Account-validation and auth-gate bugs ARE verifiable — build the
   stub model from the original prompt's example (AccountInfoStub +
   SignerStub or a `fn check_admin_sig(...)` gate stub). Do NOT
   emit CANNOT_VERIFY for those classes.
"""


def parse_llm_response(text: str) -> str:
    """Strip markdown fences if the LLM wrapped the output."""
    txt = text.strip()
    m = re.match(r"^```(?:rust|rs)?\s*\n(.*?)\n```\s*$", txt, re.DOTALL)
    if m:
        return m.group(1)
    return txt


def is_cannot_verify(body: str) -> tuple[bool, str]:
    """Detect the CANNOT_VERIFY sentinel.

    Returns ``(is_sentinel, reason)``. ``reason`` is the text after
    the colon (empty if not present).
    """
    if not body:
        return False, ""
    first_line = body.lstrip().split("\n", 1)[0]
    m = re.match(r"^//\s*CANNOT_VERIFY\s*[:\-]?\s*(.*)$", first_line)
    if m:
        return True, m.group(1).strip()
    return False, ""


__all__ = [
    "KaniProofOutcome",
    "build_anchor_l3_prompt",
    "build_kani_compile_fix_prompt",
    "is_cannot_verify",
    "parse_kani_outcome",
    "parse_llm_response",
    "run_kani_proof",
    "write_kani_sidecar",
    "_slugify",
]
