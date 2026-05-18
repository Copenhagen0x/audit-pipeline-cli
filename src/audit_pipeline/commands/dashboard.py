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
import json
import re
import socketserver
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.branding import CSS, footer_html, topbar_html
from audit_pipeline.db import FindingsDB, open_findings_db
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
@click.option("--cycles-dir", type=click.Path(path_type=Path), default=None,
              help="ALSO publish per-cycle merkle.json sidecars under this dir "
                   "(e.g. /var/www/jelleo.com/cycles/). Each sidecar is copied from "
                   "<workspace>/hunts/<cycle-id>/merkle.json to <cycles-dir>/<root>.merkle.json "
                   "where <root> is the cycle's Merkle root. Lets /cycles/<root>/ detail pages "
                   "and the public verify-offline command actually resolve.")
@click.option("--extra-db", "extra_dbs", type=click.Path(path_type=Path),
              multiple=True,
              help="ALSO merge findings/cycles from this DB into the snapshot. "
                   "Can be repeated. Use when the primary workspace is one tenant "
                   "(e.g. percolator-live) and additional DBs hold other customers' "
                   "data (e.g. ottersec-eval) that should appear on the public dashboard.")
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
    cycles_dir: Path | None,
    extra_dbs: tuple[Path, ...],
    serve: bool,
    port: int,
    auto_refresh: int,
) -> None:
    """Generate (and optionally serve) the customer-facing dashboard.

    Four artifacts are produced, each scoped to a different audience:

      1. dashboard.html (always)         — the rich HTML view.
      2. snapshot.json (--snapshot-json) — public homepage feed; only
         disclosed/fixed/verified findings, with title + hyp_id surfaced.
      3. customer/<token>/manifest.json (--customer-manifest-dir) — per-
         customer JSON behind the token gate; INCLUDES that customer's
         confirmed (in-progress) findings, since the customer owns the data.
      4. cycles/<root>.merkle.json (--cycles-dir) — per-cycle public
         attestations, one file per cycle keyed by Merkle root. Source of
         truth for /cycles/<root>/ detail pages and the public verify-
         offline command. Safe to publish: contains only root, leaves
         schema, n_findings count, engine_sha — no per-finding content.
    """
    import json

    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)

    out = output or (workspace / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(db, auto_refresh), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")

    if snapshot_json:
        snapshot_path = Path(snapshot_json)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        # Build primary snapshot, then merge in each --extra-db. The
        # merge unions: by_target, public_findings, recent_cycles,
        # cycle_merkle_roots, and sums cycles_total / receipts_signed.
        snap = _build_snapshot(db, workspace)
        for extra_db_path in extra_dbs:
            try:
                extra_db = FindingsDB(Path(extra_db_path))
            except Exception as e:
                console.print(f"[yellow]skip --extra-db {extra_db_path}: {e}[/yellow]")
                continue
            snap_x = _build_snapshot(extra_db, None)
            snap = _merge_snapshots(snap, snap_x)
            console.print(f"[green]merged[/green] extra-db {extra_db_path}")
        snapshot_path.write_text(
            json.dumps(snap, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(f"[green]wrote[/green] {snapshot_path}")

    if customer_manifest_dir:
        cdir = Path(customer_manifest_dir).resolve()
        # Disclosure audit Defect 04 (MED): the previous code did
        # `cdir / cust["id"] / "manifest.json"` with NO validation of
        # `cust["id"]`. A `customers.json` row carrying
        # `"id": "../../../etc"` would write outside cdir. Now: strict
        # `^[A-Za-z0-9_-]{1,64}$` regex + path-resolution check so a
        # malformed id can't escape the manifest tree.
        _SAFE_CUST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}[A-Za-z0-9]$|^[A-Za-z0-9]$")
        for cust in _customers_to_publish(workspace):
            cid = (cust.get("id") or "").strip()
            if not _SAFE_CUST_RE.fullmatch(cid):
                console.print(
                    f"[yellow]skip customer manifest:[/yellow] id "
                    f"{cid!r} fails safe-name regex"
                )
                continue
            target = (cdir / cid / "manifest.json").resolve()
            if cdir not in target.parents:
                console.print(
                    f"[red]refused[/red] customer manifest write: resolved "
                    f"path {target} escapes {cdir}"
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(_build_customer_manifest(db, cust, workspace), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            console.print(f"[green]wrote[/green] {target}")

    if cycles_dir:
        n_published, n_skipped = _publish_cycle_sidecars(workspace, Path(cycles_dir))
        if n_published:
            console.print(f"[green]published[/green] {n_published} cycle sidecar(s) to {cycles_dir}")
        if n_skipped:
            console.print(f"[dim]skipped[/dim] {n_skipped} cycle(s) (missing merkle_root field)")

    if serve:
        _serve(out.parent, out.name, port)


def _publish_cycle_sidecars(workspace: Path, cycles_dir: Path) -> tuple[int, int]:
    """Copy every cycle's merkle.json sidecar into the public web docroot.

    Source: <workspace>/hunts/<cycle-id>/merkle.json
    Target: <cycles-dir>/<merkle-root>.merkle.json

    Keying by Merkle root (not cycle-id) means the public URL is
    self-attesting — anyone who has the root can fetch the sidecar
    and verify against it without needing to know the internal
    cycle_id naming. Idempotent: re-running overwrites with same content
    if nothing changed.

    Returns (n_published, n_skipped).
    """
    import json as _json
    import shutil

    cycles_dir.mkdir(parents=True, exist_ok=True)
    hunts = workspace / "hunts"
    if not hunts.is_dir():
        return (0, 0)

    n_published = 0
    n_skipped = 0
    for cycle_dir in hunts.iterdir():
        if not cycle_dir.is_dir():
            continue
        sidecar = cycle_dir / "merkle.json"
        if not sidecar.is_file():
            continue
        try:
            data = _json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            n_skipped += 1
            continue
        root = data.get("merkle_root")
        if not root:
            n_skipped += 1
            continue
        target = cycles_dir / f"{root}.merkle.json"
        shutil.copyfile(sidecar, target)
        n_published += 1

    return (n_published, n_skipped)


def _merge_snapshots(primary: dict, extra: dict) -> dict:
    """Union the public-relevant lists from two snapshots.

    Cycles + findings keep distinct entries (cycle_id and (target_id,id)
    are stable across DBs). Targets dedup by name. Top-level counts
    are summed. Used by ``dashboard --extra-db`` to surface
    multi-tenant data on a single public snapshot.
    """
    out = dict(primary)
    out["public_findings"] = list(primary.get("public_findings", [])) + list(extra.get("public_findings", []))
    seen_cycles: set[str] = set()
    merged_recent = []
    for c in list(primary.get("recent_cycles", [])) + list(extra.get("recent_cycles", [])):
        cid = c.get("cycle_id") or ""
        if cid and cid in seen_cycles:
            continue
        seen_cycles.add(cid)
        merged_recent.append(c)
    merged_recent.sort(
        key=lambda x: (x.get("started_at") or x.get("finished_at") or ""),
        reverse=True,
    )
    out["recent_cycles"] = merged_recent[:20]
    seen_roots: set[str] = set()
    merged_roots = []
    for r in list(primary.get("cycle_merkle_roots", [])) + list(extra.get("cycle_merkle_roots", [])):
        cid = r.get("cycle_id") or ""
        if cid and cid in seen_roots:
            continue
        seen_roots.add(cid)
        merged_roots.append(r)
    out["cycle_merkle_roots"] = merged_roots
    seen_targets: set[str] = set()
    merged_targets = []
    for t in list(primary.get("targets", [])) + list(extra.get("targets", [])):
        nm = t.get("name") or ""
        if nm and nm in seen_targets:
            continue
        seen_targets.add(nm)
        merged_targets.append(t)
    out["targets"] = merged_targets
    out["cycles_total"] = (primary.get("cycles_total") or 0) + (extra.get("cycles_total") or 0)
    out["receipts_signed"] = (primary.get("receipts_signed") or 0) + (extra.get("receipts_signed") or 0)
    pstats = dict(primary.get("stats") or {})
    estats = (extra.get("stats") or {})
    for k, v in estats.items():
        if isinstance(v, (int, float)) and isinstance(pstats.get(k), (int, float)):
            pstats[k] = pstats[k] + v
    out["stats"] = pstats
    fb_p = dict(primary.get("fix_bundle_stats") or {})
    fb_e = (extra.get("fix_bundle_stats") or {})
    for k, v in fb_e.items():
        if isinstance(v, (int, float)) and isinstance(fb_p.get(k), (int, float)):
            fb_p[k] = fb_p[k] + v
        elif isinstance(v, dict) and isinstance(fb_p.get(k), dict):
            merged_d = dict(fb_p[k])
            for kk, vv in v.items():
                if isinstance(vv, (int, float)) and isinstance(merged_d.get(kk), (int, float)):
                    merged_d[kk] = merged_d[kk] + vv
                else:
                    merged_d.setdefault(kk, vv)
            fb_p[k] = merged_d
        else:
            fb_p.setdefault(k, v)
    out["fix_bundle_stats"] = fb_p
    ps_p = dict(primary.get("propagation_stats") or {})
    ps_e = (extra.get("propagation_stats") or {})
    for k, v in ps_e.items():
        if isinstance(v, (int, float)) and isinstance(ps_p.get(k), (int, float)):
            ps_p[k] = ps_p[k] + v
        else:
            ps_p.setdefault(k, v)
    out["propagation_stats"] = ps_p
    return out


def _build_snapshot(db: FindingsDB, workspace: Path | None = None) -> dict:
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
    #
    # POST-AUDIT FIX (2026-05-12 re-audit): include CLOSED_NOT_PLANNED.
    # Maintainer-closed-as-not-planned findings are still REAL paths the
    # engine surfaced — different signal from REJECTED (which is OUR
    # false-positive). Customers asked for these to appear in the public
    # archive as "engine found it, upstream chose not to fix".
    PUBLIC_STATUSES = {"disclosed", "fixed", "verified", "closed_not_planned"}

    stats = db.stats()
    targets = db.list_targets()
    cycles = db.list_cycles(limit=20)
    findings = db.list_findings(limit=200)
    # id→name lookup so every finding + cycle entry can ship a
    # target_name string (the multi-target bridge view filters by
    # data-target which expects the name, not the numeric id).
    _target_name_by_id = {t["id"]: t.get("name") for t in targets}

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
            "target_name": _target_name_by_id.get(f["target_id"]) or "",
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

    recent_cycles = []
    for c in cycles:
        entry = {
            "cycle_id": c.get("cycle_id"),
            "target_id": c.get("target_id"),
            "target_name": _target_name_by_id.get(c.get("target_id")) or "",
            "engine_sha": (c.get("engine_sha") or "")[:10],
            "started_at": c.get("started_at"),
            "finished_at": c.get("finished_at"),
            "n_dispatched": c.get("n_dispatched"),
            "n_confirmed": c.get("n_confirmed"),
            # cost_usd surfaces the per-cycle LLM spend so the bridge
            # view can compute per-tab spend (sum cycles by target_name).
            # Without this, the cost row stays pinned at $ 0.000 / $ 800
            # because the manifest carries no spend data at all.
            "cost_usd": c.get("total_cost_usd") or 0,
            "receipt_fingerprint": _read_receipt_fingerprint(c.get("cycle_id")),
        }
        # Triage counts from hunt_summary.json — persisted across refresh.
        # Without these the L2.5 panel reset to all zeros on every reload
        # because in-memory state.triage was the only source.
        try:
            cid = c.get("cycle_id") or ""
            # Multi-workspace OSec layout: hunts live under
            # workspace/workspaces/<cell>/hunts/<cycle_id>. Single-workspace
            # layout (Percolator): workspace/hunts/<cycle_id>. Try both.
            for hs_path in [
                workspace / "hunts" / cid / "hunt_summary.json" if workspace and cid else None,
                *(workspace / "workspaces" / cell / "hunts" / cid / "hunt_summary.json"
                  for cell in ((workspace / "workspaces").iterdir() if workspace and (workspace / "workspaces").is_dir() else [])
                  if cid),
            ]:
                if hs_path and hs_path.is_file():
                    hs_doc = json.loads(hs_path.read_text(encoding="utf-8"))
                    trg = hs_doc.get("triage") or {}
                    if isinstance(trg, dict) and trg.get("counts"):
                        entry["triage_counts"] = trg["counts"]
                        break
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        # Mid-flight progress: while finished_at is null, n_dispatched is
        # still 0 in the DB (it's only set when Layer 1 completes). Override
        # with the live count of *_response.md files in the cycle's recon/
        # dir so the dashboard counter ticks up every snapshot tick instead
        # of staying at 0 for an hour.
        # Phase-aware in-progress detection (filesystem authoritative — DB
        # finished_at can be stale from a prior failed run that has since
        # been resumed). If the cycle has reached "publishing" phase
        # (hunt_summary.json present), it's fully done and we leave the
        # DB-state alone. Otherwise we surface phase + counters so the
        # dashboard can render "Layer 1.5 debate · 87 / 284 · 30.6%"
        # instead of just "0 dispatched · 18 hours ago".
        prog = _in_progress_cycle_progress(workspace, c.get("cycle_id"))
        # DB.finished_at is authoritative once set — `audit-pipeline cycle
        # finish` is the explicit operator decision to close a row, and
        # the dashboard MUST honor it. The filesystem detector exists
        # only to surface mid-flight progress for cycles whose row
        # hasn't been touched yet (Layer 1 still running, finished_at
        # still NULL).
        db_finished_at = c.get("finished_at")
        if prog and prog.get("phase") and prog["phase"] != "publishing" \
                and not db_finished_at:
            entry["in_progress"] = True
            entry["phase"] = prog["phase"]
            entry["phase_label"] = prog["phase_label"]
            entry["phase_done"] = prog["phase_done"]
            entry["phase_total"] = prog["phase_total"]
            # Keep n_dispatched / n_planned for back-compat with old JS that
            # only knew about Layer 1. Map them to the current phase so the
            # headline counter stays correct as phases advance.
            entry["n_dispatched"] = prog["phase_done"]
            entry["n_planned"] = prog["phase_total"]
            entry["progress_pct"] = prog["pct_complete"]
            entry["finished_at"] = None
        # n_true_layer1, n_contested etc. computed from recon_summary —
        # surface them on BOTH in_progress AND finished cycles so the
        # dashboard hyp grid can paint the correct # of fire tiles
        # when looking at a finished cycle (previously these fields
        # were only set inside the in_progress branch → finished
        # cycles showed all 40 tiles as "ok" even when 14 were TRUE).
        if prog:
            entry["n_contested"] = prog.get("n_contested", 0)
            entry["n_true_layer1"] = prog.get("n_true_layer1", 0)
            entry["n_debate_done"] = prog.get("n_debate_done", 0)
            entry["n_poc_logs"] = prog.get("n_poc_logs", 0)
            entry["n_kani_harnesses"] = prog.get("n_kani_harnesses", 0)
            entry["n_litesvm"] = prog.get("n_litesvm", 0)
        recent_cycles.append(entry)

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
    # Workspace was historically derived from db.path.parent (SQLite-only).
    # Postgres backend has no .path; callers must pass workspace explicitly.
    if workspace is None:
        # Best-effort fallback for the SQLite path.
        workspace = getattr(db, "path", None)
        if workspace is not None:
            workspace = workspace.parent
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
        "loop_uptime_source": "jelleo-shadow.service",
        # Real LLM spend pulled from llm.py's per-call event log.
        # Replaces the previous flat-rate $0.05/call estimate that
        # under-counted by ~9× (full target_file grounding pushed actual
        # cost to ~$0.45/call at Sonnet 4.6 prices).
        "spend": _spend_summary(),
        # G27: P2 propagation surface — what's been tagged, derived, swept,
        # queued. None of these expose customer-private data; everything
        # is cumulative-platform stats. Drives the /status/ counter row.
        "propagation_stats": _propagation_stats(workspace, db) if workspace else {},
        # P3 Item 16: cumulative fix-bundle counters for the public snapshot.
        # Counts only — no per-finding leak (matches pre-disclosure rule).
        "fix_bundle_stats":  _fix_bundle_stats(workspace) if workspace else {},
        # P4 Y0: per-cycle Merkle roots for recent cycles. The root is
        # tamper-evident (modifies → root changes), reproducible from DB
        # rows, and on-chain ready (single 32-byte digest per cycle).
        "cycle_merkle_roots": _recent_cycle_merkle_roots(workspace) if workspace else [],
    }


def _recent_cycle_merkle_roots(workspace: Path, limit: int = 30) -> list[dict]:
    """P4 Y0: surface the most recent cycles' Merkle roots on snapshot.json.

    Reads merkle.json sidecars under <workspace>/hunts/<cycle-id>/.
    Public-safe: contains cycle_id + root + n_findings + engine_sha only.
    Never includes per-finding identifiers.
    """
    import json as _json
    out: list[dict] = []
    hunts = workspace / "hunts"
    if not hunts.is_dir():
        return out
    pairs: list[tuple[float, dict]] = []
    for cycle_dir in hunts.iterdir():
        sidecar = cycle_dir / "merkle.json"
        if not sidecar.is_file():
            continue
        try:
            d = _json.loads(sidecar.read_text(encoding="utf-8"))
            mtime = sidecar.stat().st_mtime
            pairs.append((mtime, {
                "cycle_id":    d.get("cycle_id"),
                "engine_sha":  d.get("engine_sha"),
                "merkle_root": d.get("merkle_root"),
                "n_findings":  d.get("n_findings"),
                "schema":      d.get("schema"),
            }))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[0], reverse=True)
    return [p[1] for p in pairs[:limit]]


def _fix_bundle_stats(workspace: Path) -> dict:
    """P3 Item 16: cumulative fix-bundle counters for snapshot.json.

    Counts only — never per-finding identifiers, never bug_class names.
    Public snapshot must not leak which findings have bundles drafted
    (pre-disclosure rule from `--public/--full` filter).
    """
    import json as _json
    out = {
        "bundles_drafted":   0,
        "bundles_verified":  0,
        "bundles_authorized": 0,
        "prs_opened":        0,
        "prs_merged":        0,
        "by_status":         {},
    }
    bdir = workspace / "recon" / "bundles"
    if not bdir.is_dir():
        return out
    for d in bdir.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        try:
            m = _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = m.get("status") or "drafted"
        out["by_status"][status] = out["by_status"].get(status, 0) + 1
        out["bundles_drafted"] += 1
        if status in ("verified", "authorized", "pr-opened", "merged", "fixed"):
            out["bundles_verified"] += 1
        if status in ("authorized", "pr-opened", "merged", "fixed"):
            out["bundles_authorized"] += 1
        if status in ("pr-opened", "merged", "fixed"):
            out["prs_opened"] += 1
        if status in ("merged", "fixed"):
            out["prs_merged"] += 1
    return out


def _propagation_stats(workspace: Path, db: FindingsDB) -> dict:
    """G27 + G28: P2 propagation activity counters for the public snapshot.

    Drawn from filesystem state + DB. None of these expose private data:
      * bug_classes_catalogued — count of distinct bug_class values across
        the bundled YAML library (a public-facing taxonomy stat)
      * findings_with_bug_class — DB count of findings with bug_class set
        (operational hygiene signal)
      * sibling_files — count of derived/<id>-siblings.yaml files
      * propagation_reports — count of recon/propagate/auto-fire/*.md
      * dispatches_queued — count of pending Layer-1 hunts in the
        scheduled queue
      * dispatches_pending — same, only items still in 'pending' state

    All counts are cumulative-since-DB-init.
    """
    import json as _json

    # Two distinct counts (clarified 2026-05-08 audit):
    #   bug_classes_declared:        distinct bug_class values across YAMLs
    #                                ("what classes does the library mention?")
    #   bug_classes_with_signatures: subset that has regex signatures registered
    #                                ("what classes can propagation actually
    #                                sweep for?"; gap is the C9 backlog)
    bug_classes_declared = 0
    bug_classes_with_signatures = 0
    try:
        import yaml as _yaml

        from audit_pipeline.commands.propagate import BUG_CLASS_SIGNATURES
        from audit_pipeline.scoping import hypotheses_dir
        seen: set[str] = set()
        for p in hypotheses_dir().glob("*.yaml"):
            try:
                raw = _yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for h in (raw or {}).get("hypotheses", []):
                if isinstance(h, dict) and h.get("bug_class"):
                    seen.add(h["bug_class"])
        bug_classes_declared = len(seen)
        bug_classes_with_signatures = len(BUG_CLASS_SIGNATURES)
    except Exception:
        pass

    # DB hygiene
    findings_with_bug_class = 0
    try:
        with db._conn() as c:  # noqa: SLF001
            row = c.execute("SELECT COUNT(*) AS n FROM findings WHERE bug_class IS NOT NULL").fetchone()
            findings_with_bug_class = int(row["n"] or 0) if row else 0
    except Exception:
        pass

    # Filesystem signals
    sibling_files = 0
    propagation_reports = 0
    dispatches_queued = 0
    dispatches_pending = 0
    try:
        derived = workspace / "derived"
        if derived.is_dir():
            sibling_files = sum(1 for p in derived.glob("*-siblings.yaml"))
        autofire = workspace / "recon" / "propagate" / "auto-fire"
        if autofire.is_dir():
            propagation_reports = sum(1 for p in autofire.glob("*.md"))
        scheduled = workspace / "recon" / "propagate" / "scheduled"
        if scheduled.is_dir():
            for q in scheduled.glob("*.json"):
                try:
                    data = _json.loads(q.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for item in (data.get("items") or []):
                    dispatches_queued += 1
                    if item.get("status") == "pending":
                        dispatches_pending += 1
    except Exception:
        pass

    return {
        "bug_classes_declared":          bug_classes_declared,
        "bug_classes_with_signatures":   bug_classes_with_signatures,
        "findings_with_bug_class":       findings_with_bug_class,
        "sibling_files":                 sibling_files,
        "propagation_reports":           propagation_reports,
        "dispatches_queued":             dispatches_queued,
        "dispatches_pending":            dispatches_pending,
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
    from datetime import datetime
    from datetime import timezone as _tz

    KNOWN = [
        ("shadow",            "jelleo-shadow.service"),
        ("watch",             "jelleo-watch.service"),
        ("scheduler-24h",     "jelleo-scheduler-24h.timer"),
        ("scheduler-weekly",  "jelleo-scheduler-weekly.timer"),
        ("scheduler-monthly", "jelleo-scheduler-monthly.timer"),
        ("snapshot",          "jelleo-snapshot.timer"),
        ("backup",            "jelleo-backup.timer"),
        ("health",            "jelleo-health.timer"),
        ("heartbeat",         "jelleo-heartbeat.timer"),  # P4 Y0
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
        # For .timer units, prefer LastTriggerUSec (when the timer last fired)
        # over ActiveEnterTimestamp (when the timer was activated, which for
        # long-running units is just the boot/restart time and reads as stale).
        prop = "LastTriggerUSec" if unit.endswith(".timer") else "ActiveEnterTimestamp"
        try:
            r = subprocess.run(
                ["systemctl", "show", unit, f"--property={prop}", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            ts = (r.stdout or "").strip()
            if ts and ts != "0":
                # systemctl prints both formats with weekday prefix
                last_tick_ms = int(
                    datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z")
                    .replace(tzinfo=_tz.utc)
                    .timestamp() * 1000
                )
        except Exception:
            pass

        # If the systemctl says active but we can see the unit hasn't ticked
        # recently (timer never fired in the last day), surface that as
        # "stale" instead of false-positive "up". Threshold = 25h to allow
        # daily timers to count as fresh.
        STALE_MS = 25 * 60 * 60 * 1000
        if state == "up" and last_tick_ms is not None:
            now_ms = int(datetime.now(_tz.utc).timestamp() * 1000)
            if (now_ms - last_tick_ms) > STALE_MS:
                state = "stale"

        out.append({"key": key, "unit": unit, "state": state, "last_tick_ms": last_tick_ms})
    return out


def _count_signed_receipts() -> int:
    """Count signed cycle receipts under the public cycles dir.

    Path is resolved via ``audit_pipeline.utils.vps_paths.public_cycles_dir``
    so dev/CI can override ``JELLEO_PUBLIC_ROOT``. Returns 0 on hosts
    without that directory.
    """
    from audit_pipeline.utils.vps_paths import public_cycles_dir
    root = public_cycles_dir()
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
            # Cross-cutting audit Defect 01 (HIGH): the previous
            # ``target_match: ""`` allow-all default leaked confirmed-but-
            # undisclosed findings from every onboarded protocol into the
            # public demo manifest. Bound demo to the literal targets it
            # is permitted to see — exact name match (case-insensitive),
            # split on whitespace/comma. Add to this list deliberately
            # when demo should preview a new target.
            "target_match":  "percolator,default",
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


def _build_customer_target_rows(
    owned_targets: list[dict],
    customer_findings: list[dict],
    cycles: list[dict],
    db,
) -> list[dict]:
    """Return the per-target rows the customer dashboard JS expects.

    Each row carries:
      id, name, engine_repo (back-compat)
      n_findings, severity_counts (used by tab badges + lobby cards)
      last_cycle_id, last_cycle_at, engine_sha (used by "Last scan ...")
      status (scanned / scanning / idle, used by lobby card classes)
    """
    # Badge counts must reflect what the customer has actually DISCLOSED
    # (matches the published PDF totals on each cycle).
    # Triaged + confirmed findings are still visible in the detail list but
    # not counted in target / lobby badges.
    _DISCLOSED_STATUSES = {"disclosed", "fixed", "verified", "closed_not_planned"}
    by_target_id: dict[int, dict] = {}
    for f in customer_findings:
        tid = f.get("target_id")
        if tid is None:
            continue
        if (f.get("status") or "") not in _DISCLOSED_STATUSES:
            continue
        d = by_target_id.setdefault(tid, {
            "n": 0,
            "sev": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0},
        })
        d["n"] += 1
        s = f.get("severity") or "Info"
        if s in d["sev"]:
            d["sev"][s] += 1
    last_cycle_by_tid: dict[int, dict] = {}
    for c in cycles:
        tid = c.get("target_id")
        if tid is None:
            continue
        cur = last_cycle_by_tid.get(tid)
        if not cur or (c.get("started_at") or "") > (cur.get("started_at") or ""):
            last_cycle_by_tid[tid] = c
    rows = []
    for t in owned_targets:
        agg = by_target_id.get(t["id"], {"n": 0, "sev": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}})
        lc = last_cycle_by_tid.get(t["id"])
        status = "scanned" if lc and lc.get("finished_at") else ("scanning" if lc else "idle")
        rows.append({
            "id": t["id"],
            "name": t["name"],
            "engine_repo": (t.get("engine_repo") or "").replace("https://github.com/", ""),
            "n_findings": agg["n"],
            "severity_counts": agg["sev"],
            "last_cycle_id": (lc or {}).get("cycle_id"),
            "last_cycle_at": (lc or {}).get("started_at"),
            "engine_sha": ((lc or {}).get("engine_sha") or "")[:10],
            "status": status,
        })
    return rows


def _build_customer_manifest(db: FindingsDB, customer: dict, workspace: Path | None = None) -> dict:
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
    # confirmed, disclosed, fixed, verified, closed_not_planned) is
    # actionable to the customer.
    #
    # POST-AUDIT FIX (2026-05-12): closed_not_planned was introduced as a
    # distinct terminal state from rejected (upstream chose not to fix vs.
    # engine false-positive). The customer absolutely wants to see these in
    # their portal — that's their disclosure history with the maintainer.
    CUSTOMER_STATUSES = {
        "triaged", "confirmed", "disclosed", "fixed", "verified",
        "closed_not_planned",
    }

    # Cross-cutting audit Defect 01 (HIGH): substring-based scoping let the
    # `demo` customer's empty `target_match=""` match every target — any
    # new real customer onboarded would silently leak confirmed-but-
    # undisclosed findings into /customer/demo/manifest.json. Now:
    #   * Empty target_match = OWNS NOTHING (hard, not allow-all)
    #   * Otherwise: split on whitespace/comma → exact target-NAME tokens
    #     (case-insensitive). No substring match. A customer wanting
    #     percolator owns "percolator", not "percolator-staging-v2".
    raw = (customer.get("target_match") or "").strip().lower()
    targets = db.list_targets()
    if not raw:
        owned_targets: list[dict] = []
    else:
        wanted = {tok.strip() for tok in re.split(r"[\s,;]+", raw) if tok.strip()}
        owned_targets = [
            t for t in targets
            if (t.get("name") or "").lower() in wanted
        ]
    owned_target_ids = {t["id"] for t in owned_targets}

    # CRITICAL audit fix (2026-05-13): the previous filters used
    # `not owned_target_ids or ...` which, when owned_target_ids was
    # EMPTY (customer's target_match doesn't yet match any DB rows —
    # the normal state of a freshly-onboarded customer before any
    # cycles have run), evaluated the OR to True and let EVERY cycle
    # + EVERY finding through. That's how the Percolator cycle
    # 20260511-183154 leaked into the OtterSec manifest.json.
    #
    # New semantics: empty owned_target_ids = OWNS NOTHING. The cycles
    # and findings arrays become empty for any customer that hasn't
    # yet seeded their targets in this DB, which is correct — they
    # have no data, so they should see no data, NOT someone else's.
    cycles = [
        c for c in db.list_cycles(limit=20)
        if c.get("target_id") in owned_target_ids
    ]
    # cycles_total: count of ALL cycles owned by this customer's
    # targets across all time. Previously was len(recent_cycles)
    # (capped at 20), which made the counter RATCHET DOWN as older
    # cycles aged out of the 20-row window. Now pull a much larger
    # set and count owned ones for the headline number.
    cycles_total_for_customer = sum(
        1 for c in db.list_cycles(limit=10000)
        if c.get("target_id") in owned_target_ids
    )
    findings_all = db.list_findings(limit=500)
    findings = [
        f for f in findings_all
        if f.get("target_id") in owned_target_ids
        and (f.get("status") or "") in CUSTOMER_STATUSES
    ]

    # Enrich each finding with the same envelope as the public snapshot,
    # but here title + hyp_id are always included (customer owns the data).
    # Build id→name map for owned targets so each finding can carry the
    # full target_name string (matches the multi-target bridge view's
    # tab `data-target` values; without target_name the JS can't filter
    # findings per tab and they all collapse into a single shared view).
    _target_name_by_id = {t["id"]: t.get("name") for t in owned_targets}
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
            "target_name": _target_name_by_id.get(f["target_id"]) or "",
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
    # CUSTOMER_STATUSES (line ~762) explicitly includes both
    # disclosure-history terminals (disclosed/fixed/verified) AND the
    # retraction terminal (closed_not_planned). Previously the bucket
    # selector at line below ONLY checked the disclosure-history set,
    # so closed_not_planned findings fell through to sev_in_progress
    # and stayed there forever — the dashboard read "active critical
    # bug" for a finding that had been formally retracted.
    sev_disclosed = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
    sev_in_progress = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
    _TERMINAL_STATUSES = ("disclosed", "fixed", "verified", "closed_not_planned")
    for f in customer_findings:
        st = f["status"]
        sev = f.get("severity")
        bucket = sev_disclosed if st in _TERMINAL_STATUSES else sev_in_progress
        if sev in bucket:
            bucket[sev] += 1

    recent_cycles = []
    for c in cycles:
        entry = {
            "cycle_id": c.get("cycle_id"),
            "target_id": c.get("target_id"),
            "target_name": _target_name_by_id.get(c.get("target_id")) or "",
            "engine_sha": (c.get("engine_sha") or "")[:10],
            "started_at": c.get("started_at"),
            "finished_at": c.get("finished_at"),
            "n_dispatched": c.get("n_dispatched"),
            "n_confirmed": c.get("n_confirmed"),
            # cost_usd surfaces the per-cycle LLM spend so the bridge
            # view can compute per-tab spend (sum cycles by target_name).
            # Without this, the cost row stays pinned at $ 0.000 / $ 800
            # because the manifest carries no spend data at all.
            "cost_usd": c.get("total_cost_usd") or 0,
            "receipt_fingerprint": _read_receipt_fingerprint(c.get("cycle_id")),
        }
        # Triage counts from hunt_summary.json — persisted across refresh.
        # Without these the L2.5 panel reset to all zeros on every reload
        # because in-memory state.triage was the only source.
        try:
            cid = c.get("cycle_id") or ""
            # Multi-workspace OSec layout: hunts live under
            # workspace/workspaces/<cell>/hunts/<cycle_id>. Single-workspace
            # layout (Percolator): workspace/hunts/<cycle_id>. Try both.
            for hs_path in [
                workspace / "hunts" / cid / "hunt_summary.json" if workspace and cid else None,
                *(workspace / "workspaces" / cell / "hunts" / cid / "hunt_summary.json"
                  for cell in ((workspace / "workspaces").iterdir() if workspace and (workspace / "workspaces").is_dir() else [])
                  if cid),
            ]:
                if hs_path and hs_path.is_file():
                    hs_doc = json.loads(hs_path.read_text(encoding="utf-8"))
                    trg = hs_doc.get("triage") or {}
                    if isinstance(trg, dict) and trg.get("counts"):
                        entry["triage_counts"] = trg["counts"]
                        break
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        # Phase-aware in-progress detection (filesystem authoritative — DB
        # finished_at can be stale from a prior failed run that has since
        # been resumed). If the cycle has reached "publishing" phase
        # (hunt_summary.json present), it's fully done and we leave the
        # DB-state alone. Otherwise we surface phase + counters so the
        # dashboard can render "Layer 1.5 debate · 87 / 284 · 30.6%"
        # instead of just "0 dispatched · 18 hours ago".
        prog = _in_progress_cycle_progress(workspace, c.get("cycle_id"))
        if prog and prog.get("phase") and prog["phase"] != "publishing":
            entry["in_progress"] = True
            entry["phase"] = prog["phase"]
            entry["phase_label"] = prog["phase_label"]
            entry["phase_done"] = prog["phase_done"]
            entry["phase_total"] = prog["phase_total"]
            entry["n_dispatched"] = prog["phase_done"]
            entry["n_planned"] = prog["phase_total"]
            entry["progress_pct"] = prog["pct_complete"]
            entry["finished_at"] = None
        else:
            # Finished cycle: emit progress_pct=100 explicitly so the
            # dashboard JS can render the bar correctly. The grid +
            # stage code expects progress_pct to be present on EVERY
            # row (else `cycle.progress_pct.toFixed(1)` throws).
            entry["progress_pct"] = 100.0
        # Surface n_true_layer1 etc. on BOTH in_progress AND finished
        # cycles so the dashboard grid paints correct fire counts on
        # finished cycles too. Without this, finished cycles showed
        # all 40 hyps as "ok" tiles even when 14 were L1-TRUE.
        if prog:
            entry["n_contested"] = prog.get("n_contested", 0)
            entry["n_true_layer1"] = prog.get("n_true_layer1", 0)
            entry["n_debate_done"] = prog.get("n_debate_done", 0)
            entry["n_poc_logs"] = prog.get("n_poc_logs", 0)
            entry["n_kani_harnesses"] = prog.get("n_kani_harnesses", 0)
            entry["n_litesvm"] = prog.get("n_litesvm", 0)
        # Backfill n_confirmed for in-progress cycles by counting
        # owned + cycle-scoped findings with poc_fired=True. The DB
        # row's n_confirmed column is ONLY populated at cycle close
        # — for cycles mid-flight it would be None → JS shows "—".
        # Count the live state of the shared findings DB instead.
        if entry.get("n_confirmed") is None:
            cid = c.get("cycle_id") or ""
            entry["n_confirmed"] = sum(
                1 for f in findings_all
                if f.get("cycle_id") == cid
                and f.get("target_id") in owned_target_ids
                and bool(f.get("poc_fired"))
            )
        recent_cycles.append(entry)

    # G28: per-customer propagation slice. Counts are scoped to findings
    # whose hypothesis_id (or derived siblings) belong to this customer's
    # owned targets. Today the customer can see their own in-progress
    # confirmed findings — propagation hits associated with those go here.
    if workspace is None:
        workspace = getattr(db, "path", None)
        workspace = workspace.parent if workspace is not None else None
    customer_propagation = (
        _customer_propagation_slice(workspace, customer_findings, owned_target_ids)
        if workspace else {}
    )

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
            # n_cycles = lifetime count of customer-owned cycles, NOT
            # the count of the recent window (which is capped at 20
            # and ratchets DOWN as old cycles age out).
            "n_cycles":              cycles_total_for_customer,
            "n_findings_total":      len(customer_findings),
            "by_severity_disclosed":   sev_disclosed,
            "by_severity_in_progress": sev_in_progress,
        },
        "targets": _build_customer_target_rows(
            owned_targets, customer_findings, cycles, db,
        ),
        "recent_cycles": recent_cycles,
        "public_findings": customer_findings,  # name kept for shape compatibility with snapshot.json
        "services": _probe_services(),
        # cycles_total — same as stats.n_cycles but at top level
        # for back-compat with older dashboard JS readers.
        "cycles_total":    cycles_total_for_customer,
        "receipts_signed": _count_signed_receipts(),
        "loop_uptime_human": _loop_uptime_human(),
        "loop_uptime_source": "jelleo-shadow.service",
        # NOTE: `spend` is intentionally OMITTED from the per-customer
        # manifest. `_spend_summary()` returns PLATFORM-WIDE LLM
        # spend totals — including spend on other customers' cycles
        # — so surfacing it in a per-customer view leaks the rough
        # scale + cadence of other customers' audits to this one.
        # The customer's heartbeat.json gets per-customer-scoped
        # spend (active_cycle_spend_usd + session_total_spend_usd)
        # via osec-heartbeat-writer.py instead.
        # G28: customer-scoped propagation activity
        "propagation_stats": customer_propagation,
        # `totals` mirrors the schema that customer/<id>/index.html
        # JS expects (snap.totals.n_findings + snap.totals.by_severity).
        # Without this the dashboard counters all show 0 even when
        # stats.* has the real numbers — a schema-mismatch we keep
        # back-compat for by emitting BOTH `stats` and `totals`.
        "totals": {
            "n_targets": len(owned_targets),
            # n_findings reflects disclosed only (lobby counter shows what
            # is published, not what is pending).
            "n_findings": sum(sev_disclosed.values()),
            # Totals reflect what is DISCLOSED only (matches published PDF
            # totals). In-progress findings remain in stats / detail list
            # but are not summed in the lobby counter cards.
            "by_severity": {
                k: sev_disclosed.get(k, 0)
                for k in ("Critical", "High", "Medium", "Low", "Info")
            },
        },
    }


def _customer_propagation_slice(
    workspace: Path,
    customer_findings: list[dict],
    owned_target_ids: set[int],
) -> dict:
    """G28: customer-private propagation counters.

    Mirrors the public _propagation_stats shape but scopes counts to
    findings owned by this customer (so they see only THEIR class library
    growth + propagation activity).
    """
    # Distinct bug_class values across THIS customer's findings
    customer_bug_classes = {
        f.get("bug_class")
        for f in customer_findings
        if f.get("bug_class")
    }

    # Count this customer's findings that have a bug_class set
    findings_with_bug_class = sum(
        1 for f in customer_findings if f.get("bug_class")
    )

    # Filesystem-level counts: sibling YAMLs derived from this customer's
    # findings, propagation reports for this customer's findings.
    derived_dir = workspace / "derived"
    autofire_dir = workspace / "recon" / "propagate" / "auto-fire"
    sibling_files = 0
    propagation_reports = 0

    customer_finding_ids = {f.get("id") for f in customer_findings}
    customer_hyp_slugs = {
        (f.get("hypothesis_id") or f"finding-{f.get('id')}").replace("/", "-")
        for f in customer_findings
    }

    if derived_dir.is_dir():
        for p in derived_dir.glob("*-siblings.yaml"):
            stem = p.stem  # "<slug>-siblings"
            slug = stem.removesuffix("-siblings")
            if slug in customer_hyp_slugs:
                sibling_files += 1

    if autofire_dir.is_dir():
        for p in autofire_dir.glob("propagation_finding_*.md"):
            # filename = propagation_finding_<id>_<bug_class>.md
            try:
                fid_str = p.stem.split("_")[2]
                fid = int(fid_str)
                if fid in customer_finding_ids:
                    propagation_reports += 1
            except (IndexError, ValueError):
                continue

    # G26: per-finding chain links. For each customer finding that has
    # a rendered chain.html in <workspace>/recon/propagate/chains/, expose
    # the relative path so the gated portal can link directly to it.
    chains_dir = workspace / "recon" / "propagate" / "chains"
    chain_links: list[dict] = []
    if chains_dir.is_dir():
        for f in customer_findings:
            fid = f.get("id")
            if fid is None:
                continue
            cpath = chains_dir / f"{fid}.html"
            if cpath.is_file():
                chain_links.append({
                    "finding_id":      fid,
                    "hypothesis_id":   f.get("hypothesis_id"),
                    "title":           (f.get("title") or "")[:120],
                    "severity":        f.get("severity"),
                    "bug_class":       f.get("bug_class"),
                    # Path relative to the customer's manifest dir; the
                    # portal joins it with the workspace public-cycles base.
                    "chain_html_path": f"recon/propagate/chains/{fid}.html",
                })

    # P3 Item 12: per-customer fix-bundle status. For each customer finding
    # that has a bundle directory under <workspace>/recon/bundles/<id>/,
    # surface the bundle status + verification summary so the gated portal
    # can show "fix bundle drafted / verified / authorized / pr-opened /
    # merged" alongside the finding.
    import json as _json
    bundles_dir = workspace / "recon" / "bundles"
    fix_bundles: list[dict] = []
    bundle_counts: dict[str, int] = {}
    if bundles_dir.is_dir():
        for f in customer_findings:
            fid = f.get("id")
            if fid is None:
                continue
            mp = bundles_dir / str(fid) / "meta.json"
            if not mp.is_file():
                continue
            try:
                m = _json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = m.get("status") or "drafted"
            bundle_counts[status] = bundle_counts.get(status, 0) + 1
            entry = {
                "finding_id": fid,
                "status":     status,
                "bug_class":  m.get("bug_class"),
                "updated_at": m.get("updated_at"),
                # Public archive URL — only resolves AFTER `bundle publish-archive`
                # has been run for this bundle (which strips operator-private
                # files: verification.json, authorization.json, hooks/, pr-body.md).
                # Customer portal MUST NOT serve raw recon/bundles/<id>/ from the
                # workspace — that would leak authorization.json + pr-body.md.
                "public_bundle_url": f"https://api.jelleo.com/bundles/{fid}/",
            }
            # Surface gate-pass count if verification.json present
            vp = bundles_dir / str(fid) / "verification.json"
            if vp.is_file():
                try:
                    v = _json.loads(vp.read_text(encoding="utf-8"))
                    n_pass = sum(1 for g in (v.get("gates") or {}).values()
                                  if g.get("passed") is True)
                    n_total = len(v.get("gates") or {})
                    entry["gates_passed"] = f"{n_pass}/{n_total}"
                except Exception:
                    pass
            fix_bundles.append(entry)

    return {
        "bug_classes_seen":         len(customer_bug_classes),
        "findings_with_bug_class":  findings_with_bug_class,
        "sibling_files":            sibling_files,
        "propagation_reports":      propagation_reports,
        "chain_links":              chain_links,
        "fix_bundles":              fix_bundles,
        "fix_bundle_counts":        bundle_counts,
    }


def _in_progress_cycle_progress(
    workspace: Path | None, cycle_id: str | None
) -> dict | None:
    """For a cycle that isn't yet fully published, derive live mid-flight
    progress: current PHASE (recon / debate / poc / kani / litesvm /
    publishing) plus how many per-hyp artifacts have landed vs how many
    are expected for that phase.

    Phase detection is filesystem-driven so the answer is correct even
    when the DB rows are stale from a prior failed run:
      - recon_summary.json missing  -> recon
      - debate/ has artifacts AND poc/ empty  -> debate
      - poc/ has cargo logs AND kani/ empty  -> poc
      - kani/ has artifacts AND hunt_summary.json missing  -> kani / litesvm
      - hunt_summary.json present -> publishing / done

    Returns None if no cycle dir found or the cycle hasn't even started Layer 1.
    """
    if not workspace or not cycle_id:
        return None
    # Per-workspace: try the direct location first (single-workspace
    # setup like percolator-live). If not found there, walk into
    # workspaces/<cell>/hunts/<cycle_id> — multi-workspace setups
    # like ottersec-eval keep cycles under per-cell subdirs and the
    # top-level workspace's hunts/ is always empty for them.
    cycle_dir = workspace / "hunts" / cycle_id
    if not (cycle_dir / "recon").is_dir():
        # Walk into workspaces/* looking for the cycle
        cells_root = workspace / "workspaces"
        if cells_root.is_dir():
            for cell in cells_root.iterdir():
                candidate = cell / "hunts" / cycle_id
                if (candidate / "recon").is_dir():
                    cycle_dir = candidate
                    break
    recon = cycle_dir / "recon"
    if not recon.is_dir():
        return None
    # A retracted cycle is terminal — even if hunt_summary.json never
    # landed (because the cycle was killed mid-publish), the
    # retraction.json sidecar from `audit-pipeline cycle retract` IS
    # the authoritative "this cycle is dead" marker. Treat it like
    # finished/publishing so the dashboard doesn't keep reporting
    # "in_progress: true" on a cycle that was retracted hours ago.
    if (cycle_dir / "retraction.json").is_file():
        return None
    try:
        n_prompts = sum(1 for _ in recon.glob("*_prompt.md"))
        n_responses = sum(1 for _ in recon.glob("*_response.md"))
        recon_summary = recon / "recon_summary.json"
        hunt_summary = cycle_dir / "hunt_summary.json"
        debate_dir = cycle_dir / "debate"
        poc_dir = cycle_dir / "poc"
        kani_dir = cycle_dir / "kani"
        litesvm_dir = cycle_dir / "litesvm"
        n_debate_done = (
            sum(1 for _ in debate_dir.glob("*_challenger_response.md"))
            if debate_dir.is_dir() else 0
        )
        # Multi-language PoC detection: test files land at .rs (Solana),
        # .move (Aptos), .t.sol (Solidity), .c (C). Run logs land as
        # cargo_*.log (Solana) or runlog_*.log (other adapters). Without
        # this, Aptos hunts stayed at "phase: debate" forever because
        # n_poc_logs only looked for cargo_*.log.
        n_poc_tests = (
            sum(1 for ext in ("*.rs", "*.move", "*.t.sol", "*.c")
                for _ in poc_dir.glob(f"test_{ext}"))
            if poc_dir.is_dir() else 0
        )
        n_poc_logs = (
            sum(1 for pattern in ("cargo_*.log", "runlog_*.log")
                for _ in poc_dir.glob(pattern))
            if poc_dir.is_dir() else 0
        )
        # L3 formal-verification artifacts.
        # Solana: kani/*.rs (Kani harnesses).
        # Aptos:  formal/*.move (Move Prover spec modules).
        # C:      formal/*.c   (CBMC harnesses).
        # Solidity: formal/*.sol (SMTChecker harnesses).
        # Without counting the non-Solana paths the dashboard's L3
        # counter stays at 0 forever for Aptos / C / Solidity cycles
        # even when L3 ran, and the waterfall paints "skipped" for a
        # layer that actually executed. Operator caught this on the
        # aptos-small cycle 20260513-191318 dry-fire plan.
        formal_dir = cycle_dir / "formal"
        n_kani_harnesses = (
            sum(1 for _ in kani_dir.glob("*.rs"))
            if kani_dir.is_dir() else 0
        )
        if formal_dir.is_dir():
            for pattern in ("*.move", "*.sol", "*.c"):
                n_kani_harnesses += sum(1 for _ in formal_dir.glob(pattern))
        # L4 runtime/fuzz artifacts. Solana: litesvm/*.rs; Aptos/C/Sol:
        # fuzz/<lang-ext>.
        fuzz_dir = cycle_dir / "fuzz"
        n_litesvm = (
            sum(1 for _ in litesvm_dir.glob("*.rs"))
            if litesvm_dir.is_dir() else 0
        )
        if fuzz_dir.is_dir():
            for pattern in ("*.move", "*.sol", "*.c"):
                n_litesvm += sum(1 for _ in fuzz_dir.glob(pattern))
    except OSError:
        return None
    if n_prompts == 0:
        return None

    # Pull contested count from recon_summary if it exists
    n_contested = 0
    n_true = 0
    # ALSO compute the debate-planning denominator. The hunt orchestrator
    # at default debate_scope=all_high debates EVERY HIGH-confidence
    # verdict (both TRUE and FALSE), not just the L2 candidates. Without
    # this, the dashboard's "L1.5 X/Y" reads e.g. "27/14" because the
    # denominator was the L2-queue size (14) while debates run on 40.
    n_debate_planned = 0
    if recon_summary.is_file():
        try:
            data = json.loads(recon_summary.read_text(encoding="utf-8"))
            verdicts = data.get("verdicts", [])
            for v in verdicts:
                vd = v.get("verdict") or ""
                conf = (v.get("confidence") or "").upper()
                if vd == "TRUE":
                    n_true += 1
                    n_contested += 1
                elif vd == "NEEDS_LAYER_2_TO_DECIDE":
                    n_contested += 1
                if conf == "HIGH" and vd in ("TRUE", "FALSE"):
                    n_debate_planned += 1
        except (OSError, json.JSONDecodeError):
            pass

    # Phase detection
    if not recon_summary.is_file():
        phase = "recon"
        n_done, n_total = n_responses, n_prompts
        label = "Layer 1 recon"
    elif hunt_summary.is_file():
        phase = "publishing"
        n_done, n_total = 1, 1
        label = "publishing"
    elif n_litesvm > 0:
        phase = "litesvm"
        n_done, n_total = n_litesvm, max(n_litesvm, 1)
        label = "Layer 4 LiteSVM"
    elif n_kani_harnesses > 0:
        phase = "kani"
        n_done, n_total = n_kani_harnesses, max(n_kani_harnesses, 1)
        label = "Layer 3 Kani"
    elif n_poc_logs > 0 or n_poc_tests > 0:
        phase = "poc"
        # PoC progress: prefer hunt_summary.json's authoritative
        # n_candidates (the orchestrator's count of hyps that survived
        # debate and were actually dispatched to L2) when available.
        # Otherwise fall back to the L2 queue size from recon_summary.
        #
        # History — bugs this code has had:
        #
        # 1. Old denominator was just the L2 queue from recon (TRUE +
        #    NEEDS_LAYER_2). Missed debate promotions, displayed
        #    "L2 · 0/14" while 24 PoCs were actually running.
        #
        # 2. Old event filter only matched `poc_test_run` (Solana).
        #    Adapter-based languages (Aptos / C / Solidity) emit
        #    `poc_adapter_done`, so non-Solana progress read 0.
        #
        # 3. Denominator was ``max(l2_queue, tested_ids, n_poc_logs)``.
        #    All three inflate across resumes — n_poc_logs is the
        #    physical file count in ``poc/`` which accumulates, and
        #    tested_ids unions all unique hyps that EVER got a PoC in
        #    this cycle dir, including hyps promoted by earlier debate
        #    attempts that aren't in the final L2 queue. Operator saw
        #    "L2 38/38" on an aptos-small cycle whose hunt_summary
        #    said n_candidates=24. Use the authoritative summary value
        #    when present, and cap n_done at n_total so the gauge can
        #    never read >100%.
        l2_queue_ids: set[str] = set()
        if recon_summary.is_file():
            try:
                rs = json.loads(recon_summary.read_text(encoding="utf-8"))
                for v in rs.get("verdicts", []):
                    vd = (v.get("verdict") or "").upper()
                    if vd in ("TRUE", "NEEDS_LAYER_2_TO_DECIDE"):
                        hid = v.get("hypothesis_id")
                        if hid:
                            l2_queue_ids.add(hid)
            except (OSError, json.JSONDecodeError):
                pass
        tested_ids: set[str] = set()
        log_path = cycle_dir / "hunt.log.jsonl"
        if log_path.is_file():
            try:
                with log_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if '"event":' not in line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # poc_test_run = Solana legacy
                        # poc_adapter_done = Aptos / C / Solidity
                        if ev.get("event") in ("poc_test_run", "poc_adapter_done"):
                            hid = ev.get("hypothesis_id")
                            if hid:
                                tested_ids.add(hid)
            except OSError:
                pass

        # Authoritative L2 denominator: hunt_summary.json's n_candidates
        # is the orchestrator's own count of hyps it dispatched to L2.
        # Beats both file-count and event-count heuristics.
        #
        # Fallback chain (in priority order):
        #   1. hunt_summary.json (cycle finished + summary fresh)
        #   2. hunt_summary.json.pre-resume (cycle was resumed; the
        #      reopen-cycle code in hunt.py moves the live summary
        #      aside so the orchestrator can re-write it at re-finish.
        #      During the live resume the .pre-resume backup has the
        #      previous run's n_candidates — still authoritative for
        #      "how many hyps the orchestrator dispatched to L2".)
        #   3. L2 queue size from recon (TRUE + NEEDS_LAYER_2) — only
        #      a lower bound; misses debate-promoted FALSE hyps.
        summary_candidates: int | None = None
        for hs_candidate in (
            hunt_summary,
            cycle_dir / "hunt_summary.json.pre-resume",
        ):
            if hs_candidate.is_file():
                try:
                    _summary = json.loads(hs_candidate.read_text(encoding="utf-8"))
                    if isinstance(_summary.get("n_candidates"), int):
                        summary_candidates = _summary["n_candidates"]
                        break
                except (OSError, json.JSONDecodeError):
                    pass
        if summary_candidates is not None:
            n_total = max(summary_candidates, 1)
        elif l2_queue_ids:
            # Mid-flight: use L2 queue size. Don't union with tested_ids
            # because tested_ids accumulates across resume attempts that
            # may have run different debate-promoted sets.
            n_total = max(len(l2_queue_ids), 1)
        else:
            # Fallback if neither hunt_summary nor recon_summary readable
            n_total = max(n_contested, 1)
        # Numerator: hyps that completed L2 in THIS cycle. Cap at n_total
        # so the gauge never reads >100%.
        n_done = min(len(tested_ids), n_total)
        # When Layer 2 has fully covered the queue but Layer 3 hasn't started,
        # advance the label so the dashboard doesn't look stuck. The phase
        # itself stays "poc" (filesystem-driven) so downstream filters keep
        # working, but the label hints "ready for next layer".
        if n_total > 0 and n_done >= n_total:
            label = "Layer 2 PoC complete · ready for Layer 3"
        else:
            label = "Layer 2 PoC"
    elif n_debate_done > 0 or n_contested > 0:
        phase = "debate"
        n_done = n_debate_done
        # Denominator = total debates the orchestrator plans to run.
        # Default debate_scope=all_high → every HIGH-confidence verdict
        # (TRUE + FALSE). Falls back to n_contested if recon_summary
        # didn't surface enough info, and guards against n_done >
        # n_total (which produced the confusing "27/14" display).
        n_total = max(n_debate_planned, n_contested, n_debate_done, 1)
        label = "Layer 1.5 debate"
    else:
        phase = "recon"
        n_done, n_total = n_responses, n_prompts
        label = "Layer 1 recon"

    pct = (n_done / n_total * 100.0) if n_total else 0.0
    # If hunt_summary.json is present, prefer its authoritative L3/L4
    # run counts over the filesystem heuristic (which may miss
    # non-Solana artifact layouts even with the formal/fuzz dir fix
    # above). hunt_summary records `n_kani_runs` and `n_litesvm_runs`
    # at cycle end — the orchestrator's own count of executions.
    #
    # Same .pre-resume fallback as for n_candidates above: when
    # --resume-cycle moves the live hunt_summary aside, the .pre-
    # resume backup still has the previous run's authoritative
    # counts. Without this fallback the dashboard reports
    # n_kani_harnesses=0 / n_litesvm=0 during a live resume of a
    # previously-finished cycle.
    for hs_candidate in (
        hunt_summary,
        cycle_dir / "hunt_summary.json.pre-resume",
    ):
        if hs_candidate.is_file():
            try:
                _hs = json.loads(hs_candidate.read_text(encoding="utf-8"))
                if isinstance(_hs.get("n_kani_runs"), int):
                    n_kani_harnesses = max(n_kani_harnesses, _hs["n_kani_runs"])
                if isinstance(_hs.get("n_litesvm_runs"), int):
                    n_litesvm = max(n_litesvm, _hs["n_litesvm_runs"])
                break
            except (OSError, json.JSONDecodeError):
                pass
    return {
        # Legacy fields kept for back-compat with previous dashboard.py callers
        "n_prompts": n_prompts,
        "n_responses": n_responses,
        "n_verdicts": 0,
        # New phase-aware fields
        "phase": phase,
        "phase_label": label,
        "phase_done": n_done,
        "phase_total": n_total,
        "pct_complete": round(pct, 1),
        # Headline numbers regardless of phase
        "n_contested": n_contested,
        "n_true_layer1": n_true,
        "n_debate_done": n_debate_done,
        "n_poc_tests": n_poc_tests,
        "n_poc_logs": n_poc_logs,
        "n_kani_harnesses": n_kani_harnesses,
        "n_litesvm": n_litesvm,
    }


def _read_receipt_fingerprint(cycle_id: str | None) -> str | None:
    """Read a short fingerprint of the Ed25519 cycle receipt for display.

    The signing pipeline writes <cycle>/cycle.html.sig — a PEM-armoured file
    with a base64 signature line between BEGIN/END markers. We extract the
    base64 payload, decode it, and turn the first 8 bytes of the actual
    signature into a colon-separated hex string so the customer portal can
    show "3a:c1:8e:42:7f:11:b9:dd…" the way SSH host fingerprints are
    presented. Returns None on missing files / non-VPS hosts.
    """
    if not cycle_id:
        return None
    from audit_pipeline.utils.vps_paths import public_cycles_dir
    sig_path = public_cycles_dir() / cycle_id / "cycle.html.sig"
    if not sig_path.is_file():
        return None
    try:
        import base64
        raw = sig_path.read_text(encoding="utf-8")
        sig_b64 = ""
        in_block = False
        for line in raw.splitlines():
            if line.startswith("-----BEGIN JELLEO"):
                in_block = True
                continue
            if line.startswith("-----END JELLEO"):
                break
            if in_block:
                stripped = line.strip()
                # Skip header lines like "Algorithm: Ed25519" (contain ':')
                # and blank separator lines. The base64 payload follows.
                if stripped and ":" not in stripped:
                    sig_b64 += stripped
        if not sig_b64:
            # Tolerate a raw-base64 file (no PEM armour) too.
            sig_b64 = raw.strip()
        try:
            sig_bytes = base64.b64decode(sig_b64, validate=False)
        except Exception:
            return None
        if len(sig_bytes) < 4:
            return None
        return ":".join(f"{b:02x}" for b in sig_bytes[:8]) + "…"
    except Exception:
        return None


def _spend_summary() -> dict:
    """Read /root/.audit_api_calls.jsonl (written by llm.py per-call) and
    compute real spend totals. The schema for each line is:
      {"ts": ISO8601, "model": str, "input_tokens": int, "output_tokens": int,
       "cost_usd": float, "stop_reason": str, "caller": str}
    Returns: {today_usd, total_usd, last_24h_usd, last_call_at, n_calls_total,
              source}. Returns zeros if log absent.
    """
    import os as _os
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    log_path = Path(_os.environ.get("JELLEO_SPEND_LOG", "/root/.audit_api_calls.jsonl"))
    # Defensive .is_file() — if the path is unreadable (CI runs as a
    # non-root user where /root/ is permission-denied) we treat it as
    # missing rather than crashing the whole snapshot/dashboard cycle.
    try:
        log_exists = log_path.is_file()
    except (PermissionError, OSError):
        log_exists = False
    out = {
        "today_usd": 0.0,
        "total_usd": 0.0,
        "last_24h_usd": 0.0,
        "last_call_at": None,
        "n_calls_total": 0,
        "source": str(log_path) if log_exists else "missing",
    }
    if not log_exists:
        return out

    today_str = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    cutoff_24h = datetime.now(_tz.utc) - timedelta(hours=24)
    last_ts = None
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = float(ev.get("cost_usd") or 0)
                ts_str = ev.get("ts") or ""
                out["total_usd"] += cost
                out["n_calls_total"] += 1
                if ts_str.startswith(today_str):
                    out["today_usd"] += cost
                try:
                    ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=_tz.utc)
                    if ts_dt >= cutoff_24h:
                        out["last_24h_usd"] += cost
                    if last_ts is None or ts_dt > last_ts:
                        last_ts = ts_dt
                except (ValueError, TypeError):
                    pass
    except OSError:
        return out

    out["today_usd"] = round(out["today_usd"], 2)
    out["total_usd"] = round(out["total_usd"], 2)
    out["last_24h_usd"] = round(out["last_24h_usd"], 2)
    if last_ts is not None:
        out["last_call_at"] = last_ts.isoformat(timespec="seconds")
    return out


def _loop_uptime_human() -> str:
    """Best-effort uptime string for jelleo-shadow.service.

    Returns "—" on non-systemd hosts.
    """
    import shutil
    import subprocess
    from datetime import datetime
    from datetime import timezone as _tz

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
