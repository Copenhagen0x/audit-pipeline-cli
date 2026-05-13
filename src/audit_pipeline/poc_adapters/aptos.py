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
    "TODO",
    "FIXME",
    "CANNOT_TEST",
    "// placeholder",
)


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

# Your task

Write a single self-contained Move test module `test_<finding_name>.move`
that:

1. Declares `module <address>::test_<finding_name>` (use the address
   from the Move.toml `[addresses]` section as the module address;
   typically `mutatis` or similar — read the manifest if needed).
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

Output ONLY a single ```move ... ``` (or ```rust ... ``` — Move uses
Rust-like syntax) fenced code block containing the complete test
module. Do not output any prose, explanation, or markdown outside
the fenced block.

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
        # Primary: move-tagged fence
        m = re.search(r"```(?:move|Move)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        # Secondary: rust-tagged (Move uses rust syntax highlighting often)
        m = re.search(r"```(?:rust|Rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "module" in body and "#[test" in body:
                return body + "\n"
        # Tertiary: any fenced block that looks like Move
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "module " in body or "#[test" in body:
                return body + "\n"
        # Bare body
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
        stdout = run_proc.stdout[:8000]
        stderr = run_proc.stderr[:4000]

        # aptos move test prints lines like:
        #   [ PASS    ] 0x<addr>::test_<name>::test_func
        #   [ FAIL    ] 0x<addr>::test_<name>::test_func
        # FAIL lines mean the test demonstrated the bug (= fired).
        fail_lines = re.findall(r"^\s*\[\s*FAIL\s*\]\s*(.+)$", stdout, re.MULTILINE)
        pass_lines = re.findall(r"^\s*\[\s*PASS\s*\]\s*(.+)$", stdout, re.MULTILINE)
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
