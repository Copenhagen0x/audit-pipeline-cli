"""`audit-pipeline heartbeat` — public proof-of-running attestation.

Tier 5 #29. Different from a finding: a finding is a security claim about the
target. A heartbeat is a security claim about *us* — that the loop itself is
still running, the engine SHA is what we say it is, and the platform private
key is still in operator custody. Quiet weeks (no Criticals) are still
trustworthy because the heartbeat keeps ticking.

Cadence: hourly, dispatched by ``deploy/jelleo-heartbeat.{service,timer}``.
Output: ``<workspace>/public/heartbeat.json`` + ``.sig``, mirrored to
``api.jelleo.com/heartbeat.json`` by the existing snapshot publisher hook
(``deploy/publish_cycle.sh`` style).

What's signed: the canonical bytes of ``heartbeat.json``. Re-rendering with
different formatting breaks the signature, so the signed bytes are the source
of truth — not a re-rendered visual.

What's exposed (public):
  - generated_at        : ISO-8601 timestamp, UTC
  - engine_sha          : git rev-parse HEAD on the implementation repo
  - implementation      : "Copenhagen0x/audit-pipeline-cli@<sha>"
  - hostname            : platform hostname
  - cycles_total        : total cycles ever produced
  - cycles_last_24h     : cycles produced in the last 24h
  - last_cycle_ts       : ISO timestamp of the most recent cycle
  - signing_pubkey_b64  : base64(SHA256(platform pubkey PEM)) — fingerprint,
                          stable across heartbeats; consumers can pin it
  - registered_customers: count only (NOT the list — privacy)
  - service_summary     : map of systemd unit → "active"|"inactive"|"unknown"

What's NOT exposed: customer ids, target SHA, finding contents, API keys, SMTP creds.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console

console = Console()

DEFAULT_SERVICES = (
    "jelleo-shadow.service",
    "jelleo-watch.service",
    "jelleo-snapshot.timer",
    "jelleo-scheduler-24h.timer",
    "jelleo-scheduler-weekly.timer",
    "jelleo-scheduler-monthly.timer",
    "jelleo-backup.timer",
    "jelleo-health.timer",
    "jelleo-heartbeat.timer",
)


# ---------------------------------------------------------------------------
# Public payload builder (also unit-testable in isolation)
# ---------------------------------------------------------------------------


def _previous_heartbeat_sha(out_path: Path | None) -> str | None:
    """P3+P4 audit Defect 07 (MED): return the sha256 of the prior signed
    heartbeat payload at ``out_path``, if any. Used to chain heartbeats
    so an observer can detect replay (an attacker who exfiltrated the
    key but is now offline cannot re-serve yesterday's heartbeat as
    today's — the chain breaks)."""
    if not out_path or not out_path.is_file():
        return None
    try:
        import hashlib
        return hashlib.sha256(out_path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_heartbeat(
    workspace: Path,
    *,
    repo_dir: Path | None = None,
    services: tuple[str, ...] = DEFAULT_SERVICES,
    now: datetime | None = None,
    prev_path: Path | None = None,
) -> dict[str, Any]:
    """Build the heartbeat payload (no signing). Pure data-collection function.

    When ``prev_path`` points at a prior signed heartbeat (e.g. the
    output file from the previous run), embed its sha256 as
    ``prev_heartbeat_sha256`` so observers can verify the chain. First
    heartbeat (no prior) gets ``prev_heartbeat_sha256: null``.
    """
    now = now or datetime.now(timezone.utc)

    payload: dict[str, Any] = {
        "schema":        "jelleo-heartbeat-v2",   # bumped to v2 for chain field
        "generated_at":  now.isoformat(timespec="seconds"),
        "hostname":      socket.gethostname(),
        "prev_heartbeat_sha256": _previous_heartbeat_sha(prev_path),
    }

    sha = _engine_sha(repo_dir)
    payload["engine_sha"] = sha
    if sha:
        payload["implementation"] = f"Copenhagen0x/audit-pipeline-cli@{sha[:12]}"

    pubkey_path = workspace / "keys" / "jelleo.ed25519.pub"
    payload["signing_pubkey_fingerprint"] = _pubkey_fingerprint(pubkey_path)

    cycles_total, cycles_last_24h, last_cycle_ts = _cycle_stats(workspace, now)
    payload["cycles_total"] = cycles_total
    payload["cycles_last_24h"] = cycles_last_24h
    payload["last_cycle_ts"] = last_cycle_ts

    payload["registered_customers"] = _customer_count(workspace)

    payload["service_summary"] = _service_summary(services)

    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(name="heartbeat")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Output path (default: <workspace>/public/heartbeat.json)")
@click.option("--no-sign", is_flag=True, default=False,
              help="Skip signing (useful in dev / CI).")
@click.option("--key", type=click.Path(path_type=Path), default=None,
              help="Platform private key (default: <workspace>/keys/jelleo.ed25519)")
@click.option("--repo-dir", type=click.Path(path_type=Path), default=None,
              help="Path to the audit-pipeline-cli repo for engine_sha (default: current dir)")
@click.option("--print", "print_only", is_flag=True, default=False,
              help="Print to stdout instead of writing files.")
@click.pass_context
def heartbeat_cmd(
    ctx: click.Context,
    out_path: Path | None,
    no_sign: bool,
    key: Path | None,
    repo_dir: Path | None,
    print_only: bool,
) -> None:
    """Emit a signed, public proof-of-running heartbeat."""
    workspace = Path(ctx.obj["workspace"])
    # P3+P4 Defect 07: pass the existing heartbeat file (if any) so the
    # new payload commits to its sha256 as ``prev_heartbeat_sha256`` —
    # forming a chain observers can verify for replay-resistance.
    target_path = out_path or (workspace / "public" / "heartbeat.json")
    payload = build_heartbeat(workspace, repo_dir=repo_dir, prev_path=target_path)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if print_only:
        click.echo(body)
        return

    out_path = out_path or (workspace / "public" / "heartbeat.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    console.print(f"[green]Wrote[/green] {out_path}")

    if no_sign:
        console.print("[yellow]--no-sign set; skipped signing.[/yellow]")
        return

    from audit_pipeline.commands.sign import SignError, default_key_path, sign_file
    priv_path = key or default_key_path(workspace)
    try:
        sig_path = sign_file(out_path, priv_path)
    except SignError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]Signed[/green] {sig_path}")


# ---------------------------------------------------------------------------
# Helpers — kept private to this module
# ---------------------------------------------------------------------------


def _engine_sha(repo_dir: Path | None) -> str:
    """Return ``git rev-parse HEAD`` for the implementation repo (or '')."""
    candidate = repo_dir or Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _pubkey_fingerprint(pubkey_path: Path) -> str:
    """First 8 bytes of SHA256(pubkey_pem) as colon-separated hex."""
    if not pubkey_path.exists():
        return ""
    digest = hashlib.sha256(pubkey_path.read_bytes()).digest()
    return ":".join(f"{b:02x}" for b in digest[:8])


def _cycle_stats(workspace: Path, now: datetime) -> tuple[int, int, str | None]:
    """Read cycles from the findings DB (best-effort, tolerant of missing)."""
    import os as _os
    pg_url = _os.environ.get("JELLEO_DB_URL", "").strip()
    db_path = workspace / "findings.db"
    if not pg_url and not db_path.exists():
        return (0, 0, None)
    try:
        from audit_pipeline.db import open_findings_db
        db = open_findings_db(workspace)
    except Exception:
        return (0, 0, None)

    try:
        cycles = db.list_cycles(limit=200)
    except Exception:
        return (0, 0, None)

    if not cycles:
        return (0, 0, None)

    cutoff = now - timedelta(hours=24)
    last_24h = 0
    last_ts = None
    for c in cycles:
        ts_raw = c.get("created_at") or c.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if last_ts is None or ts > last_ts:
            last_ts = ts
        if ts >= cutoff:
            last_24h += 1

    return (
        len(cycles),
        last_24h,
        last_ts.isoformat(timespec="seconds") if last_ts else None,
    )


def _customer_count(workspace: Path) -> int:
    """Count registered customers (excludes the hard-coded demo fallback)."""
    try:
        from audit_pipeline import customers as customers_mod
        return len(customers_mod.load_registry(workspace))
    except Exception:
        return 0


def _service_summary(services: tuple[str, ...]) -> dict[str, str]:
    """Map systemd unit → 'active'|'inactive'|'unknown'.

    On non-systemd hosts (dev laptops) every unit gets 'unknown'. On the VPS
    where systemctl is available, the actual state shows up.
    """
    out: dict[str, str] = {}
    for svc in services:
        out[svc] = _systemctl_is_active(svc)
    return out


def _systemctl_is_active(unit: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    state = (proc.stdout or "").strip()
    if state in {"active", "inactive", "failed"}:
        return state
    return "unknown"


# ---------------------------------------------------------------------------
# Module-level export
# ---------------------------------------------------------------------------


__all__ = ["heartbeat_cmd", "build_heartbeat"]


def _signed_pubkey_fingerprint_b64(pubkey_path: Path) -> str:
    """Base64-encoded SHA256(pubkey) — used by consumers who pin a fingerprint."""
    if not pubkey_path.exists():
        return ""
    digest = hashlib.sha256(pubkey_path.read_bytes()).digest()
    return base64.b64encode(digest).decode()
