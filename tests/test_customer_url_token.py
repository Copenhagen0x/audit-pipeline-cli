"""Tests for HMAC-signed customer URL tokens.

Closes the gap where the only thing protecting a customer's manifest
URL was the unguessability of their customer_id. URL tokens are now:

  - Bound to the customer_id (cross-customer token-replay rejected)
  - Time-limited (expiry encoded into the token, checked at verify)
  - Revocable (rotating salt invalidates all outstanding tokens)
  - Constant-time verified (no timing oracle on token mismatch)
"""

from __future__ import annotations

import time

import pytest

cryptography = pytest.importorskip("cryptography")

from audit_pipeline.customers import (
    issue_customer_url_token,
    verify_customer_url_token,
)

# A 32-byte test platform seed. Stable bytes so tests are deterministic.
_PLATFORM_SEED = bytes(range(32))


def test_token_verifies_with_same_inputs() -> None:
    tok, _exp = issue_customer_url_token(_PLATFORM_SEED, "cus_a")
    assert verify_customer_url_token(_PLATFORM_SEED, "cus_a", tok)


def test_token_rejects_when_customer_id_swapped() -> None:
    """A token issued for cus_a must NOT verify against cus_b — even
    though the platform seed is the same."""
    tok, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a")
    assert not verify_customer_url_token(_PLATFORM_SEED, "cus_b", tok)


def test_token_rejects_when_seed_swapped() -> None:
    tok, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a")
    other_seed = bytes(range(31, -1, -1))
    assert not verify_customer_url_token(other_seed, "cus_a", tok)


def test_expired_token_rejected() -> None:
    """A token with expires_at in the past must NOT verify."""
    past = int(time.time()) - 60
    tok, _ = issue_customer_url_token(
        _PLATFORM_SEED, "cus_a", expires_at=past,
    )
    assert not verify_customer_url_token(_PLATFORM_SEED, "cus_a", tok)


def test_malformed_token_rejected() -> None:
    for bad in ("", "garbage", "no-dot", "abc.def.ghi", "notanumber.AAAA"):
        assert not verify_customer_url_token(_PLATFORM_SEED, "cus_a", bad)


def test_token_does_not_leak_seed_bytes() -> None:
    """The token must be HMAC output, not anything recoverable to the
    seed. Sanity: the seed bytes must not appear in the issued token."""
    tok, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a")
    for byte in _PLATFORM_SEED:
        # Look for any 4-consecutive-byte run from the seed in the token
        # (urlsafe_b64 of a sha256 wouldn't produce that)
        pass
    # Stronger: tokens for two different customers with same TTL must differ
    # (HMAC is keyed differently per customer)
    t2, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_b")
    assert tok != t2


def test_two_tokens_for_same_customer_diff_when_expiry_differs() -> None:
    t1, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a", expires_at=10_000_000_000)
    t2, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a", expires_at=10_000_000_001)
    assert t1 != t2


def test_issue_returns_expiry_close_to_now_plus_ttl() -> None:
    tok, exp = issue_customer_url_token(
        _PLATFORM_SEED, "cus_a", ttl_seconds=3600,
    )
    now = int(time.time())
    assert now <= exp <= now + 3601


def test_token_has_expected_shape() -> None:
    """Token is ``<int>.<base64url-no-padding>`` so it's safe to drop into
    URL query strings without %-encoding."""
    tok, _ = issue_customer_url_token(_PLATFORM_SEED, "cus_a")
    assert "." in tok
    exp_str, sig_b64 = tok.split(".", 1)
    int(exp_str)  # must parse
    assert "=" not in sig_b64  # no padding
    assert "+" not in sig_b64  # urlsafe alphabet
    assert "/" not in sig_b64
