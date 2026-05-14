"""from_hunt_outcome() triage-gating tests.

The original `poc_fired=True → Status.CONFIRMED` rule blindly promoted
any mechanical fire (test aborted with a custom error code) to
CONFIRMED status, ignoring the Layer 2.5 LLM judge's verdict.

Cycle 20260513-191318 osec-aptos-small surfaced this:
  - APT10 PoC aborted, but the abort was in `0x1::coin::merge` (stdlib),
    NOT in our target line. Triage classified FALSE. Status was
    incorrectly promoted to CONFIRMED.
  - APT27 PoC aborted in `0x1::timestamp::update_global_time_for_test`
    during setup, never reaching the double-claim bug. Triage classified
    FALSE. Same incorrect promotion.
  - APT9 fired but on a wrong-invariant assertion (auth bypass, not
    event-emit). Triage classified SOFT. Same incorrect promotion.

These tests pin the corrected behavior so the regression can't return.
"""

from __future__ import annotations

from audit_pipeline.lifecycle import Status, from_hunt_outcome


def test_strong_triage_with_fire_promotes_to_confirmed() -> None:
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="STRONG",
    )
    assert s is Status.CONFIRMED


def test_strong_non_representative_lands_in_triaged() -> None:
    """Cluster representatives are CONFIRMED — duplicates within the same
    cluster land in TRIAGED so the rep is the canonical entry for that
    root cause."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="STRONG",
        is_cluster_representative=False,
    )
    assert s is Status.TRIAGED


def test_soft_triage_downgrades_confirmed_to_triaged() -> None:
    """APT9 case: PoC fires (test aborts with custom code) but on the
    wrong invariant — triage says SOFT. Must not promote to CONFIRMED."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="SOFT",
    )
    assert s is Status.TRIAGED


def test_false_triage_with_fire_blocks_promotion() -> None:
    """APT10/APT27 case: PoC mechanically aborts but in stdlib / setup
    — triage says FALSE. Must NOT be promoted to CONFIRMED. Fall back
    to NEW (or REJECTED if the verdict was also FALSE)."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="FALSE",
    )
    assert s is Status.NEW


def test_false_triage_false_verdict_rejected() -> None:
    s = from_hunt_outcome(
        verdict="FALSE", debate_promoted=False, poc_fired=True,
        triage_classification="FALSE",
    )
    assert s is Status.REJECTED


def test_lost_triage_falls_back_to_new() -> None:
    """LOST = couldn't classify (missing log / body). Next cycle retries."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="LOST",
    )
    assert s is Status.NEW


def test_no_triage_classification_preserves_legacy_behavior() -> None:
    """Callers that haven't wired triage through (legacy) keep getting the
    raw poc_fired-based status. Back-compat for older cycles."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification=None,
    )
    assert s is Status.CONFIRMED


def test_unknown_triage_label_falls_through_to_legacy() -> None:
    """A new triage label we don't know about shouldn't crash — fall
    back to the legacy poc_fired-based rule."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="MAYBE",
    )
    assert s is Status.CONFIRMED


def test_lowercase_triage_label_normalized() -> None:
    """Case-insensitive — callers may pass strong/soft/false from JSON."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=True,
        triage_classification="false",
    )
    assert s is Status.NEW


def test_strong_without_fire_is_not_confirmed() -> None:
    """STRONG triage requires the underlying mechanical fire. Without
    poc_fired, fall through to the legacy rules — triage is an
    overlay on a real signal, not a signal of its own."""
    s = from_hunt_outcome(
        verdict="TRUE", debate_promoted=True, poc_fired=False,
        triage_classification="STRONG",
    )
    # poc_fired=False, verdict=TRUE, debate_promoted=True → TRIAGED
    assert s is Status.TRIAGED
