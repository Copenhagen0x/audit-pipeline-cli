"""Layer-4 LiteSVM runtime witness for Anchor programs — isolated.

Builds an Anchor program in a sandbox (no contact with the audit
target's workspace — see :mod:`audit_pipeline.anchor_builder`), then
runs an LLM-authored LiteSVM test that loads the resulting .so and
constructs a runtime exploit transaction.

Each test lives under ``<cycle>/litesvm/`` in a single-purpose sidecar
Cargo workspace. Nothing is ever written into the audited repository.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit_pipeline.anchor_builder import (
    _solana_augmented_path,
)

# Sidecar workspace Cargo.toml template. anchor-lang version is pinned
# to match the program's own dependency; the rest of the deps are the
# minimum surface needed to construct + send a transaction in LiteSVM.
_SIDECAR_CARGO_TOML = """\
# Synthesised by audit_pipeline.anchor_litesvm_runner — sandbox crate.
# Lives entirely under <cycle>/litesvm/. Target repo is read-only.
[package]
name = "anchor-litesvm-tests"
version = "0.0.1"
edition = "2021"

[lib]
path = "src/lib.rs"

[features]
default = []

# Solana's SDK was split into many small crates that are currently on
# different majors (e.g. solana-sdk 4.x but solana-keypair 3.x). The
# umbrella ``solana-sdk`` crate re-exports the surface the test needs;
# pinning the small crates separately causes resolver mismatches. The
# LLM author is told to import everything from ``solana_sdk``.
[dependencies]
# litesvm 0.12 pulls solana-* crates at v3.x — pinning solana-sdk to v3
# keeps the resolver happy. Bump in lockstep with litesvm.
litesvm = "0.12"
solana-sdk = "3"
borsh = "1"
sha2 = "0.10"
{tests}
"""

_SIDECAR_LIB_RS = """\
//! Sidecar crate root for L4 LiteSVM tests. Tests live as integration
//! tests next to this file.
"""

# Each PoC registered as an integration test target. cargo auto-
# discovers `tests/*.rs` but our PoC files are at the crate root with
# the prefix `test_*`, so explicit registration keeps things simple.
_TEST_TARGET_TEMPLATE = """\
[[test]]
name = "{name}"
path = "{rel_path}"
harness = true
"""


@dataclass
class LiteSVMTestOutcome:
    """Outcome of a single L4 LiteSVM run for one hypothesis."""

    hyp_id: str
    test_path: Path
    sidecar_dir: Path
    program_built: bool
    program_so_path: Path | None
    build_log_path: Path | None
    compile_rc: int | None
    run_rc: int | None
    fired: bool
    outcome: str
    reason: str = ""
    test_log_path: Path | None = None
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower()
    return s or "anon"


def _detect_program_id(program_src: Path) -> str | None:
    """Extract the declare_id! string from a program's lib.rs."""
    lib_rs = program_src / "src" / "lib.rs"
    if not lib_rs.is_file():
        return None
    txt = lib_rs.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'declare_id!\(\s*"([^"]+)"\s*\)', txt)
    return m.group(1) if m else None


def resolve_program_for_hyp(
    *,
    target_file: str,
    scaffold_path: Path | None,
    anchor_programs: list[str],
) -> tuple[str | None, str]:
    """Resolve which Anchor program crate a hypothesis applies to.

    The hyp library uses ``programs/*/src/lib.rs`` as a wildcard
    ``target_file`` for class-level bugs (e.g. "any program with
    admin declared as AccountInfo"). When the L2 PoC author runs,
    it picks ONE concrete program where the bug manifests and
    cites it in the PoC body. This resolver chains:

      1. If ``target_file`` is fully qualified
         (``programs/<name>/src/lib.rs`` with no wildcard) and
         ``<name>`` is a real program crate in ``anchor_programs``,
         use it. This is the authoritative path for specific hyps.

      2. Otherwise, read the L2 PoC body at ``scaffold_path`` and
         extract the most-cited ``programs/<name>/...`` reference.
         The L2 PoC body always cites the exact source location
         where the bug was witnessed — that's the program L4 must
         build and exploit against.

      3. If neither yields a real program, return
         ``(None, reason)`` so the caller can record a skip with
         provenance instead of silently building "*".

    Returns ``(program_name, source)`` where ``source`` is one of
    ``"target_file"``, ``"poc_citation"``, or ``"unresolved"``.
    """
    tf = (target_file or "").replace("\\", "/")
    # 1. Specific target_file — no wildcards allowed.
    m = re.match(r"^programs/([^/*]+)/", tf)
    if m:
        cand = m.group(1)
        if cand in anchor_programs:
            return cand, "target_file"

    # 2. Read PoC scaffold body for the concrete program the L2
    #    author selected. Vote by citation count so a body that
    #    mentions vault three times and escrow once resolves to
    #    vault. Fall back to first occurrence on a tie.
    if scaffold_path is not None and scaffold_path.is_file():
        try:
            body = scaffold_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        if body:
            cites = re.findall(
                r"programs/([a-zA-Z][a-zA-Z0-9_]*)/src/lib\.rs",
                body,
            )
            if cites:
                from collections import Counter
                ranked = Counter(cites).most_common()
                for name, _count in ranked:
                    if name in anchor_programs:
                        return name, "poc_citation"

    # 3. Genuinely unresolved.
    return None, "unresolved"


def _gather_program_source(program_src: Path, max_bytes: int = 60_000) -> str:
    """Concatenate the program's Rust source for the L4 author prompt."""
    src_dir = program_src / "src"
    if not src_dir.is_dir():
        return ""
    parts: list[str] = []
    total = 0
    for rs in sorted(src_dir.rglob("*.rs")):
        try:
            body = rs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"// ========== {rs.relative_to(program_src).as_posix()} ==========\n"
        chunk = header + body + "\n"
        if total + len(chunk) > max_bytes:
            chunk = chunk[: max_bytes - total]
            parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def write_sidecar_workspace(
    *,
    sidecar_dir: Path,
    test_specs: list[tuple[str, Path]],
) -> Path:
    """Write a fresh sidecar Cargo workspace containing the given tests.

    ``test_specs`` is a list of ``(test_name, abs_path_to_test_rs)`` —
    each file is copied (or symlinked) into the sidecar's root and
    registered as a ``[[test]]`` target. Returns the sidecar root.
    """
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    src_dir = sidecar_dir / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "lib.rs").write_text(_SIDECAR_LIB_RS, encoding="utf-8")

    test_entries: list[str] = []
    for test_name, src_path in test_specs:
        dest = sidecar_dir / f"{test_name}.rs"
        try:
            shutil.copyfile(src_path, dest)
        except OSError:
            dest.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
        test_entries.append(
            _TEST_TARGET_TEMPLATE.format(name=test_name, rel_path=dest.name)
        )

    cargo_toml = sidecar_dir / "Cargo.toml"
    cargo_toml.write_text(
        _SIDECAR_CARGO_TOML.format(tests="".join(test_entries)),
        encoding="utf-8",
    )
    return sidecar_dir


def run_sidecar_test(
    *,
    sidecar_dir: Path,
    test_name: str,
    timeout_s: int = 900,
) -> tuple[int, str]:
    """Compile + run one test in the sidecar workspace.

    Returns ``(returncode, combined_log)``. The sidecar workspace is
    self-contained — this command never touches the audit target.
    """
    env = os.environ.copy()
    env["PATH"] = _solana_augmented_path()
    cargo_bin = shutil.which("cargo", path=env["PATH"]) or "cargo"
    proc = subprocess.run(
        [cargo_bin, "test", "--test", test_name, "--", "--nocapture", "--test-threads=1"],
        cwd=str(sidecar_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    combined = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    return proc.returncode, combined


_INFRA_PANIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # File-not-found / IO panics (the .so wasn't loaded, vault.so missing, etc.)
    (re.compile(r"Failed to read [\w./-]+\.so", re.IGNORECASE),
     "infra: .so file not readable"),
    (re.compile(r"failed to read [\w./-]+\.so", re.IGNORECASE),
     "infra: .so file not readable"),
    (re.compile(r"Os \{ code: 2,"),  # ENOENT
     "infra: ENOENT (file not found)"),
    (re.compile(r"NotFound,? message"),
     "infra: NotFound on filesystem read"),
    # LiteSVM setup panics (airdrop on un-funded keypair, etc.)
    (re.compile(r"called `Result::unwrap\(\)` on an `Err`.*airdrop",
                re.IGNORECASE | re.DOTALL),
     "infra: airdrop failed"),
    (re.compile(r"Failed to add_program|failed to add_program", re.IGNORECASE),
     "infra: add_program failed"),
    # Generic unwrap on infrastructure plumbing (very narrow — only when
    # the unwrap site is clearly setup code; we use the test-name file
    # line plus the literal panic phrase that flags missing prerequisites)
    (re.compile(r"called `Option::unwrap\(\)` on a `None` value.*latest_blockhash",
                re.IGNORECASE | re.DOTALL),
     "infra: latest_blockhash returned None"),
    # LiteSVM transaction-metadata setup failures. When the LLM author
    # writes `svm.send_transaction(init_tx).expect("initialize failed")`
    # and the tx FAILS at runtime, the .expect() bubbles up with
    # `FailedTransactionMetadata { err: ... }` in the message. The
    # exploit never reached the assertion. Any of the following error
    # categories appearing in the panic = setup failed, not bug fired.
    (re.compile(r"InsufficientFundsForRent", re.IGNORECASE),
     "infra: setup tx hit InsufficientFundsForRent"),
    (re.compile(r"InsufficientFundsForFee", re.IGNORECASE),
     "infra: setup tx hit InsufficientFundsForFee"),
    (re.compile(
        r"(?:initialize|deposit|airdrop|transfer|fund|setup|init)"
        r"\s+(?:tx\s+)?failed:\s*Err\(FailedTransactionMetadata",
        re.IGNORECASE | re.DOTALL,
    ),
     "infra: setup transaction failed"),
)


# Bug-witness phrases the L4 prompt instructs the LLM to put in the
# assertion message. The post-patch assertion is the ONLY place where
# any of these strings should appear in a passing test's output, so
# the parser uses them to discriminate between "test FAILED because
# the bug fired" vs "test FAILED because setup blew up."
_BUG_WITNESS_PATTERNS = (
    re.compile(r"post-patch invariant", re.IGNORECASE),
    re.compile(r"BUG\s*WITNESS\s*:", re.IGNORECASE),
    re.compile(r"\bBUG\s*:", re.IGNORECASE),
    re.compile(r"exploit\s+tx\s+(?:should|must|expected)", re.IGNORECASE),
    re.compile(r"should\s+have\s+been\s+rejected", re.IGNORECASE),
    re.compile(r"body\s+executed\s+but", re.IGNORECASE),
    re.compile(r"invariant\s+violated", re.IGNORECASE),
)


def _detect_infra_panic(log: str) -> tuple[bool, str]:
    """Return (is_infra_panic, reason) by matching known setup-failure
    patterns in the panic body. Used by ``parse_litesvm_outcome`` to
    reject FAILED outcomes that came from missing prerequisites rather
    than the bug witness."""
    for pat, label in _INFRA_PANIC_PATTERNS:
        if pat.search(log):
            return True, label
    return False, ""


def _panic_contains_bug_witness(log: str, test_name: str) -> bool:
    """Check if the panic body cites the bug-witness invariant.

    A real L4 fire panics inside the post-patch assertion the LLM
    author wrote. That assertion message contains a recognisable
    phrase (the hyp ID, "post-patch invariant", "should have been
    rejected", etc.). If the panic is from `.expect()` on a setup tx
    or some other infra unwrap, none of these phrases appear and the
    classifier rejects the FAILED outcome.
    """
    panic_idx = log.find("panicked at")
    if panic_idx < 0:
        return False
    panic_body = log[panic_idx:panic_idx + 1500]
    # Match any of the standard witness phrases
    for pat in _BUG_WITNESS_PATTERNS:
        if pat.search(panic_body):
            return True
    # Match the hyp ID (e.g. SOL34) derived from test_name. test_name
    # is shaped ``test_<slug>_litesvm`` where <slug> is the slugified
    # hyp_id. Extract a stem to match against the panic body.
    m = re.match(r"^test_([a-zA-Z0-9_]+?)(_litesvm)?$", test_name)
    if m:
        stem = m.group(1)
        # The slug looks like ``sol34_vault_withdraw_no_signer``. The
        # informative bit is the first token (e.g. ``sol34``). Match
        # case-insensitively.
        first_token = stem.split("_", 1)[0]
        if len(first_token) >= 3 and re.search(
            rf"\b{re.escape(first_token)}\b", panic_body, re.IGNORECASE,
        ):
            return True
    return False


def parse_litesvm_outcome(log: str, test_name: str) -> tuple[bool, str, str]:
    """Classify the LiteSVM cargo log.

    Returns ``(fired, outcome_label, reason)``. A LiteSVM bug-witness
    test is constructed so the assertion fails when the bug is present
    (e.g. ``assert!(result.is_err(), "{HYP_ID}: ...")``) and passes
    once the patch is applied. So a FAILED test = bug fires; OK = no
    bug (or patched).

    HOWEVER: a FAILED outcome can also come from infrastructure errors
    (missing .so file, airdrop failure, add_program failure). Those
    are NOT bug witnesses — the test never reached the exploit. The
    classifier rejects those via ``_detect_infra_panic``.
    """
    if re.search(r"^error: could not compile", log, re.MULTILINE):
        return False, "compile_error", "sidecar workspace failed to compile"
    if re.search(r"^error\[E\d+\]", log, re.MULTILINE):
        return False, "compile_error", "rustc error in sidecar"

    # If the test result is FAILED, classify in priority order:
    #   (1) Known infra panic (file-not-found, setup tx failed, etc.)
    #       → infra_panic, not a fire.
    #   (2) Panic body contains a bug-witness phrase / hyp ID
    #       → test_failed_bug_reproduced, real fire.
    #   (3) Otherwise → test_failed_unknown (still NOT a fire).
    # The (3) branch is conservative on purpose: we'd rather miss a
    # genuine fire than ship a false positive into L4 results.
    if "test result: FAILED" in log:
        is_infra, reason = _detect_infra_panic(log)
        if is_infra:
            return False, "infra_panic", reason
        if _panic_contains_bug_witness(log, test_name):
            return True, "test_failed_bug_reproduced", "LiteSVM exploit succeeded"
        return False, "test_failed_unknown", (
            "FAILED but panic body does not match any bug-witness phrase "
            "or hyp-id pattern — treating as setup/infrastructure error"
        )

    if "test result: ok" in log and "0 failed" in log:
        return False, "test_passed_no_bug", "exploit tx rejected (good)"
    return False, "unknown", "no terminal test_result line in log"


# ---------------------------------------------------------------------------
# LLM author — build the per-finding prompt and parse the response
# ---------------------------------------------------------------------------


def build_anchor_l4_prompt(
    *,
    hyp_id: str,
    claim: str,
    bug_class: str,
    target_file: str,
    program_name: str,
    program_id: str,
    so_abs_path: str,
    program_source: str,
    test_fn_name: str,
) -> str:
    """Construct the L4 LiteSVM authoring prompt for one Anchor hyp."""
    return f"""\
# Layer-4 LiteSVM exploit-chain author (Anchor mode)

You author ONE Rust integration test that empirically demonstrates the
bug described below by constructing a transaction in the LiteSVM in-
process Solana VM, sending it against the compiled Anchor program, and
asserting the bug-witness condition.

## Hypothesis under test

- Hyp ID:    {hyp_id}
- Bug class: {bug_class}
- Claim:     {claim}
- Source:    {target_file}

## Anchor program

- Crate name:       `{program_name}`
- Declared program ID: `{program_id}`
- Built .so (absolute path): `{so_abs_path}`

## Full program source (read every line)

```rust
{program_source}
```

## Sidecar Cargo workspace dependencies (already wired)

You do NOT need to declare dependencies. The sidecar `Cargo.toml`
exposes:

- `litesvm = "0.12"`           — load this with `use litesvm::LiteSVM;`
- `solana-sdk = "3"`           — import everything from `solana_sdk::`
                                  (pubkey, signer::Signer, keypair::Keypair,
                                  instruction::{{Instruction, AccountMeta}},
                                  message::Message, transaction::Transaction,
                                  system_instruction, signature::Keypair).
- `borsh = "1"`                — for serialising instruction args.
- `sha2 = "0.10"`              — for computing Anchor instruction
                                  discriminators in-line.

DO NOT import from `solana_pubkey::`, `solana_keypair::`, `solana_program::`,
`solana_signer::`, `solana_instruction::`, `solana_message::`,
`solana_transaction::`, `solana_system_interface::` — those small split
crates are on different majors than `solana-sdk` and the resolver will
refuse. Use the `solana_sdk::*` re-exports exclusively.

## Authoring rules

1. Output ONLY the complete Rust file content (no markdown fences, no
   commentary). It must compile as a Rust integration test file.
2. The test fn MUST be `pub fn {test_fn_name}()` (annotated `#[test]`).
3. Begin by loading the program: read the .so file from
   `{so_abs_path}` via `std::fs::read`, then call
   `svm.add_program(program_id, &so_bytes)` (or equivalent for the
   `litesvm` 0.12 API).
4. Reconstruct the exact account layout from the program source above
   (PDAs via `Pubkey::find_program_address` with the seeds you can
   read in the `#[account(seeds = ...)]` constraints).
5. Construct the exploit transaction that demonstrates the bug. For
   account-validation bugs (Signer-vs-AccountInfo, missing has_one,
   missing seeds+bump, owner-check absent), the exploit tx must
   omit the relevant signature / supply the unauthorised account.
   For arithmetic / state-machine bugs, drive the program to the
   boundary that triggers the buggy branch.
6. Compute Anchor instruction discriminators in-line if you need them:
   the discriminator is the first 8 bytes of
   `sha2::Sha256("global:<function_name>".as_bytes())`. The borsh-
   encoded args follow.
6.5. **Setup robustness (read carefully — the most common L4 failure
    mode is setup that aborts before the exploit runs).**

    a) Airdrop **lots** of lamports to every keypair you sign with:
       `svm.airdrop(&kp.pubkey(), 100_000_000_000).unwrap();` (100 SOL).
       Default Solana rent + per-tx fees + transfer amounts will eat
       a 1-SOL airdrop on a few txs. 100 SOL is generous and safe.

    b) Airdrop to **PDAs** too if your initialize step uses them as
       system-program transfer targets. `svm.airdrop(&pda, ...)` is
       legal in LiteSVM even when the PDA isn't a keypair.

    c) When constructing the **initialize** transaction, the
       Anchor-generated `init` constraint allocates the config /
       state account via the system program. The payer (admin) must
       have enough lamports to cover the rent (~0.002 SOL per
       account is a safe ceiling, but airdrop 1 SOL minimum).

    d) Use `.unwrap()` ONLY on setup operations that genuinely
       cannot fail in LiteSVM (`svm.airdrop`, `svm.add_program`,
       `svm.latest_blockhash`). For `svm.send_transaction`, capture
       the Result explicitly so you can distinguish setup-tx-failed
       from exploit-tx-result.

    e) If a setup transaction fails (e.g. initialize or deposit
       returns Err), panic with a message that starts with
       `SETUP FAILED:` rather than embedding any of the bug-witness
       phrases. The classifier treats `SETUP FAILED` as infra error
       (not a bug fire). Example:

       ```rust
       let init_res = svm.send_transaction(init_tx);
       if let Err(e) = init_res {{
           panic!("SETUP FAILED: initialize tx aborted — {{e:?}}");
       }}
       ```

       NEVER let a setup-tx Err produce a message that mentions
       the hyp ID or "post-patch invariant" — that would false-fire.

7. **Assertion convention (CRITICAL — read carefully).** Assert the
   *post-patch invariant* — the property that MUST hold once the bug
   is fixed. The test is designed so that:
   - on the BUGGY code (current state of the program): the assertion
     FAILS → cargo reports `test result: FAILED` → engine classifies
     as "bug fired"
   - on the PATCHED code: the assertion holds → cargo reports
     `test result: ok` → bug no longer reproducible.

   Concretely for each bug class:
   - *Missing auth gate* (Signer-vs-AccountInfo, missing has_one,
     missing seeds+bump, owner-check absent): the patched runtime
     would REJECT the exploit tx. The post-patch invariant is
     "exploit tx is rejected." Assert `assert!(result.is_err(), "{hyp_id}: \
     exploit tx should have been rejected — admin/maker/owner did not \
     sign / has_one missing / seeds invalid / owner check absent. \
     See programs/<...>/src/lib.rs:<line>.")`. On the current buggy
     code, `result.is_ok()` is true so the assertion fails (bug
     fires); on the patched code, `result.is_err()` is true so the
     assertion holds (no bug).
   - *State divergence / leak* (close-without-zeroing, balance
     desync, etc.): the patched invariant is "state stays consistent."
     Assert the consistency property: `assert!(post_state_matches_expected, \
     "{hyp_id}: state diverged — observed X, expected Y. See …:<line>.")`.
   - *Arithmetic / state-machine bug* (overflow, off-by-one):
     assert the safe-result property post-patch. The current code
     produces the buggy value; the assertion will fail on buggy
     code, hold on patched code.

   In every case the assertion message MUST cite the hyp ID and the
   exact source line.
8. If — and ONLY if — the bug class genuinely cannot be empirically
   demonstrated at LiteSVM level (e.g. requires multi-block timing,
   requires another deployed program, requires off-chain oracle data
   the program never deploys with), output ONLY this line:
   `// CANNOT_TEST: <one-sentence reason>`. Do NOT fabricate APIs.

## Output

Begin your output immediately with the first line of the Rust file.
End it with the closing brace of the test function. No extra prose.
"""


def parse_llm_response(text: str) -> str:
    """Strip markdown fences if the LLM wraps the output anyway."""
    txt = text.strip()
    # remove ```rust ... ``` fence if present
    m = re.match(r"^```(?:rust|rs)?\s*\n(.*?)\n```\s*$", txt, re.DOTALL)
    if m:
        return m.group(1)
    return txt


def build_compile_fix_prompt(
    *,
    original_prompt: str,
    previous_attempt: str,
    compile_log: str,
) -> str:
    """Construct a follow-up prompt that asks the LLM to fix compile errors.

    Feeds the LLM the prior attempt plus the cargo error log and asks
    for a corrected file. Same output rules as the original prompt
    (no markdown, just the Rust file).
    """
    # Trim the compile log to the most relevant error lines so the
    # context budget isn't wasted on cargo-update noise.
    error_lines: list[str] = []
    capture = 0
    for line in compile_log.splitlines():
        s = line.rstrip()
        if s.startswith("error[") or s.startswith("error:") or s.startswith("  --> "):
            capture = 4  # capture this + next 3 lines for context
        if capture > 0:
            error_lines.append(s)
            capture -= 1
    trimmed = "\n".join(error_lines[:120])

    return f"""\
Your previous attempt at the LiteSVM L4 test did not compile. Fix the
compile errors and re-emit the FULL file.

## Original task

{original_prompt}

## Your previous attempt

```rust
{previous_attempt}
```

## Cargo compile errors

```
{trimmed}
```

## Fix rules

1. Output the COMPLETE corrected Rust file (not just a diff).
2. Same output rules as the original task: no markdown fences, no
   prose, just the Rust file from first line to closing brace.
3. The most common fix is import-path drift in solana-sdk 3.x. The
   small split crates that may NOT be re-exported under
   `solana_sdk::` include `system_instruction`, `system_program`,
   `sysvar`, and `program_pack`. If a `solana_sdk::system_instruction`
   import fails, use `solana_sdk::system_program::ID` for the
   program-id constant and construct the transfer instruction
   manually via `Instruction {{ program_id: solana_sdk::system_program::ID, ... }}`.
   Or import `solana_program::system_instruction` if you need it (the
   sidecar pulls solana-program transitively).
4. `LiteSVM::add_program` in 0.12 returns `()` (no Result). Do NOT
   `.unwrap()` it. Same for `LiteSVM::airdrop` — check the actual
   return type and don't unwrap if it's not a Result.
5. `LiteSVM::send_transaction` returns
   `Result<TransactionMetadata, FailedTransactionMetadata>`. To assert
   "the tx succeeded" use `assert!(result.is_ok(), "...")`. To assert
   "the tx failed" use `assert!(result.is_err(), "...")`.
6. Do not re-introduce any error you previously made. Read the error
   messages above carefully.
"""


__all__ = [
    "LiteSVMTestOutcome",
    "build_anchor_l4_prompt",
    "parse_llm_response",
    "parse_litesvm_outcome",
    "run_sidecar_test",
    "write_sidecar_workspace",
    "_slugify",
    "_detect_program_id",
    "_gather_program_source",
]
