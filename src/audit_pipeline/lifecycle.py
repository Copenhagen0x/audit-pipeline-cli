"""Finding lifecycle state machine.

States:
  new                 — fresh from a hunt cycle, not yet reviewed
  triaged             — human (or automation) confirmed it's a real candidate
  confirmed           — empirical proof exists (PoC fired)
  disclosed           — reported to the maintainer (issue filed / email sent)
  fixed               — maintainer shipped a patch
  verified            — patch confirmed effective via a re-run cycle
  rejected            — refuted INTERNALLY (debate flipped it, PoC didn't
                        fire, retraction). We got the call wrong.
  closed_not_planned  — maintainer reviewed and CLOSED upstream as
                        "won't fix" / not-planned / by-design. We were
                        right that it's a real path, but the maintainer
                        chose not to address it. Different signal from
                        REJECTED for renewal conversations + dashboards.

Transitions are restricted: you can't jump from `new` straight to
`fixed`, you have to walk the chain. This keeps the audit trail
honest and prevents bookkeeping mistakes.
"""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    NEW = "new"
    TRIAGED = "triaged"
    CONFIRMED = "confirmed"
    DISCLOSED = "disclosed"
    FIXED = "fixed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    # POST-AUDIT: distinct terminal state for upstream "closed as
    # not-planned / won't-fix". Previously conflated with REJECTED,
    # which lost important signal for customer dashboards.
    CLOSED_NOT_PLANNED = "closed_not_planned"


VALID_TRANSITIONS: dict[Status, set[Status]] = {
    Status.NEW: {Status.TRIAGED, Status.CONFIRMED, Status.REJECTED},
    Status.TRIAGED: {Status.CONFIRMED, Status.REJECTED},
    Status.CONFIRMED: {Status.DISCLOSED, Status.REJECTED},
    Status.DISCLOSED: {
        Status.FIXED, Status.REJECTED, Status.CLOSED_NOT_PLANNED,
    },
    Status.FIXED: {Status.VERIFIED, Status.REJECTED},
    Status.VERIFIED: set(),
    Status.REJECTED: set(),
    Status.CLOSED_NOT_PLANNED: set(),
}


class InvalidTransition(Exception):
    pass


def validate_transition(frm: Status, to: Status) -> bool:
    return to in VALID_TRANSITIONS.get(frm, set())


def assert_transition(frm: Status, to: Status) -> None:
    if not validate_transition(frm, to):
        raise InvalidTransition(f"Cannot transition {frm.value} -> {to.value}")


def from_hunt_outcome(
    verdict: str,
    debate_promoted: bool,
    poc_fired: bool,
) -> Status:
    """Initial status for a finding straight out of a hunt cycle.

    A finding can be inserted into the DB at any non-terminal state,
    but for traceability we always start at NEW and let the auto-advance
    pipeline walk it forward.
    """
    if poc_fired:
        return Status.CONFIRMED
    if verdict == "TRUE" and debate_promoted:
        return Status.TRIAGED
    if verdict == "TRUE":
        return Status.NEW
    if verdict == "NEEDS_LAYER_2_TO_DECIDE":
        return Status.NEW
    return Status.REJECTED


def emoji(s: Status) -> str:
    return {
        Status.NEW: "🆕",
        Status.TRIAGED: "👀",
        Status.CONFIRMED: "✅",
        Status.DISCLOSED: "📨",
        Status.FIXED: "🔧",
        Status.VERIFIED: "🛡️",
        Status.REJECTED: "❌",
        Status.CLOSED_NOT_PLANNED: "🚫",
    }[s]
