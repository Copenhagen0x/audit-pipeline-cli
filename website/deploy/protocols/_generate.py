#!/usr/bin/env python3
"""Generate per-protocol public pages under website/deploy/protocols/<slug>/.

Run:
    python3 website/deploy/protocols/_generate.py

Today the only protocol with a written hypothesis library is Percolator.
The /protocols/ index lists candidate protocols (no library written yet) —
those do NOT get individual pages until their library is scoped under
Tier 2 #7.
"""
from __future__ import annotations

import html
from pathlib import Path
from textwrap import dedent

HERE = Path(__file__).parent

PROTOCOLS = {
    "percolator": {
        "name": "Percolator",
        "status": "live",
        "status_label": "Active monitor",
        "status_blurb": "Running on the Jelleo loop today.",
        "pclass": "Perpetual DEX · F7 origin",
        "tagline": "Anatoly Yakovenko's perpetual-futures protocol. The first protocol on the Jelleo loop, where the F7 structural-residual drain was confirmed and disclosed.",
        "program_id": "6qWZvUtfyShbxTQkwjCayk3LuGqTGJwBo2QfkePK5jdJ",
        "github": "https://github.com/aeyakovenko/percolator-prog",
        "docs": "https://github.com/aeyakovenko/percolator-prog/blob/main/README.md",
        "hyp_count": 31,
        "bug_classes": 12,
        "cadence": "Live shadow + 24h cycle + weekly digest",
        "cluster_node": "Active · node 01",
        "first_cycle": "2026-04-22",
        "last_cycle": "Continuous (60s polling)",
        "history": [
            {"id": "F7", "title": "Insurance-residual drain via use_insurance_buffer", "severity": "Critical", "state": "Disclosed · PR #39", "date": "2026-04-30", "url": "https://github.com/aeyakovenko/percolator-prog/pull/39"},
        ],
        "scope_notes": "The library covers the matching-engine settlement path, the insurance-buffer / haircut-residual coupling (F7's root-cause class), the K/F PnL split arithmetic, and the maker/taker fee accounting. Scope conditions exclude the Anchor IDL surface and frontend-only code.",
        "files_in_scope": [
            "src/matching_engine/settle.rs",
            "src/insurance/buffer.rs",
            "src/risk/haircut.rs",
            "src/state/account.rs",
            "src/instructions/place_order.rs",
            "src/instructions/cancel_order.rs",
            "src/instructions/liquidate.rs",
        ],
        "bug_class_tags": [
            "insurance-residual coupling",
            "haircut arithmetic",
            "settlement asymmetry",
            "epoch-staleness",
            "vault/counter divergence",
            "K/F split error",
            "rounding-direction abuse",
            "unauthorized state mutation",
            "panic-on-overflow",
            "cross-margin contagion",
            "oracle-staleness",
            "signed-vs-unsigned conversion",
        ],
        "extra_links": [
            {"label": "F7 disclosure (PR #39)", "url": "https://github.com/aeyakovenko/percolator-prog/pull/39"},
            {"label": "Cycle reports archive", "url": "https://api.jelleo.com/cycles/"},
            {"label": "Methodology — F7 worked example", "url": "/methodology.html#f7"},
        ],
    },
}


PAGE_CSS = """
section.doc { padding: 100px 0; }
@media (max-width: 768px) { section.doc { padding: 64px 0; } }
section.doc h3 {
  font-size: clamp(20px, 2vw, 26px);
  line-height: 1.25; font-weight: 600;
  margin: 48px 0 18px; color: var(--ink);
  letter-spacing: -0.01em;
}
section.doc p { color: var(--ink-2); margin-bottom: 16px; max-width: 76ch; line-height: 1.65; }
section.doc p.lede { font-size: 18px; color: var(--ink); margin-bottom: 32px; max-width: 70ch; }
section.doc ul { color: var(--ink-2); margin: 16px 0 16px 24px; line-height: 1.7; }
section.doc li { margin-bottom: 6px; }

/* Identity card under hero */
.id-card {
  margin: 40px 0 32px;
  padding: 28px 32px;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--amber);
  border-radius: 0 8px 8px 0;
}
.id-card.live   { border-left-color: var(--ok);    }
.id-card.scoped { border-left-color: var(--amber); }
.id-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 24px 40px;
}
@media (max-width: 720px) { .id-grid { grid-template-columns: 1fr; } }
.id-cell .id-label {
  font-family: var(--mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.18em;
  color: var(--ink-3); margin-bottom: 6px;
}
.id-cell .id-value {
  font-size: 14px; color: var(--ink);
  font-family: var(--mono);
  word-break: break-all;
}
.id-cell .id-value.dim { color: var(--ink-2); font-family: var(--font); }
.id-cell .id-value a {
  color: var(--amber);
  border-bottom: 1px dashed rgba(245,184,0,0.3);
}
.id-cell .id-value a:hover { border-bottom-color: var(--amber); }

/* Severity table */
.history-table {
  width: 100%;
  border: 1px solid var(--rule);
  border-radius: 10px;
  overflow: hidden;
  background: var(--surface);
  margin: 24px 0;
  border-collapse: separate;
  border-spacing: 0;
}
.history-table th, .history-table td {
  padding: 14px 18px;
  text-align: left;
  font-size: 13px;
  border-bottom: 1px solid var(--rule);
}
.history-table th {
  font-family: var(--mono); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.18em;
  color: var(--ink-3); font-weight: 500;
  background: var(--bg-2);
}
.history-table tr:last-child td { border-bottom: none; }
.history-table .sev-pill {
  display: inline-block;
  padding: 2px 10px; border-radius: 4px;
  font-family: var(--mono); font-size: 10px;
  letter-spacing: 0.14em; text-transform: uppercase;
}
.sev-pill.critical { background: rgba(239,68,68,0.12);  color: var(--critical); border: 1px solid rgba(239,68,68,0.3); }
.sev-pill.high     { background: rgba(249,115,22,0.12); color: var(--high);     border: 1px solid rgba(249,115,22,0.3); }
.sev-pill.medium   { background: rgba(234,179,8,0.12);  color: var(--medium);   border: 1px solid rgba(234,179,8,0.3); }
.sev-pill.low      { background: rgba(96,165,250,0.12); color: var(--low);      border: 1px solid rgba(96,165,250,0.3); }
.history-empty {
  padding: 32px;
  text-align: center;
  color: var(--ink-3);
  font-family: var(--mono); font-size: 12px;
  letter-spacing: 0.06em;
  background: var(--surface);
  border: 1px dashed var(--rule);
  border-radius: 8px;
  margin: 24px 0;
}

/* Tag row (bug classes) */
.tag-row {
  display: flex; flex-wrap: wrap; gap: 8px;
  margin: 18px 0 32px;
}
.tag-row .tag {
  font-family: var(--mono); font-size: 12px;
  padding: 5px 12px;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 4px;
  color: var(--ink-2);
}

/* Files-in-scope code block */
.file-list {
  background: var(--bg-2);
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 18px 22px;
  margin: 22px 0;
  font-family: var(--mono); font-size: 13px;
  line-height: 1.7;
  color: var(--ink-2);
}
.file-list .file-arrow { color: var(--amber); margin-right: 10px; }

/* Status pill in hero */
.protocol-hero-status {
  display: inline-flex; align-items: center; gap: 8px;
  font-family: var(--mono); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.18em;
  padding: 6px 14px; border-radius: 999px;
  margin-bottom: 24px;
}
.protocol-hero-status.live    { color: var(--ok);    border: 1px solid rgba(74,222,128,0.3); background: rgba(74,222,128,0.06); }
.protocol-hero-status.scoped  { color: var(--amber); border: 1px solid rgba(245,184,0,0.3); background: rgba(245,184,0,0.06); }
.protocol-hero-status .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 8px currentColor;
}
""".strip()


def render_history(history):
    if not history:
        return '<div class="history-empty">No findings yet — protocol awaiting first hunt cycle.</div>'
    rows = []
    for h in history:
        sev = h["severity"].lower()
        title_link = f'<a href="{html.escape(h["url"], quote=True)}" target="_blank" rel="noopener">{html.escape(h["title"])}</a>' if h.get("url") else html.escape(h["title"])
        rows.append(
            f"""<tr>
  <td><strong>{html.escape(h['id'])}</strong></td>
  <td>{title_link}</td>
  <td><span class="sev-pill {sev}">{html.escape(h['severity'])}</span></td>
  <td>{html.escape(h['state'])}</td>
  <td>{html.escape(h['date'])}</td>
</tr>"""
        )
    return f"""<table class="history-table">
<thead><tr><th>ID</th><th>Title</th><th>Severity</th><th>State</th><th>Date</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>"""


def render_files(files):
    items = "\n".join(
        f'<div><span class="file-arrow">→</span>{html.escape(f)}</div>' for f in files
    )
    return f'<div class="file-list">\n{items}\n</div>'


def render_tags(tags):
    spans = "\n".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
    return f'<div class="tag-row">\n{spans}\n</div>'


def render_extra_links(links):
    items = []
    for l in links:
        external = l["url"].startswith("http")
        attr = ' target="_blank" rel="noopener"' if external else ""
        arrow = " ↗" if external else " →"
        items.append(
            f'<li><a href="{html.escape(l["url"], quote=True)}"{attr}>{html.escape(l["label"])}{arrow}</a></li>'
        )
    return "<ul>\n" + "\n".join(items) + "\n</ul>"


def render_page(slug: str, p: dict) -> str:
    name = p["name"]
    status = p["status"]
    status_label = p["status_label"]
    history_html = render_history(p["history"])
    files_html = render_files(p["files_in_scope"])
    tags_html = render_tags(p["bug_class_tags"])
    extra_html = render_extra_links(p["extra_links"])

    is_live = status == "live"
    page_title = f"Jelleo · {name}"
    page_desc = f"{name} — Solana protocol under continuous Jelleo audit. {p['tagline']}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(page_title)}</title>
<meta name="description" content="{html.escape(page_desc)}">
<meta name="theme-color" content="#050504">
<link rel="canonical" href="https://jelleo.com/protocols/{slug}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Jelleo">
<meta property="og:url" content="https://jelleo.com/protocols/{slug}/">
<meta property="og:title" content="{html.escape(page_title)}">
<meta property="og:description" content="{html.escape(page_desc)}">
<meta property="og:image" content="https://jelleo.com/og.png?v=2">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://jelleo.com/og.png?v=2">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300..900&family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' fill='%23050504'/%3E%3Cpath d='M58 28 H78 V48 M22 52 V72 H42' stroke='%23f5b800' stroke-width='5' fill='none' stroke-linecap='square'/%3E%3C/svg%3E">

<link rel="stylesheet" href="/shared.css">

<style>
{PAGE_CSS}
</style>
</head>
<body>

<div class="bg-aurora"></div>
<div class="bg-blobs">
  <div class="blob blob-1"></div>
  <div class="blob blob-2"></div>
  <div class="blob blob-3"></div>
</div>
<div class="bg-grid"></div>
<canvas id="particles"></canvas>
<div class="bg-scan"></div>
<div class="bg-noise"></div>

<div class="cursor-ring" id="ring"></div>
<div class="cursor-dot" id="dot"></div>
<div class="scroll-progress" id="progress"></div>

<nav class="nav" id="nav">
  <a href="/" class="nav-logo">jelleo</a>
  <div class="nav-center">
    <a href="/protocols/" class="active">Protocols</a>
    <a href="/#product">Product</a>
    <a href="/#live-ops">Live ops</a>
    <a href="/methodology.html">Methodology</a>
    <a href="/security.html">Security</a>
  </div>
  <div class="nav-right">
    <span class="nav-status">
      <span class="live-dot"></span>
      {html.escape(name)} · {html.escape(status_label)}
    </span>
    <a href="/customer/" class="nav-portal">Customer portal</a>
    <a href="/integrate/" class="nav-cta">Request integration</a>
    <button class="nav-toggle" id="nav-toggle" aria-label="Open menu" aria-expanded="false" aria-controls="mobile-menu">
      <span></span><span></span><span></span>
    </button>
  </div>
</nav>
<div class="mobile-menu" id="mobile-menu" role="menu" aria-labelledby="nav-toggle">
  <a href="/" role="menuitem">Home</a>
  <a href="/protocols/" role="menuitem">Protocols</a>
  <a href="/methodology.html" role="menuitem">Methodology</a>
  <a href="/security.html" role="menuitem">Security</a>
  <a href="/customer/" role="menuitem">Customer portal</a>
  <a href="https://github.com/Copenhagen0x/audit-pipeline-cli" target="_blank" rel="noopener" role="menuitem">Source</a>
  <a href="/integrate/" role="menuitem" style="color: var(--amber);">Request integration →</a>
</div>

<section class="hero">
  <span class="hero-bracket tl"></span>
  <span class="hero-bracket br"></span>
  <div class="container">
    <div data-reveal>
      <div class="protocol-hero-status {status}"><span class="dot"></span>{html.escape(status_label)}</div>
      <div class="hero-eyebrow">{html.escape(p['pclass'])}</div>
      <h1 class="hero-title">{html.escape(name)} <span class="accent">on Jelleo.</span></h1>
      <p class="hero-lede">{html.escape(p['tagline'])}</p>
      <div class="hero-meta">
        <span><strong>{p['hyp_count']}</strong> hypotheses</span>
        <span><strong>{p['bug_classes']}</strong> bug classes</span>
        <span><strong>{html.escape(p['cluster_node'])}</strong></span>
      </div>
    </div>

    <div class="id-card {status}" data-reveal data-delay="1">
      <div class="id-grid">
        <div class="id-cell">
          <div class="id-label">Program ID</div>
          <div class="id-value">{html.escape(p['program_id'])}</div>
        </div>
        <div class="id-cell">
          <div class="id-label">Source repository</div>
          <div class="id-value"><a href="{html.escape(p['github'], quote=True)}" target="_blank" rel="noopener">{html.escape(p['github'].replace('https://', ''))}</a></div>
        </div>
        <div class="id-cell">
          <div class="id-label">Cadence</div>
          <div class="id-value dim">{html.escape(p['cadence'])}</div>
        </div>
        <div class="id-cell">
          <div class="id-label">First cycle</div>
          <div class="id-value dim">{html.escape(p['first_cycle'])}</div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- ============== STATUS ============== -->
<section class="doc">
  <div class="container">
    <div data-reveal>
      <div class="section-label">01 · Status</div>
      <h2 class="section-title">{html.escape(p['status_blurb'])}</h2>
      <p>{html.escape(p['tagline'])}</p>
      {'<p>This protocol is currently the only one running on the Jelleo loop. Layer-1 (static analysis), Layer-2 (LLM hypothesis generation), Layer-3 (Anchor-aware analysis), Layer-4 (LiteSVM PoC), Layer-5 (commit watch), and Layer-6 (live mainnet shadow) all execute against this target on a continuous schedule.</p>' if is_live else '<p>The hypothesis library and scoping artifacts for this protocol are complete. Deployment runs on the cluster ramp — currently 12 active nodes across the funded plan, with 24 provisioned at Year-1 exit. This protocol flips to <em>active</em> as the assigned cluster node comes online.</p>'}
    </div>
  </div>
</section>

<!-- ============== HISTORY ============== -->
<section class="doc" style="padding-top: 32px;">
  <div class="container">
    <div data-reveal>
      <div class="section-label">02 · Severity history</div>
      <h2 class="section-title">Findings to date.</h2>
      <p class="lede">All findings published with full lifecycle state, severity rubric application, and a cryptographic receipt. Critical and High disclosures are linked to their public PR or GitHub issue.</p>
      {history_html}
    </div>
  </div>
</section>

<!-- ============== SCOPE ============== -->
<section class="doc" style="padding-top: 32px;">
  <div class="container">
    <div data-reveal>
      <div class="section-label">03 · Library scope</div>
      <h2 class="section-title">Hypothesis library.</h2>
      <p class="lede">The library lists the bug classes, file ranges, and scope conditions that the platform tests against this protocol. Each hypothesis maps to a target file, an applies-to range, a severity rubric, and a Layer-3/4 verification path.</p>
      <p>{html.escape(p['scope_notes'])}</p>

      <h3>Files in scope</h3>
      {files_html}

      <h3>Bug-class coverage ({p['bug_classes']} classes · {p['hyp_count']} hypotheses)</h3>
      {tags_html}
    </div>
  </div>
</section>

<!-- ============== METHODOLOGY ============== -->
<section class="doc" style="padding-top: 32px;">
  <div class="container">
    <div data-reveal>
      <div class="section-label">04 · Methodology applied</div>
      <h2 class="section-title">How {html.escape(name)} is audited.</h2>
      <p class="lede">The same four-pillar loop runs against every protocol. Detection feeds propagation; propagation feeds fix-bundle delivery; fix delivery feeds the on-chain attestation registry. See the public methodology for the full reference.</p>
      <ul>
        <li><strong>P1 — detection:</strong> static analysis (Layer-1) → LLM hypothesis generation (Layer-2) → Anchor-aware analysis (Layer-3) → LiteSVM PoC (Layer-4) → commit-triggered re-runs (Layer-5) → live mainnet shadow (Layer-6).</li>
        <li><strong>P2 — propagation:</strong> a confirmed finding's bug class fires sibling hypotheses across applicable protocols.</li>
        <li><strong>P3 — fix bundle:</strong> Critical/High findings ship with a candidate patch and a regression test.</li>
        <li><strong>P4 — attestation:</strong> every cycle receipt is signed Ed25519; the public key is published at <a href="/keys/jelleo.ed25519.pub">/keys/jelleo.ed25519.pub</a>.</li>
      </ul>

      <h3>Reference links</h3>
      {extra_html}
    </div>
  </div>
</section>

<!-- ============== CTA ============== -->
<section class="doc" style="padding-bottom: 120px;">
  <div class="container">
    <div data-reveal style="max-width: 720px; margin: 0 auto; text-align: center;">
      <div class="section-label" style="justify-content: center;">05 · Integration</div>
      <h2 class="section-title" style="margin-left: auto; margin-right: auto;">Maintain {html.escape(name)}? <span class="accent">Get the live dashboard.</span></h2>
      <p class="lede" style="margin-left: auto; margin-right: auto;">Protocol teams get an authenticated customer-portal view with full finding titles, hypothesis IDs, propagation hits, and signed cycle receipts. Scoping is free; deployment runs on the customer-portal track.</p>
      <p style="margin-left: auto; margin-right: auto; margin-top: 32px;">
        <a href="/integrate/" class="btn btn-primary">Request integration <span class="arrow">→</span></a>
      </p>
    </div>
  </div>
</section>

<footer class="footer">
  <div class="container">
    <div class="footer-grid">
      <div class="footer-brand">
        <a href="/" class="nav-logo">jelleo</a>
        <p>Autonomous immune system for Solana DeFi. Continuous, AI-driven, code-grounded. Built by Kirill Sakharuk.</p>
      </div>
      <div class="footer-col">
        <h5>Product</h5>
        <ul>
          <li><a href="/protocols/">Protocols</a></li>
          <li><a href="/methodology.html">Methodology</a></li>
          <li><a href="/security.html">Security &amp; disclosure</a></li>
          <li><a href="/customer/">Customer portal</a></li>
        </ul>
      </div>
      <div class="footer-col">
        <h5>Open source</h5>
        <ul>
          <li><a href="https://github.com/Copenhagen0x/audit-pipeline-cli" target="_blank" rel="noopener">Platform</a></li>
          <li><a href="https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/docs/HYPOTHESIS_SCHEMA.md" target="_blank" rel="noopener">Schema spec</a></li>
          <li><a href="https://github.com/aeyakovenko/percolator-prog/pull/39" target="_blank" rel="noopener">F7 disclosure</a></li>
        </ul>
      </div>
      <div class="footer-col">
        <h5>Contact</h5>
        <ul>
          <li><a href="mailto:security@jelleo.com">security@jelleo.com</a></li>
          <li><a href="mailto:kirill@jelleo.com">kirill@jelleo.com</a></li>
          <li><a href="mailto:info@jelleo.com">info@jelleo.com</a></li>
          <li><a href="https://jelleo.com">jelleo.com</a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span>© 2026 Jelleo · Apache-2.0</span>
      <span>v0.1 · {html.escape(name)} · 2026-05-07</span>
    </div>
  </div>
</footer>

<script src="/shared.js"></script>

</body>
</html>
"""


def main() -> None:
    written = []
    for slug, p in PROTOCOLS.items():
        out_dir = HERE / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "index.html"
        out_file.write_text(render_page(slug, p), encoding="utf-8")
        written.append(str(out_file.relative_to(HERE.parent)))
    print(f"Wrote {len(written)} pages:")
    for w in written:
        print(f"  {w}")


if __name__ == "__main__":
    main()
