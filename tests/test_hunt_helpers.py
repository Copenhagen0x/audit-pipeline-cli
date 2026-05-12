"""Direct tests for hunt.py helper surfaces.

Cross-cutting audit gap: hunt.py is 2000+ lines of orchestrator code
and only `_hunt_run` had end-to-end coverage. The webhook SSRF guard,
the slug helper, and the report formatter were untested at the
function level.

This file plugs that gap by exercising the helpers directly so
regressions surface fast.
"""

from __future__ import annotations

import pytest


# ─────────────────── SSRF guard (Orchestration Defect 08) ───────────────────


def test_webhook_url_safe_accepts_slack() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, reason = _webhook_url_safe("https://hooks.slack.com/services/T/B/X")
    assert ok, reason


def test_webhook_url_safe_accepts_discord() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, reason = _webhook_url_safe("https://discord.com/api/webhooks/123/abc")
    assert ok, reason


def test_webhook_url_safe_rejects_non_https() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, reason = _webhook_url_safe("http://hooks.slack.com/abc")
    assert not ok
    assert "https" in reason or "scheme" in reason


def test_webhook_url_safe_rejects_aws_metadata() -> None:
    """AWS instance metadata IP must be blocked — leaking engine_sha
    + finding severities to 169.254.169.254 is an SSRF exfil vector."""
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, reason = _webhook_url_safe("https://169.254.169.254/latest/meta-data/")
    assert not ok
    assert "internal" in reason or "metadata" in reason


def test_webhook_url_safe_rejects_loopback() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, _ = _webhook_url_safe("https://127.0.0.1/spy")
    assert not ok
    ok, _ = _webhook_url_safe("https://localhost/spy")
    assert not ok


def test_webhook_url_safe_rejects_private_ranges() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    for url in (
        "https://10.0.0.5/x",
        "https://192.168.1.1/x",
        "https://172.16.0.1/x",
        "https://172.31.255.255/x",
    ):
        ok, _ = _webhook_url_safe(url)
        assert not ok, f"private range URL slipped past SSRF guard: {url}"


def test_webhook_url_safe_rejects_unknown_host() -> None:
    """Hosts not in the allow-list must be refused even on https."""
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, reason = _webhook_url_safe("https://attacker.example.com/x")
    assert not ok
    assert "allow-list" in reason or "not in" in reason


def test_webhook_url_safe_handles_malformed_url() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, _ = _webhook_url_safe("not-a-url")
    assert not ok


def test_webhook_url_safe_rejects_gcp_metadata() -> None:
    from audit_pipeline.commands.hunt import _webhook_url_safe
    ok, _ = _webhook_url_safe("https://metadata.google.internal/computeMetadata/v1/")
    assert not ok


# ─────────────────── slug helper (L1.5+L2 Defect 03 alignment) ───────────────────


def test_slugify_matches_canonical_slug_for_hypothesis() -> None:
    """hunt._slugify must be a thin wrapper over the canonical
    utils.slug.slug_for_hypothesis so PoC scaffolds route the same
    way as hypothesis IDs."""
    from audit_pipeline.commands.hunt import _slugify
    from audit_pipeline.utils.slug import slug_for_hypothesis
    samples = [
        "H1-vault-residual",
        "this-is-a-very-long-hypothesis-id-far-longer-than-sixty-characters-was-the-limit",
        "weird@chars#that/should!be$sanitized",
    ]
    for s in samples:
        assert _slugify(s) == slug_for_hypothesis(s)


# ─────────────────── time helpers ───────────────────


def test_now_returns_iso8601_utc() -> None:
    from audit_pipeline.commands.hunt import _now
    s = _now()
    # ISO-8601 UTC: YYYY-MM-DDTHH:MM:SS+00:00 or Z suffix
    assert "T" in s
    assert s.endswith("+00:00") or s.endswith("Z")


def test_now_from_epoch() -> None:
    from audit_pipeline.commands.hunt import _now_from
    # 2026-01-01T00:00:00Z = 1767225600
    s = _now_from(1767225600)
    assert s.startswith("2026-01-01T00:00:00")
