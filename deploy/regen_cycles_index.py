#!/usr/bin/env python3
"""regen_cycles_index.py — Regenerate the cycle archive landing page.

Scans <docroot>/cycles/ for cycle directories with signed receipts, then
writes <docroot>/cycles/index.html — a Jelleo-styled archive page listing
every signed cycle (most recent first) with links to each per-cycle
landing page.

Without this, the bare URL ``api.jelleo.com/cycles/`` returns 404 because
nginx doesn't auto-index the directory. Per-cycle URLs (``/cycles/<id>/``)
already work — they have their own index.html written by publish_cycle_signed.sh.

Run after every publish, or as a standalone refresh:

    python3 deploy/regen_cycles_index.py [--docroot /var/www/jelleo.com]

Idempotent. Atomic write.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DOCROOT = Path("/var/www/jelleo.com")
DEFAULT_CHROME_DIR = Path(__file__).resolve().parent / "jelleo_chrome"
SIG_TS_RE = re.compile(r"^Signed-At:\s*(.+)$", re.MULTILINE)
SIG_BYTES_RE = re.compile(r"^Signed-Bytes:\s*(\d+)$", re.MULTILINE)


def _read_signed_at(sig_path: Path) -> str | None:
    """Pull the ``Signed-At`` field out of a JELLEO armoured sig file."""
    try:
        text = sig_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = SIG_TS_RE.search(text)
    return m.group(1).strip() if m else None


def _short_id(cid: str) -> str:
    """Render a long cycle id as a compact pair (yyyy-mm-dd · sha)."""
    parts = cid.split("-")
    if len(parts) >= 4 and parts[0].isdigit() and len(parts[0]) == 8:
        date = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
        time = parts[1] if len(parts) > 1 else ""
        sha = parts[-1] if len(parts) > 2 else ""
        return f"{date} · {time[:6]} · <code>{sha}</code>"
    return f"<code>{cid}</code>"


def _scan_cycles(cycles_dir: Path) -> list[dict[str, str]]:
    """Return a list of {cycle_id, signed_at, has_pdf, html_name, sig_name, pdf_name}
    dicts, sorted newest-first.

    Handles BOTH historical naming conventions:
      - Legacy: cycle.html / cycle.pdf / cycle.html.sig
      - Auto-publish: hunt_report.html / hunt_report.pdf / hunt_report.html.sig

    Whichever pair exists in the cycle dir is the pair we link to.
    """
    out: list[dict[str, str]] = []
    if not cycles_dir.is_dir():
        return out
    # Disclosure audit Defect 07 (MED): validate cycle dir names BEFORE
    # interpolating them into HTML (and into the verify-command snippet
    # in wrap_per_cycle_landing.py). A maliciously-named subdir like
    # `foo"><script>alert(1)</script>` would otherwise reach output raw.
    _SAFE_CYCLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    for child in cycles_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not _SAFE_CYCLE_RE.match(child.name):
            # Skip cycle dirs whose names don't match the strict format.
            continue
        # Detect the signed HTML artefact (either naming convention).
        sig: Path | None = None
        html_name = ""
        sig_name = ""
        for candidate_html in ("cycle.html", "hunt_report.html"):
            candidate_sig = child / f"{candidate_html}.sig"
            if candidate_sig.is_file() and (child / candidate_html).is_file():
                sig = candidate_sig
                html_name = candidate_html
                sig_name = candidate_sig.name
                break
        if sig is None:
            continue
        # Optional PDF, same naming family as the HTML.
        pdf_name = f"{html_name[:-5]}.pdf"  # strip .html, append .pdf
        # Disclosure audit Defect 08 (MED): surface retracted cycles
        # visually in the archive. Two signals:
        #   * directory name starts with ``RETRACTED-``
        #   * ``retraction.json`` sidecar exists
        retracted = (
            child.name.upper().startswith("RETRACTED-")
            or (child / "retraction.json").is_file()
        )
        # Read target name from hunt_summary.json if present so the
        # cycles archive can label each cycle by its audit target
        # (e.g. `osec-aptos-large`, `percolator`). Without this every
        # row looks identical to a reader who doesn't know cycle IDs.
        target = ""
        for summary_name in ("hunt_summary.json",):
            summary_path = child / summary_name
            if summary_path.is_file():
                try:
                    import json as _json
                    summary = _json.loads(summary_path.read_text(encoding="utf-8"))
                    target = (summary.get("target") or "").strip()
                    if target:
                        break
                except (OSError, ValueError):
                    pass
        out.append({
            "cycle_id":  child.name,
            "signed_at": _read_signed_at(sig) or "",
            "has_pdf":   "y" if (child / pdf_name).is_file() else "n",
            "html_name": html_name,
            "pdf_name":  pdf_name,
            "sig_name":  sig_name,
            "retracted": "y" if retracted else "n",
            "target":    target,
        })
    # Disclosure audit Defect 08: sort by parsed signed_at timestamp,
    # NOT by directory name. A ``RETRACTED-`` prefix would otherwise
    # alpha-sort above a fresh ``20260512-`` cycle in some locales.
    # Cycles without a parseable timestamp fall to the end.
    def _sort_key(r: dict[str, str]) -> tuple[int, str]:
        ts = r.get("signed_at") or ""
        # Negative-string sort by ISO timestamp = newest first
        return (0 if ts else 1, ts)
    out.sort(key=_sort_key, reverse=True)
    return out


def _row_html(row: dict[str, str]) -> str:
    from html import escape as _esc
    # Defense-in-depth on top of the _SAFE_CYCLE_RE filter in _scan_cycles.
    cid = _esc(row["cycle_id"])
    label = _short_id(cid)
    pdf_cell = (
        f'<a class="cyc-link" href="{cid}/{_esc(row["pdf_name"])}">PDF</a>'
        if row["has_pdf"] == "y" else '<span class="cyc-muted">—</span>'
    )
    signed_at = _esc(row["signed_at"]) if row["signed_at"] else '<span class="cyc-muted">unknown</span>'
    # Disclosure audit Defect 08: render a visible "RETRACTED" badge on
    # retracted cycle rows so the public archive doesn't display a bad
    # cycle as if it were signed-and-valid.
    retracted_badge = (
        ' <span style="background:#7a1a1a;color:#fff;padding:1px 6px;'
        'border-radius:3px;font-size:10px;margin-left:6px;letter-spacing:.1em;">'
        'RETRACTED</span>'
        if row.get("retracted") == "y" else ""
    )
    row_class = ' class="cyc-retracted"' if row.get("retracted") == "y" else ""
    target = _esc(row.get("target") or "")
    target_cell = (
        f'<td><code class="cyc-target">{target}</code></td>'
        if target else '<td class="cyc-muted">—</td>'
    )
    return (
        f'<tr{row_class}>'
        f'<td><a class="cyc-row-link" href="{cid}/{_esc(row["html_name"])}">{label}</a>{retracted_badge}</td>'
        f'{target_cell}'
        f'<td class="cyc-muted">{signed_at}</td>'
        f'<td><a class="cyc-link" href="{cid}/{_esc(row["html_name"])}">HTML</a></td>'
        f'<td>{pdf_cell}</td>'
        f'<td><a class="cyc-link" href="{cid}/{_esc(row["sig_name"])}">sig</a></td>'
        # Pubkey link is platform-wide — every row points at the same
        # canonical key on api.jelleo.com. Surfaced per-row so a reviewer
        # has the full `audit-pipeline sign verify` triple
        # (file + .sig + .pub) without leaving the cycles index.
        f'<td><a class="cyc-link" href="https://api.jelleo.com/keys/jelleo.ed25519.pub">.pub</a></td>'
        '</tr>'
    )


def _read_chrome_partials(chrome_dir: Path) -> tuple[str, str] | None:
    """Read jelleo.com chrome (top + bottom) for full-theme wrapping.

    chrome_top.html ends right before <main id="main">; chrome_bottom.html
    starts at the <footer> tag. Anything between them is the page body.
    Returns (top, bottom) or None if either is missing.
    """
    top = chrome_dir / "chrome_top.html"
    bot = chrome_dir / "chrome_bottom.html"
    if not (top.is_file() and bot.is_file()):
        return None
    try:
        return top.read_text(encoding="utf-8"), bot.read_text(encoding="utf-8")
    except OSError:
        return None


def _render_cycles_body(rows: list[dict[str, str]]) -> str:
    """The <main>...</main> body for the cycle archive page. Uses a single
    scoped <style> block keyed on .cyc-* classes so it doesn't fight the
    chrome's global CSS, and rows render with consistent column widths +
    typography across the table."""
    n = len(rows)
    body = "\n          ".join(_row_html(r) for r in rows) if rows else (
        '<tr><td colspan="7" class="cyc-muted">no signed cycles yet</td></tr>'
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<main id="main">
<style>
  .cyc-page {{ max-width: 1080px; margin: 48px auto 80px; padding: 0 32px; }}
  @media (max-width: 768px) {{ .cyc-page {{ padding: 0 20px; margin: 32px auto 56px; }} }}
  .cyc-h1 {{ font-size: clamp(28px, 4vw, 44px); font-weight: 600; letter-spacing: -0.015em; color: var(--ink); margin-bottom: 14px; line-height: 1.15; }}
  .cyc-h1 .cyc-accent {{ color: var(--amber); }}
  .cyc-lede {{ color: var(--ink-3); font-size: 15px; max-width: 720px; margin-bottom: 36px; line-height: 1.6; }}
  .cyc-stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 0 0 48px; }}
  .cyc-stat {{ background: var(--surface); border: 1px solid var(--rule-2); border-radius: 8px; padding: 18px 22px; transition: border-color .2s, background .2s; }}
  .cyc-stat:hover {{ border-color: rgba(245,184,0,.4); background: var(--surface-2); }}
  .cyc-stat-num {{ font-size: 1.8rem; font-weight: 600; color: var(--amber); letter-spacing: -.015em; line-height: 1.1; font-feature-settings: 'tnum'; }}
  .cyc-stat-lbl {{ font-family: var(--mono); font-size: 10px; letter-spacing: .18em; text-transform: uppercase; color: var(--ink-3); margin-top: 8px; }}
  .cyc-h2 {{ color: var(--amber); font-weight: 500; font-size: 0.85rem; letter-spacing: .14em; text-transform: uppercase; margin: 48px 0 16px; padding-bottom: 12px; border-bottom: 1px solid var(--rule); }}
  .cyc-table-wrap {{ border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; background: var(--surface); }}
  .cyc-table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  .cyc-table thead th {{ text-align: left; font-family: var(--mono); font-weight: 500; font-size: 10px; letter-spacing: .18em; text-transform: uppercase; color: var(--ink-3); padding: 14px 18px; border-bottom: 1px solid var(--rule); background: rgba(245,243,237,.02); }}
  .cyc-table tbody td {{ padding: 14px 18px; border-bottom: 1px solid var(--rule); font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); vertical-align: middle; }}
  .cyc-table tbody tr:last-child td {{ border-bottom: none; }}
  .cyc-table tbody tr:hover td {{ background: rgba(245,184,0,.025); }}
  .cyc-table th:nth-child(1), .cyc-table td:nth-child(1) {{ width: 38%; }}
  .cyc-table th:nth-child(2), .cyc-table td:nth-child(2) {{ width: 28%; }}
  .cyc-table th:nth-child(3), .cyc-table td:nth-child(3) {{ width: 12%; }}
  .cyc-table th:nth-child(4), .cyc-table td:nth-child(4) {{ width: 12%; }}
  .cyc-table th:nth-child(5), .cyc-table td:nth-child(5) {{ width: 10%; }}
  .cyc-link, .cyc-row-link {{ color: var(--amber); text-decoration: none; border-bottom: 1px dashed rgba(245,184,0,.3); transition: border-bottom-style .15s; }}
  .cyc-link:hover, .cyc-row-link:hover {{ color: var(--amber-2); border-bottom-style: solid; }}
  .cyc-row-link {{ color: var(--ink); font-weight: 500; }}
  .cyc-row-link code {{ font-size: .92em; opacity: .75; }}
  .cyc-muted {{ color: var(--ink-4); }}
  .cyc-pre {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 18px 22px; overflow-x: auto; font-family: var(--mono); font-size: 12.5px; line-height: 1.7; color: var(--ink-2); margin: 16px 0; }}
  .cyc-pre .cyc-cmt {{ color: var(--ink-4); }}
  .cyc-footer {{ color: var(--ink-4); font-family: var(--mono); font-size: 11px; letter-spacing: .06em; margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--rule); }}
  .cyc-footer a {{ color: var(--ink-3); border-bottom: 1px dashed var(--rule-2); }}
  .cyc-footer a:hover {{ color: var(--amber); border-bottom-color: var(--amber); }}
</style>

<div class="cyc-page">
  <h1 class="cyc-h1">Cycle <span class="cyc-accent">archive</span></h1>
  <p class="cyc-lede">
    Every publicly-verifiable signed cycle receipt the Jelleo continuous-audit loop has produced — sorted newest-first. Click any row to open the per-cycle signed report.
  </p>

  <div class="cyc-stats">
    <div class="cyc-stat"><div class="cyc-stat-num">{n}</div><div class="cyc-stat-lbl">Signed cycles</div></div>
    <div class="cyc-stat"><div class="cyc-stat-num">Ed25519</div><div class="cyc-stat-lbl">Attestation scheme</div></div>
    <div class="cyc-stat"><div class="cyc-stat-num">v0.1</div><div class="cyc-stat-lbl">Platform tier</div></div>
  </div>

  <h2 class="cyc-h2">Cycles · newest first</h2>
  <div class="cyc-table-wrap">
    <table class="cyc-table">
      <thead>
        <tr><th>Cycle</th><th>Target</th><th>Signed at</th><th>HTML</th><th>PDF</th><th>Sig</th><th>Pubkey</th></tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
  </div>

  <h2 class="cyc-h2">Independent verification</h2>
  <p class="cyc-lede" style="margin-bottom: 14px;">
    Pin the platform public key once, then verify any cycle artefact without trusting the operator:
  </p>
<pre class="cyc-pre">curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/hunt_report.html
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/hunt_report.html.sig

audit-pipeline sign verify --pubkey jelleo.ed25519.pub \\
  --artifact hunt_report.html --sig hunt_report.html.sig
<span class="cyc-cmt"># &rarr; "&check; signature valid"</span></pre>

  <p class="cyc-footer">
    Generated {now} · all artefacts Ed25519-signed · <a href="https://jelleo.com/methodology.html">methodology spec</a>
  </p>
</div>
</main>"""


def _render_index(rows: list[dict[str, str]], chrome_dir: Path = DEFAULT_CHROME_DIR) -> str:
    """Render the cycle archive page, wrapping the body in jelleo.com chrome
    when the chrome partials are present (gives full nav + bg + theme parity).
    Falls back to a self-contained minimal layout otherwise."""
    body = _render_cycles_body(rows)
    chrome = _read_chrome_partials(chrome_dir)
    if chrome:
        top, bot = chrome
        return top + body + bot
    # Fallback (no chrome partials available — shouldn't happen on the VPS)
    n = len(rows)
    rows_html = "\n          ".join(_row_html(r) for r in rows) if rows else (
        '<tr><td colspan="7" class="muted">no signed cycles yet</td></tr>'
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jelleo · cycle archive</title>
<meta name="description" content="Every publicly-verifiable signed cycle receipt produced by the Jelleo continuous-audit loop, sorted newest-first. Each cycle has signed HTML, PDF, and Ed25519 signatures.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:        #050504;
  --bg-2:      #0a0908;
  --surface:   rgba(245,243,237,0.025);
  --surface-2: rgba(245,243,237,0.045);
  --ink:       #f5f3ed;
  --ink-2:     rgba(245,243,237,0.72);
  --ink-3:     rgba(245,243,237,0.46);
  --ink-4:     rgba(245,243,237,0.28);
  --rule:      rgba(245,243,237,0.08);
  --rule-2:    rgba(245,243,237,0.16);
  --amber:     #f5b800;
  --amber-2:   #ffce4a;
  --font:      'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --mono:      'JetBrains Mono', 'SF Mono', Menlo, monospace;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }}
body {{
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font);
  font-size: 16px;
  line-height: 1.6;
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse 60% 40% at 15% 0%, rgba(245,184,0,0.06), transparent 60%),
    radial-gradient(ellipse 70% 40% at 85% 100%, rgba(245,184,0,0.04), transparent 60%);
}}
a {{ color: var(--amber); text-decoration: none; }}
a:hover {{ color: var(--amber-2); }}
::selection {{ background: var(--amber); color: var(--bg); }}

/* topnav (matches jelleo.com pattern) */
.topnav {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 32px;
  border-bottom: 1px solid var(--rule);
  background: rgba(5,5,4,0.7);
  backdrop-filter: blur(10px);
  position: sticky; top: 0; z-index: 10;
}}
.nav-logo {{
  font-family: var(--mono);
  font-weight: 600; font-size: 14px; letter-spacing: 0.04em;
  color: var(--ink); text-transform: lowercase;
}}
.nav-logo:hover {{ color: var(--amber); }}
.nav-links {{ display: flex; gap: 24px; font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }}
.nav-links a {{ color: var(--ink-3); }}
.nav-links a:hover {{ color: var(--ink); }}

/* main */
main {{ max-width: 1080px; margin: 0 auto; padding: 56px 32px 80px; }}
@media (max-width: 768px) {{ main {{ padding: 32px 20px 48px; }} }}

.hero {{ margin-bottom: 40px; }}
.hero h1 {{
  font-size: clamp(28px, 4vw, 40px);
  font-weight: 600;
  letter-spacing: -0.015em;
  color: var(--ink);
  margin-bottom: 12px;
}}
.hero h1 .accent {{ color: var(--amber); }}
.hero p {{ color: var(--ink-3); font-size: 15px; max-width: 720px; }}

/* stat grid (matches Operations panel) */
.stat-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
  margin: 32px 0 40px;
}}
.stat {{
  background: var(--surface);
  border: 1px solid var(--rule-2);
  border-radius: 6px;
  padding: 16px 20px;
  transition: border-color 0.2s ease, background 0.2s ease;
}}
.stat:hover {{ border-color: rgba(245,184,0,0.4); background: var(--surface-2); }}
.stat .num {{
  font-size: 1.7rem;
  font-weight: 600;
  color: var(--amber);
  letter-spacing: -0.015em;
  font-feature-settings: 'tnum';
}}
.stat .lbl {{
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin-top: 6px;
}}

/* section header */
h2 {{
  color: var(--amber);
  font-weight: 500;
  font-size: 1rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin: 40px 0 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--rule);
}}

/* table */
.table-wrap {{
  border: 1px solid var(--rule);
  border-radius: 6px;
  overflow: hidden;
  background: var(--surface);
}}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{
  text-align: left;
  font-family: var(--mono);
  font-weight: 500;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  padding: 14px 16px;
  border-bottom: 1px solid var(--rule);
  background: rgba(245,243,237,0.015);
}}
td {{
  padding: 13px 16px;
  border-bottom: 1px solid var(--rule);
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--ink-2);
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: rgba(245,184,0,0.025); }}
td a {{ border-bottom: 1px dashed rgba(245,184,0,0.3); }}
td a:hover {{ border-bottom-style: solid; }}
.muted {{ color: var(--ink-4); }}

/* code block */
pre {{
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 16px 20px;
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--ink-2);
  margin: 16px 0;
}}
pre .cmt {{ color: var(--ink-4); }}

/* footer */
footer {{
  border-top: 1px solid var(--rule);
  margin-top: 56px;
  padding: 24px 0;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--ink-4);
  display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: center;
}}
footer a {{ color: var(--ink-3); border-bottom: 1px dashed var(--rule-2); }}
footer a:hover {{ color: var(--amber); border-bottom-color: var(--amber); }}
</style>
</head>
<body>

<nav class="topnav">
  <a href="https://jelleo.com" class="nav-logo">jelleo</a>
  <div class="nav-links">
    <a href="https://jelleo.com/methodology.html">methodology</a>
    <a href="https://jelleo.com/protocols/">protocols</a>
    <a href="https://jelleo.com/status/">status</a>
    <a href="https://jelleo.com/security.html">security</a>
  </div>
</nav>

<main>
  <div class="hero">
    <h1>Cycle <span class="accent">archive</span></h1>
    <p>Every publicly-verifiable signed cycle receipt the Jelleo continuous-audit loop has produced — sorted newest-first. Click a row for the per-cycle landing page with HTML, PDF, and Ed25519 signature artefacts.</p>
  </div>

  <div class="stat-grid">
    <div class="stat"><div class="num">{n}</div><div class="lbl">Signed cycles</div></div>
    <div class="stat"><div class="num">Ed25519</div><div class="lbl">Attestation scheme</div></div>
    <div class="stat"><div class="num">v0.1</div><div class="lbl">Platform tier</div></div>
  </div>

  <h2>Cycles · newest first</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>Cycle</th><th>Target</th><th>Signed at</th><th>HTML</th><th>PDF</th><th>Sig</th><th>Pubkey</th></tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
  </div>

  <h2>Independent verification</h2>
  <p style="color: var(--ink-3); margin-bottom: 8px;">Pin the platform public key once, then verify any cycle artefact without trusting the operator:</p>
<pre>curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/hunt_report.html
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/hunt_report.html.sig

audit-pipeline sign verify --pubkey jelleo.ed25519.pub \\
  --artifact hunt_report.html --sig hunt_report.html.sig
<span class="cmt"># &rarr; "&check; signature valid"</span></pre>

  <footer>
    <span>Generated {now}</span>
    <span>·</span>
    <a href="https://jelleo.com/methodology.html">methodology spec</a>
    <a href="https://api.jelleo.com/keys/jelleo.ed25519.pub">platform public key</a>
    <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">audit-pipeline-cli</a>
  </footer>
</main>

</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--docroot", type=Path, default=DEFAULT_DOCROOT,
                   help=f"Web docroot (default: {DEFAULT_DOCROOT}).")
    args = p.parse_args()

    cycles_dir = args.docroot / "cycles"
    rows = _scan_cycles(cycles_dir)
    html_text = _render_index(rows)

    out = cycles_dir / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(html_text, encoding="utf-8")
    tmp.replace(out)
    print(f"regen_cycles_index: wrote {out} ({len(rows)} cycles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
