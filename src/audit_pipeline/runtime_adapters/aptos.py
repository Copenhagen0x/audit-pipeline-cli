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

        return f"""You are authoring a property-based Move test for the Jelleo audit engine.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Run command

  aptos move test --filter property_<name> --package-dir {target_repo_root}

A failing test = bug confirmed.

# Harness pattern

```move
module <addr>::property_<name> {{
    use std::signer;
    use aptos_framework::account;
    use <addr>::<module_under_test>;

    /// Property-based test — runs the function under test against a
    /// range of inputs derived from the test seed.
    #[test(s = @0xCAFE)]
    fun property_test(s: signer) {{
        account::create_account_for_test(@0xCAFE);
        // Iterate over a range of values
        let i = 0u64;
        while (i < 64) {{
            // Construct witness state from i
            <module>::function_under_test(&s, i);
            // Assert invariant after each step
            assert!(invariant_holds(&s), 1);
            i = i + 1;
        }};
    }}
}}
```

# Your task

Write `property_<finding_name>.move` that:

1. Declares a property-based test module.
2. Iterates over a range of attacker-controlled inputs.
3. After each iteration, asserts the conservation/correctness
   invariant the hypothesis claims should hold.
4. assert!() with a unique abort code per failure scenario.

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

        fail_lines = re.findall(r"^\s*\[\s*FAIL\s*\]\s*(.+)$", stdout, re.MULTILINE)
        if fail_lines:
            abort_match = re.search(r"abort code:?\s*(\d+)", stdout + stderr)
            return RuntimeOutcome(
                crash_found=True, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=f"property test failed: {fail_lines[0][:120]}",
                witness_inputs=[{"abort_code": abort_match.group(1) if abort_match else None}],
                metadata={"fail_lines": fail_lines[:5]},
            )

        if proc.returncode != 0:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason="aptos move test failed without parseable FAIL (compile error?)",
                metadata={"phase": "compile"},
            )

        return RuntimeOutcome(
            crash_found=False, ran_clean=True, harness_path=harness_path,
            stdout=stdout, stderr=stderr,
            returncode=0, duration_s=duration,
            fuzzer=self.fuzzer,
            reason="property test ran clean — no invariant violations",
        )
