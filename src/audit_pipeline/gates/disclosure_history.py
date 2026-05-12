"""Gate 5 — L5.disclosure_history.

Filters out (or annotates) hypotheses that restate patterns previously
disclosed-and-rejected by the upstream team. Built in response to the
cycle-20260511-183154 retraction, where 7 of 20 findings (residual-
conservation cluster) were variants of patterns already declined in
``aeyakovenko/percolator-prog#39`` with documented rationale and live
regression coverage in ``tests/test_a1_siphon_regression.rs``. The Jelleo
pipeline had no notion of "this pattern was already considered" and
re-derived the same surface as if it were a fresh defect.

Schema addition to the hypothesis YAML:

    - id: H1-residual-conservation
      class: state_transition
      ...
      prior_disclosure:
        pr: https://github.com/aeyakovenko/percolator-prog/pull/39
        decision: rejected
        rationale: >
          Insurance absorption intentionally remains in residual for honest
          two-party loss sharing. The A1 self-dealing siphon class is handled
          by bounded dt, bounded price movement, exact solvency-envelope
          validation, and A1 regression coverage (tests/test_a1_siphon_regression.rs).
        regression_test: tests/test_a1_siphon_regression.rs
        decision_date: 2026-04-20

When ``prior_disclosure.decision == "rejected"``, this gate returns FAIL
unless the hypothesis ALSO carries ``revisit_justification`` — i.e. the
hypothesis author explicitly states why this should be re-considered
despite the prior decision (e.g. new commit invalidated the rationale,
new attack vector found, etc.).

``decision == "merged"`` or ``"fixed"`` — gate returns SKIP with a note;
this hypothesis is now covered upstream, no need to re-derive.

``decision == "pending"`` or ``"superseded"`` — gate passes, finding is
still in flight.
"""

from __future__ import annotations

import time
from typing import Any

from audit_pipeline.gates import GateResult


_REJECTED_DECISIONS = {"rejected", "wontfix", "declined", "closed-not-planned"}
_RESOLVED_DECISIONS = {"merged", "fixed", "resolved", "patched"}
_VALID_DECISIONS = (
    _REJECTED_DECISIONS | _RESOLVED_DECISIONS | {"pending", "superseded", "deferred"}
)


def _normalize_decision(d: Any) -> str:
    return str(d or "").strip().lower()


def check_disclosure_history(hypothesis: dict) -> GateResult:
    """Validate a hypothesis against its ``prior_disclosure`` annotation.

    Args:
        hypothesis: a single hypothesis dict (one entry from the
            ``hypotheses:`` list in the YAML library)

    Returns:
        ``GateResult(True, ...)``  — no prior disclosure OR prior decision
            is non-blocking (pending/superseded), proceed with hunt
        ``GateResult(None, ...)`` — prior decision is "merged/fixed"; finding
            is covered upstream, skip processing
        ``GateResult(False, ...)`` — prior decision is "rejected" and no
            ``revisit_justification`` field. Block re-derivation.
    """
    t0 = time.time()
    prior = hypothesis.get("prior_disclosure")
    if not prior:
        return GateResult(
            passed=True,
            reason="no prior disclosure recorded",
            duration_s=time.time() - t0,
        )
    if not isinstance(prior, dict):
        return GateResult(
            passed=False,
            reason=(
                f"prior_disclosure must be a mapping (got {type(prior).__name__}). "
                "See docs/hypothesis-schema.md for the expected shape."
            ),
            duration_s=time.time() - t0,
        )

    decision = _normalize_decision(prior.get("decision"))
    pr = prior.get("pr") or "(unspecified PR)"
    rationale = (prior.get("rationale") or "").strip()
    revisit = (hypothesis.get("revisit_justification") or "").strip()

    if not decision:
        return GateResult(
            passed=False,
            reason="prior_disclosure missing required 'decision' field",
            duration_s=time.time() - t0,
        )

    if decision not in _VALID_DECISIONS:
        return GateResult(
            passed=False,
            reason=(
                f"prior_disclosure.decision='{decision}' is not in the "
                f"known set ({sorted(_VALID_DECISIONS)})."
            ),
            duration_s=time.time() - t0,
        )

    details = {
        "decision": decision,
        "pr": pr,
        "has_rationale": bool(rationale),
        "has_revisit_justification": bool(revisit),
    }

    if decision in _RESOLVED_DECISIONS:
        return GateResult(
            passed=None,
            reason=(
                f"prior disclosure {pr} was {decision} — patch is upstream; "
                "skipping re-derivation (set ``force_rerun: true`` on the "
                "hypothesis to override)."
            ),
            duration_s=time.time() - t0,
            details=details,
        )

    if decision in _REJECTED_DECISIONS:
        if revisit:
            return GateResult(
                passed=True,
                reason=(
                    f"prior PR {pr} was {decision}, but revisit_justification "
                    f"is present: {revisit[:120]}..."
                ),
                duration_s=time.time() - t0,
                details=details,
            )
        return GateResult(
            passed=False,
            reason=(
                f"prior PR {pr} was {decision} with rationale "
                f"\"{rationale[:160]}...\" and no ``revisit_justification`` "
                "on this hypothesis. Add ``revisit_justification:`` if the "
                "prior decision no longer applies, OR remove this hypothesis "
                "from the library."
            ),
            duration_s=time.time() - t0,
            details=details,
        )

    # decision in {"pending", "superseded", "deferred"} → still in flight, OK
    return GateResult(
        passed=True,
        reason=f"prior disclosure {pr} status={decision} (in flight, proceeding)",
        duration_s=time.time() - t0,
        details=details,
    )


def filter_hypotheses_by_disclosure_history(
    hypotheses: list[dict],
) -> tuple[list[dict], list[tuple[dict, GateResult]]]:
    """Split a hypothesis list into (allowed, blocked-or-skipped).

    Allowed: gate returns ``passed=True``.
    Blocked: gate returns ``False`` or ``None`` (the caller logs and excludes
    these from the cycle).

    Used by ``commands/hunt.py`` to filter the hypothesis library at cycle
    start, before any LLM spend.
    """
    allowed: list[dict] = []
    blocked: list[tuple[dict, GateResult]] = []
    for h in hypotheses:
        result = check_disclosure_history(h)
        if result.passed is True:
            allowed.append(h)
        else:
            blocked.append((h, result))
    return allowed, blocked


__all__ = [
    "check_disclosure_history",
    "filter_hypotheses_by_disclosure_history",
]
