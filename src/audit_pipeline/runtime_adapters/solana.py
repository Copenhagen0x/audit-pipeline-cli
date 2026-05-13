"""L4 runtime adapter for Solana / Rust — LiteSVM.

Wraps the existing ``audit-pipeline litesvm`` command (BPF-level
runtime verification of Solana programs) under the
``LanguageRuntimeAdapter`` interface so the L4 dispatch is uniform
across languages.

LiteSVM runs the compiled BPF program in a deterministic mini-runtime
and checks reachability of invariant-violation paths. Faster than
solana-validator + less infrastructure than localnet.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.runtime_adapters.base import LanguageRuntimeAdapter, RuntimeOutcome


class SolanaRuntimeAdapter(LanguageRuntimeAdapter):
    language = "solana"
    harness_file_extension = ".rs"
    fuzzer = "litesvm"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a LiteSVM-based Layer-4 runtime verification harness.

LiteSVM runs the compiled BPF program in a deterministic mini-runtime,
mutating instruction inputs and asserting that invariant violations
either cannot occur OR produce concrete crash witnesses.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Your task

Write a Rust integration test `litesvm_<finding_name>.rs` that:

1. Sets up a LiteSVM instance + deploys the program.
2. Iterates over a range of attacker-controlled inputs.
3. For each input, constructs the instruction and sends it.
4. Asserts the response + state satisfy the hypothesis invariant.
5. assert!() on violations.

# Output format

Output ONLY a ```rust ... ``` fenced block.

If unable: `// CANNOT_FUZZ: <reason>` stub.
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:rust|Rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "use litesvm" in body or "LiteSVM" in body or "#[test]" in body:
                return body + "\n"
        raise ValueError("Could not extract a LiteSVM harness from the LLM response.")

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "fuzz" / "litesvm"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"litesvm_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_fuzzer(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        time_budget_s: int = 600,
    ) -> RuntimeOutcome:
        harness_path = (
            workspace / "fuzz" / "litesvm"
            / f"litesvm_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(f"LiteSVM harness not found at {harness_path}")

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_FUZZ" in body:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="CANNOT_FUZZ stub", returncode=-1,
                duration_s=0.0, fuzzer=self.fuzzer, reason="harness stub",
            )

        deployed = target_repo_root / "tests" / f"jelleo_l4_litesvm_{harness_name}.rs"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        cmd = [
            "cargo", "test",
            "--features", "test",
            "--test", f"jelleo_l4_litesvm_{harness_name}",
            "--", "--nocapture",
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(target_repo_root),
                capture_output=True, text=True,
                timeout=time_budget_s + 60,
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="cargo not installed",
                returncode=-3, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="toolchain missing: cargo",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout="", stderr="cargo test timeout",
                returncode=-5, duration_s=time.time() - t0,
                fuzzer=self.fuzzer, reason="cargo test timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]

        failed = re.search(r"test result: FAILED.*\b(\d+) failed", stdout)
        if failed and int(failed.group(1)) > 0:
            return RuntimeOutcome(
                crash_found=True, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason=f"LiteSVM test failed: {failed.group(0)}",
                metadata={"n_failed": int(failed.group(1))},
            )

        if proc.returncode != 0:
            return RuntimeOutcome(
                crash_found=False, ran_clean=False, harness_path=harness_path,
                stdout=stdout, stderr=stderr,
                returncode=proc.returncode, duration_s=duration,
                fuzzer=self.fuzzer,
                reason="cargo test failed without parseable FAILED (compile error?)",
                metadata={"phase": "compile"},
            )

        return RuntimeOutcome(
            crash_found=False, ran_clean=True, harness_path=harness_path,
            stdout=stdout, stderr=stderr,
            returncode=0, duration_s=duration,
            fuzzer=self.fuzzer,
            reason="LiteSVM ran clean — no invariant violations",
        )
