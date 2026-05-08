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
    """Return a list of {cycle_id, signed_at, has_pdf} dicts, sorted newest-first."""
    out: list[dict[str, str]] = []
    if not cycles_dir.is_dir():
        return out
    for child in cycles_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        sig = child / "cycle.html.sig"
        if not sig.is_file():
            continue
        out.append({
            "cycle_id":  child.name,
            "signed_at": _read_signed_at(sig) or "",
            "has_pdf":   "y" if (child / "cycle.pdf").is_file() else "n",
        })
    out.sort(key=lambda r: r["cycle_id"], reverse=True)
    return out


def _row_html(row: dict[str, str]) -> str:
    cid = row["cycle_id"]
    label = _short_id(cid)
    pdf_cell = (
        f'<a href="{cid}/cycle.pdf">PDF</a>'
        if row["has_pdf"] == "y" else "<span class=\"muted\">—</span>"
    )
    signed_at = row["signed_at"] or "<span class=\"muted\">unknown</span>"
    return (
        f'<tr><td><a href="{cid}/">{label}</a></td>'
        f'<td class="muted">{signed_at}</td>'
        f'<td><a href="{cid}/cycle.html">HTML</a></td>'
        f'<td>{pdf_cell}</td>'
        f'<td><a href="{cid}/cycle.html.sig">sig</a></td></tr>'
    )


def _render_index(rows: list[dict[str, str]]) -> str:
    n = len(rows)
    body = "\n          ".join(_row_html(r) for r in rows) if rows else (
        '<tr><td colspan="5" class="muted">no signed cycles yet</td></tr>'
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jelleo · cycle archive</title>
<meta name="description" content="Every publicly-verifiable signed cycle receipt produced by the Jelleo continuous-audit loop, sorted newest-first. Each cycle has cycle.html, cycle.pdf, and Ed25519 signatures over both.">
<style>
  body{{font-family:Inter,system-ui,sans-serif;background:#050504;color:#e6e1d8;margin:0;padding:48px 32px;max-width:920px;margin-inline:auto;line-height:1.55}}
  h1{{color:#f5b800;font-weight:600;letter-spacing:-0.01em;margin:0 0 8px}}
  h2{{color:#f5b800;font-weight:500;font-size:1.05rem;margin:32px 0 12px;letter-spacing:-0.005em}}
  p{{color:#bdb5a8;margin:8px 0}}
  a{{color:#f5b800;text-decoration:none;border-bottom:1px dashed rgba(245,184,0,0.3)}}
  a:hover{{border-bottom-style:solid}}
  .muted{{color:#7a7163}}
  .meta{{color:#7a7163;font-size:0.85rem}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:20px 0 28px}}
  .stat{{background:rgba(245,184,0,0.04);border:1px solid rgba(245,184,0,0.18);border-radius:6px;padding:14px 18px}}
  .stat .num{{font-size:1.6rem;font-weight:600;color:#f5b800;letter-spacing:-0.01em}}
  .stat .lbl{{font-size:0.7rem;letter-spacing:0.18em;text-transform:uppercase;color:#7a7163;margin-top:4px;font-family:'JetBrains Mono',ui-monospace,monospace}}
  table{{width:100%;border-collapse:collapse;margin:8px 0 24px;font-size:0.9rem}}
  th{{text-align:left;color:#7a7163;font-weight:500;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.7rem;letter-spacing:0.18em;text-transform:uppercase;padding:10px 12px;border-bottom:1px solid rgba(245,184,0,0.18)}}
  td{{padding:10px 12px;border-bottom:1px solid rgba(245,184,0,0.06);font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.85rem}}
  tr:hover td{{background:rgba(245,184,0,0.03)}}
  pre{{background:rgba(245,184,0,0.04);border:1px solid rgba(245,184,0,0.18);border-radius:6px;padding:14px 18px;overflow-x:auto;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:0.8rem;color:#d4cdc0}}
  hr{{border:0;border-top:1px solid rgba(245,184,0,0.18);margin:32px 0}}
</style>
</head>
<body>
<h1>Jelleo · cycle archive</h1>
<p class="meta">Every publicly-verifiable signed cycle receipt the Jelleo continuous-audit loop has produced. Click a row to see the per-cycle landing page with HTML, PDF, and Ed25519 signature artefacts.</p>

<div class="stat-grid">
  <div class="stat"><div class="num">{n}</div><div class="lbl">signed cycles</div></div>
  <div class="stat"><div class="num">Ed25519</div><div class="lbl">attestation</div></div>
  <div class="stat"><div class="num">v0.1</div><div class="lbl">platform</div></div>
</div>

<h2>Cycles · newest first</h2>

<table>
  <thead>
    <tr><th>Cycle</th><th>Signed at</th><th>HTML</th><th>PDF</th><th>Sig</th></tr>
  </thead>
  <tbody>
          {body}
  </tbody>
</table>

<h2>Verify any of these independently</h2>
<p>Pin the platform public key once, then verify any cycle artefact without trusting the operator:</p>
<pre>curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/cycle.html
curl -O https://api.jelleo.com/cycles/&lt;cycle-id&gt;/cycle.html.sig

audit-pipeline sign verify --pubkey jelleo.ed25519.pub \\
  --artifact cycle.html --sig cycle.html.sig
# &rarr; "&check; signature valid"</pre>

<hr>
<p class="meta">Generated {now} &middot; <a href="https://jelleo.com/methodology.html">methodology spec</a> &middot; <a href="https://api.jelleo.com/keys/jelleo.ed25519.pub">platform public key</a> &middot; <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">audit-pipeline-cli</a></p>
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
