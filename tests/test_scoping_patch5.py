"""Patch #5 — customer-manifest scoping fixes.

Covers two audit findings in scoping.py:

  * CRITICAL 5587d02c: unknown scope_condition predicate now RAISES
    ValueError (was silently treated as False, dropping every affected
    hypothesis without operator alert).
  * HIGH 9f1ae5aa + MED 59c6faa7: near-duplicate dedup no longer
    requires `all(key)` truthy — duplicates with empty key components
    are now correctly skipped.
"""

from __future__ import annotations

import pytest

from audit_pipeline.scoping import filter_hypotheses, load_class_library


# ─────────────── CRITICAL 5587d02c — unknown predicate raises ───────────────


def test_unknown_scope_condition_predicate_raises() -> None:
    """Patch #5 (audit CRITICAL 5587d02c): a typo in a predicate name
    (e.g. 'is_solana' vs 'is-solana') previously caused the matching
    hypothesis to be silently scoped out with no operator alert. Now
    raises ValueError so the operator catches the typo loudly."""
    hyps = [
        {
            "id": "TEST-1",
            "claim": "test claim",
            "applies_to": ["*"],
            "scope_conditions": ["this_predicate_does_not_exist"],
        }
    ]
    with pytest.raises(ValueError, match="unknown scope_condition predicate"):
        filter_hypotheses(
            hyps,
            target_name="anyproto",
            target_conditions={"is_solana": True, "is_evm": False},
        )


def test_known_predicate_evaluates_normally() -> None:
    """Sanity: a KNOWN predicate that's True still passes the hypothesis,
    a KNOWN predicate that's False still skips it (no raise)."""
    hyps = [
        {
            "id": "TEST-2",
            "claim": "test claim 2",
            "applies_to": ["*"],
            "scope_conditions": ["is_solana"],
        }
    ]
    # Pass
    result_pass = filter_hypotheses(
        hyps, target_name="x", target_conditions={"is_solana": True}
    )
    assert len(result_pass.applicable) == 1
    assert not result_pass.skipped
    # Skip (False predicate)
    result_skip = filter_hypotheses(
        hyps, target_name="x", target_conditions={"is_solana": False}
    )
    assert not result_skip.applicable
    assert len(result_skip.skipped) == 1


# ─────────────── HIGH 9f1ae5aa — near-dup dedup with empty key ───────────────


def test_near_dup_dedup_source_drops_all_key_guard() -> None:
    """Patch #5 (audit HIGH 9f1ae5aa + MED 59c6faa7): source-level check
    that the load_class_library dedup loop no longer has the
    `and all(key)` guard in the executable code path. (The same phrase
    appears in the explanatory comment, so we look for the SQL-ish
    `and all(key):` form which only appears in the buggy code line.)"""
    import inspect
    from audit_pipeline import scoping
    src = inspect.getsource(scoping.load_class_library)
    # The buggy form was: `if key in seen_class_target_claim and all(key):`
    # The `:` at end distinguishes the buggy line from the comment text.
    assert "and all(key):" not in src, (
        "load_class_library still has the buggy `and all(key):` guard — "
        "near-duplicates with empty key components will leak through."
    )
    # The fixed form must check membership alone
    assert "if key in seen_class_target_claim:" in src


def test_filter_hypotheses_unknown_predicate_source_check() -> None:
    """Source-level: must use `in cond` membership check, not the
    `cond.get(p, False)` form that silently absorbed typos."""
    import inspect
    from audit_pipeline import scoping
    src = inspect.getsource(scoping.filter_hypotheses)
    # Behavior: the buggy `cond.get(p, False)` form must be gone from
    # the executable path (it can still appear in comments). The fixed
    # form uses an explicit `not in cond` membership check before the
    # truth test.
    assert "p not in cond" in src
    # The new error message words split across two f-string lines —
    # check both halves are present.
    assert "unknown" in src and "scope_condition predicate" in src
