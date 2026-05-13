"""Layer-2 PoC adapter for Solana / Rust / Anchor source repos.

Wraps the existing ``poc_llm`` + ``poc`` machinery so the new
language-agnostic hunt dispatch can call it uniformly. The existing
Solana PoC path is battle-tested through every Percolator cycle —
this adapter doesn't change that path, just exposes it under the
``LanguagePocAdapter`` interface.

For Solana the framework is ``cargo test --features test``. The
"fired" signal is an assertion failure or panic during the test (rust
test framework reports non-zero exit on any failed test).

Notes:
  * The existing ``poc_llm.py`` builds the author prompt with extensive
    grounded-source injection + stop-word-filtered identifier extraction.
    We reuse it via ``build_poc_authoring_prompt`` rather than
    reimplementing.
  * The existing ``poc.py`` handles test scaffolding via a different
    code path (template-based). The adapter prefers the LLM-author
    path since it's what the hunt cycle uses.
  * ``write_test_file`` writes to ``<workspace>/tests/engine/test_<name>.rs``
    matching the Cargo target layout that ``poc_llm`` already uses.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.poc_adapters.base import LanguagePocAdapter, PocOutcome


_PSEUDO_PASS_MARKERS = (
    "#[ignore]",
    "unimplemented!()",
    "todo!()",
    "CANNOT_TEST",
    "Insufficient source grounding",
)


class SolanaAdapter(LanguagePocAdapter):
    """Solana / Rust PoC adapter (cargo test + LiteSVM)."""

    language = "solana"
    test_file_extension = ".rs"
    framework = "cargo"

    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        # Delegate to the existing poc_llm prompt builder, which has
        # extensive Percolator-tuned grounding logic. Lazy import so
        # this module's import cost stays small for callers that only
        # need other adapters.
        #
        # Signature: build_poc_authoring_prompt(*, hyp, engine_source,
        # finding_name, strategy). We pass the hyp dict directly, the
        # grounded source as engine_source, and derive finding_name +
        # strategy from the hyp.
        from audit_pipeline.commands.poc_llm import (
            build_poc_authoring_prompt,
            strategy_for,
        )
        finding_name = (hyp.get("id") or "unknown").lower()
        strategy = strategy_for(hyp.get("bug_class"))
        return build_poc_authoring_prompt(
            hyp=hyp,
            engine_source=source_context,
            finding_name=finding_name,
            strategy=strategy,
        )

    def parse_test_body(self, llm_response: str) -> str:
        from audit_pipeline.commands.poc_llm import extract_rust_from_response
        body = extract_rust_from_response(llm_response)
        if not body.strip():
            raise ValueError(
                "Could not extract a Rust source block from the LLM response. "
                "Expected ```rust ... ``` fenced code block."
            )
        return body if body.endswith("\n") else body + "\n"

    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "tests" / "engine"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"test_{test_name}{self.test_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 600,
    ) -> PocOutcome:
        """Run `cargo test --features test --test test_<name>`.

        The Cargo target layout requires the test file be at the right
        location (``tests/engine/test_<name>.rs`` for an integration
        test). The wrapper Cargo.toml should reference it as a
        ``[[test]]`` target named ``test_<name>``.
        """
        test_path = (
            workspace / "tests" / "engine"
            / f"test_{test_name}{self.test_file_extension}"
        )
        if not test_path.is_file():
            raise FileNotFoundError(
                f"PoC test file not found at {test_path}. Did write_test_file run?"
            )

        # Pseudo-pass check BEFORE we burn cargo time
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

        t0 = time.time()
        try:
            run_proc = subprocess.run(
                [
                    "cargo", "test",
                    "--features", "test",
                    "--test", f"test_{test_name}",
                    "--", "--nocapture",
                ],
                cwd=str(target_repo_root),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="cargo not installed — install rustup + cargo",
                returncode=-3,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="toolchain missing: cargo",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="cargo test timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="cargo test timeout",
            )

        duration = time.time() - t0
        stdout = run_proc.stdout[:8000]
        stderr = run_proc.stderr[:4000]

        # Detect compile failure (cargo's exit code is non-zero AND no
        # `running N tests` line appears in stdout).
        ran_tests = bool(re.search(r"running \d+ tests?", stdout))
        if not ran_tests and run_proc.returncode != 0:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason="cargo build failed — PoC source has errors",
                metadata={"phase": "compile"},
            )

        # Fired = at least one test FAILED (panic / assertion). cargo
        # reports `test result: FAILED. N passed; M failed;` when so.
        failed_match = re.search(
            r"test result: FAILED\.\s*(\d+) passed;\s*(\d+) failed",
            stdout,
        )
        if failed_match and int(failed_match.group(2)) > 0:
            # Extract the failure reason from the FAILURES block
            fail_reason = "test failed"
            fr = re.search(r"---- (\S+) stdout ----\s*\n([\s\S]*?)\n\n", stdout)
            if fr:
                fail_reason = fr.group(2).splitlines()[0][:200]
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=f"cargo test failed: {fail_reason}",
                metadata={"n_failed": int(failed_match.group(2))},
            )

        return PocOutcome(
            fired=False,
            test_path=test_path,
            stdout=stdout,
            stderr=stderr,
            returncode=run_proc.returncode,
            duration_s=duration,
            framework=self.framework,
            reason="cargo test passed — bug not reachable from witness state",
        )
