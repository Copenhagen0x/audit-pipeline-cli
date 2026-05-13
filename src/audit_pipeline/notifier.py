"""Email transport for Jelleo notifications and reports.

Two channels:

  * Immediate alert:    on a confirmed Critical or High finding, the customer's
                        primary on-call gets an email within seconds. No batching.
  * Cadence digest:     24h / weekly / monthly rollups, with the signed PDF +
                        signature attached.

Configuration is read from environment variables (preferred — no plaintext
credentials in the repo) or the workspace's `notifier.json` (for non-secret
recipient lists). See the module docstring of `commands/notify.py` for the
full configuration spec.

Required env vars (or settings file equivalents) for SMTP transport:
    JELLEO_SMTP_HOST
    JELLEO_SMTP_PORT       (default 587)
    JELLEO_SMTP_USER
    JELLEO_SMTP_PASSWORD
    JELLEO_SMTP_FROM       (default = SMTP_USER)
    JELLEO_SMTP_TLS        (default 'starttls' — also: 'ssl' or 'none')
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any


class NotifierError(Exception):
    """Raised when email transport fails or configuration is incomplete."""


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    from_addr: str = ""
    tls_mode: str = "starttls"  # 'starttls' | 'ssl' | 'none'
    timeout_sec: int = 30

    @classmethod
    def from_env(cls) -> SmtpConfig:
        host = os.environ.get("JELLEO_SMTP_HOST")
        if not host:
            raise NotifierError(
                "JELLEO_SMTP_HOST not set. Configure SMTP via env or "
                "workspace config (see commands/notify.py docstring)."
            )
        user = os.environ.get("JELLEO_SMTP_USER") or None
        return cls(
            host=host,
            port=int(os.environ.get("JELLEO_SMTP_PORT", "587")),
            user=user,
            password=os.environ.get("JELLEO_SMTP_PASSWORD") or None,
            from_addr=os.environ.get("JELLEO_SMTP_FROM") or user or "",
            tls_mode=os.environ.get("JELLEO_SMTP_TLS", "starttls"),
        )


@dataclass
class NotifierSettings:
    """Per-workspace notifier configuration. Loaded from workspace/notifier.json.

    Recipients is a dict of channel-name -> list of email addresses. Channels:
        critical_oncall   — primary on-call, gets immediate Critical/High alerts
        critical_team     — secondary CC list, also gets immediate alerts
        cadence_24h       — daily rollup recipients
        cadence_weekly    — weekly rollup recipients
        cadence_monthly   — monthly rollup recipients

    active_targets is an optional allow-list of target names. When present and
    non-empty, the cadence scheduler will only fire reports for these targets
    (instead of every target in the DB). Lets stale/internal scopes stay in
    the DB without spamming the inbox.
    """
    recipients: dict[str, list[str]] = field(default_factory=dict)
    active_targets: list[str] | None = None
    smtp: SmtpConfig | None = None
    dry_run: bool = False

    @classmethod
    def load(cls, workspace: Path, dry_run: bool = False) -> NotifierSettings:
        path = workspace / "notifier.json"
        active_targets: list[str] | None = None
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            recipients = {k: list(v) for k, v in (data.get("recipients") or {}).items()}
            raw_targets = data.get("active_targets")
            if isinstance(raw_targets, list) and raw_targets:
                active_targets = [str(t) for t in raw_targets if isinstance(t, str)]
        else:
            recipients = {}
        smtp = None
        if not dry_run:
            try:
                smtp = SmtpConfig.from_env()
            except NotifierError:
                smtp = None
        return cls(
            recipients=recipients,
            active_targets=active_targets,
            smtp=smtp,
            dry_run=dry_run,
        )

    def recipients_for(self, channel: str) -> list[str]:
        return list(self.recipients.get(channel, []))


# ---------------------------------------------------------------------------
# Low-level send
# ---------------------------------------------------------------------------


def _build_message(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[Path] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jelleo.com")
    msg["X-Jelleo-Version"] = "v0.1"
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    for attach_path in attachments or []:
        if not attach_path.exists():
            raise NotifierError(f"Attachment missing: {attach_path}")
        data = attach_path.read_bytes()
        # Naive MIME guess by extension — sufficient for our payload set
        # (PDF reports + .sig text + occasional .md/.html).
        suffix = attach_path.suffix.lower()
        if suffix == ".pdf":
            maintype, subtype = "application", "pdf"
        elif suffix in (".md", ".txt", ".sig"):
            maintype, subtype = "text", "plain"
        elif suffix == ".html":
            maintype, subtype = "text", "html"
        elif suffix == ".json":
            maintype, subtype = "application", "json"
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=attach_path.name,
        )

    return msg


def _send(message: EmailMessage, smtp: SmtpConfig) -> None:
    context = ssl.create_default_context()
    if smtp.tls_mode == "ssl":
        with smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=smtp.timeout_sec, context=context) as s:
            if smtp.user:
                s.login(smtp.user, smtp.password or "")
            s.send_message(message)
    elif smtp.tls_mode == "starttls":
        with smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout_sec) as s:
            s.ehlo()
            s.starttls(context=context)
            s.ehlo()
            if smtp.user:
                s.login(smtp.user, smtp.password or "")
            s.send_message(message)
    elif smtp.tls_mode == "none":
        with smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout_sec) as s:
            if smtp.user:
                s.login(smtp.user, smtp.password or "")
            s.send_message(message)
    else:
        raise NotifierError(f"Unknown JELLEO_SMTP_TLS mode: {smtp.tls_mode!r}")


def send_email(
    settings: NotifierSettings,
    to: list[str],
    subject: str,
    body_text: str,
    *,
    cc: list[str] | None = None,
    body_html: str | None = None,
    attachments: list[Path] | None = None,
) -> dict[str, Any]:
    """Send an email. Returns a result dict.

    If settings.dry_run is True, no SMTP call is made; the message is
    rendered and returned for inspection only. Used by `audit-pipeline
    notify --dry-run` and unit tests.
    """
    if not to:
        raise NotifierError("send_email called with empty `to` list")
    if not settings.smtp and not settings.dry_run:
        raise NotifierError(
            "SMTP not configured. Set JELLEO_SMTP_HOST + credentials, or "
            "pass --dry-run to inspect the message without sending."
        )

    sender = (settings.smtp.from_addr if settings.smtp else "no-reply@jelleo.com")
    msg = _build_message(
        sender=sender,
        to=to,
        cc=cc or [],
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )

    if settings.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "to": to,
            "cc": cc or [],
            "subject": subject,
            "body_text_len": len(body_text),
            "body_html_len": len(body_html or ""),
            "n_attachments": len(attachments or []),
        }

    _send(msg, settings.smtp)  # raises on failure
    return {
        "ok": True,
        "dry_run": False,
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# High-level notification senders
# ---------------------------------------------------------------------------


_CRITICAL_TEXT_TEMPLATE = """\
{severity} finding confirmed on {target_name}.

Bug class:    {bug_class}
Hypothesis:   {hypothesis_id}
Cycle:        {cycle_id}
Status:       confirmed
First seen:   {created_at}

Title:
  {title}

Repro:
  {repro_link}

Dashboard:
  {dashboard_link}

This is an immediate notification per the Jelleo reporting policy
(jelleo.com/methodology.html#reporting). The 24-hour rollup will follow
on the next cadence cycle.

— Jelleo · jelleo.com
"""


# POST-AUDIT FIX: per-(finding_id, hour) email dedup so a runaway loop
# can't mail-bomb the on-call channel. Keyed by (cycle, finding_id, hour
# bucket); cleared on a per-process basis. NOT persistent across hunt
# restarts — that's a feature: a fresh hunt invocation should re-alert
# on its own findings (since the alert is also the "I noticed this"
# signal, not just a courtesy ping).
_critical_alert_cache: dict[tuple, float] = {}
_critical_alert_window_s = 3600.0   # 1 hour per (cycle, finding) key


def _critical_alert_already_sent(cycle_id: str, finding_id: object) -> bool:
    """Return True if we've sent this alert within the dedup window."""
    import time as _time
    key = (str(cycle_id), str(finding_id))
    now = _time.time()
    last = _critical_alert_cache.get(key)
    if last is None:
        return False
    return (now - last) < _critical_alert_window_s


def _critical_alert_record_sent(cycle_id: str, finding_id: object) -> None:
    import time as _time
    _critical_alert_cache[(str(cycle_id), str(finding_id))] = _time.time()


def send_critical_alert(
    settings: NotifierSettings,
    *,
    target_name: str,
    finding: dict[str, Any],
    cycle_id: str,
    repro_link: str = "",
    dashboard_link: str = "https://jelleo.com/dashboard.html",
) -> dict[str, Any]:
    """Send the immediate alert for a confirmed Critical/High finding.

    Goes to the 'critical_oncall' channel; CCs the 'critical_team' channel.

    POST-AUDIT FIX: dedups by (cycle_id, finding_id) within a 1-hour
    window so a buggy auto-promote loop or propagation sweep that mints
    N CRITICAL findings doesn't mail-bomb the on-call channel — and so
    real alerts don't get silently dropped by SMTP-provider rate limits.
    """
    fid = finding.get("id")
    if fid is not None and _critical_alert_already_sent(cycle_id, fid):
        return {
            "skipped": "rate_limited",
            "cycle_id": cycle_id,
            "finding_id": fid,
            "reason": "duplicate alert within 1h window",
        }

    severity = finding.get("severity", "High")
    body = _CRITICAL_TEXT_TEMPLATE.format(
        severity=severity,
        target_name=target_name,
        bug_class=finding.get("bug_class") or "(unclassified)",
        hypothesis_id=finding.get("hypothesis_id") or "(none)",
        cycle_id=cycle_id,
        created_at=finding.get("created_at") or "(unknown)",
        title=finding.get("title") or finding.get("hypothesis_id") or "(no title)",
        repro_link=repro_link or "(no public repro yet — embargoed)",
        dashboard_link=dashboard_link,
    )

    subject = (
        f"[Jelleo] {severity} confirmed · {target_name} · "
        f"{finding.get('bug_class') or finding.get('hypothesis_id') or 'finding'}"
    )

    to = settings.recipients_for("critical_oncall")
    cc = settings.recipients_for("critical_team")
    if not to and not settings.dry_run:
        raise NotifierError(
            "No 'critical_oncall' recipients configured in workspace/notifier.json"
        )
    if not to and settings.dry_run:
        to = ["oncall@example.com"]  # dry-run placeholder

    result = send_email(settings, to=to, cc=cc, subject=subject, body_text=body)
    if fid is not None:
        _critical_alert_record_sent(cycle_id, fid)
    return result


_CADENCE_TEXT_TEMPLATE = """\
{cadence} report for {target_name}.

Window:       {window_label}
Cycles:       {n_cycles}
Findings:     {n_findings}
Critical:     {n_critical}
High:         {n_high}
Medium:       {n_medium}
Low:          {n_low}
Info:         {n_info}

Signed PDF and signature attached. Verify the signature with:

  audit-pipeline sign verify {report_filename} {report_filename}.sig \\
      --pubkey jelleo.ed25519.pub

Methodology:    https://jelleo.com/methodology.html
Security:       https://jelleo.com/security.html
Dashboard:      {dashboard_link}

— Jelleo · jelleo.com
"""


def send_cadence_report(
    settings: NotifierSettings,
    *,
    cadence: str,                     # '24h' | 'weekly' | 'monthly'
    target_name: str,
    report_path: Path,
    sig_path: Path | None = None,
    summary: dict[str, Any] | None = None,
    dashboard_link: str = "https://jelleo.com/dashboard.html",
) -> dict[str, Any]:
    """Send a scheduled cadence report (24h/weekly/monthly) to the customer.

    Attaches the signed report and its signature if present.
    """
    if cadence not in {"24h", "weekly", "monthly"}:
        raise NotifierError(f"unknown cadence {cadence!r}")
    s = summary or {}

    body = _CADENCE_TEXT_TEMPLATE.format(
        cadence=cadence.capitalize() if cadence != "24h" else "24-hour",
        target_name=target_name,
        window_label=s.get("window_label", "(window)"),
        n_cycles=s.get("n_cycles", "?"),
        n_findings=s.get("n_findings", "?"),
        n_critical=s.get("n_critical", "?"),
        n_high=s.get("n_high", "?"),
        n_medium=s.get("n_medium", "?"),
        n_low=s.get("n_low", "?"),
        n_info=s.get("n_info", "?"),
        report_filename=report_path.name,
        dashboard_link=dashboard_link,
    )

    subject = f"[Jelleo] {cadence} report · {target_name} · {datetime.now(timezone.utc):%Y-%m-%d}"

    channel = f"cadence_{cadence}" if cadence != "24h" else "cadence_24h"
    to = settings.recipients_for(channel)
    if not to and not settings.dry_run:
        raise NotifierError(
            f"No '{channel}' recipients configured in workspace/notifier.json"
        )
    if not to and settings.dry_run:
        to = ["customer@example.com"]

    attachments: list[Path] = [report_path]
    if sig_path and sig_path.exists():
        attachments.append(sig_path)

    return send_email(
        settings, to=to, subject=subject, body_text=body, attachments=attachments,
    )


def smtp_test(settings: NotifierSettings, to: list[str]) -> dict[str, Any]:
    """Send a test email through the configured SMTP transport."""
    return send_email(
        settings,
        to=to,
        subject="[Jelleo] SMTP test",
        body_text=(
            "This is a test from `audit-pipeline notify test`. Receipt of "
            "this email confirms the workspace's SMTP configuration is "
            "correct.\n\n— Jelleo · jelleo.com"
        ),
    )
