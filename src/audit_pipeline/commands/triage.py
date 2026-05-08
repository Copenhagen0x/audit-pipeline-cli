"""`audit-pipeline triage` — local web UI for walking through `new` findings.

Tier 2 #12.

Starts a local HTTP server that serves a single-page UI for triaging
findings one at a time. Backlog of `new` findings → clicked-through
verdicts in an afternoon, instead of CLI typing.

Usage:
    audit-pipeline triage                       # default port 8080
    audit-pipeline triage --port 8765
    audit-pipeline triage --bind 0.0.0.0       # exposing externally (use with care)

API surface:
    GET  /                    → serve the SPA HTML
    GET  /api/next            → return next finding to triage (or {"done": true})
    GET  /api/stats           → return per-status counters
    POST /api/transition      → apply lifecycle transition
                                body: {"finding_id": int, "to": str, "reason": str}

Server is single-threaded http.server. Trivial to run, no deps.
"""

from __future__ import annotations

import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.lifecycle import Status

console = Console()


# ─────────────────────────── SPA HTML ──────────────────────────────────────

# Single-page app. Inlined so triage works with no extra static-file path.
TRIAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jelleo · Triage</title>
<meta name="robots" content="noindex,nofollow">
<style>
:root {
  --bg: #050504; --bg-2: #0a0908;
  --ink: #f5f3ed; --ink-2: rgba(245,243,237,0.72); --ink-3: rgba(245,243,237,0.46); --ink-4: rgba(245,243,237,0.28);
  --rule: rgba(245,243,237,0.08); --rule-2: rgba(245,243,237,0.16);
  --surface: rgba(245,243,237,0.025); --surface-2: rgba(245,243,237,0.045);
  --amber: #f5b800; --amber-2: #ffce4a;
  --ok: #4ade80; --warn: #fbbf24; --alert: #ef4444; --info: #60a5fa;
  --critical: #ef4444; --high: #f97316; --medium: #eab308; --low: #60a5fa;
  --font: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 15px; line-height: 1.5; min-height: 100vh; }
.shell { max-width: 1100px; margin: 0 auto; padding: 24px; }
header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 18px; border-bottom: 1px solid var(--rule); margin-bottom: 28px; }
header .brand { font-weight: 700; font-size: 18px; letter-spacing: -0.01em; }
header .brand .accent { color: var(--amber); }
header .stats { display: flex; gap: 20px; font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.16em; color: var(--ink-3); }
header .stats .n { color: var(--ink); font-weight: 600; }
.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 32px 36px; }
.empty { text-align: center; padding: 80px 20px; color: var(--ink-3); }
.empty h2 { color: var(--ink); font-size: 22px; margin-bottom: 8px; }
.row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; align-items: baseline; }
.row .pill { font-family: var(--mono); font-size: 11px; padding: 3px 10px; border-radius: 4px; text-transform: uppercase; letter-spacing: 0.14em; }
.pill.critical { background: rgba(239,68,68,0.12); color: var(--critical); border: 1px solid rgba(239,68,68,0.3); }
.pill.high { background: rgba(249,115,22,0.12); color: var(--high); border: 1px solid rgba(249,115,22,0.3); }
.pill.medium { background: rgba(234,179,8,0.12); color: var(--medium); border: 1px solid rgba(234,179,8,0.3); }
.pill.low { background: rgba(96,165,250,0.12); color: var(--low); border: 1px solid rgba(96,165,250,0.3); }
.pill.info { background: var(--bg-2); color: var(--ink-3); border: 1px solid var(--rule); }
.pill.status { background: var(--bg-2); color: var(--ink-2); border: 1px solid var(--rule); }
.hyp-id { font-family: var(--mono); font-size: 13px; color: var(--amber); font-weight: 500; }
.title { font-size: 20px; font-weight: 600; line-height: 1.35; margin-bottom: 18px; color: var(--ink); }
.meta { font-family: var(--mono); font-size: 12px; color: var(--ink-3); margin-bottom: 22px; letter-spacing: 0.04em; }
.meta a { color: var(--amber); border-bottom: 1px dashed rgba(245,184,0,0.3); text-decoration: none; }
.section { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--rule); }
.section h3 { font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--ink-3); margin-bottom: 12px; }
.section pre, .claim { background: var(--bg-2); border: 1px solid var(--rule); border-radius: 8px; padding: 16px 18px; font-family: var(--mono); font-size: 12.5px; line-height: 1.65; color: var(--ink-2); overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }
.claim { font-family: var(--font); font-size: 14px; color: var(--ink); }
.actions { display: flex; gap: 12px; margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--rule); flex-wrap: wrap; }
.btn { padding: 12px 22px; border: none; border-radius: 8px; font-family: var(--font); font-size: 14px; font-weight: 600; cursor: pointer; transition: transform .15s, box-shadow .15s, background .15s; }
.btn:hover { transform: translateY(-1px); }
.btn:active { transform: translateY(0); }
.btn.confirm { background: var(--amber); color: var(--bg); }
.btn.confirm:hover { background: var(--amber-2); box-shadow: 0 0 24px rgba(245,184,0,0.4); }
.btn.triage { background: rgba(96,165,250,0.18); color: var(--info); border: 1px solid rgba(96,165,250,0.4); }
.btn.triage:hover { background: rgba(96,165,250,0.28); }
.btn.reject { background: rgba(239,68,68,0.18); color: var(--alert); border: 1px solid rgba(239,68,68,0.4); }
.btn.reject:hover { background: rgba(239,68,68,0.28); }
.btn.skip { background: var(--surface); color: var(--ink-2); border: 1px solid var(--rule-2); margin-left: auto; }
.btn.skip:hover { background: var(--surface-2); color: var(--ink); }
.kbd { display: inline-block; font-family: var(--mono); font-size: 10px; padding: 1px 5px; background: rgba(0,0,0,0.4); border: 1px solid var(--rule); border-radius: 3px; margin-left: 6px; color: var(--ink-3); }
.toast { position: fixed; bottom: 24px; right: 24px; padding: 14px 22px; background: var(--surface-2); border: 1px solid var(--rule-2); border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .25s; pointer-events: none; }
.toast.show { opacity: 1; }
.toast.ok { border-left: 3px solid var(--ok); }
.toast.err { border-left: 3px solid var(--alert); color: var(--alert); }
</style>
</head>
<body>
<div class="shell">
  <header>
    <div class="brand">jelleo<span class="accent">/</span>triage</div>
    <div class="stats" id="stats">
      <div><span class="n" id="s-new">—</span> new</div>
      <div><span class="n" id="s-triaged">—</span> triaged</div>
      <div><span class="n" id="s-confirmed">—</span> confirmed</div>
      <div><span class="n" id="s-rejected">—</span> rejected</div>
    </div>
  </header>

  <div id="container"></div>
</div>

<div id="toast" class="toast"></div>

<script>
let CURRENT = null;

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts || {});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function pillClass(level) {
  const m = String(level || '').toLowerCase();
  if (['critical', 'high', 'medium', 'low'].includes(m)) return m;
  return 'info';
}

async function renderStats() {
  try {
    const s = await fetchJSON('/api/stats');
    document.getElementById('s-new').textContent = s.new || 0;
    document.getElementById('s-triaged').textContent = s.triaged || 0;
    document.getElementById('s-confirmed').textContent = s.confirmed || 0;
    document.getElementById('s-rejected').textContent = s.rejected || 0;
  } catch (e) {}
}

async function renderNext() {
  const c = document.getElementById('container');
  c.innerHTML = '<div class="card empty"><h2>Loading…</h2></div>';
  try {
    const f = await fetchJSON('/api/next');
    if (f.done) {
      c.innerHTML = '<div class="card empty"><h2>Backlog clear</h2><p>No more findings in <code>new</code> status. Either rerun a hunt cycle or close the page.</p></div>';
      CURRENT = null;
      return;
    }
    CURRENT = f;
    const sev = pillClass(f.severity);
    const detailsLine = [];
    if (f.target_file) detailsLine.push('<a href="javascript:void(0)" title="' + escapeHtml(f.target_file) + '">' + escapeHtml(f.target_file) + '</a>');
    if (f.bug_class) detailsLine.push('bug class: <strong style="color: var(--ink-2);">' + escapeHtml(f.bug_class) + '</strong>');
    if (f.engine_sha) detailsLine.push('engine: ' + escapeHtml(String(f.engine_sha).slice(0, 10)));
    if (f.cycle_id) detailsLine.push('cycle: ' + escapeHtml(f.cycle_id));
    c.innerHTML = `
      <div class="card">
        <div class="row">
          <span class="hyp-id">${escapeHtml(f.hypothesis_id || '#' + f.id)}</span>
          <span class="pill ${sev}">${escapeHtml(f.severity || 'Info')}</span>
          <span class="pill status">${escapeHtml(f.status || 'new')}</span>
          ${f.poc_fired ? '<span class="pill critical">PoC fired</span>' : ''}
          ${f.debate_promoted ? '<span class="pill medium">debate promoted</span>' : ''}
        </div>
        <div class="title">${escapeHtml(f.title || '(no title)')}</div>
        <div class="meta">${detailsLine.join(' · ')}</div>

        <div class="section">
          <h3>Verdict</h3>
          <div class="claim">${escapeHtml(f.verdict || '(no verdict text)')}</div>
        </div>

        ${f.details_text ? '<div class="section"><h3>Details</h3><pre>' + escapeHtml(f.details_text) + '</pre></div>' : ''}

        <div class="actions">
          <button class="btn confirm" onclick="apply('confirmed', 'human-review confirmed during triage')">Confirm <span class="kbd">C</span></button>
          <button class="btn triage" onclick="apply('triaged', 'human-review triaged: needs more investigation')">Triage <span class="kbd">T</span></button>
          <button class="btn reject" onclick="apply('rejected', 'human-review rejected as false positive')">Reject <span class="kbd">R</span></button>
          <button class="btn skip" onclick="skipNext()">Skip <span class="kbd">N</span></button>
        </div>
      </div>
    `;
  } catch (e) {
    c.innerHTML = '<div class="card empty"><h2>Error</h2><p>Could not load: ' + escapeHtml(e.message) + '</p></div>';
  }
}

async function apply(toStatus, reason) {
  if (!CURRENT) return;
  const fid = CURRENT.id;
  try {
    const r = await fetchJSON('/api/transition', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ finding_id: fid, to: toStatus, reason: reason }),
    });
    showToast('ok', '#' + fid + ' → ' + toStatus);
  } catch (e) {
    showToast('err', 'transition failed: ' + e.message);
  }
  await renderStats();
  await renderNext();
}

async function skipNext() {
  // Re-fetch /api/next with skip-id so server can rotate
  if (!CURRENT) return;
  try {
    const f = await fetchJSON('/api/next?skip=' + CURRENT.id);
    if (f.done) {
      document.getElementById('container').innerHTML = '<div class="card empty"><h2>Backlog clear</h2></div>';
      return;
    }
    CURRENT = f;
    await renderNext();
  } catch (e) {
    showToast('err', e.message);
  }
}

function showToast(kind, msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.remove('ok', 'err');
  t.classList.add(kind, 'show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'c' || e.key === 'C') apply('confirmed', 'human-review confirmed during triage');
  else if (e.key === 't' || e.key === 'T') apply('triaged', 'human-review triaged: needs more investigation');
  else if (e.key === 'r' || e.key === 'R') apply('rejected', 'human-review rejected as false positive');
  else if (e.key === 'n' || e.key === 'N') skipNext();
});

renderStats();
renderNext();
setInterval(renderStats, 15000);
</script>
</body>
</html>
"""


# ─────────────────────────── CLI ────────────────────────────────────────────


@click.command(name="triage")
@click.option("--port", type=int, default=8080, show_default=True,
              help="Port to bind the local triage server")
@click.option("--bind", default="127.0.0.1", show_default=True,
              help="Address to bind. Use 0.0.0.0 only when exposing externally.")
@click.option("--severity-floor", default=None,
              help="Only show findings of this severity or higher (Critical|High|Medium|Low|Info)")
@click.option("--target", default=None,
              help="Only show findings for this target name")
@click.pass_context
def triage_cmd(
    ctx: click.Context,
    port: int,
    bind: str,
    severity_floor: str | None,
    target: str | None,
) -> None:
    """Walk through the `new` findings backlog one at a time in a browser UI."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")

    # Resolve target_id once if filter passed
    target_id_filter = None
    if target:
        for t in db.list_targets():
            if (t.get("name") or "").lower() == target.lower():
                target_id_filter = t["id"]
                break
        if target_id_filter is None:
            raise click.ClickException(f"target {target!r} not found in DB")

    handler_factory = _handler_factory(
        db=db,
        severity_floor=severity_floor,
        target_id_filter=target_id_filter,
    )
    addr = (bind, port)
    try:
        server = HTTPServer(addr, handler_factory)
    except OSError as e:
        raise click.ClickException(f"could not bind {bind}:{port} — {e}")

    url = f"http://{bind if bind != '0.0.0.0' else '127.0.0.1'}:{port}/"
    console.print(f"[bold green]triage server[/bold green] · [link={url}]{url}[/link]")
    console.print(f"  workspace = {workspace}")
    if severity_floor:
        console.print(f"  severity floor = {severity_floor}")
    if target:
        console.print(f"  target = {target}")
    console.print("  shortcuts: [bold]C[/bold]onfirm · [bold]T[/bold]riage · [bold]R[/bold]eject · [bold]N[/bold]ext")
    console.print("  press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]triage stopped[/dim]")
        server.shutdown()


def _handler_factory(
    *,
    db: FindingsDB,
    severity_floor: str | None,
    target_id_filter: int | None,
):
    """Build an HTTPRequestHandler subclass with the needed closure state."""

    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}
    sev_floor_idx = severity_order.get(severity_floor, 99) if severity_floor else None

    def passes_severity(sev: str | None) -> bool:
        if sev_floor_idx is None:
            return True
        return severity_order.get(sev or "Info", 99) <= sev_floor_idx

    def passes_target(target_id) -> bool:
        if target_id_filter is None:
            return True
        return target_id == target_id_filter

    class TriageHandler(BaseHTTPRequestHandler):
        # Quiet logs — too noisy for an interactive tool
        def log_message(self, fmt, *args): pass

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload, code: int = 200):
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlparse
            url = urlparse(self.path)
            if url.path == "/" or url.path == "/index.html":
                self._send(200, TRIAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if url.path == "/api/stats":
                rows = db.list_findings(limit=10_000)
                cnt = Counter(r.get("status") for r in rows)
                self._send_json({k: int(v) for k, v in cnt.items()})
                return
            if url.path == "/api/next":
                qs = parse_qs(url.query or "")
                skip_id = None
                if "skip" in qs:
                    try:
                        skip_id = int(qs["skip"][0])
                    except (ValueError, IndexError):
                        skip_id = None
                rows = db.list_findings(status=Status.NEW, limit=10_000)
                # Sort by severity then by recency for review usefulness
                rows.sort(
                    key=lambda r: (severity_order.get(r.get("severity") or "Info", 99),
                                   r.get("updated_at") or ""),
                )
                for r in rows:
                    if not passes_severity(r.get("severity")):
                        continue
                    if not passes_target(r.get("target_id")):
                        continue
                    if skip_id is not None and r.get("id") == skip_id:
                        continue
                    # Truncate big text fields for the browser payload
                    payload = {
                        "id": r["id"],
                        "hypothesis_id": r.get("hypothesis_id"),
                        "title": r.get("title"),
                        "severity": r.get("severity"),
                        "status": r.get("status"),
                        "verdict": (r.get("verdict") or "")[:4000],
                        "bug_class": r.get("bug_class"),
                        "target_id": r.get("target_id"),
                        "cycle_id": r.get("cycle_id"),
                        "engine_sha": r.get("engine_sha"),
                        "poc_fired": bool(r.get("poc_fired")),
                        "debate_promoted": bool(r.get("debate_promoted")),
                        "details_text": (r.get("details_json") or "")[:2000] or None,
                    }
                    self._send_json(payload)
                    return
                self._send_json({"done": True})
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            from urllib.parse import urlparse
            url = urlparse(self.path)
            if url.path != "/api/transition":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, code=400)
                return
            try:
                fid = int(body.get("finding_id"))
                to_str = str(body.get("to") or "").lower()
                reason = str(body.get("reason") or "triaged via web ui")
                to_status = Status(to_str)
            except (ValueError, TypeError):
                self._send_json({"error": "bad request"}, code=400)
                return
            try:
                db.transition_finding(fid, to_status, reason, actor="triage-ui")
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, code=409)
                return
            self._send_json({"ok": True, "finding_id": fid, "to": to_str})

    return TriageHandler
