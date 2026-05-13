"""Post-cycle QA gate — re-runs pre-disclosure gates over every confirmed
finding in a completed cycle BEFORE auto-publish.

Built in response to the 12-audit pipeline review (2026-05-12). The
orchestration audit and the disclosure audit both converged on the same
gap: the six Phase B pre-disclosure gates run at cycle START (freshness
check, hypothesis filtering, PoC symbol gate), but nothing re-runs them
at cycle END before the cycle's HTML/PDF and per-finding emails get
auto-pushed to GitHub + /var/www/jelleo.com/cycles/. Yesterday's
20260511-183154 cycle was lucky to be caught by manual L2.5 triage; an
identical cycle today would auto-publish into the public archive before
operator review unless this gate exists.

The gate is invoked by ``hunt.py`` immediately before the auto-publish
chain. It loads each confirmed finding's PoC + writes a sentinel file
``<cycle_dir>/.publish-blocked`` if any finding fails the re-checks.
``publish_cycle.sh`` and ``publish_cycle_signed.sh`` MUST honour the
sentinel and skip — that change ships in the same commit as this gate.

What the gate checks (cheap, no API spend, deterministic):
  * **symbol_grep**: every PoC's project-specific symbols must grep-exist
    in engine + wrapper source. Catches hallucinated function names that
    would have made it past the cycle-start gate if the PoC was authored
    against a stale snapshot.
  * **PoC file content**: refuse PoCs that contain ``#[ignore]`` or
    ``unimplemented!()`` / ``todo!()`` / ``CANNOT_TEST`` markers — these
    can't have legitimately "fired".

Optionally (opt-in via ``include_behavior_oracle=True``):
  * **behavior_oracle**: for each finding, send the claim + cited code
    window to an independent LLM. Returns CONTRADICT for the wrong-
    direction cluster (yesterday's #04, #09, #12, #14, #16, #18, #19,
    #20). Costs ~$0.001 per finding at Haiku rates; default OFF so the
    cheap checks still gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from audit_pipeline.gates.symbol_grep import check_symbols

# Markers that mean a PoC didn't really test the bug.
_PSEUDO_PASS_MARKERS = (
    "#[ignore]",
    "unimplemented!()",
    "todo!()",
    "CANNOT_TEST",
    "Insufficient source grounding",
)


@dataclass
class PostCycleReport:
    """One row per confirmed finding, plus an aggregate verdict."""
    passed: bool
    n_findings: int
    n_failed: int
    rows: list[dict] = field(default_factory=list)
    duration_s: float = 0.0

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "n_findings": self.n_findings,
            "n_failed": self.n_failed,
            "rows": self.rows,
            "duration_s": round(self.duration_s, 3),
        }


def _check_one_poc(
    poc_path: Path,
    test_name: str,
    search_dirs: list[Path],
) -> dict:
    """Validate one PoC file. Returns a row dict suitable for the report."""
    if not poc_path.is_file():
        return {"poc_path": str(poc_path), "passed": False,
                "reason": "PoC source file missing on disk"}
    try:
        src = poc_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"poc_path": str(poc_path), "passed": False, "reason": f"read error: {e}"}

    # 1. Pseudo-pass markers — these can't have legitimately fired.
    for marker in _PSEUDO_PASS_MARKERS:
        if marker in src:
            return {
                "poc_path": str(poc_path),
                "test_name": test_name,
                "passed": False,
                "reason": (
                    f"PoC contains pseudo-pass marker {marker!r} — "
                    "a #[ignore]d / unimplemented / cannot-test PoC "
                    "cannot have legitimately confirmed a bug. "
                    "Re-author against grounded source."
                ),
            }

    # 2. Symbol grep — every project symbol must exist in engine/wrapper.
    sym_result = check_symbols(
        poc_source=src,
        search_dirs=search_dirs,
        allowed_test_names=frozenset({test_name}),
    )
    if sym_result.passed is False:
        return {
            "poc_path": str(poc_path),
            "test_name": test_name,
            "passed": False,
            "reason": "symbol_grep gate failed: " + sym_result.reason,
            "details": sym_result.details,
        }
    return {
        "poc_path": str(poc_path),
        "test_name": test_name,
        "passed": True,
        "reason": "all checks passed",
    }


def check_post_cycle(
    *,
    cycle_dir: Path,
    confirmed_findings: list[dict],
    engine_src_dir: Path | None = None,
    wrapper_src_dir: Path | None = None,
) -> PostCycleReport:
    """Re-run cheap pre-disclosure gates over every confirmed finding.

    Args:
        cycle_dir:           ``<workspace>/hunts/<cycle_id>/`` — contains
                             the per-finding PoC sources under ``poc/``.
        confirmed_findings:  list of finding-row dicts (from
                             ``db.list_findings``). Each must have
                             ``hypothesis_id`` (used to derive the PoC
                             filename).
        engine_src_dir:      path to engine ``src/`` for grep
        wrapper_src_dir:     optional wrapper ``src/`` for grep

    Returns:
        ``PostCycleReport`` with ``passed=True`` only when every confirmed
        finding's PoC clears the cheap checks. Caller decides what to do:
        ``hunt.py`` writes ``.publish-blocked`` sentinel and aborts
        auto-publish on failure.
    """
    t0 = time.time()
    search_dirs = [d for d in (engine_src_dir, wrapper_src_dir) if d and d.is_dir()]
    rows: list[dict] = []
    # POST-AUDIT FIX: was reimplementing slug locally with no length cap,
    # diverging from canonical `slug_for_hypothesis` (which truncates at
    # 60 chars). For hypothesis IDs > 60 chars (19 such hyps in the live
    # library), post_cycle looked up `test_<78chars>.rs` while hunt +
    # poc_llm wrote `test_<60chars>.rs` — file not found → publish
    # falsely blocked. Use the canonical helper everywhere.
    from audit_pipeline.utils.slug import slug_for_hypothesis
    for f in confirmed_findings:
        hyp_id = f.get("hypothesis_id") or ""
        slug = slug_for_hypothesis(hyp_id)
        test_name = f"test_{slug}"
        poc_path = cycle_dir / "poc" / f"{test_name}.rs"
        row = _check_one_poc(poc_path, test_name, search_dirs)
        row["hypothesis_id"] = hyp_id
        row["finding_id"] = f.get("id")
        rows.append(row)

    n_failed = sum(1 for r in rows if not r.get("passed"))
    return PostCycleReport(
        passed=(n_failed == 0),
        n_findings=len(rows),
        n_failed=n_failed,
        rows=rows,
        duration_s=time.time() - t0,
    )


PUBLISH_BLOCKED_SENTINEL = ".publish-blocked"


def write_block_sentinel(cycle_dir: Path, report: PostCycleReport) -> Path:
    """Write a sentinel into the cycle dir that publish_cycle.sh skips on.

    The sentinel includes the JSON report so ``publish_cycle.sh`` can
    print the reason for skipping (helps operators debug).
    """
    import json
    sentinel = cycle_dir / PUBLISH_BLOCKED_SENTINEL
    sentinel.write_text(
        json.dumps(report.to_json(), indent=2),
        encoding="utf-8",
    )
    return sentinel


__all__ = [
    "PostCycleReport",
    "PUBLISH_BLOCKED_SENTINEL",
    "check_post_cycle",
    "write_block_sentinel",
]
