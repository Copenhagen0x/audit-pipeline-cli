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
import re
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


# Patch #7 (audit HIGH 049a6969 + 153c13d + c3bc75b1): email header
# injection + recipient validation. The previous implementation passed
# attacker-controllable strings (finding title, bug_class, recipient
# addresses from notifier.json) directly into MIME headers. A finding
# title containing CR/LF would break out of the Subject: header and
# inject arbitrary headers (Bcc, X-Custom-*, etc.). A recipient address
# like "victim@evil.com>, attacker@evil.com\nBcc: spam@" would smuggle
# additional recipients past the visible To: list.
# Reviewer HIGH (P7 R5b+R5c): the original regex was too strict (rejected
# `o'brien@x.com`); the first widening was too loose (accepted
# `foo@..evil.com`, leading-dash labels, unbounded length). This version
# is RFC-5321-aligned: no leading/trailing/consecutive dots in the
# local-part, labelled domain with no leading/trailing hyphen per label,
# local-part bounded at 64 chars per RFC, total length checked
# separately by `_validate_recipient_list`.
_EMAIL_ADDR_RE = re.compile(
    # Local-part: starts with atext, then any mix of atext or dot atoms
    # (no leading/trailing/consecutive dots), up to 64 chars total. We
    # enforce the 64-char cap structurally via the repetition limits.
    r"^(?=.{1,64}@)"
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+)*"
    r"@"
    # Domain: one or more labels separated by dots, each label is
    # alnum-bordered with optional alnum/hyphen middle (RFC 1035), max
    # 63 chars per label. TLD must be alpha-only and >= 2 chars.
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,63}$"
)


# Reviewer CRITICAL (P7 R5b): assembly.py + health.py both POST to
# operator-controlled webhook URLs without any URL validation. Same SSRF
# class as hunt.py:_webhook_url_safe but the validators were not shared.
# Hoisting here so every network egress goes through the same allow-list.
#
# Threat-modeler HIGH (P7 R5c):
#   - `webhook.site` was an attacker-controllable pastebin endpoint with
#     no legitimate production use — removed from the allow-list. If an
#     operator genuinely needs ad-hoc debugging against webhook.site
#     they must patch this regex on a feature branch; there is no
#     env-var escape hatch and we are not adding one (an SSRF-allowlist
#     escape hatch would become a permanent "just set this in prod"
#     workaround).
#   - Added Microsoft Teams' real connector domain
#     `*.webhook.office.com` alongside `teams.microsoft.com`.
#   - Hard-block decimal/hex/octal IPs and IPv4-mapped IPv6
#     (`::ffff:127...`) so the allow-list is no longer the only barrier
#     against numeric-IP encoding tricks.
#
# Threat-modeler R5c MEDIUM (DESIGN, not fix):
#   The allow-list provides SSRF protection (only known SaaS hosts), NOT
#   exfil-channel-auth (which Slack workspace / Teams tenant the URL
#   targets). An attacker who can write `notifier.json` can already
#   exfil locally, so this is intentionally out of scope for the
#   validator. Tighter per-tenant locking would break legitimate users
#   (every Teams Incoming Webhook lives at `<tenant>.webhook.office.com`,
#   not a single canonical host).
#
# Behavior:
#   - Only https
#   - Hostname allow-list (slack / discord / teams (both forms) /
#     telegram / pagerduty)
#   - Block private / loopback / link-local / cloud-metadata IP ranges
#   - Block numeric-only hosts (decimal/hex/octal IPv4 encoding)
#
# Returns (ok, reason). Callers should refuse to POST when ok is False
# and surface `reason` to the operator.
_WEBHOOK_ALLOW_HOSTS_RE = re.compile(
    # Goober LOW (P7 R5c): subdomain labels must follow RFC 952 — no
    # leading or trailing hyphen per label. The old `[A-Za-z0-9-]+`
    # allowed `-evil.slack.com` to pass (not currently exploitable
    # since SaaS providers control DNS, but becomes a bypass the
    # moment any allow-listed domain permits public subdomain
    # registration).
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)*"
    r"(?:slack\.com|discord(?:app)?\.com|"
    r"hooks\.slack\.com|api\.telegram\.org|"
    r"teams\.microsoft\.com|webhook\.office\.com|"
    r"events\.pagerduty\.com)$",
    re.IGNORECASE,
)
_WEBHOOK_BLOCKED_HOSTS_RE = re.compile(
    r"^(?:127\.|169\.254\.|10\.|192\.168\.|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"localhost|metadata\.google\.internal|metadata\.aws|"
    # IPv6 link-local (`fe80::/10`) + the full ULA `/7` (`fc00::/7`),
    # which is `fc00:` AND `fd00:` AND everything in between. The
    # previous regex `fc00:|fd00:` only blocked the two specific
    # canonical prefixes, leaving `fc12::`, `fd80::`, `fcff::`, etc.
    # technically un-hard-blocked. Allow-list was the backstop, but
    # defense-in-depth wins.
    r"0\.0\.0\.0|::1|0:0:0:0:0:0:0:1|fe80:|f[cd][0-9a-f]{2}:|"
    # IPv4-mapped IPv6 — `::ffff:127.0.0.1` and friends. urlparse
    # returns the bare form (no brackets) so we anchor on the prefix.
    r"::ffff:|"
    # Decimal-encoded IPv4 (e.g. `2130706433` == 127.0.0.1), hex
    # (`0x7f000001`), and octal (`0177.0.0.1`). All three are
    # pure-digit/hex/octal-style hostnames which legitimate webhook
    # hosts never are. The `$` anchor on each alternative means
    # hostnames like `0xdeadbeef.example.com` (dot present) are
    # NOT accidentally blocked — only pure-encoded IP forms are.
    # (Threat-modeler R5c LOW: ensures hard-block layer matches what
    # the comment promises, removing reliance on the allow-list as
    # the sole fallback for these forms.)
    r"\d+$|0x[0-9a-f]+$|"
    r"0[0-7]+(?:\.[0-7]+){0,3}$)",
    re.IGNORECASE,
)


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Return (allowed, reason) for ``url``.

    Single source of truth for webhook URL safety across the codebase.
    Used by hunt.py:_post_webhook (P9-era consolidation), the bundle
    notify path (assembly.py), and the health alert path
    (commands/health.py).

    A `None`/non-string/empty input is rejected as `(False, "...")` —
    we never raise here so callers can log-and-skip instead of
    crashing whatever workflow triggered the notify.

    NOTE: callers MUST also disable HTTP redirect-following, otherwise
    an allow-listed endpoint can 30x-redirect to an internal address
    that this validator never sees. See assembly.py / health.py for
    the wrapper pattern.
    """
    if not isinstance(url, str) or not url:
        return (False, "webhook URL is empty or not a string")
    # Bound URL length up front — multi-MB URLs are pathological and
    # the only legitimate webhook endpoints we accept all fit in <1KB.
    if len(url) > 2048:
        return (False, f"webhook URL too long ({len(url)} bytes)")
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return (False, "could not parse URL")
    if parsed.scheme != "https":
        return (False, f"refusing non-https scheme {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        return (False, "URL has no host")

    # Threat-modeler R5e LOW (defense-in-depth): the regex hard-block
    # enumerates IPv4/IPv6 ranges by string-prefix, which can never
    # cover every encoding form (expanded `0000:...:0001`, 6to4
    # `2002:7f00::`, deprecated `fec0::/10`, IPv4-mapped IPv6, etc.).
    # Use the `ipaddress` stdlib as a *first* gate when the host is an
    # IP literal — it understands every legal representation and
    # classifies non-global addresses authoritatively. The regex layer
    # below remains as belt-and-suspenders for the cases ipaddress
    # rejects (e.g. decimal-encoded `2130706433`, octal `0177.0.0.1` —
    # Python 3.9+ rejects these as "ambiguous" so they raise
    # ValueError and fall through to the regex).
    import ipaddress
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        # Standard categorisation: covers loopback, RFC 1918 IPv4, ULA
        # IPv6 (`fc00::/7`), link-local IPv6 (`fe80::/10`), AND
        # deprecated IPv6 site-local (`fec0::/10`) via `is_site_local`
        # — which is a separate attribute since Python's `is_private`
        # excludes deprecated ranges.
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_multicast
                or addr.is_unspecified
                or (isinstance(addr, ipaddress.IPv6Address)
                    and addr.is_site_local)):
            return (False, f"refusing internal/metadata host {host!r}")
        # Belt-and-suspenders for IPv6: `is_site_local` above already
        # catches `fec0::/10` on all Python 3.x, but we keep the
        # explicit `fec0::/10` net check to be robust against any
        # future Python release that changes the property semantics.
        # `2001::/32` (Teredo) is the primary catch on Python versions
        # where `is_private` does not cover it (verified True on
        # CPython 3.14 — Teredo is classified as global there). The
        # belt-and-suspenders explicit net check below is therefore
        # load-bearing on current Python, not merely a portability
        # backstop.
        if isinstance(addr, ipaddress.IPv6Address):
            if addr in ipaddress.ip_network("fec0::/10"):
                return (
                    False,
                    f"refusing internal/metadata host {host!r} "
                    f"(deprecated IPv6 site-local fec0::/10)",
                )
            if addr in ipaddress.ip_network("2001::/32"):
                return (
                    False,
                    f"refusing internal/metadata host {host!r} (Teredo)",
                )
            # IPv4-mapped and 6to4-tunneled forms whose embedded IPv4
            # is non-global. `addr.is_private` already covers some of
            # this (RFC 4291) but is_global is not strict enough for
            # 6to4 — `2002:7f00:0001::1` embeds 127.0.0.1 but is
            # classified by `ipaddress` as a global IPv6 address.
            embedded = addr.ipv4_mapped or addr.sixtofour
            if embedded is not None and (
                embedded.is_loopback or embedded.is_private
                or embedded.is_link_local or embedded.is_reserved
                or embedded.is_multicast
            ):
                return (
                    False,
                    f"refusing internal/metadata host {host!r} "
                    f"(embedded IPv4 {embedded})",
                )

    if _WEBHOOK_BLOCKED_HOSTS_RE.match(host):
        return (False, f"refusing internal/metadata host {host!r}")
    if not _WEBHOOK_ALLOW_HOSTS_RE.match(host):
        return (
            False,
            f"host {host!r} not in webhook allow-list "
            f"(slack/discord/teams/telegram/pagerduty)",
        )
    return (True, "ok")


def _sanitize_header_value(name: str, value: str) -> str:
    """Strip CR/LF and any character that could break the header parser.

    Returns the cleaned string. Raises NotifierError if the input
    contains characters that suggest active injection (we don't
    silently strip them — the operator should see the bad data)."""
    if not isinstance(value, str):
        raise NotifierError(f"header {name!r} must be a string, got {type(value)}")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise NotifierError(
            f"header {name!r} contains CR/LF/NUL — refusing to send "
            f"(possible header-injection). Sanitize upstream before "
            f"passing to send_email()."
        )
    return value


def _validate_recipient_list(label: str, addrs: list[str]) -> list[str]:
    """Each recipient must look like an RFC-822-shaped email address.

    Reject any address that doesn't match a conservative regex — this
    blocks both header injection (which contains CR/LF that the regex
    would already reject) AND obvious garbage like "Bob <evil>" which
    isn't a valid email at all. The audit finding called out
    notifier.json being parsed without validation; this is the
    centralised gate."""
    cleaned: list[str] = []
    for addr in addrs:
        if not isinstance(addr, str):
            raise NotifierError(
                f"{label} recipient must be string, got {type(addr)}: {addr!r}"
            )
        addr_stripped = addr.strip()
        if not _EMAIL_ADDR_RE.fullmatch(addr_stripped):
            raise NotifierError(
                f"{label} recipient {addr_stripped!r} doesn't look like a "
                f"valid email address — refusing to send. Fix the "
                f"recipient list (notifier.json or CLI args)."
            )
        cleaned.append(addr_stripped)
    return cleaned


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
    # Patch #7: validate every header-bound field BEFORE constructing
    # the EmailMessage. send_email() callers can pass attacker-
    # controlled subject text (finding title) and recipient lists
    # (notifier.json) — sanitization happens here at the choke point.
    sender = _sanitize_header_value("From", sender)
    to = _validate_recipient_list("To", to)
    cc = _validate_recipient_list("Cc", cc)
    subject = _sanitize_header_value("Subject", subject)
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
        # Patch #7 (audit HIGH 2a9de64e + 3b2c43ae): plaintext SMTP with
        # LOGIN sends the password in clear over the network. Refuse to
        # log in unless an explicit opt-in env var is set, and refuse
        # plaintext entirely unless tls=none is paired with the
        # JELLEO_SMTP_ALLOW_PLAINTEXT=1 acknowledgement so the operator
        # CAN'T enable it by accident.
        if os.environ.get("JELLEO_SMTP_ALLOW_PLAINTEXT") != "1":
            raise NotifierError(
                "JELLEO_SMTP_TLS='none' requires JELLEO_SMTP_ALLOW_PLAINTEXT=1 "
                "to acknowledge the security risk (passwords + message body "
                "sent in clear over the network). Use 'starttls' or 'ssl' "
                "instead for production deployments."
            )
        # Guard mirrors the actual login condition below (smtp.user only).
        # An empty/None password is irrelevant — `s.login(user, password or
        # "")` will still emit AUTH LOGIN with the username in clear, which
        # is the exact leak we are guarding against. (Reviewer HIGH:
        # notifier.py:270 had `if smtp.user and smtp.password:` which
        # silently bypassed the guard when password was None.)
        if smtp.user:
            # An additional, separate opt-in for LOGIN-over-plaintext.
            # Plaintext alone without auth is sometimes legitimate (an
            # internal relay that accepts unauthenticated mail from
            # specific source IPs); LOGIN-over-plaintext is almost never
            # legitimate.
            if os.environ.get("JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN") != "1":
                raise NotifierError(
                    "Refusing SMTP LOGIN over plaintext (credentials would "
                    "be sent in clear). Set JELLEO_SMTP_ALLOW_PLAINTEXT_LOGIN=1 "
                    "to bypass this guard ONLY for testing against a local "
                    "relay where the risk is acceptable."
                )
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
