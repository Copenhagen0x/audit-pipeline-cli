"""Layer-2 PoC adapter for Aptos Move source repos.

Test framework: ``aptos move test``. The "fired" signal is a Move
unit test that aborts when it should NOT (or runs to completion when
it shouldn't). Move's abort-on-violation discipline gives us a clean
fire detector: a `#[test]` annotated function aborts (= test fails)
when the bug is triggered.

Three idioms the LLM is steered toward:

  * Direct assertion: `assert!(actual == expected, ERR_CONSERVATION)`
    fires when the conservation invariant breaks.
  * Expected-but-absent abort: `#[expected_failure]` annotates a test
    that SHOULD abort; the test fires when the abort doesn't happen
    (Move treats "unexpected success" as a fail).
  * State-after-call: call the function under test, then read state
    + assert it matches the expected invariant.

Move catches: borrow_global without auth, signer-resource binding,
capability leak, u64 overflow as DoS, divide-by-zero, resource leak,
share math direction, oracle staleness, governance flash-loan vote.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.poc_adapters.base import LanguagePocAdapter, PocOutcome

_PSEUDO_PASS_MARKERS = (
    "CANNOT_TEST",
    "// placeholder",
    # NOTE: we intentionally DO NOT match bare "TODO" or "FIXME" — many
    # real Move tests have a `// TODO:` comment for follow-up work and are
    # still exercising the bug. See Phase 1d audit finding C-2.
)


def _detect_move_named_addresses(repo_root: Path) -> dict[str, str]:
    """Parse Move.toml `[addresses]` block and return {name -> hex}.

    Move modules are addressed by a NAMED address declared in the
    package's Move.toml (e.g. `program_b = "0x1"` or `osec = "_"` if
    deferred). Hard-coding `0x0` or `mutatis` in the PoC fails the
    compiler when the actual address has a different name. We read
    Move.toml so the LLM's prompt can reference the real names.

    Returns an empty dict if Move.toml is missing or unparseable —
    callers should fall back to `0x0` and treat that as best-effort.
    """
    manifest = repo_root / "Move.toml"
    if not manifest.is_file():
        return {}
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    # Find the [addresses] section + parse name="hex" or name='hex' pairs
    section = re.search(
        r"^\s*\[addresses\][^\[]*",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        return {}
    pairs = re.findall(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]",
        section.group(0),
        re.MULTILINE,
    )
    return dict(pairs)


class AptosAdapter(LanguagePocAdapter):
    """Aptos Move PoC adapter (`aptos move test`)."""

    language = "aptos"
    test_file_extension = ".move"
    framework = "aptos-cli"

    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        # Parse the target repo's Move.toml so the LLM uses the actual
        # named addresses (not a hardcoded `mutatis` / `0x0`).
        named_addrs = _detect_move_named_addresses(target_repo_root)
        if named_addrs:
            addr_lines = "\n".join(
                f"  {name} = \"{val}\""
                for name, val in named_addrs.items()
            )
            addr_block = (
                "The target package declares the following named addresses "
                "in Move.toml (use these in your `module <name>::...` "
                "declarations):\n\n"
                f"{addr_lines}\n"
            )
        else:
            addr_block = (
                "Could not read Move.toml — fall back to `0x0` for the test "
                "module address. If the compiler rejects it, the runtime "
                "harness will surface that as a compile error.\n"
            )

        return f"""You are authoring a Layer-2 Proof-of-Concept Aptos Move test for the Jelleo audit engine.

Your test will be run with:

  aptos move test --filter <test_function_name> --package-dir {target_repo_root}

The test FIRES (= bug confirmed) when ANY of these happen:
  * An explicit `assert!(...)` fails (abort with non-zero code)
  * A `#[test]` function is reached and aborts with an unexpected code
  * `#[expected_failure(abort_code = N)]` annotation is present but
    the test runs to completion without abort

The test PASSES (= bug NOT reachable from your witness state) when
the function runs to completion with all assertions satisfied AND no
abort.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Target file: {target_file}
Engine function: {engine_function}
Relevant instructions: {relevant}

# Move-language semantics

* Resources are linear. You must `move` them, can't copy.
* `borrow_global<T>(addr)` reads a resource; `borrow_global_mut<T>(addr)`
  mutates. Both are auth points — check signer identity.
* `signer::address_of(&signer)` returns the caller's address.
* `assert!(cond, code)` aborts with `code` if `cond` is false.
* Integer arithmetic ABORTS on overflow (not wraps). Reachable abort
  on attacker-supplied input = severity High.

# Grounded source

{source_context}

# Repo layout

* Package manifest: {target_repo_root}/Move.toml
* Source modules:   {target_repo_root}/sources/*.move
* Tests dir:        {target_repo_root}/tests/*.move

# Move.toml named addresses

{addr_block}
# Your task

Write a single self-contained Move test module `test_<finding_name>.move`
that:

1. Declares `module <named_address>::test_<finding_name>` (use one of
   the named addresses listed above — DO NOT invent `mutatis` or `0x0`
   if the manifest declares different names).
2. `use`s the modules under test.
3. Defines a `#[test(signer = @<addr>)]` function `test_<finding_name>`
   that:
     (a) Sets up witness state (initial resources, balances, oracle prices)
     (b) Calls the function under test with the bug-triggering inputs
     (c) Either:
           * `assert!(invariant_holds, E_BUG_HIT)` — fails when the
             bug breaks the invariant.
           * Reads state after the call and `assert!(actual == expected)`.
           * For abort-on-DoS bugs: omit `#[expected_failure]` so the
             unexpected abort marks the test failed (= fired).
           * For "this should abort but doesn't": add
             `#[expected_failure(abort_code = X)]`.
4. Uses `aptos_framework::account` / `aptos_framework::coin` helpers
   to set up signers / balances. Use `account::create_account_for_test`
   or `account::create_signer_for_test` for ephemeral test signers.

# Output format

Output ONLY a single ```move ... ``` fenced code block containing
the COMPLETE test module. The contents MUST begin with
`module <named_address>::<test_module_name> {{` and end with the
matching `}}`. Do NOT paste raw `public entry fun` snippets from the
source — those compile only inside their original modules. Do NOT
output any prose, explanation, or markdown outside the fenced
block. Do NOT output multiple fenced blocks — only ONE.

Bad output (rejected):
  ```move
  public entry fun withdraw(...) {{ ... }}   // bare function — NOT a module
  ```

Good output (accepted):
  ```move
  module mutatis::test_apt_X {{
      use mutatis::token_vault;
      #[test]
      fun test_witness() {{
          // setup + call + assert
      }}
  }}
  ```

If you can't write a real PoC (e.g. the hypothesis is wrong, the bug
isn't reachable, or you don't have enough information), output:

  // CANNOT_TEST: <one-line reason>
  module 0x0::no_op_test {{
      #[test]
      fun test_no_op() {{ }}
  }}

The `CANNOT_TEST:` marker is recognized by the post-cycle gate as a
non-fire — it doesn't count as a passed test. Don't use it lightly.
"""

    def parse_test_body(self, llm_response: str) -> str:
        # Find ALL move/Move/rust/bare fenced blocks and pick the one
        # that LOOKS like a complete test module. Without this, the
        # parser used to grab the FIRST fence — which is often the
        # LLM showing "here's the source I'm testing against:" as a
        # quoted context block, NOT the actual test. The buggy
        # behavior was caught during the aptos-small dry-run where
        # APT11's first fence was bare `public entry fun withdraw...`
        # source code (no module wrapper) → compile failed.
        fences: list[str] = []
        fence_re = re.compile(
            r"```(?:move|Move|rust|Rust)?\s*\n([\s\S]*?)\n```"
        )
        for m in fence_re.finditer(llm_response):
            fences.append(m.group(1).strip())

        # Score each fence: prefer ones with BOTH module wrapper AND
        # #[test] annotation. Fall back to fences with just module,
        # then just #[test]. Reject bare source pastes.
        def _score(body: str) -> int:
            has_module = bool(re.search(r"\bmodule\s+\S+\s*\{", body))
            has_test = "#[test" in body
            has_cant = "CANNOT_TEST" in body
            if has_cant:
                # CANNOT_TEST stub — accept (caller's pseudo-pass
                # marker catches it). Score lower than a real test.
                return 1 if has_module else 0
            if has_module and has_test:
                return 100
            if has_module:
                return 50
            if has_test:
                return 30
            return -100  # bare source paste — REJECT

        best = -1000
        best_body = None
        for body in fences:
            s = _score(body)
            if s > best:
                best = s
                best_body = body

        if best_body and best > 0:
            return best_body + "\n"

        # Bare body fallback (LLM forgot fences)
        stripped = llm_response.strip()
        if stripped.startswith("module ") or "#[test" in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Move source block from the LLM response. "
            "Expected ```move ... ``` fenced code block."
        )

    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "tests" / "aptos"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"test_{test_name}{self.test_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 240,
    ) -> PocOutcome:
        test_path = (
            workspace / "tests" / "aptos"
            / f"test_{test_name}{self.test_file_extension}"
        )
        if not test_path.is_file():
            raise FileNotFoundError(
                f"PoC test file not found at {test_path}. Did write_test_file run?"
            )

        body = test_path.read_text(encoding="utf-8", errors="replace")
        for marker in _PSEUDO_PASS_MARKERS:
            if marker in body:
                return PocOutcome(
                    fired=False,
                    test_path=test_path,
                    stdout="",
                    stderr=f"pseudo-pass marker {marker!r} present",
                    returncode=-1,
                    duration_s=0.0,
                    framework=self.framework,
                    reason=f"pseudo-pass: contains {marker!r}",
                    metadata={"pseudo_pass": True, "marker": marker},
                )

        # Aptos move test expects tests in <package>/tests/. Deploy our
        # test there, run, then clean up.
        repo_tests_dir = target_repo_root / "tests"
        repo_tests_dir.mkdir(parents=True, exist_ok=True)
        deployed_test = repo_tests_dir / f"jelleo_l2_{test_name}.move"
        deployed_test.write_text(body, encoding="utf-8")

        # Derive the test function name from the test_name slug —
        # typically `test_<slug>` per the prompt's instructions.
        filter_name = f"test_{test_name}"

        t0 = time.time()
        try:
            run_proc = subprocess.run(
                [
                    "aptos", "move", "test",
                    "--filter", filter_name,
                    "--package-dir", str(target_repo_root),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="aptos CLI not installed — install via https://aptos.dev/tools/install-cli/",
                returncode=-3,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="toolchain missing: aptos CLI",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="aptos move test timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="aptos move test timeout",
            )
        finally:
            deployed_test.unlink(missing_ok=True)

        duration = time.time() - t0
        # Strip ANSI color escapes that `aptos move test` emits with TTY
        # detection failing under capture_output=True on some platforms.
        # Without this strip, the PASS/FAIL regex below misses lines that
        # are wrapped in `\x1b[32m...\x1b[0m` etc.
        ansi_strip = re.compile(r"\x1b\[[0-9;]*m")
        stdout = ansi_strip.sub("", run_proc.stdout)[:8000]
        stderr = ansi_strip.sub("", run_proc.stderr)[:4000]

        # aptos move test prints lines like:
        #   [ PASS    ] 0x<addr>::test_<name>::test_func
        #   [ FAIL    ] 0x<addr>::test_<name>::test_func
        # FAIL lines mean the test demonstrated the bug (= fired).
        # We anchor on the EXACT test function name we authored so a
        # substring-match in --filter that pulled in `test_foo_bar` when
        # we asked for `test_foo` doesn't accidentally claim the wrong
        # bug fired.
        all_fail = re.findall(r"^\s*\[\s*FAIL\s*\]\s*(.+?)\s*$", stdout, re.MULTILINE)
        all_pass = re.findall(r"^\s*\[\s*PASS\s*\]\s*(.+?)\s*$", stdout, re.MULTILINE)
        # Filter to the test we actually authored. Pattern: <addr>::test_<test_name>::test_<test_name>
        # (or sometimes <addr>::test_<test_name>::<helper>). Require the
        # OUR test_name to appear as a `::test_<name>::` segment, not as
        # a substring of another test function name.
        anchor = f"::{filter_name}::"
        fail_lines = [line for line in all_fail if anchor in line or line.endswith(f"::{filter_name}")]
        pass_lines = [line for line in all_pass if anchor in line or line.endswith(f"::{filter_name}")]
        # Fallback: if the anchor filter is too strict (e.g. the CLI
        # printed a different format), accept the unfiltered set so we
        # don't lose signal entirely.
        if not fail_lines and not pass_lines and (all_fail or all_pass):
            fail_lines = all_fail
            pass_lines = all_pass
        # aptos move test also prints abort-code details on failure:
        abort_match = re.search(
            r"abort code\s*[:=]?\s*(\d+|0x[0-9a-fA-F]+)",
            stdout + stderr,
        )
        abort_code = abort_match.group(1) if abort_match else None

        if fail_lines:
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=(
                    f"aptos move test failed: {fail_lines[0][:120]}"
                    + (f" (abort code {abort_code})" if abort_code else "")
                ),
                metadata={
                    "fail_lines": fail_lines[:5],
                    "abort_code": abort_code,
                },
            )

        if run_proc.returncode != 0 and not pass_lines:
            # Non-zero exit without any PASS/FAIL parsed → likely compile error
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason="aptos move test exited non-zero without parseable PASS/FAIL (likely compile error)",
                metadata={"phase": "compile"},
            )

        return PocOutcome(
            fired=False,
            test_path=test_path,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            duration_s=duration,
            framework=self.framework,
            reason=f"all tests passed ({len(pass_lines)} PASS) — bug not reachable from witness state",
            metadata={"pass_lines": pass_lines[:5]},
        )
