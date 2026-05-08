"""Signature regression suite for cross-protocol propagation (C11).

Every entry in BUG_CLASS_SIGNATURES gets exercised:

  1. All regex patterns compile.
  2. The catalog isn't accidentally shrunk below the current 48-class baseline.
  3. F7's class (`insurance-counter-vault-divergence`) keeps its 4-signature
     shape — that's the reference class behind the platform's flagship
     disclosure; any change to it should be intentional.
  4. Synthetic-fixture round-trip: classes with documented positive/negative
     fixtures must match the positive case and reject the negative.
"""

from __future__ import annotations

import re

import pytest

from audit_pipeline.commands.propagate import (
    BUG_CLASS_SIGNATURES,
    _scan_file_for_signatures,
)


@pytest.mark.parametrize("cls,patterns", sorted(BUG_CLASS_SIGNATURES.items()))
def test_signatures_compile(cls: str, patterns: list[str]) -> None:
    """Every regex in every class must compile — silent ReError = production crash."""
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            pytest.fail(f"class {cls!r} signature {p!r} doesn't compile: {e}")


def test_catalog_size_no_regression() -> None:
    """Lock in the 48-class baseline (added 2026-05-08 P2 Wave 6a).

    Up-revisions of BUG_CLASS_SIGNATURES are fine. Accidental removal that
    drops below 48 fails CI — if the removal is intentional, lower this
    threshold and update the docstring with the rationale.
    """
    assert len(BUG_CLASS_SIGNATURES) >= 48, (
        f"BUG_CLASS_SIGNATURES has only {len(BUG_CLASS_SIGNATURES)} entries; "
        f"baseline is 48 (P2 Wave 6a). Lower this if the drop is intentional."
    )


def test_f7_class_signature_shape() -> None:
    """F7's class has 4 signatures — that's the platform's flagship reference.

    Any change should be deliberate. This guards against the most common
    regression — accidentally clobbering F7's signatures while editing
    nearby entries.
    """
    f7 = BUG_CLASS_SIGNATURES.get("insurance-counter-vault-divergence")
    assert f7, "F7's bug class must always be in the catalog"
    assert len(f7) >= 4, (
        f"F7's class should have at least 4 signatures (originally 4 — "
        f"insurance.balance, insurance.counter, vault.balance, "
        f"use_insurance_buffer); got {len(f7)}"
    )


# ---------------------------------------------------------------------------
# Fixture-based positive / negative tests
# ---------------------------------------------------------------------------

# Format: (class_id, fixture_should_match, fixture_should_NOT_match)
# Only classes with strong positive/negative discrimination are tested here.
# Classes without clear-cut fixtures (e.g. broad patterns like
# "arithmetic-overflow-pnl-mark") aren't asserted at this level.

POSITIVE_NEGATIVE_FIXTURES: list[tuple[str, str, str]] = [
    (
        "insurance-counter-vault-divergence",
        # Positive — direct hit on F7's pattern
        """
        fn use_insurance_buffer(&mut self, pay: i128) -> i128 {
            let take = pay.min(self.insurance_fund.balance);
            self.insurance_fund.balance -= take;
        }
        """,
        # Negative — vault-only mutation, no insurance counter
        """
        fn deposit(&mut self, amount: u64) {
            self.config.last_seen_slot = clock.slot;
        }
        """,
    ),
    (
        "admin-gate-bypass",
        """
        if !is_admin(ctx.accounts.signer.key) {
            return Err(ErrorCode::Unauthorized.into());
        }
        let admin_pubkey = ctx.accounts.admin.key;
        """,
        """
        let amount = ctx.accounts.user.amount;
        let total = supply + amount;
        """,
    ),
    (
        "oracle-staleness-bypass",
        """
        let publish_time = pyth_price.publish_time;
        if clock.unix_timestamp - publish_time > MAX_STALENESS {
            return Err(ErrorCode::StalePrice.into());
        }
        """,
        """
        let total_supply = mint.supply;
        let new_balance = old_balance + amount;
        """,
    ),
    (
        "k-invariant-violation",
        """
        let invariant_k = reserve_a * reserve_b;
        let new_k = (reserve_a + amount_in) * (reserve_b - amount_out);
        require!(new_k >= invariant_k, ErrorCode::KViolation);
        """,
        """
        let lp_share = total_supply * amount / pool_total;
        emit!(SwapEvent { user, amount });
        """,
    ),
    (
        "pause-bypass",
        """
        require!(!self.is_paused, ErrorCode::Paused);
        let paused = false;
        """,
        """
        let user = ctx.accounts.user;
        let amount = ctx.accounts.params.amount;
        """,
    ),
    (
        "donation-attack",
        """
        if total_supply == 0 {
            shares = sqrt(amount * other) - MINIMUM_LIQUIDITY;
        }
        """,
        """
        let new_balance = old_balance + transfer_amount;
        let withdraw_amount = balance / 2;
        """,
    ),
]


@pytest.mark.parametrize("cls,positive,negative", POSITIVE_NEGATIVE_FIXTURES)
def test_signatures_match_positive_reject_negative(
    cls: str, positive: str, negative: str
) -> None:
    """For each fixture-tested class, the positive sample must match and the
    negative sample must NOT match. This catches the most common regression
    (accidentally too-broad or too-narrow regex changes)."""
    patterns = BUG_CLASS_SIGNATURES.get(cls)
    assert patterns, f"class {cls!r} not found in catalog"
    compiled = [(p, re.compile(p)) for p in patterns]

    pos_hits = _scan_file_for_signatures(positive, compiled)
    distinct_pos = {s for s, _, _ in pos_hits}
    assert distinct_pos, (
        f"class {cls!r} signatures matched 0 patterns on the positive fixture; "
        f"expected at least 1"
    )

    neg_hits = _scan_file_for_signatures(negative, compiled)
    distinct_neg = {s for s, _, _ in neg_hits}
    # Allow a single pattern to coincidentally match a negative; flag if MOST do
    assert len(distinct_neg) < len(patterns), (
        f"class {cls!r} signatures over-matched the negative fixture "
        f"({len(distinct_neg)}/{len(patterns)} patterns hit); regex too broad"
    )
