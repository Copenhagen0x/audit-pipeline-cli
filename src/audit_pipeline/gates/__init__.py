"""audit_pipeline.gates — pre-disclosure validation gates.

Each gate is a pure function that returns a GateResult. Failed gates block
the cycle / disclosure step from proceeding. Built in response to the
cycle-20260511-183154 retraction (LGopus teardown) which surfaced six
defects in the pre-disclosure flow:

  L0.freshness          — workspace clones must be at upstream HEAD
  L2.symbol_grep        — PoC tests must only cite symbols that exist
  L2.cargo_check        — PoC tests must compile against real source
  L4.behavior_oracle    — claimed code behaviour must match real code
  L5.disclosure_history — hyps with prior-PR rejections get filtered/annotated
  L5.repo_pin           — issue header SHA must match the repo being filed against

Convention follows ``audit_pipeline.bundle.verifier.GateResult``: passed is
``True`` / ``False`` / ``None`` (skipped, e.g. tool unavailable).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GateResult:
    """Result of a pre-disclosure gate. Mirrors bundle.verifier.GateResult.

    ``passed=True``  → gate passed
    ``passed=False`` → gate failed, block disclosure
    ``passed=None``  → gate skipped (e.g. tool unavailable, opt-out flag)
    """
    passed: bool | None
    reason: str
    duration_s: float = 0.0
    details: dict | None = None      # optional structured payload

    def to_json(self) -> dict:
        out: dict = {
            "passed": self.passed,
            "reason": self.reason,
            "duration_s": round(self.duration_s, 3),
        }
        if self.details:
            out["details"] = self.details
        return out


__all__ = ["GateResult"]
