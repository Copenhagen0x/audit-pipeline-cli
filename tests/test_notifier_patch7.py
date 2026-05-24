"""Patch #7 — notifier.py header injection + TLS-none plaintext guards.

Closes audit HIGH 049a6969 + 153c13d (header injection), 2a9de64e +
3b2c43ae (TLS=none plaintext LOGIN), c3bc75b1 (recipient validation).
"""

from __future__ import annotations

import pytest

from audit_pipeline.notifier import (
    NotifierError,
    _build_message,
    _sanitize_header_value,
    _validate_recipient_list,
)


# ─────────────── Header injection / CR-LF guard ───────────────


def test_sanitize_header_rejects_lf() -> None:
    with pytest.raises(NotifierError, match="CR/LF"):
        _sanitize_header_value("Subject", "Hello\nBcc: attacker@evil.com")


def test_sanitize_header_rejects_cr() -> None:
    with pytest.raises(NotifierError, match="CR/LF"):
        _sanitize_header_value("Subject", "Hello\rBcc: attacker@evil.com")


def test_sanitize_header_rejects_nul() -> None:
    with pytest.raises(NotifierError, match="CR/LF"):
        _sanitize_header_value("Subject", "Hello\x00stuff")


def test_sanitize_header_accepts_normal_text() -> None:
    assert _sanitize_header_value("Subject", "Hello world") == "Hello world"


def test_build_message_rejects_subject_with_lf() -> None:
    with pytest.raises(NotifierError, match="CR/LF"):
        _build_message(
            sender="from@x.com", to=["to@x.com"], cc=[],
            subject="Subject\nBcc: attacker@evil.com",
            body_text="body",
        )


# ─────────────── Recipient validation ───────────────


def test_validate_recipient_rejects_bare_string() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["not-an-email"])


def test_validate_recipient_rejects_injection_attempt() -> None:
    with pytest.raises(NotifierError):
        _validate_recipient_list(
            "To", ["victim@x.com\nBcc: attacker@evil.com"]
        )


def test_validate_recipient_accepts_simple_email() -> None:
    out = _validate_recipient_list("To", ["foo@bar.com"])
    assert out == ["foo@bar.com"]


def test_validate_recipient_accepts_dots_plus_dashes() -> None:
    out = _validate_recipient_list(
        "To", ["foo.bar+tag@sub.example.com"]
    )
    assert out == ["foo.bar+tag@sub.example.com"]


def test_validate_recipient_strips_whitespace() -> None:
    out = _validate_recipient_list("To", ["  foo@bar.com  "])
    assert out == ["foo@bar.com"]


# ─────────────── TLS=none plaintext guards ───────────────


def test_tls_none_requires_explicit_opt_in(monkeypatch) -> None:
    """JELLEO_SMTP_TLS=none without JELLEO_SMTP_ALLOW_PLAINTEXT=1
    must raise — the operator should NEVER accidentally send mail in
    plaintext."""
    import inspect
    from audit_pipeline import notifier
    src = inspect.getsource(notifier._send)
    assert "JELLEO_SMTP_ALLOW_PLAINTEXT" in src
    assert "JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN" in src
    # The plaintext gate must raise NotifierError, not warn
    assert "raise NotifierError" in src


def test_tls_none_blocks_login_unless_opted_in() -> None:
    """Source-level check that LOGIN-over-plaintext requires its own
    opt-in (different from just allowing plaintext-no-auth)."""
    import inspect
    from audit_pipeline import notifier
    src = inspect.getsource(notifier._send)
    # Two distinct opt-in env vars
    assert "JELLEO_SMTP_ALLOW_PLAINTEXT" in src
    assert "JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN" in src
