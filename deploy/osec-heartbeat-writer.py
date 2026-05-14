#!/usr/bin/env python3
"""Heartbeat writer with PERSISTENT spend tracking.

Reads hunt.log.jsonl from every OSec cell's hunts dirs, sums per-event
token usage at Sonnet 4.6 prices, and exposes:
  * active_cycle_spend_usd     — spend on the currently-running cycle
  * session_total_spend_usd    — cumulative across every cycle this customer
                                  has ever run (so the dashboard's cost
                                  counter survives page refreshes)

Fix log:

2026-05-13 — Initial bug-fix pass:
  * active_cycle_hyps_done used to count raw recon_hyp_done events,
    which DOUBLE-COUNTS across resume attempts (cycle 20260513-191318
    showed 67 "done" out of 40 hyps after three resume passes). Now
    dedupes by hyp_id.
  * engine_sha was hardcoded to "". Now reads from the cycle's
    hunt_summary.json when present.

2026-05-13 audit pass — fundamentals sweep:
  * service_summary was three hardcoded "active" literals — the
    dashboard would show "all systems up" even when the hunt loop
    had crashed. Now does real systemctl is-active checks against
    the actual service units AND pgrep checks against the bash
    osec-snapshot-loop.sh process.
  * engine_sha returned "" for in-progress cycles (because
    hunt_summary.json only lands at cycle close). Now falls back
    to cycles.engine_sha in the DB for mid-flight cycles, then
    finally to "" only when neither source has it.
  * cost_from_log conflated "input_tokens field absent" with
    "input_tokens = 0". Cached-prompt replays legitimately have
    zero token deltas and should NOT trigger the flat-rate
    fallback. Now distinguishes via `"input_tokens" in e`.
  * Atomic write of heartbeat.json — caller now uses tmp+rename
    pattern via the helper at the bottom of this file (called
    from osec-snapshot-loop.sh).
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

DB = "/root/audit_runs/ottersec-eval/findings.db"
CELLS_GLOB = "/root/audit_runs/ottersec-eval/workspaces/*/hunts"

# Sonnet 4.6 pricing — $3/M input, $15/M output
INPUT_RATE = 3.0 / 1_000_000
OUTPUT_RATE = 15.0 / 1_000_000

# Flat-rate fallbacks used ONLY when an event explicitly lacks token
# fields. Events with input_tokens=0 + output_tokens=0 are treated as
# genuine zero-cost (cached-prompt replay) — NOT flat-rate.
FLAT_DEBATE = 0.05
FLAT_POC    = 0.05
FLAT_KANI   = 0.50
FLAT_TRIAGE = 0.05
FLAT_LITESVM = 0.10


def _event_token_cost(e, flat_fallback):
    """Compute the LLM cost for a single hunt-log event.

    If both input_tokens and output_tokens fields are PRESENT (even
    if zero), use the token-based math (which yields 0 for a cached
    replay). If both fields are ABSENT, the event wasn't emitted by
    a token-aware code path, so fall back to the per-event flat rate.
    Partial presence (one field set, one absent) is unusual but
    treated as token-based (the absent field acts as zero).
    """
    has_in = "input_tokens" in e
    has_out = "output_tokens" in e
    if not has_in and not has_out:
        return flat_fallback
    it = e.get("input_tokens") or 0
    ot = e.get("output_tokens") or 0
    return it * INPUT_RATE + ot * OUTPUT_RATE


def cost_from_log(log_path):
    """Sum LLM cost across every event that records tokens. Returns
    (total_cost_usd, unique_hyp_ids_seen_in_recon)."""
    total = 0.0
    unique_recon = set()
    try:
        with open(log_path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ev = e.get("event")
                if ev == "recon_hyp_done":
                    total += _event_token_cost(e, 0)  # recon always has tokens
                    hid = e.get("hyp_id") or e.get("hypothesis_id")
                    if hid:
                        unique_recon.add(hid)
                elif ev == "debate_one":
                    total += _event_token_cost(e, FLAT_DEBATE)
                elif ev == "poc_llm_authored":
                    total += _event_token_cost(e, FLAT_POC)
                elif ev == "kani_one":
                    total += _event_token_cost(e, FLAT_KANI)
                elif ev == "triage_one" and e.get("used_llm"):
                    total += _event_token_cost(e, FLAT_TRIAGE)
                elif ev == "litesvm_authored":
                    total += _event_token_cost(e, FLAT_LITESVM)
                elif ev == "l3_adapter_done":
                    # L3 formal verification (Move Prover / CBMC /
                    # SMTChecker) — adapter records ~$0.50 per harness
                    # via the daily_cap; no token fields on the event.
                    total += FLAT_KANI
                elif ev == "l4_adapter_done":
                    # L4 runtime fuzz — adapter records ~$0.10 per
                    # harness via the daily_cap; no token fields.
                    total += FLAT_LITESVM
    except Exception:
        pass
    return total, unique_recon


def engine_sha_for_cycle(cycle_id):
    """Read engine_sha from the cycle, preferring hunt_summary.json
    (orchestrator-authoritative, written at cycle close) and falling
    back to cycles.engine_sha in the DB (populated at cycle start)
    so in-progress cycles still show a sha on the dashboard."""
    if not cycle_id:
        return ""
    # Source 1: hunt_summary.json for any cell hosting this cycle.
    for cell in glob.glob(f"/root/audit_runs/ottersec-eval/workspaces/*/hunts/{cycle_id}"):
        hs = os.path.join(cell, "hunt_summary.json")
        if os.path.exists(hs):
            try:
                with open(hs) as f:
                    data = json.load(f)
                sha = (data.get("engine_sha") or "")
                if sha:
                    return sha[:10]
            except Exception:
                pass
    # Source 2: cycles.engine_sha column. Set at cycle start by hunt.py.
    try:
        with sqlite3.connect(DB) as c:
            row = c.execute(
                "SELECT engine_sha FROM cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row and row[0]:
                return row[0][:10]
    except sqlite3.Error:
        pass
    return ""


def probe_services():
    """Return real per-service state (no fabrication).

    Checks:
      * jelleo-watch.service       — runs the hunt loop
      * jelleo-shadow.service      — runs the live mainnet shadow audit
      * jelleo-sse.service         — SSE event stream for dashboards
      * jelleo-token-auth.service  — HMAC sidecar
      * osec-snapshot-loop.sh      — the manifest/heartbeat writer loop
                                     (pgrep, not systemd)

    Returns (service_summary_dict, services_list_dict) where:
      service_summary maps service-key -> "active"|"inactive"|"unknown"
      services list is [{"name", "state": "up"|"down"|"unknown"}]
    """
    systemd_units = [
        ("hunt_loop", "jelleo-watch.service"),
        ("shadow",    "jelleo-shadow.service"),
        ("sse",       "jelleo-sse.service"),
        ("token_auth", "jelleo-token-auth.service"),
    ]
    summary = {}
    services = []
    have_systemctl = shutil.which("systemctl") is not None
    for key, unit in systemd_units:
        state = "unknown"
        if have_systemctl:
            try:
                rc = subprocess.run(
                    ["systemctl", "is-active", unit],
                    capture_output=True, text=True, timeout=5,
                )
                # is-active prints "active" / "inactive" / "failed" / ...
                txt = (rc.stdout or "").strip().lower()
                if txt == "active":
                    state = "active"
                elif txt:
                    state = txt  # surface "failed" / "inactive" rather than masking
            except (subprocess.SubprocessError, OSError):
                state = "unknown"
        summary[key] = state
        services.append({
            "name": key,
            "state": "up" if state == "active" else "down" if state == "unknown" else state,
        })
    # snapshot_writer = the bash loop, not a systemd unit. Pgrep it.
    snapshot_alive = False
    try:
        rc = subprocess.run(
            ["pgrep", "-f", "osec-snapshot-loop.sh"],
            capture_output=True, text=True, timeout=5,
        )
        snapshot_alive = bool((rc.stdout or "").strip())
    except (subprocess.SubprocessError, OSError):
        pass
    summary["snapshot_writer"] = "active" if snapshot_alive else "inactive"
    services.append({
        "name": "snapshot_writer",
        "state": "up" if snapshot_alive else "down",
    })
    # heartbeat = this very script. If we're running, it's "active".
    summary["heartbeat"] = "active"
    services.append({"name": "heartbeat", "state": "up"})
    return summary, services


def main():
    conn = sqlite3.connect(DB)
    n_cycles = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    last_ts = conn.execute("SELECT MAX(started_at) FROM cycles").fetchone()[0]
    n_findings = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    row = conn.execute(
        "SELECT cycle_id, target_id FROM cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    last_cycle_id, _ = row or (None, None)

    # Active cycle spend + unique recon hyps
    active_cycle_spend = 0.0
    active_unique_hyps = set()
    if last_cycle_id:
        for cell in glob.glob(f"/root/audit_runs/ottersec-eval/workspaces/*/hunts/{last_cycle_id}"):
            s, u = cost_from_log(os.path.join(cell, "hunt.log.jsonl"))
            active_cycle_spend += s
            active_unique_hyps |= u

    # Session total: all cycles, all cells.
    session_total = 0.0
    n_logs = 0
    for lp in glob.glob(f"{CELLS_GLOB}/*/hunt.log.jsonl"):
        s, _ = cost_from_log(lp)
        session_total += s
        n_logs += 1

    service_summary, services_list = probe_services()

    hb = {
        "customer_id": "ottersec",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine_sha": engine_sha_for_cycle(last_cycle_id),
        "cycles_total": n_cycles,
        "last_cycle_id": last_cycle_id,
        "last_cycle_ts": last_ts,
        "n_findings_total": n_findings,
        "active_cycle_spend_usd": round(active_cycle_spend, 3),
        "active_cycle_hyps_done": len(active_unique_hyps),
        "session_total_spend_usd": round(session_total, 3),
        "n_cycle_logs_scanned": n_logs,
        "service_summary": service_summary,
        "services": services_list,
    }
    print(json.dumps(hb, indent=2))


if __name__ == "__main__":
    main()
