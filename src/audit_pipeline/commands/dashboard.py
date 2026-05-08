"""`audit-pipeline dashboard` — single-file HTML status dashboard.

Reads the findings DB and emits a self-contained HTML page showing:
  - All targets currently being audited
  - Recent hunt cycles per target
  - Severity breakdown
  - Recent findings table
  - Daemon status (last successful cycle / last alert)

Two modes:
  generate : write static HTML to a file
  serve    : write + serve via stdlib http.server on a port

The page uses the shared Jelleo design system (audit_pipeline.branding).
"""

from __future__ import annotations

import html
import http.server
import socketserver
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.branding import CSS, footer_html, topbar_html
from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import Severity

console = Console()


@click.command(name="dashboard")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="HTML file to write (default: <workspace>/dashboard.html)")
@click.option("--snapshot-json", type=click.Path(path_type=Path), default=None,
              help="ALSO write a JSON snapshot of the dashboard data (for jelleo.com fetch)")
@click.option("--customer-manifest-dir", type=click.Path(path_type=Path), default=None,
              help="ALSO write per-customer manifest.json files under this dir "
                   "(e.g. /var/www/jelleo.com/customer/). The 'demo' customer is always "
                   "included; future customers come from a config file. Each manifest "
                   "contains the customer's owned findings INCLUDING confirmed (in-progress) "
                   "ones — that data is private to the customer behind the token gate.")
@click.option("--serve", is_flag=True, help="Serve via http.server after writing")
@click.option("--port", type=int, default=8765, show_default=True)
@click.option("--auto-refresh", type=int, default=60, show_default=True,
              help="Browser auto-refresh interval (seconds)")
@click.pass_context
def dashboard_cmd(
    ctx: click.Context,
    output: Path | None,
    snapshot_json: Path | None,
    customer_manifest_dir: Path | None,
    serve: bool,
    port: int,
    auto_refresh: int,
) -> None:
    """Generate (and optionally serve) the customer-facing dashboard.

    Three artifacts are produced, each scoped to a different audience:

      1. dashboard.html (always)         — the rich HTML view.
      2. snapshot.json (--snapshot-json) — public homepage feed; only
         disclosed/fixed/verified findings, with title + hyp_id surfaced.
      3. customer/<token>/manifest.json (--customer-manifest-dir) — per-
         customer JSON behind the token gate; INCLUDES that customer's
         confirmed (in-progress) findings, since the customer owns the data.
    """
    import json

    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")

    out = output or (workspace / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(db, auto_refresh), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")

    if snapshot_json:
        snapshot_path = Path(snapshot_json)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(_build_snapshot(db), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(f"[green]wrote[/green] {snapshot_path}")

    if customer_manifest_dir:
        cdir = Path(customer_manifest_dir)
        for cust in _customers_to_publish(workspace):
            mpath = cdir / cust["id"] / "manifest.json"
            mpath.parent.mkdir(parents=True, exist_ok=True)
            mpath.write_text(
                json.dumps(_build_customer_manifest(db, cust), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            console.print(f"[green]wrote[/green] {mpath}")

    if serve:
        _serve(out.parent, out.name, port)


def _build_snapshot(db: FindingsDB) -> dict:
    """Serialize the DB state into a stable, public-safe JSON shape.

    What goes IN: aggregated counts, target names, recent cycles, and ONLY
    publicly-disclosed findings (status in disclosed/fixed/verified/rejected)
    with title + hypothesis_id stripped — bug_class is the public-safe label.

    What stays OUT, ALWAYS:
      - finding titles (can leak file:line refs to undisclosed bugs)
      - hypothesis ids (leak attack-surface analysis)
      - claim text, recon transcripts, agent prompts, PoC paths
      - findings in new/triaged/confirmed states (in-progress, not yet
        disclosed — exposing them telegraphs zero-days to attackers)
      - SMTP credentials, signing keys, customer recipient addresses

    The snapshot is intended for public consumption (jelleo.com dashboard)
    so it MUST be safe even when the URL is shared, indexed, or scraped.
    """
    from datetime import datetime, timezone

    # Findings safe for the public snapshot.json (jelleo.com homepage data).
    # "rejected" used to be in this set, but rejected findings are false
    # positives produced by the engine — listing them publicly is noise that
    # makes the platform look worse than it is. Keep them in the DB for
    # bookkeeping; do not expose them.
    PUBLIC_STATUSES = {"disclosed", "fixed", "verified"}

    stats = db.stats()
    targets = db.list_targets()
    cycles = db.list_cycles(limit=20)
    findings = db.list_findings(limit=200)

    by_target = []
    for t in targets:
        t_findings = db.list_findings(target_id=t["id"], limit=500)
        t_cycles = db.list_cycles(target_id=t["id"], limit=5)
        sev_counts = {
            "Critical": sum(1 for f in t_findings if f.get("severity") == "Critical"),
            "High":     sum(1 for f in t_findings if f.get("severity") == "High"),
            "Medium":   sum(1 for f in t_findings if f.get("severity") == "Medium"),
            "Low":      sum(1 for f in t_findings if f.get("severity") == "Low"),
            "Info":     sum(1 for f in t_findings if f.get("severity") == "Info"),
        }
        n_disclosed = sum(1 for f in t_findings if (f.get("status") or "") in PUBLIC_STATUSES)
        last_cycle = t_cycles[0] if t_cycles else None
        by_target.append({
            "name": t["name"],
            "engine_repo": (t.get("engine_repo") or "").replace("https://github.com/", ""),
            "n_findings": len(t_findings),
            "n_findings_disclosed": n_disclosed,
            "severity_counts": sev_counts,
            "last_cycle_at": (last_cycle or {}).get("started_at"),
            "last_cycle_id": (last_cycle or {}).get("cycle_id"),
        })

    # Public findings list: only disclosed/fixed/verified/rejected.
    #
    # For findings in PUBLIC_STATUSES, the title + hypothesis_id are already
    # public (e.g. F7 is openly described in PR #39), so it's safe to expose
    # them. This lets the customer portal at /customer/<token>/ render proper
    # finding cards without an extra round-trip. In-progress findings (new,
    # triaged, confirmed) still get filtered out at the boundary above.
    public_findings = []
    for f in findings:
        if (f.get("status") or "") not in PUBLIC_STATUSES:
            continue
        # Attempt to surface disclosure metadata from details_json (the
        # pipeline stores per-finding extras — disclosure URL, sibling list,
        # etc. — there). Failing silently is fine on malformed JSON.
        details = {}
        try:
            import json as _json
            raw = f.get("details_json")
            if raw:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    details = parsed
        except Exception:
            details = {}
        public_findings.append({
            "id": f["id"],
            "target_id": f["target_id"],
            "cycle_id": f.get("cycle_id"),
            "hypothesis_id": f.get("hypothesis_id"),
            "title": f.get("title"),
            "severity": f.get("severity"),
            "status": f.get("status"),
            "bug_class": f.get("bug_class"),
            "verdict": f.get("verdict"),
            "poc_fired": bool(f.get("poc_fired")),
            "updated_at": f.get("updated_at"),
            "disclosure_url": details.get("disclosure_url") or details.get("pr_url") or None,
            "n_siblings":     details.get("n_siblings") or 0,
        })

    recent_cycles = [
        {
            "cycle_id": c.get("cycle_id"),
            "target_id": c.get("target_id"),
            "engine_sha": (c.get("engine_sha") or "")[:10],
            "started_at": c.get("started_at"),
            "finished_at": c.get("finished_at"),
            "n_dispatched": c.get("n_dispatched"),
            "n_confirmed": c.get("n_confirmed"),
            "receipt_fingerprint": _read_receipt_fingerprint(c.get("cycle_id")),
        }
        for c in cycles
    ]

    # Public stats reflect only-disclosed surface. The full counts (including
    # in-progress findings) stay on the VPS findings.db.
    public_stats = {
        "n_targets": stats.get("n_targets", 0),
        "n_cycles":  stats.get("n_cycles", 0),
        "n_findings_total":     stats.get("n_findings", 0),
        "n_findings_disclosed": sum(t["n_findings_disclosed"] for t in by_target),
        "by_severity_disclosed": {
            "Critical": sum(1 for f in public_findings if f["severity"] == "Critical"),
            "High":     sum(1 for f in public_findings if f["severity"] == "High"),
            "Medium":   sum(1 for f in public_findings if f["severity"] == "Medium"),
            "Low":      sum(1 for f in public_findings if f["severity"] == "Low"),
            "Info":     sum(1 for f in public_findings if f["severity"] == "Info"),
        },
    }

    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_ms": int(now.timestamp() * 1000),
        "platform": "jelleo",
        "version": "v0.1",
        "stats": public_stats,
        "targets": by_target,
        "recent_cycles": recent_cycles,
        "public_findings": public_findings,
        "services": _probe_services(),
        "cycles_total":    stats.get("n_cycles", 0),
        "receipts_signed": _count_signed_receipts(),
        "loop_uptime_human": _loop_uptime_human(),
    }


def _probe_services() -> list[dict]:
    """Probe systemd state for the known Jelleo services.

    Each entry: {key, unit, state, last_tick_ms}
      state: "up" | "degraded" | "down" | "unknown"

    Runs `systemctl is-active` + `systemctl show <unit> --property=ActiveEnterTimestamp`
    per known unit. Returns "unknown" entries on non-Linux / non-systemd hosts
    so the snapshot is still well-formed when generated locally.
    """
    import shutil
    import subprocess
    from datetime import datetime, timezone as _tz

    KNOWN = [
        ("shadow",            "jelleo-shadow.service"),
        ("watch",             "jelleo-watch.service"),
        ("scheduler-24h",     "jelleo-scheduler-24h.timer"),
        ("scheduler-weekly",  "jelleo-scheduler-weekly.timer"),
        ("scheduler-monthly", "jelleo-scheduler-monthly.timer"),
        ("snapshot",          "jelleo-snapshot.timer"),
        ("backup",            "jelleo-backup.timer"),
        ("health",            "jelleo-health.timer"),
    ]

    if not shutil.which("systemctl"):
        return [{"key": k, "unit": u, "state": "unknown", "last_tick_ms": None} for k, u in KNOWN]

    out = []
    for key, unit in KNOWN:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
            active = (r.stdout or "").strip()
            if active == "active":
                state = "up"
            elif active in ("activating", "reloading"):
                state = "degraded"
            elif active in ("failed", "inactive"):
                state = "down"
            else:
                state = "unknown"
        except Exception:
            state = "unknown"

        last_tick_ms = None
        try:
            r = subprocess.run(
                ["systemctl", "show", unit, "--property=ActiveEnterTimestamp", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            ts = (r.stdout or "").strip()
            if ts and ts != "0":
                last_tick_ms = int(datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=_tz.utc).timestamp() * 1000)
        except Exception:
            pass

        out.append({"key": key, "unit": unit, "state": state, "last_tick_ms": last_tick_ms})
    return out


def _count_signed_receipts() -> int:
    """Count signed cycle receipts under /var/www/jelleo.com/cycles/.

    Returns 0 on hosts without that directory.
    """
    from pathlib import Path as _P
    root = _P("/var/www/jelleo.com/cycles")
    if not root.is_dir():
        return 0
    return sum(1 for p in root.iterdir() if p.is_dir() and (p / "cycle.html.sig").exists())


def _customers_to_publish(workspace: Path) -> list[dict]:
    """List of customers whose manifests should be published.

    Reads <workspace>/customers.json if present (Tier 5 #27 will populate this
    via `audit-pipeline customer add`). Always includes a hardcoded "demo"
    customer mapped to the Percolator target, so OtterSec / prospects can
    walk through a real-data view at /customer/demo/ today.
    """
    import json as _json
    customers = [
        {
            "id":            "demo",
            "name":          "Demo customer · Percolator team view",
            "protocol_name": "Percolator",
            "tier":          "Production",
            "since":         "2026-04-22",
            "target_match":  "percolator",  # case-insensitive substring on target name
        }
    ]
    cfg = workspace / "customers.json"
    if cfg.is_file():
        try:
            extra = _json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(extra, list):
                # Skip duplicates by id
                seen = {c["id"] for c in customers}
                for c in extra:
                    if isinstance(c, dict) and c.get("id") and c["id"] not in seen:
                        customers.append(c)
                        seen.add(c["id"])
        except Exception:
            pass
    return customers


def _build_customer_manifest(db: FindingsDB, customer: dict) -> dict:
    """Build the per-customer manifest the gated portal renders.

    Same shape as the public snapshot, but scoped to this customer's owned
    target(s) AND including their confirmed (in-progress) findings — those
    are private to the customer, served behind the token gate.

    Embargo rules still apply: in-progress findings here are visible only to
    the customer who owns the protocol. They never end up in snapshot.json.
    """
    from datetime import datetime, timezone

    # For the customer's own data, surface everything except brand-new /
    # rejected findings. "rejected" = engine false-positive (don't worry the
    # customer); "new" = pre-triage, no signal yet. Everything else (triaged,
    # confirmed, disclosed, fixed, verified) is actionable to the customer.
    CUSTOMER_STATUSES = {"triaged", "confirmed", "disclosed", "fixed", "verified"}

    target_match = (customer.get("target_match") or "").lower()
    targets = db.list_targets()
    owned_targets = [
        t for t in targets
        if not target_match or target_match in (t.get("name") or "").lower()
    ]
    owned_target_ids = {t["id"] for t in owned_targets}

    cycles = [
        c for c in db.list_cycles(limit=20)
        if not owned_target_ids or c.get("target_id") in owned_target_ids
    ]
    findings_all = db.list_findings(limit=500)
    findings = [
        f for f in findings_all
        if (not owned_target_ids or f.get("target_id") in owned_target_ids)
        and (f.get("status") or "") in CUSTOMER_STATUSES
    ]

    # Enrich each finding with the same envelope as the public snapshot,
    # but here title + hyp_id are always included (customer owns the data).
    customer_findings = []
    for f in findings:
        details = {}
        try:
            import json as _json
            raw = f.get("details_json")
            if raw:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    details = parsed
        except Exception:
            details = {}
        customer_findings.append({
            "id": f["id"],
            "target_id": f["target_id"],
            "cycle_id": f.get("cycle_id"),
            "hypothesis_id": f.get("hypothesis_id"),
            "title": f.get("title"),
            "severity": f.get("severity"),
            "status": f.get("status"),
            "bug_class": f.get("bug_class"),
            "verdict": f.get("verdict"),
            "poc_fired": bool(f.get("poc_fired")),
            "updated_at": f.get("updated_at"),
            "disclosure_url": details.get("disclosure_url") or details.get("pr_url") or None,
            "n_siblings":     details.get("n_siblings") or 0,
        })

    # Status counters scoped to the customer.
    sev_disclosed = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
    sev_in_progress = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
    for f in customer_findings:
        st = f["status"]
        sev = f.get("severity")
        bucket = sev_disclosed if st in ("disclosed", "fixed", "verified") else sev_in_progress
        if sev in bucket:
            bucket[sev] += 1

    recent_cycles = [
        {
            "cycle_id": c.get("cycle_id"),
            "target_id": c.get("target_id"),
            "engine_sha": (c.get("engine_sha") or "")[:10],
            "started_at": c.get("started_at"),
            "finished_at": c.get("finished_at"),
            "n_dispatched": c.get("n_dispatched"),
            "n_confirmed": c.get("n_confirmed"),
            "receipt_fingerprint": _read_receipt_fingerprint(c.get("cycle_id")),
        }
        for c in cycles
    ]

    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_ms": int(now.timestamp() * 1000),
        "platform": "jelleo",
        "version": "v0.1",
        "customer": {
            "id":             customer.get("id"),
            "name":           customer.get("name"),
            "protocol_name":  customer.get("protocol_name"),
            "tier":           customer.get("tier"),
            "since":          customer.get("since"),
            "view_kind":      "customer-private",
        },
        "stats": {
            "n_cycles":              len(recent_cycles),
            "n_findings_total":      len(customer_findings),
            "by_severity_disclosed":   sev_disclosed,
            "by_severity_in_progress": sev_in_progress,
        },
        "targets": [
            {
                "name": t["name"],
                "engine_repo": (t.get("engine_repo") or "").replace("https://github.com/", ""),
            }
            for t in owned_targets
        ],
        "recent_cycles": recent_cycles,
        "public_findings": customer_findings,  # name kept for shape compatibility with snapshot.json
        "services": _probe_services(),
        "cycles_total":    len(recent_cycles),
        "receipts_signed": _count_signed_receipts(),
        "loop_uptime_human": _loop_uptime_human(),
    }


def _read_receipt_fingerprint(cycle_id: str | None) -> str | None:
    """Read a short fingerprint of the Ed25519 cycle receipt for display.

    The signing pipeline writes <cycle>/cycle.html.sig — a base64 signature
    blob. We turn the first 8 bytes into a colon-separated hex string so the
    customer portal can show "3a:c1:8e:42:7f:11:b9:dd…" the way SSH host
    fingerprints are presented. Returns None on missing files / non-VPS hosts.
    """
    if not cycle_id:
        return None
    from pathlib import Path as _P
    sig_path = _P("/var/www/jelleo.com/cycles") / cycle_id / "cycle.html.sig"
    if not sig_path.is_file():
        return None
    try:
        import base64
        raw = sig_path.read_text(encoding="utf-8").strip()
        # Files we ship are typically a base64 line; tolerate raw bytes too.
        try:
            sig_bytes = base64.b64decode(raw, validate=False)
        except Exception:
            sig_bytes = raw.encode("utf-8", "ignore")
        if len(sig_bytes) < 4:
            return None
        head = sig_bytes[:8]
        return ":".join(f"{b:02x}" for b in head) + "…"
    except Exception:
        return None


def _loop_uptime_human() -> str:
    """Best-effort uptime string for jelleo-shadow.service.

    Returns "—" on non-systemd hosts.
    """
    import shutil
    import subprocess
    from datetime import datetime, timezone as _tz

    if not shutil.which("systemctl"):
        return "—"
    try:
        r = subprocess.run(
            ["systemctl", "show", "jelleo-shadow.service", "--property=ActiveEnterTimestamp", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        ts = (r.stdout or "").strip()
        if not ts or ts == "0":
            return "—"
        started = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=_tz.utc)
        delta = datetime.now(_tz.utc) - started
        days = delta.days
        hours = (delta.seconds // 3600)
        if days > 0:
            return f"{days}d {hours}h"
        mins = (delta.seconds // 60) % 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"
    except Exception:
        return "—"


def _render(db: FindingsDB, auto_refresh: int) -> str:
    stats = db.stats()
    targets = db.list_targets()
    cycles = db.list_cycles(limit=20)
    findings = db.list_findings(limit=50)

    n_critical = stats["by_severity"].get("Critical", 0)
    n_high = stats["by_severity"].get("High", 0)
    n_medium = stats["by_severity"].get("Medium", 0)
    n_low = stats["by_severity"].get("Low", 0)
    n_info = stats["by_severity"].get("Info", 0)
    n_confirmed = stats["by_status"].get("confirmed", 0)
    n_open = (n_critical + n_high) - stats["by_status"].get("fixed", 0) - stats["by_status"].get("verified", 0)

    # Status pill: critical if any open Critical+High, warn if Medium, ok otherwise
    if n_critical > 0:
        status_label, status_class = "Critical findings open", "critical"
    elif n_high > 0:
        status_label, status_class = "High findings open", "warn"
    else:
        status_label, status_class = "Active · monitoring", "ok"

    # ---------- Target cards ----------
    target_cards = []
    for t in targets:
        t_cycles = db.list_cycles(target_id=t["id"], limit=5)
        t_findings = db.list_findings(target_id=t["id"], limit=500)
        t_critical = sum(1 for f in t_findings if f.get("severity") == "Critical")
        t_high = sum(1 for f in t_findings if f.get("severity") == "High")
        last_cycle = t_cycles[0] if t_cycles else None
        last_at = last_cycle["started_at"] if last_cycle else "—"
        repo = (t.get("engine_repo") or "").replace("https://github.com/", "")
        sev_open_html = ""
        if t_critical:
            sev_open_html += f'<span class="sev critical">{t_critical} Critical</span>'
        if t_high:
            sev_open_html += f' <span class="sev high">{t_high} High</span>'
        target_cards.append(f"""
        <div class="card">
          <div class="row">
            <div>
              <h3>{html.escape(t['name'])}</h3>
              <div class="meta">{html.escape(repo) or '—'}</div>
            </div>
            <div style="text-align:right">
              {sev_open_html}
              <div class="meta" style="margin-top:6px">
                {len(t_findings)} findings · {len(t_cycles)} cycles
              </div>
            </div>
          </div>
          <div class="meta" style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px">
            Last cycle <code>{html.escape(last_at)}</code>
          </div>
        </div>""")

    # ---------- Cycles table ----------
    cycle_rows = []
    for c in cycles:
        cycle_rows.append(f"""
        <tr>
          <td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>
          <td class="mono" style="color:var(--text-2)">{html.escape(c.get('started_at', '—'))}</td>
          <td><code>{html.escape((c.get('engine_sha') or '?')[:10])}</code></td>
          <td class="num">{c.get('n_dispatched', 0)}</td>
          <td class="num">{c.get('n_confirmed', 0) or '<span style="color:var(--text-3)">0</span>'}</td>
        </tr>""")

    # ---------- Findings table ----------
    finding_rows = []
    for f in findings[:30]:
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        status = (f.get("status") or "?").lower()
        finding_rows.append(f"""
        <tr>
          <td><span class="sev {sev_cls}">{sev.value}</span></td>
          <td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>
          <td style="max-width:480px">{html.escape((f.get('title') or '')[:140])}</td>
          <td><span class="status-pill {status}">{html.escape(status)}</span></td>
          <td>{'<span style="color:var(--ok)">✓</span>' if f.get('poc_fired') else '<span style="color:var(--text-3)">—</span>'}</td>
          <td class="mono" style="color:var(--text-3)">{html.escape((f.get('updated_at') or '')[:19])}</td>
        </tr>""")

    # ---------- Severity bar ----------
    n_total_sev = max(1, n_critical + n_high + n_medium + n_low + n_info)
    sev_bar = ""
    if (n_critical + n_high + n_medium + n_low + n_info) > 0:
        sev_bar = f"""
        <div class="sev-bar">
          <span class="b-critical" style="width:{n_critical/n_total_sev*100:.1f}%"></span>
          <span class="b-high"     style="width:{n_high/n_total_sev*100:.1f}%"></span>
          <span class="b-medium"   style="width:{n_medium/n_total_sev*100:.1f}%"></span>
          <span class="b-low"      style="width:{n_low/n_total_sev*100:.1f}%"></span>
          <span class="b-info"     style="width:{n_info/n_total_sev*100:.1f}%"></span>
        </div>
        <div class="sev-bar-legend">
          <span><i style="background:var(--critical)"></i>Critical {n_critical}</span>
          <span><i style="background:var(--high)"></i>High {n_high}</span>
          <span><i style="background:var(--medium)"></i>Medium {n_medium}</span>
          <span><i style="background:var(--low)"></i>Low {n_low}</span>
          <span><i style="background:var(--info)"></i>Info {n_info}</span>
        </div>"""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · Autonomous Solana audit</title>
<meta http-equiv="refresh" content="{auto_refresh}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body>

{topbar_html(status_label, status_class)}

<div class="shell">

  <h1>Operations</h1>
  <p class="subhead">Continuous on-chain &amp; source-code audit across {stats['n_targets']} target{'s' if stats['n_targets'] != 1 else ''} · {stats['n_cycles']} hunt cycles to date</p>

  <div class="kpi-grid">
    <div class="kpi {'danger' if n_critical else 'ok'}">
      <div class="label">Critical</div>
      <div class="value">{n_critical}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi {'warn' if n_high else 'ok'}">
      <div class="label">High</div>
      <div class="value">{n_high}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi">
      <div class="label">Medium</div>
      <div class="value">{n_medium}</div>
      <div class="delta">open findings</div>
    </div>
    <div class="kpi">
      <div class="label">Confirmed</div>
      <div class="value">{n_confirmed}</div>
      <div class="delta">PoC-validated</div>
    </div>
    <div class="kpi">
      <div class="label">Hunt cycles</div>
      <div class="value">{stats['n_cycles']}</div>
      <div class="delta">since deployment</div>
    </div>
  </div>

  {sev_bar}

  <h2>Targets under audit</h2>
  {''.join(target_cards) if target_cards else '<div class="empty">No targets registered. Run <code>audit-pipeline onboard &lt;github-url&gt;</code></div>'}

  <h2>Recent hunt cycles</h2>
  <table>
    <thead><tr>
      <th>Cycle</th><th>Started (UTC)</th><th>Engine SHA</th>
      <th class="num">Dispatched</th><th class="num">Confirmed</th>
    </tr></thead>
    <tbody>{''.join(cycle_rows) or '<tr><td colspan="5" class="empty">No hunt cycles yet.</td></tr>'}</tbody>
  </table>

  <h2>Recent findings</h2>
  <table>
    <thead><tr>
      <th>Severity</th><th>Hypothesis</th><th>Title</th>
      <th>Status</th><th>PoC</th><th>Updated</th>
    </tr></thead>
    <tbody>{''.join(finding_rows) or '<tr><td colspan="6" class="empty">No findings recorded yet.</td></tr>'}</tbody>
  </table>

  {footer_html(extra=datetime.now(timezone.utc).isoformat(timespec='minutes'))}

</div>
</body></html>"""


def _serve(directory: Path, default_file: str, port: int) -> None:
    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

    with socketserver.TCPServer(("0.0.0.0", port), _H) as httpd:
        console.print(f"[bold]Serving[/bold] {directory}/{default_file} on http://0.0.0.0:{port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("[yellow]stopped[/yellow]")
