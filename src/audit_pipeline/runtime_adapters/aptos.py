"""L4 runtime adapter for Aptos Move — property-based aptos move test.

Aptos's Move test framework supports property-based testing via the
`#[test]` attribute combined with random-seeded inputs. While Move
doesn't have a dedicated coverage-guided fuzzer like AFL, we can
emulate fuzz coverage by:

  1. Authoring a #[test] function that takes inputs derived from
     a seeded PRNG (Move stdlib provides `aptos_std::random`).
  2. Running the test N times with different seeds.
  3. Counting abort()s with codes that match the invariant violation.

For coverage-guided behavior we lean on the spec module from L3 if
available; for raw stress testing we run repeated property tests.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.runtime_adapters.base import LanguageRuntimeAdapter, RuntimeOutcome


class AptosRuntimeAdapter(LanguageRuntimeAdapter):
    language = "aptos"
    harness_file_extension = ".move"
    fuzzer = "aptos-move-test"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        # Cross-module signature index — same fix as L2 adapter.
        # Aptos-large 2026-05-15: 4 L4 harnesses failed to compile
        # (APT38, APTL4 etc.) because the LLM hallucinated function
        # signatures for cross-module setup calls.
        from audit_pipeline.poc_adapters.aptos import _build_move_signature_index
        sig_index = _build_move_signature_index(target_repo_root)

        return f"""You are authoring a property-based Move test for the Jelleo audit engine.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}
Slug (use this for module + function names): {hyp.get("_slug", hyp_id.lower().replace("-", "_"))}

# Grounded source

{source_context}

# Cross-module function signatures (USE THESE EXACT SIGNATURES)

Setup code in property tests routinely calls `<module>::initialize(...)`,
`<module>::register_id(...)`, etc. on modules other than the target.
Use the EXACT signature from this index — do not guess argument count
or types. Aptos-large 2026-05-15: 4 L4 harnesses failed to compile
because the LLM hallucinated cross-module signatures.

```move
{sig_index or "(signature index unavailable — proceed but expect cross-module compile errors)"}
```

# Run command (the FILTER substring is fixed — your test function
# MUST be named to match this exactly)

  aptos move test --filter property_<slug> --package-dir {target_repo_root}

CRITICAL NAMING RULE — the adapter dispatches via the filter
`property_<slug>`. Your file MUST declare:

   * module name: `<target_addr>::property_<slug>`
   * test function name: `fun property_<slug>(...)`

… with the slug taken VERBATIM from the line above. E.g. if the
slug is `apt1_borrow_global_no_auth`, the module is
`@<addr>::property_apt1_borrow_global_no_auth` and the test fn is
`fun property_apt1_borrow_global_no_auth(...)`. DO NOT strip the
`apt1_` (or whatever) prefix — the adapter's filter won't find a
test that was renamed to drop it, and the L4 result will read as
"no signal" even though the harness was authored.

The harness is run as a Move unit test. A test that ABORTS = the
inverted-assertion fired = bug confirmed. A test that PASSES =
the bug-exploit ran end-to-end successfully without aborting.
EITHER is meaningful evidence the bug is reachable.

# Address literals — STRICT hex format

Aptos Move addresses are HEX literals: `@0x` followed by 1-64 digits
chosen ONLY from `0-9` and `A-F` (case-insensitive). Any other
character is a parser error:

  error: unexpected token. Expected ')'

  ❌ `@0xATTACK`   (T, K are not hex digits)
  ❌ `@0xUSER`     (U, S, R are not hex digits)
  ❌ `@0xBADGUY`   (G, U, Y are not hex digits)
  ✓  `@0xDEAD`    (4 hex digits)
  ✓  `@0xBEEF`    (4 hex digits)
  ✓  `@0xBAD`     (3 hex digits)
  ✓  `@0xCAFE`    (4 hex digits)
  ✓  `@0xC0FFEE`  (6 hex digits)
  ✓  `@0xDEADBEEF`(8 hex digits)
  ✓  `@0xABCDEF`  (6 hex digits)
  ✓  `@0x1234`    (digits-only also valid)

When you need an "attacker" address, pick one of: 0xDEAD, 0xBEEF,
0xBAD, 0xCAFE, 0xC0FFEE, 0xDEADBEEF. Use the same address consistently
within the test (the test attribute `#[test(attacker = @0xDEAD)]`
binds it for the function).

# Doc comments rule

Move's `///` is a DOC comment that the parser only accepts on
module-level / function-level / struct-level items. Putting `///`
inside a function body produces:

  warning: invalid documentation comment ... cannot be matched to
  a language item

Use REGULAR `//` comments inside function bodies. Reserve `///` for
the module description + function description (line directly above
the `fun` keyword).

# Harness pattern

```move
module <target_addr>::property_<slug> {{
    use std::signer;
    use aptos_framework::account;
    use <target_addr>::<module_under_test>;

    /// Property-based test that demonstrates the bug exists.
    /// Setup: legitimate state. Attack: invoke with hostile inputs.
    /// Assert: either (a) the bug-exploit succeeded silently (test
    /// PASSES — invariant violation reachable without abort), or
    /// (b) an assertion fires confirming the predicted attacker gain.
    #[test(
        aptos_framework = @aptos_framework,
        admin = @<target_addr>,
        attacker = @0xDEAD
    )]
    fun property_<slug>(
        aptos_framework: signer,
        admin: signer,
        attacker: signer,
    ) {{
        // Use only `//` for comments inside the function body.
        account::create_account_for_test(@aptos_framework);
        account::create_account_for_test(@<target_addr>);
        account::create_account_for_test(@0xDEAD);

        // <setup>: legitimate initialization

        // <attack loop>: iterate over witness inputs, each iteration
        // invokes the buggy function with an attacker signer and
        // asserts the expected attacker-gain happens.
        let i = 0u64;
        while (i < 64) {{
            <module>::buggy_function(&attacker, i);
            // Assert the attacker observed the predicted gain.
            assert!(<attacker observed predicted gain>, 100 + (i as u64));
            i = i + 1;
        }};
    }}
}}
```

# Your task

Write `property_<finding_name>.move` that:

1. Declares a Move test module under `<target_addr>::property_<name>`.
2. Uses VALID HEX addresses only (see above — 0-9 and A-F only).
3. Uses `//` comments inside function bodies (NEVER `///`).
4. Sets up legitimate state, then runs the bug-exploit in a loop.
5. assert!() with a unique abort code per failure scenario so the
   abort-code in the output identifies the failure point.

# Output format

Output ONLY a single ```move ... ``` fenced code block.

If unable: `// CANNOT_FUZZ: <reason>` stub.
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:move|Move|rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "module " in body or "#[test" in body:
                return body + "\n"
        raise ValueError("Could not extract a Move property test from the LLM response.")

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "fuzz" / "aptos"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"property_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_fuzzer(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        time_budget_s: int = 180,
    ) -> RuntimeOutcome:
        harness_path = (
            workspace / "fuzz" / "aptos"
            / f"property_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(f"Move property test not found at {harness_path}")

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_FUZZ" in body:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="CANNOT_FUZZ stub", returncode=-1,
                duration_s=0.0, fuzzer=self.fuzzer, reason="harness stub",
            )

        deployed = (
            target_repo_root / "tests"
            / f"jelleo_l4_property_{harness_name}.move"
        )
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        cmd = [
            "aptos", "move", "test",
            "--filter", f"property_{harness_name}",
            "--package-dir", str(target_repo_root),
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=time_budget_s + 60,
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="aptos CLI not installed",
                returncode=-3, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="toolchain missing: aptos CLI",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="aptos move test timeout",
                returncode=-5, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="aptos move test timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]
        combined = stdout + "\n" + stderr

        # Detect Move compile / parse errors FIRST. The `aptos move
        # test` CLI bundles compile-step errors and runtime test
        # results both in stdout; if compilation fails the test step
        # is skipped. Without this branch the adapter mis-reported
        # compile errors as "ran clean with no signal" — operator
        # caught this on cycle 20260513-191318 APT4 fuzz which had
        # an invalid hex literal (`@0xATTACK`) the LLM produced.
        # Move error codes:
        #   E00xxx-E10xxx, E12xxx+ → compile / context-checking errors
        #   E11xxx              → TEST FAILURE (runtime test abort)
        # The compile-error fast-path must NOT match E11xxx — that's the
        # signal of a real test failure (the harness's assert fired) and
        # MUST fall through to the [FAIL]/[PASS] inner-test marker detector
        # below so the adapter reports crash_found=true with the abort
        # code, not compile_error=true. Operator caught this on cycle
        # 20260513-191318 APT5: harness aborted at runtime (MISSING_DATA
        # in timestamp setup) → error[E11001]: test failure → adapter
        # falsely classified as compile_error.
        compile_error_re = re.compile(
            r"error: unexpected token|"
            r"error: parsing|"
            r"Move compilation failed|"
            r"Failed to run tests: exiting with context checking errors|"
            r"error\[E(?:0\d{4}|10\d{3}|1[2-9]\d{3}|[2-9]\d{4})\]:|"
            r"error: unbound module|"
            r"error: unbound function",
            re.IGNORECASE,
        )
        compile_error_match = compile_error_re.search(combined)
        if compile_error_match:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=(
                    f"property-test harness did not compile: "
                    f"{compile_error_match.group(0)[:120]}. The auto-"
                    "authored harness has a syntax / type / hex-address "
                    "error — L2 PoC fire + L3 Move Prover counterexample "
                    "remain the authoritative bug signals."
                ),
                metadata={"compile_error": True,
                          "failure_signal": compile_error_match.group(0)},
            )

        # Count [PASS] and [FAIL] inner-test lines. The aptos move
        # test runner emits one line per `#[test]` function:
        #   [ PASS    ] <addr>::<module>::<fn>   = test ran end-to-end
        #                                          without abort
        #   [ FAIL    ] <addr>::<module>::<fn>   = test aborted somewhere
        fail_lines = re.findall(r"^\s*\[\s*FAIL\s*\]\s*(.+)$", stdout, re.MULTILINE)
        pass_lines = re.findall(r"^\s*\[\s*PASS\s*\]\s*(.+)$", stdout, re.MULTILINE)

        if fail_lines:
            # Test aborted — for our inverted-assertion harness, this
            # is bug-confirmation evidence. The abort code in the
            # output identifies which assertion fired.
            abort_match = re.search(
                r"abort(?:ed)? (?:with )?code:?\s*(\d+)", combined,
            )
            return RuntimeOutcome(
                crash_found=True, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=(
                    f"property test aborted ({len(fail_lines)} FAIL, "
                    f"{len(pass_lines)} PASS): {fail_lines[0][:120]}"
                    + (f" — abort code {abort_match.group(1)}"
                       if abort_match else "")
                ),
                witness_inputs=[
                    {"abort_code": abort_match.group(1) if abort_match else None}
                ],
                metadata={
                    "fail_lines": fail_lines[:5],
                    "pass_lines": pass_lines[:5],
                    "abort_code": abort_match.group(1) if abort_match else None,
                },
            )

        if pass_lines:
            # All inner tests PASSED — for an inverted-assertion
            # harness this means the bug-exploit ran end-to-end
            # without abort. The attacker SUCCEEDED at the predicted
            # gain. Mark as crash_found=True (the property-fuzz
            # constructively demonstrated the bug).
            return RuntimeOutcome(
                crash_found=True, ran_clean=True, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=(
                    f"property test passed ({len(pass_lines)} PASS) — "
                    "bug-exploit ran end-to-end without abort, "
                    "demonstrating the attacker's predicted gain"
                ),
                metadata={"pass_lines": pass_lines[:5]},
            )

        if proc.returncode != 0:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=(
                    "aptos move test exited non-zero without parseable "
                    "[FAIL]/[PASS] markers — likely no tests matched the "
                    "filter, or runner infra error"
                ),
                metadata={"phase": "no_tests_matched"},
            )

        return RuntimeOutcome(
            crash_found=False, ran_clean=True, harness_path=harness_path,
            stdout=stdout, stderr=stderr,
            returncode=0, duration_s=duration,
            fuzzer=self.fuzzer,
            reason="property test ran clean — no FAIL/PASS markers seen",
        )
