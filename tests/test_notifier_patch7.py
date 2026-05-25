"""Patch #7 — notifier.py header injection + TLS-none plaintext guards +
webhook URL SSRF allow-list.

Closes audit HIGH 049a6969 + 153c13d (header injection), 2a9de64e +
3b2c43ae (TLS=none plaintext LOGIN), c3bc75b1 (recipient validation),
plus the P7 R5b reviewer CRITICALs on assembly.py +
commands/health.py unguarded webhook sinks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from audit_pipeline.notifier import (
    NotifierError,
    SmtpConfig,
    _build_message,
    _sanitize_header_value,
    _send,
    _validate_recipient_list,
    validate_webhook_url,
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


# Reviewer HIGH (P7 R5b): the original regex was too strict and rejected
# legitimate addresses with apostrophes, ampersands, etc. Lock in the
# broader RFC-compatible local-part charset so we don't silently bounce
# real users.


def test_validate_recipient_accepts_apostrophe_in_local_part() -> None:
    # Irish/Scottish surnames commonly produce apostrophes in addresses.
    out = _validate_recipient_list("To", ["sean.o'brien@example.com"])
    assert out == ["sean.o'brien@example.com"]


def test_validate_recipient_accepts_ampersand_in_local_part() -> None:
    # Shared corporate mailboxes — "support&billing@…" is in the wild.
    out = _validate_recipient_list("To", ["support&billing@example.com"])
    assert out == ["support&billing@example.com"]


def test_validate_recipient_accepts_subdomained_address() -> None:
    out = _validate_recipient_list(
        "To", ["finding@cycle.audit.team.example.com"]
    )
    assert out == ["finding@cycle.audit.team.example.com"]


def test_validate_recipient_still_rejects_no_at() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["just-text"])


def test_validate_recipient_still_rejects_no_tld() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["foo@bar"])


def test_validate_recipient_still_rejects_leading_hyphen_label() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["foo@-bad.com"])


# ─────────────── TLS=none plaintext guards (BEHAVIORAL) ───────────────
#
# Reviewer HIGH (P7 R5b): the previous tests were source-inspection only
# — they grepped the function body for env-var names but never proved
# the guard actually fires. Replace with mocked-SMTP behavioral tests
# that exercise the real control flow.


def _msg() -> object:
    """Build a minimal EmailMessage stand-in (just needs to be
    something _send can pass to s.send_message). Real EmailMessage works
    fine but we don't need any of its behavior here."""
    from email.message import EmailMessage
    em = EmailMessage()
    em["Subject"] = "test"
    em["From"] = "a@x.com"
    em["To"] = "b@x.com"
    em.set_content("body")
    return em


def test_tls_none_without_optin_raises(monkeypatch) -> None:
    """JELLEO_SMTP_TLS=none without JELLEO_SMTP_ALLOW_PLAINTEXT=1
    must raise — the operator should NEVER accidentally send mail in
    plaintext."""
    monkeypatch.delenv("JELLEO_SMTP_ALLOW_PLAINTEXT", raising=False)
    monkeypatch.delenv("JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN", raising=False)
    cfg = SmtpConfig(host="relay.example.com", port=25, tls_mode="none")
    with patch("smtplib.SMTP") as mock_smtp:
        with pytest.raises(NotifierError, match="JELLEO_SMTP_ALLOW_PLAINTEXT=1"):
            _send(_msg(), cfg)  # type: ignore[arg-type]
        mock_smtp.assert_not_called()


def test_tls_none_with_optin_but_user_no_password_still_blocks_login(monkeypatch) -> None:
    """Reviewer HIGH: previously `if smtp.user and smtp.password:`
    silently bypassed the LOGIN-plaintext guard when password was None,
    BUT the actual s.login() call still emitted AUTH LOGIN with the
    username in clear. Lock in that the guard fires off `smtp.user`
    alone (matching the real login condition)."""
    monkeypatch.setenv("JELLEO_SMTP_ALLOW_PLAINTEXT", "1")
    monkeypatch.delenv("JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN", raising=False)
    cfg = SmtpConfig(
        host="relay.example.com",
        port=25,
        tls_mode="none",
        user="alice@example.com",
        password=None,  # <-- the exact bypass condition
    )
    with patch("smtplib.SMTP") as mock_smtp:
        with pytest.raises(NotifierError, match="JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN"):
            _send(_msg(), cfg)  # type: ignore[arg-type]
        # CRITICAL: the SMTP connection must NEVER be opened — the
        # guard must fire BEFORE we contact the relay.
        mock_smtp.assert_not_called()


def test_tls_none_with_both_optins_actually_sends(monkeypatch) -> None:
    """Both opt-ins set: the message goes out and login() is called."""
    monkeypatch.setenv("JELLEO_SMTP_ALLOW_PLAINTEXT", "1")
    monkeypatch.setenv("JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN", "1")
    cfg = SmtpConfig(
        host="relay.example.com",
        port=25,
        tls_mode="none",
        user="alice@example.com",
        password="hunter2",
    )
    mock_session = MagicMock()
    mock_smtp_cls = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_session
    with patch("smtplib.SMTP", mock_smtp_cls):
        _send(_msg(), cfg)  # type: ignore[arg-type]
    mock_smtp_cls.assert_called_once()
    mock_session.login.assert_called_once_with("alice@example.com", "hunter2")
    mock_session.send_message.assert_called_once()


def test_tls_none_no_auth_with_plaintext_optin_works(monkeypatch) -> None:
    """Plaintext-no-auth (e.g. internal relay accepting unauthenticated
    mail from specific source IPs) is allowed with only the first
    opt-in."""
    monkeypatch.setenv("JELLEO_SMTP_ALLOW_PLAINTEXT", "1")
    monkeypatch.delenv("JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN", raising=False)
    cfg = SmtpConfig(
        host="relay.internal", port=25, tls_mode="none", user=None,
    )
    mock_session = MagicMock()
    mock_smtp_cls = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_session
    with patch("smtplib.SMTP", mock_smtp_cls):
        _send(_msg(), cfg)  # type: ignore[arg-type]
    mock_session.login.assert_not_called()
    mock_session.send_message.assert_called_once()


def test_starttls_mode_does_login(monkeypatch) -> None:
    """Starttls is the prod default; LOGIN over TLS is fine."""
    cfg = SmtpConfig(
        host="smtp.example.com",
        port=587,
        tls_mode="starttls",
        user="alice@example.com",
        password="hunter2",
    )
    mock_session = MagicMock()
    mock_smtp_cls = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_session
    with patch("smtplib.SMTP", mock_smtp_cls):
        _send(_msg(), cfg)  # type: ignore[arg-type]
    mock_session.starttls.assert_called_once()
    mock_session.login.assert_called_once_with("alice@example.com", "hunter2")


# ─────────────── Webhook URL SSRF allow-list ───────────────
#
# Reviewer CRITICAL (P7 R5b): assembly.py:_fire_bundle_notification and
# commands/health.py both POSTed to operator-controlled URLs with zero
# validation. Lock in the shared allow-list.


def test_webhook_url_rejects_http_scheme() -> None:
    ok, reason = validate_webhook_url("http://hooks.slack.com/services/T0/B0/abc")
    assert ok is False
    assert "non-https" in reason


def test_webhook_url_rejects_file_scheme() -> None:
    ok, reason = validate_webhook_url("file:///etc/passwd")
    assert ok is False
    # urlparse maps a no-host file:// to empty hostname, so it gets
    # rejected at the scheme step (non-https) — either path is fine.


def test_webhook_url_rejects_javascript_scheme() -> None:
    ok, reason = validate_webhook_url("javascript:alert(1)")
    assert ok is False
    assert "non-https" in reason


def test_webhook_url_rejects_loopback() -> None:
    ok, reason = validate_webhook_url("https://127.0.0.1/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_link_local() -> None:
    ok, reason = validate_webhook_url("https://169.254.169.254/latest/meta-data/")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_rfc1918_10() -> None:
    ok, reason = validate_webhook_url("https://10.0.0.1/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_rfc1918_192_168() -> None:
    ok, reason = validate_webhook_url("https://192.168.1.1/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_rfc1918_172_16() -> None:
    ok, reason = validate_webhook_url("https://172.16.5.5/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_localhost_hostname() -> None:
    ok, reason = validate_webhook_url("https://localhost/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_gcp_metadata_hostname() -> None:
    ok, reason = validate_webhook_url("https://metadata.google.internal/computeMetadata/v1/")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_unknown_host() -> None:
    ok, reason = validate_webhook_url("https://attacker.example.com/hook")
    assert ok is False
    assert "allow-list" in reason


def test_webhook_url_accepts_slack() -> None:
    ok, reason = validate_webhook_url(
        "https://hooks.slack.com/services/T00000000/B00000000/XXX"
    )
    assert ok is True
    assert reason == "ok"


def test_webhook_url_accepts_discord() -> None:
    ok, _ = validate_webhook_url(
        "https://discord.com/api/webhooks/123/abc"
    )
    assert ok is True


def test_webhook_url_accepts_discordapp() -> None:
    ok, _ = validate_webhook_url(
        "https://discordapp.com/api/webhooks/123/abc"
    )
    assert ok is True


def test_webhook_url_accepts_pagerduty() -> None:
    ok, _ = validate_webhook_url("https://events.pagerduty.com/v2/enqueue")
    assert ok is True


def test_webhook_url_accepts_telegram() -> None:
    ok, _ = validate_webhook_url(
        "https://api.telegram.org/bot123:abc/sendMessage"
    )
    assert ok is True


def test_webhook_url_accepts_teams_app_domain() -> None:
    # Microsoft Teams in-app (deeplink-style) webhook host.
    ok, _ = validate_webhook_url(
        "https://outlook.office365.com.teams.microsoft.com/webhook/xyz"
    )
    assert ok is True


def test_webhook_url_accepts_teams_office_connector() -> None:
    # The REAL MS Teams Incoming Webhook connector host shape:
    # <tenant>.webhook.office.com — code-reviewer LOW caught that the
    # original allow-list only had teams.microsoft.com, which silently
    # blocked operators using legit O365 connectors.
    ok, _ = validate_webhook_url(
        "https://contoso.webhook.office.com/webhookb2/abc/IncomingWebhook/def"
    )
    assert ok is True


def test_webhook_url_rejects_empty() -> None:
    ok, reason = validate_webhook_url("")
    assert ok is False
    assert "empty" in reason


def test_webhook_url_rejects_non_string() -> None:
    ok, reason = validate_webhook_url(None)  # type: ignore[arg-type]
    assert ok is False
    assert "empty" in reason or "not a string" in reason


def test_webhook_url_case_insensitive_host() -> None:
    # IDN / casing should not let an attacker dodge the allow-list.
    ok, _ = validate_webhook_url(
        "https://HOOKS.SLACK.COM/services/T0/B0/abc"
    )
    assert ok is True


def test_webhook_url_rejects_userinfo_smuggle() -> None:
    """https://hooks.slack.com@attacker.com/ — urlparse.hostname is
    attacker.com, not hooks.slack.com, so the allow-list block must
    fire."""
    ok, reason = validate_webhook_url(
        "https://hooks.slack.com@attacker.example.com/hook"
    )
    assert ok is False
    assert "allow-list" in reason


# ─────────────── Threat-modeler P7 R5c hardening ───────────────


def test_webhook_url_rejects_webhook_site() -> None:
    """Threat-modeler HIGH: webhook.site is an attacker-controllable
    pastebin-style endpoint with no legitimate production use. It was
    allow-listed historically for ad-hoc debugging but combined with
    redirect-following it became the cleanest SSRF-to-IMDS pivot. Lock
    in that it's no longer accepted."""
    ok, reason = validate_webhook_url("https://webhook.site/abc-def-ghi")
    assert ok is False
    assert "allow-list" in reason


def test_webhook_url_rejects_decimal_encoded_ip() -> None:
    """https://2130706433/ == https://127.0.0.1/ . Reviewer MEDIUM:
    must be hard-blocked, not just allow-list-fallthrough-blocked."""
    ok, reason = validate_webhook_url("https://2130706433/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_hex_encoded_ip() -> None:
    ok, reason = validate_webhook_url("https://0x7f000001/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_ipv4_mapped_ipv6_loopback() -> None:
    """`::ffff:127.0.0.1` is the IPv4-mapped IPv6 form of 127.0.0.1.
    Goober MEDIUM: the original BLOCKED regex covered `::1` but not
    `::ffff:…` — defense-in-depth gap, allow-list was the only barrier."""
    ok, reason = validate_webhook_url("https://[::ffff:127.0.0.1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_expanded_ipv6_loopback() -> None:
    """The expanded form `0:0:0:0:0:0:0:1` of `::1`."""
    ok, reason = validate_webhook_url("https://[0:0:0:0:0:0:0:1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_excessive_length() -> None:
    """Threat-modeler: bound URL length so pathological multi-MB URLs
    don't get parsed in the first place."""
    huge_url = "https://hooks.slack.com/" + ("a" * 4000)
    ok, reason = validate_webhook_url(huge_url)
    assert ok is False
    assert "too long" in reason


def test_webhook_url_rejects_ipv6_link_local_bracketed() -> None:
    ok, reason = validate_webhook_url("https://[fe80::1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_octal_encoded_ip() -> None:
    """Threat-modeler R5c LOW: hard-block layer must catch octal IPv4
    encoding too, not just rely on allow-list fallthrough."""
    ok, reason = validate_webhook_url("https://0177.0.0.1/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_leading_hyphen_subdomain() -> None:
    """Goober LOW (P7 R5c): subdomain regex must enforce RFC 952 — no
    leading/trailing hyphen per label. Was previously
    `[A-Za-z0-9-]+` which accepted `-evil.slack.com`."""
    ok, reason = validate_webhook_url("https://-evil.slack.com/hook")
    assert ok is False
    assert "allow-list" in reason


def test_webhook_url_rejects_trailing_hyphen_subdomain() -> None:
    ok, reason = validate_webhook_url("https://evil-.slack.com/hook")
    assert ok is False
    assert "allow-list" in reason


def test_webhook_url_rejects_ula_ipv6_middle_range() -> None:
    """Goober + threat-modeler R5d LOW: the original hard-block only
    matched the canonical `fc00:` and `fd00:` prefixes, leaving the
    rest of the ULA `/7` range (`fc01::`, `fd80::`, `fcff::`, etc.)
    not hard-blocked. R5d widens to `f[cd][0-9a-f]{2}:` to cover the
    full range."""
    for ula in ("fc12::1", "fd80::1", "fcff::1", "fd00::1", "fc00::1"):
        ok, reason = validate_webhook_url(f"https://[{ula}]/hook")
        assert ok is False, f"{ula} should be blocked"
        assert "internal/metadata" in reason, (
            f"{ula} should be hard-blocked, not allow-list-blocked; "
            f"got reason={reason!r}"
        )


# ─────────────── Threat-modeler R5e LOW (ipaddress hardening) ───────────────


def test_webhook_url_rejects_expanded_ipv6_loopback_all_zeros() -> None:
    """Threat-modeler R5e LOW: the regex hard-block had `0:0:0:0:0:0:0:1`
    (8-group short form) but not the fully-expanded
    `0000:0000:0000:0000:0000:0000:0000:0001`. R5f closes the class
    via Python's `ipaddress` module which understands every
    representation."""
    ok, reason = validate_webhook_url(
        "https://[0000:0000:0000:0000:0000:0000:0000:0001]/hook"
    )
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_deprecated_ipv6_site_local() -> None:
    """Threat-modeler R5e LOW: deprecated site-local `fec0::/10` was
    not hard-blocked. Caught at the ipaddress layer via `is_site_local`
    (Python excludes this deprecated range from `is_private` /
    `is_reserved`, but `is_site_local` still returns True for it).
    The explicit `fec0::/10` net-containment check is a
    belt-and-suspenders backup."""
    ok, reason = validate_webhook_url("https://[fec0::1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_ipv6_multicast() -> None:
    """Threat-modeler R5e LOW: multicast `ff02::1` had no hard-block
    coverage; ipaddress.is_multicast handles it now."""
    ok, reason = validate_webhook_url("https://[ff02::1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_6to4_with_embedded_private_ipv4() -> None:
    """Threat-modeler R5e LOW: 6to4 tunnel `2002::/16` can embed any
    IPv4 address — `2002:7f00:0001::1` is `2002:` + `7f.00.00.01` =
    loopback over 6to4. On hosts with 6to4 configured this routes to
    127.0.0.1. ipaddress.sixtofour extracts the embedded v4 so we
    can re-classify."""
    ok, reason = validate_webhook_url(
        "https://[2002:7f00:0001::1]/hook"
    )
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_ipv4_mapped_ipv6_via_ipaddress() -> None:
    """Same `::ffff:127.0.0.1` already covered by the regex prefix
    `::ffff:`, but now ALSO covered by the ipaddress check via
    .ipv4_mapped extraction — defense-in-depth verification."""
    ok, reason = validate_webhook_url("https://[::ffff:127.0.0.1]/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_rejects_ipv4_unspecified() -> None:
    """0.0.0.0 — `is_unspecified` catches it via ipaddress, in
    addition to the existing regex `0.0.0.0` match."""
    ok, reason = validate_webhook_url("https://0.0.0.0/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_still_rejects_decimal_after_ipaddress_layer() -> None:
    """Sanity: the ipaddress layer raises ValueError for `2130706433`
    (Python 3.9+ refuses ambiguous formats). Verify the regex
    fallback still catches it."""
    ok, reason = validate_webhook_url("https://2130706433/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_still_rejects_octal_after_ipaddress_layer() -> None:
    """Sanity: octal `0177.0.0.1` also raises ValueError in
    ipaddress; regex fallback still catches it."""
    ok, reason = validate_webhook_url("https://0177.0.0.1/hook")
    assert ok is False
    assert "internal/metadata" in reason


def test_webhook_url_still_accepts_normal_subdomain() -> None:
    """Sanity: tightening the subdomain regex must NOT break legit
    multi-label hosts like `hooks.slack.com` or `api-v2.slack.com`."""
    ok, _ = validate_webhook_url(
        "https://api-v2.hooks.slack.com/services/T/B/x"
    )
    assert ok is True


# ─────────────── Tighter _EMAIL_ADDR_RE (P7 R5c) ───────────────


def test_validate_recipient_rejects_consecutive_dots_in_local_part() -> None:
    """Threat-modeler MEDIUM: `ad..min@x.com` passes the original regex
    but RFC 5321 forbids consecutive dots in local-part. Many receiving
    MTAs silently drop these — security alerts vanish without trace."""
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["ad..min@example.com"])


def test_validate_recipient_rejects_leading_dot_in_local_part() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", [".admin@example.com"])


def test_validate_recipient_rejects_trailing_dot_in_local_part() -> None:
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", ["admin.@example.com"])


def test_validate_recipient_rejects_overlong_local_part() -> None:
    """RFC 5321 caps local-part at 64 octets. 65+ chars must reject."""
    too_long = "a" * 65 + "@example.com"
    with pytest.raises(NotifierError, match="valid email"):
        _validate_recipient_list("To", [too_long])


def test_validate_recipient_accepts_max_length_local_part() -> None:
    """64-char local-part is the RFC max; must accept."""
    at_limit = "a" * 64 + "@example.com"
    out = _validate_recipient_list("To", [at_limit])
    assert out == [at_limit]


# ─────────────── assembly.py SSRF guard wired ───────────────


def test_fire_bundle_notification_blocks_unallowed_webhook(tmp_path, capsys) -> None:
    """assembly.py must refuse to POST to a non-allow-listed URL even
    if the operator put it in notifier.json."""
    import json as _json

    from audit_pipeline.bundle.assembly import _fire_bundle_notification

    (tmp_path / "notifier.json").write_text(
        _json.dumps({
            "bundle_events": True,
            "webhook_url":   "https://169.254.169.254/leak",
        }),
        encoding="utf-8",
    )
    with patch("urllib.request.urlopen") as mock_urlopen:
        _fire_bundle_notification(
            workspace=tmp_path,
            finding_id=1,
            prev_status="drafted",
            new_status="ready",
            note=None,
        )
        # The validator must short-circuit BEFORE we hit the network.
        mock_urlopen.assert_not_called()
    err = capsys.readouterr().err
    assert "bundle_webhook_blocked" in err


def test_fire_bundle_notification_truncates_huge_note(tmp_path) -> None:
    """Unbounded note field must be truncated before serialisation."""
    import json as _json

    from audit_pipeline.bundle.assembly import _fire_bundle_notification

    (tmp_path / "notifier.json").write_text(
        _json.dumps({
            "bundle_events": True,
            "webhook_url":   "https://hooks.slack.com/services/T/B/x",
        }),
        encoding="utf-8",
    )
    huge = "A" * 10_000
    with patch("urllib.request.build_opener") as mock_build_opener, \
         patch("urllib.request.Request") as mock_request:
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener
        _fire_bundle_notification(
            workspace=tmp_path,
            finding_id=1,
            prev_status="drafted",
            new_status="ready",
            note=huge,
        )
        # Recover the payload we serialised
        call_kwargs = mock_request.call_args.kwargs
        body = call_kwargs["data"].decode("utf-8")
        parsed = _json.loads(body)
        assert len(parsed["note"]) <= 2100  # truncated, not 10_000
        assert parsed["note"].endswith("[truncated]")
        mock_opener.open.assert_called_once()


def test_fire_bundle_notification_caps_response_read(tmp_path) -> None:
    """Threat-modeler HIGH (P7 R5c): the previous code did `.read()`
    with no cap — a slow-drip adversary could stream GB into memory.
    Lock in `.read(<small N>)`."""
    import json as _json

    from audit_pipeline.bundle.assembly import _fire_bundle_notification

    (tmp_path / "notifier.json").write_text(
        _json.dumps({
            "bundle_events": True,
            "webhook_url":   "https://hooks.slack.com/services/T/B/x",
        }),
        encoding="utf-8",
    )
    mock_response = MagicMock()
    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_response
    with patch("urllib.request.build_opener", return_value=mock_opener):
        _fire_bundle_notification(
            workspace=tmp_path,
            finding_id=1,
            prev_status="drafted",
            new_status="ready",
            note=None,
        )
    # .read() must have been called with a size argument, NOT unbounded.
    mock_response.read.assert_called_once()
    args, _kwargs = mock_response.read.call_args
    assert len(args) >= 1, "expected .read(N), got .read() (unbounded)"
    assert isinstance(args[0], int)
    assert args[0] <= 65536, "read cap too large to count as a real bound"


def test_fire_bundle_notification_blocks_redirects(tmp_path) -> None:
    """Threat-modeler HIGH (P7 R5c): without a no-redirect handler an
    allow-listed webhook can 301-redirect to http://169.254.169.254/
    and bypass validate_webhook_url (which only sees the *initial*
    URL). Lock in that a custom HTTPRedirectHandler subclass is
    installed and that it raises rather than following."""
    import json as _json
    from urllib.error import HTTPError

    from audit_pipeline.bundle.assembly import _fire_bundle_notification

    (tmp_path / "notifier.json").write_text(
        _json.dumps({
            "bundle_events": True,
            "webhook_url":   "https://hooks.slack.com/services/T/B/x",
        }),
        encoding="utf-8",
    )

    # Capture the handler class build_opener was given so we can
    # behaviorally verify it.
    captured_handlers: list = []

    def _fake_build_opener(*handlers):
        captured_handlers.extend(handlers)
        return MagicMock()

    with patch("urllib.request.build_opener", side_effect=_fake_build_opener):
        _fire_bundle_notification(
            workspace=tmp_path,
            finding_id=1,
            prev_status="drafted",
            new_status="ready",
            note=None,
        )

    # We must have installed AT LEAST one custom handler — and one of
    # them must be a HTTPRedirectHandler subclass whose
    # redirect_request raises (rather than returning a new Request,
    # which is the default behavior that follows the 301).
    # build_opener accepts BOTH handler classes AND handler instances,
    # so check both forms.
    import urllib.request

    def _is_redirect_h(h) -> bool:
        if isinstance(h, type):
            return issubclass(h, urllib.request.HTTPRedirectHandler)
        return isinstance(h, urllib.request.HTTPRedirectHandler)

    redirect_handlers = [h for h in captured_handlers if _is_redirect_h(h)]
    assert redirect_handlers, (
        "expected a no-redirect HTTPRedirectHandler subclass "
        f"installed; got {captured_handlers!r}"
    )
    h_obj_or_cls = redirect_handlers[0]
    # Instantiate if we were given a class
    h = h_obj_or_cls() if isinstance(h_obj_or_cls, type) else h_obj_or_cls
    fake_req = MagicMock()
    fake_req.full_url = "https://hooks.slack.com/x"
    with pytest.raises(HTTPError):
        h.redirect_request(
            fake_req, MagicMock(), 301, "Moved",
            {"Location": "http://169.254.169.254/"},
            "http://169.254.169.254/",
        )


# ─────────────── hunt.py delegates to the shared validator ───────────────


def test_hunt_webhook_safe_delegates_to_notifier() -> None:
    """Reviewer MEDIUM (P7 R5c) consolidation: hunt.py's
    _webhook_url_safe must now delegate to
    notifier.validate_webhook_url, not maintain its own duplicate
    regex pair."""
    from audit_pipeline.commands.hunt import _webhook_url_safe
    # The delegating wrapper must return identical results across a
    # representative attack matrix.
    cases = [
        "https://hooks.slack.com/services/T/B/x",  # allow
        "https://127.0.0.1/hook",                  # block
        "https://attacker.example.com/hook",       # allow-list miss
        "http://hooks.slack.com/services/T/B/x",   # scheme reject
        "https://webhook.site/abc",                # newly removed
        "https://2130706433/hook",                 # decimal IP
    ]
    for url in cases:
        ok_hunt, _ = _webhook_url_safe(url)
        ok_notif, _ = validate_webhook_url(url)
        assert ok_hunt == ok_notif, (
            f"hunt._webhook_url_safe and notifier.validate_webhook_url "
            f"disagree on {url!r} — duplicate validator drift"
        )
