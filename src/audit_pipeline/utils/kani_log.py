"""Parser for `cargo kani` log output.

Extracts:
  - Per-harness verdicts (SUCCESSFUL / FAILED / TIMEOUT)
  - Per-harness verification times
  - Failure category (real failure vs unwind-noise)
  - Aggregate stats (total / pass / fail)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HarnessResult:
    name: str
    verdict: str  # PASS | FAIL | TIMEOUT | UNKNOWN
    time_seconds: float | None
    failure_category: str | None  # "noise_unwind" | "real_panic" | None

    def is_real_failure(self) -> bool:
        return self.verdict == "FAIL" and self.failure_category != "noise_unwind"


def parse_kani_log(log_text: str) -> list[HarnessResult]:
    """Parse a cargo kani log into per-harness results."""
    results = []

    # Split by "Checking harness <name>..." boundaries
    blocks = re.split(r"^Checking harness ", log_text, flags=re.MULTILINE)
    for block in blocks[1:]:  # skip preamble before first harness
        result = _parse_one_block(block)
        if result:
            results.append(result)

    return results


def _parse_one_block(block: str) -> HarnessResult | None:
    """Parse one '<name>...\\n<verdict>...\\nVerification Time: ...' chunk."""
    # First line is the harness name (until the dot-dot-dot)
    first_line_end = block.find("\n")
    if first_line_end == -1:
        return None
    name = block[:first_line_end].rstrip(".").strip()

    # Verdict
    if "VERIFICATION:- SUCCESSFUL" in block:
        verdict = "PASS"
    elif "VERIFICATION:- FAILED" in block:
        verdict = "FAIL"
    elif "TIMEOUT" in block.upper():
        verdict = "TIMEOUT"
    else:
        verdict = "UNKNOWN"

    # Time
    time_match = re.search(r"Verification Time:\s*([\d.]+)s", block)
    time_seconds = float(time_match.group(1)) if time_match else None

    # Failure category
    category = None
    if verdict == "FAIL":
        if "unwinding assertion loop 0" in block:
            category = "noise_unwind"
        elif "expect_failed.assertion.1" in block or "option.rs:2184" in block:
            category = "real_panic"
        else:
            category = "unknown_failure"

    return HarnessResult(
        name=name,
        verdict=verdict,
        time_seconds=time_seconds,
        failure_category=category,
    )


def summarize(results: list[HarnessResult]) -> dict:
    """Build aggregate stats from a list of results."""
    return {
        "total": len(results),
        "pass": sum(1 for r in results if r.verdict == "PASS"),
        "fail": sum(1 for r in results if r.verdict == "FAIL"),
        "real_failures": sum(1 for r in results if r.is_real_failure()),
        "noise_failures": sum(1 for r in results if r.verdict == "FAIL" and r.failure_category == "noise_unwind"),
        "timeout": sum(1 for r in results if r.verdict == "TIMEOUT"),
        "total_seconds": sum(r.time_seconds for r in results if r.time_seconds is not None),
    }


def parse_kani_log_file(log_path: str | Path) -> list[HarnessResult]:
    """Convenience: parse a log file directly."""
    return parse_kani_log(Path(log_path).read_text())
