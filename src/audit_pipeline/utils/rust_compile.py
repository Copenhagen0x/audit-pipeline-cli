"""Cargo compile + test helpers for the synth-kani iteration loop.

Wraps `cargo check` and `cargo kani` invocations against an engine repo,
returning structured results so callers can iterate (e.g. feed compile
errors back to an LLM until the harness compiles).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CompileResult:
    """Result of `cargo check --tests --features test`."""
    ok: bool
    raw_output: str
    errors: list[str] = field(default_factory=list)
    warnings_count: int = 0


@dataclass
class KaniResult:
    """Result of `cargo kani --tests --features test --harness <name>`."""
    ok: bool
    verdict: str  # "PASS" | "FAIL" | "TIMEOUT" | "UNKNOWN"
    raw_output: str


def cargo_check_tests(
    engine_dir: Path,
    features: str = "test",
    timeout: int = 300,
) -> CompileResult:
    """Run `cargo check --tests --features <features>` in engine_dir.

    Returns CompileResult with ok=True iff exit code 0. errors[] lists
    parsed `error[E####]` blocks (best-effort), useful for feeding back
    to an LLM iteration.
    """
    try:
        proc = subprocess.run(
            ["cargo", "check", "--tests", "--features", features],
            cwd=str(engine_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CompileResult(
            ok=False,
            raw_output=f"cargo invocation failed: {e}",
            errors=[str(e)],
        )

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    errors = _extract_compile_errors(combined)
    warnings_count = len(re.findall(r"^warning:", combined, flags=re.MULTILINE))

    return CompileResult(
        ok=proc.returncode == 0,
        raw_output=combined,
        errors=errors,
        warnings_count=warnings_count,
    )


def cargo_kani(
    engine_dir: Path,
    harness_name: str,
    features: str = "test",
    timeout: int = 1800,
) -> KaniResult:
    """Run `cargo kani --tests --features <features> --harness <name>`.

    Returns KaniResult with verdict parsed from the kani output banner.
    """
    try:
        proc = subprocess.run(
            [
                "cargo", "kani",
                "--tests", "--features", features,
                "--harness", harness_name,
            ],
            cwd=str(engine_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return KaniResult(
            ok=False,
            verdict="TIMEOUT" if isinstance(e, subprocess.TimeoutExpired) else "UNKNOWN",
            raw_output=f"cargo kani invocation failed: {e}",
        )

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    verdict = _parse_kani_verdict(combined)
    return KaniResult(
        ok=(proc.returncode == 0 and verdict == "PASS"),
        verdict=verdict,
        raw_output=combined,
    )


def _extract_compile_errors(output: str) -> list[str]:
    """Best-effort extraction of `error[...]: message` blocks from cargo output.

    Each returned string is the error header line + a few context lines,
    enough for an LLM to act on without seeing the full cargo log.
    """
    errors: list[str] = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("error[") or line.startswith("error:"):
            block = [line]
            for j in range(i + 1, min(i + 12, len(lines))):
                next_line = lines[j]
                if next_line.startswith("error[") or next_line.startswith("error:"):
                    break
                block.append(next_line)
                if next_line.strip() == "" and len(block) > 4:
                    break
            errors.append("\n".join(block))
    return errors


def _parse_kani_verdict(output: str) -> str:
    """Parse cargo kani's terminal banner: SUCCESSFUL / FAILED / TIMEOUT."""
    if re.search(r"VERIFICATION:?-?\s*SUCCESSFUL", output, re.IGNORECASE):
        return "PASS"
    if re.search(r"VERIFICATION:?-?\s*FAILED", output, re.IGNORECASE):
        return "FAIL"
    if "TIMEOUT" in output.upper():
        return "TIMEOUT"
    return "UNKNOWN"


def extract_rust_code_block(text: str) -> str | None:
    """Extract the first ```rust ... ``` fenced code block from `text`.

    Falls back to ```rs and to an unfenced heuristic if no fence found.
    Returns None if no plausible Rust code is found.
    """
    for tag in ("rust", "rs"):
        pattern = re.compile(rf"```{tag}\s*\n(.+?)\n```", re.DOTALL)
        m = pattern.search(text)
        if m:
            return m.group(1)

    # Unfenced fallback: anything between the first `#![cfg(...)` or `use`
    # and the end of the response that looks Rust-y.
    m = re.search(
        r"((?:#!\[|use\s+\w+|fn\s+\w+).+?)(?:\n```|\Z)",
        text,
        re.DOTALL,
    )
    if m:
        candidate = m.group(1).strip()
        # Sanity: must contain at least one `fn` and one `}` to look like Rust
        if "fn " in candidate and "}" in candidate:
            return candidate
    return None
