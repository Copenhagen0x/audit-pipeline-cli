"""Jelleo design system — shared CSS and chrome for HTML reports + dashboard.

Single source of truth for the product's visual identity. Both
`commands/dashboard.py` and `commands/report.py` import from here.

Aesthetic: dark + amber, matches jelleo.com (#050504 background +
#f5b800 amber accent). Inter for body, JetBrains Mono for code.

The CSS is print-optimized: @media print rules ensure proper page
breaks (avoid breaking inside table rows, repeat table headers,
hide topbar/sticky elements, force backgrounds to render). Reports
render cleanly through Chrome-headless `--print-to-pdf` and through
weasyprint / wkhtmltopdf without further tweaking.

Helpers:
  topbar_html(status_label, status_class) — sticky chrome at top
  footer_html(extra)                       — bottom credit line
  cover_page_html(...)                     — branded cover page for PDF
                                             (logo + customer + cycle
                                             + signed-receipt fingerprint)
  read_pubkey_fingerprint(workspace)       — helper to load + truncate
                                             the platform public key
                                             for cover-page display
"""

from __future__ import annotations

from pathlib import Path

PRODUCT_NAME = "JELLEO"
TAGLINE = "Autonomous Solana audit"
PUBLIC_KEY_URL = "https://jelleo.com/keys/jelleo.ed25519.pub"


CSS = """
:root {
  --bg:           #050504;
  --bg-2:         #0a0908;
  --bg-3:         #100e0c;
  --surface:      rgba(245,243,237,0.025);
  --surface-2:    rgba(245,243,237,0.045);
  --surface-3:    rgba(245,243,237,0.08);

  --ink:          #f5f3ed;
  --ink-2:        rgba(245,243,237,0.72);
  --ink-3:        rgba(245,243,237,0.46);
  --ink-4:        rgba(245,243,237,0.28);
  --ink-5:        rgba(245,243,237,0.12);

  --rule:         rgba(245,243,237,0.08);
  --rule-2:       rgba(245,243,237,0.16);

  --amber:        #f5b800;
  --amber-2:      #ffce4a;
  --amber-glow:   rgba(245,184,0,0.4);

  --text:         #f5f3ed;       /* legacy alias */
  --text-2:       rgba(245,243,237,0.72);
  --text-3:       rgba(245,243,237,0.46);
  --border:       rgba(245,243,237,0.08);
  --border-2:     rgba(245,243,237,0.16);
  --accent:       #f5b800;

  --ok:           #4ade80;
  --warn:         #fbbf24;
  --critical:     #ef4444;
  --high:         #f97316;
  --medium:       #eab308;
  --low:          #60a5fa;
  --info:         #71717a;

  --font:         'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --mono:         'JetBrains Mono', 'SF Mono', 'Cascadia Code', Menlo, Monaco, Consolas, monospace;
}

* { box-sizing: border-box; }

html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-feature-settings: 'cv02','cv03','cv04','cv11','ss01';
}

a { color: var(--amber); text-decoration: none; border-bottom: 1px solid rgba(245,184,0,0.3); }
a:hover { border-bottom-color: var(--amber); }

code, pre, .mono {
  font-family: var(--mono);
  font-size: .92em;
}
code {
  background: var(--surface);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--rule);
  color: var(--ink);
}
pre {
  background: var(--bg-2); border: 1px solid var(--rule);
  border-radius: 6px; padding: 18px 20px; overflow-x: auto;
  line-height: 1.55; color: var(--ink-2); font-size: 12.5px;
  margin: 18px 0;
}
pre code { background: none; border: none; padding: 0; color: inherit; }

::selection { background: var(--amber); color: var(--bg); }

/* ============================== LAYOUT ============================== */

.shell {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px 40px 80px;
}

/* ============================== TOPBAR ============================== */

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 40px;
  border-bottom: 1px solid var(--rule);
  background: rgba(5,5,4,.92);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 10;
}

.brand {
  display: flex; align-items: center; gap: 14px;
}
.brand .mark {
  position: relative; width: 28px; height: 28px;
  display: inline-block;
}
.brand .mark::before, .brand .mark::after {
  content: ''; position: absolute;
  width: 12px; height: 12px;
  border: 2px solid var(--amber);
  filter: drop-shadow(0 0 6px var(--amber-glow));
}
.brand .mark::before { top: 0; right: 0; border-left: none; border-bottom: none; }
.brand .mark::after  { bottom: 0; left: 0; border-right: none; border-top: none; }
.brand .wordmark { font-weight: 800; letter-spacing: .04em; font-size: 18px; color: var(--ink); }
.brand .tagline {
  color: var(--ink-3); font-size: 11px; letter-spacing: .14em; margin-left: 10px;
  border-left: 1px solid var(--rule-2); padding-left: 12px; text-transform: uppercase;
  font-family: var(--mono);
}

.status {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--ink-3);
  text-transform: uppercase; letter-spacing: .14em;
  font-family: var(--mono);
}
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.ok       { background: var(--ok);       box-shadow: 0 0 12px rgba(74,222,128,.6); }
.dot.warn     { background: var(--warn);     box-shadow: 0 0 12px rgba(251,191,36,.6); }
.dot.critical { background: var(--critical); box-shadow: 0 0 12px rgba(239,68,68,.6); }

/* ============================== TYPOGRAPHY ============================== */

h1 {
  font-size: 32px; font-weight: 700; letter-spacing: -0.02em;
  margin: 0 0 6px; color: var(--ink);
}
h2 {
  font-size: 12px; font-weight: 500; letter-spacing: .22em;
  text-transform: uppercase; color: var(--amber);
  margin: 44px 0 16px;
  padding-bottom: 12px; border-bottom: 1px solid var(--rule);
  display: flex; align-items: center; gap: 12px;
}
h2::before {
  content: ''; width: 24px; height: 1px;
  background: var(--amber); box-shadow: 0 0 6px var(--amber-glow);
}
h3 {
  font-size: 16px; font-weight: 600; margin: 0 0 6px; color: var(--ink);
}
.subhead {
  color: var(--ink-2);
  font-size: 14px;
  margin: 0 0 32px;
  font-family: var(--mono);
}
.subhead code { font-size: 12.5px; color: var(--amber); border-color: rgba(245,184,0,0.2); }

/* ============================== KPI GRID ============================== */

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin: 0 0 12px;
}
.kpi {
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 8px;
  padding: 18px 20px;
  display: flex; flex-direction: column; gap: 8px;
  transition: border-color .15s ease;
}
.kpi:hover { border-color: var(--rule-2); }
.kpi .label {
  font-size: 10px; color: var(--ink-3);
  text-transform: uppercase; letter-spacing: .18em; font-weight: 500;
  font-family: var(--mono);
}
.kpi .value {
  font-size: 32px; font-weight: 700; line-height: 1;
  letter-spacing: -0.02em; color: var(--ink);
  font-variant-numeric: tabular-nums;
}
.kpi.danger { border-color: rgba(239,68,68,.4); background: linear-gradient(180deg, rgba(239,68,68,.05), transparent); }
.kpi.danger .value { color: var(--critical); }
.kpi.warn   { border-color: rgba(249,115,22,.4); }
.kpi.warn   .value { color: var(--high); }
.kpi.ok     .value { color: var(--ok); }
.kpi .delta { font-size: 11px; color: var(--ink-3); font-family: var(--mono); }

/* ============================== TABLES ============================== */

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 16px;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 8px;
  overflow: hidden;
  font-size: 13px;
}
th, td {
  padding: 12px 16px;
  text-align: left;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}
tbody tr:last-child td { border-bottom: none; }
th {
  background: var(--surface-2);
  font-weight: 500;
  color: var(--ink-3);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: .14em;
  font-family: var(--mono);
}
tr:hover td { background: rgba(245,243,237,.012); }
td.num, th.num { font-variant-numeric: tabular-nums; text-align: right; font-family: var(--mono); }
td strong { color: var(--ink); }

/* ============================== SEVERITY ============================== */

.sev {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px;
  border-radius: 3px;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
  border: 1px solid transparent;
}
.sev.critical { background: rgba(239,68,68,.14); color: var(--critical); border-color: rgba(239,68,68,.3); }
.sev.high     { background: rgba(249,115,22,.14); color: var(--high);    border-color: rgba(249,115,22,.3); }
.sev.medium   { background: rgba(234,179,8,.14); color: var(--medium);   border-color: rgba(234,179,8,.3); }
.sev.low      { background: rgba(96,165,250,.12); color: var(--low);     border-color: rgba(96,165,250,.25); }
.sev.info     { background: rgba(113,113,122,.14); color: var(--info);   border-color: rgba(113,113,122,.3); }

.status-pill {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 3px;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: .08em;
  text-transform: uppercase;
  background: var(--surface-2); color: var(--ink-2);
  border: 1px solid var(--rule);
}
.status-pill.confirmed { background: rgba(239,68,68,.1);  color: var(--critical); border-color: rgba(239,68,68,.3); }
.status-pill.disclosed { background: rgba(96,165,250,.1); color: var(--low);      border-color: rgba(96,165,250,.25); }
.status-pill.fixed     { background: rgba(74,222,128,.1); color: var(--ok);       border-color: rgba(74,222,128,.3); }
.status-pill.verified  { background: rgba(74,222,128,.18); color: var(--ok);      border-color: rgba(74,222,128,.4); }
.status-pill.rejected  { background: var(--surface-2); color: var(--ink-3); }

/* Severity bar */
.sev-bar {
  display: flex; height: 8px; border-radius: 4px; overflow: hidden;
  background: var(--surface-2); border: 1px solid var(--rule);
  margin: 14px 0 8px;
}
.sev-bar > span { display: block; height: 100%; }
.sev-bar .b-critical { background: var(--critical); }
.sev-bar .b-high     { background: var(--high); }
.sev-bar .b-medium   { background: var(--medium); }
.sev-bar .b-low      { background: var(--low); }
.sev-bar .b-info     { background: var(--info); }
.sev-bar-legend {
  display: flex; gap: 18px; font-size: 11px; color: var(--ink-3);
  flex-wrap: wrap; font-family: var(--mono); letter-spacing: .04em;
}
.sev-bar-legend span { display: inline-flex; align-items: center; gap: 6px; }
.sev-bar-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; }

/* Empty state */
.empty {
  padding: 40px 24px;
  text-align: center;
  color: var(--ink-3);
  background: var(--surface);
  border: 1px dashed var(--rule-2);
  border-radius: 8px;
  font-size: 13px;
}

/* ============================== FOOTER ============================== */

.footer {
  margin-top: 60px;
  padding-top: 20px;
  border-top: 1px solid var(--rule);
  display: flex; justify-content: space-between; align-items: center;
  color: var(--ink-3); font-size: 11px;
  letter-spacing: .04em;
  font-family: var(--mono);
  flex-wrap: wrap; gap: 12px;
}
.footer .muted { color: var(--ink-4); }
.footer a { color: var(--ink-3); }
.footer a:hover { color: var(--amber); }

/* ============================== COVER PAGE ============================== */
/* 3-row grid: logo / hero (centered) / footer. No page-level decorative
   brackets — the logo carries the bracket motif. Tight, structured spacing. */

.cover {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 56px;
  padding: 72px 64px 56px;
  position: relative;
  page-break-after: always;
  break-after: page;
}

/* Logo: brackets wrap AROUND the JELLEO wordmark (matches nav-logo pattern
   from index.html). Wordmark + tagline are inline; tagline is divided by
   a thin rule. Width is just-enough for the word + tagline. */
.cover-logo {
  display: inline-flex; align-items: center;
  position: relative;
  height: 64px;
  padding: 0 32px;
  align-self: start;
  justify-self: start;
}
.cover-logo::before, .cover-logo::after {
  content: ''; position: absolute;
  width: 22px; height: 22px;
  border: 2.5px solid var(--amber);
  filter: drop-shadow(0 0 8px var(--amber-glow));
  pointer-events: none;
}
.cover-logo::before { top: 0; right: 0; border-left: none; border-bottom: none; }
.cover-logo::after  { bottom: 0; left: 0; border-right: none; border-top: none; }

.cover-wordmark {
  font-size: 38px; font-weight: 800; letter-spacing: .03em; color: var(--ink);
  line-height: 1;
}
.cover-tagline {
  margin-left: 18px; padding-left: 18px;
  border-left: 1px solid var(--rule-2);
  font-family: var(--mono); font-size: 12px;
  letter-spacing: .22em; text-transform: uppercase;
  color: var(--ink-3); line-height: 1.4;
}

/* Hero: vertically-centered block holding eyebrow / title / meta / severity */
.cover-hero {
  display: flex; flex-direction: column;
  justify-content: center;
  align-self: center;
  width: 100%;
  max-width: 920px;
}

.cover-eyebrow {
  font-family: var(--mono); font-size: 14px;
  letter-spacing: .28em; text-transform: uppercase; color: var(--amber);
  margin-bottom: 28px; display: flex; align-items: center; gap: 16px;
  font-weight: 500;
}
.cover-eyebrow::before {
  content: ''; width: 32px; height: 1.5px;
  background: var(--amber); box-shadow: 0 0 8px var(--amber-glow);
}

.cover-title {
  font-size: clamp(56px, 6.4vw, 88px); font-weight: 700; letter-spacing: -0.025em;
  line-height: 1.04; color: var(--ink); margin-bottom: 44px; max-width: 16ch;
}
.cover-title .accent {
  background: linear-gradient(135deg, var(--amber), var(--amber-2));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}

.cover-meta-grid {
  display: grid; grid-template-columns: 180px 1fr; gap: 14px 36px;
  font-size: 17px; line-height: 1.5;
  margin-bottom: 44px;
}
.cover-meta-grid .label {
  font-family: var(--mono); font-size: 13px; letter-spacing: .18em;
  text-transform: uppercase; color: var(--ink-3); padding-top: 4px;
  font-weight: 500;
}
.cover-meta-grid .value { color: var(--ink); font-weight: 500; font-size: 17px; }
.cover-meta-grid .value code {
  font-size: 15px; padding: 3px 10px; color: var(--amber);
  border-color: rgba(245,184,0,0.2);
}

.cover-summary {
  display: grid; grid-template-columns: repeat(5, 1fr);
  border: 1px solid var(--rule); border-radius: 8px;
  background: var(--surface); overflow: hidden;
}
.cover-summary-cell {
  padding: 24px 18px; text-align: center;
  border-right: 1px solid var(--rule);
}
.cover-summary-cell:last-child { border-right: none; }
.cover-summary-cell .num {
  font-size: 40px; font-weight: 700; line-height: 1;
  font-variant-numeric: tabular-nums; color: var(--ink);
  letter-spacing: -0.02em;
}
.cover-summary-cell .label {
  display: block; margin-top: 12px;
  font-size: 12px; letter-spacing: .2em; text-transform: uppercase;
  color: var(--ink-3); font-family: var(--mono); font-weight: 500;
}
.cover-summary-cell.crit .num { color: var(--critical); }
.cover-summary-cell.high .num { color: var(--high); }
.cover-summary-cell.med .num  { color: var(--medium); }
.cover-summary-cell.low .num  { color: var(--low); }

/* Footer: receipt + meta side-by-side */
.cover-bottom {
  display: grid; grid-template-columns: 1fr 1fr; gap: 36px;
  border-top: 1px solid var(--rule); padding-top: 32px;
  align-self: end;
}
.cover-receipt {
  background: var(--surface); border: 1px solid var(--rule);
  border-left: 3px solid var(--amber); border-radius: 0 6px 6px 0;
  padding: 20px 24px;
}
.cover-receipt .header {
  font-size: 12px; letter-spacing: .24em; text-transform: uppercase;
  color: var(--amber); margin-bottom: 14px; font-family: var(--mono);
  font-weight: 500;
}
.cover-receipt .pubkey {
  font-size: 13px; color: var(--ink-2); font-family: var(--mono);
  word-break: break-all; line-height: 1.55;
}
.cover-receipt .verify {
  margin-top: 16px; padding-top: 14px;
  border-top: 1px dashed var(--rule);
  font-size: 12px; color: var(--ink-3); font-family: var(--mono);
  line-height: 1.7;
}
.cover-receipt .verify code {
  font-size: 11.5px; padding: 2px 6px; color: var(--ink-2);
  border-color: var(--rule);
}

.cover-meta-block {
  font-size: 14px; line-height: 1.9; color: var(--ink-3); font-family: var(--mono);
}
.cover-meta-block strong { color: var(--ink); font-weight: 700; font-size: 15px; }
.cover-meta-block a { color: var(--amber); }

/* ============================== PRINT RULES ============================== */
/* Rendered by Chrome --print-to-pdf, weasyprint, wkhtmltopdf, etc.
   Hides sticky topbar, forces dark background to render, controls
   page breaks for tables and sections, repeats table headers per page. */

@page {
  size: letter;
  margin: 0.75in;
  background: #050504;
}

@media print {
  html, body {
    background: #050504 !important;
    color: var(--ink) !important;
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }
  body * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }

  .topbar { display: none !important; }    /* sticky chrome doesn't print */

  .shell { padding: 0 !important; max-width: 100% !important; }

  .cover {
    height: 100vh; padding: 0.5in 0.5in 0.6in;
    page-break-after: always; break-after: page;
  }
  .cover-title { font-size: 72px !important; }

  /* Avoid breaking inside cards, KPIs, and table rows */
  .kpi, .card, .empty, .cover-receipt, .cover-summary { page-break-inside: avoid; break-inside: avoid; }
  table { page-break-inside: auto; }
  tr    { page-break-inside: avoid; break-inside: avoid; }
  thead { display: table-header-group; }   /* repeat headers on each page */
  tfoot { display: table-footer-group; }

  /* Section headings travel with their first child */
  h1, h2, h3 { page-break-after: avoid; break-after: avoid; }

  /* Hyperlinks render as text + URL in print */
  a { color: var(--amber) !important; border-bottom: none !important; }

  /* Hide elements explicitly tagged no-print */
  .no-print { display: none !important; }
}
"""


def topbar_html(status_label: str = "Active", status_class: str = "ok") -> str:
    """Render the sticky top bar with brand + status pill (screen only)."""
    return f"""
    <div class="topbar no-print">
      <div class="brand">
        <span class="mark"></span>
        <span class="wordmark">{PRODUCT_NAME}</span>
        <span class="tagline">{TAGLINE}</span>
      </div>
      <div class="status">
        <span class="dot {status_class}"></span>
        <span>{status_label}</span>
      </div>
    </div>
    """


def footer_html(extra: str = "") -> str:
    return f"""
    <div class="footer">
      <span>{PRODUCT_NAME} · The underwriting layer for Solana DeFi · <a href="https://jelleo.com">jelleo.com</a></span>
      <span class="muted">{extra}</span>
    </div>
    """


def read_pubkey_fingerprint(workspace: Path | None = None) -> str:
    """Return the workspace's published Ed25519 public-key body, or a placeholder.

    The full key file is e.g. 113 bytes:
        -----BEGIN PUBLIC KEY-----
        MCowBQYDK2VwAyE...XsetXNMrCK=
        -----END PUBLIC KEY-----

    We return just the body (without BEGIN/END lines) so it can be embedded
    in a small fixed-size cover-page box. If the workspace's key isn't
    available, returns a non-fatal placeholder so the report still renders.
    """
    if workspace is None:
        return "(public key not available — see jelleo.com/keys/jelleo.ed25519.pub)"
    candidates = [
        workspace / "keys" / "jelleo.ed25519.pub",
        Path("/root/audit_runs/percolator-live/keys/jelleo.ed25519.pub"),
    ]
    for path in candidates:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                lines = [ln.strip() for ln in content.splitlines() if ln.strip() and "-----" not in ln]
                return "".join(lines) or "(empty key file)"
            except OSError:
                continue
    return "(public key not available — see jelleo.com/keys/jelleo.ed25519.pub)"


def cover_page_html(
    *,
    target_name: str,
    report_title: str,
    window_label: str,
    cycle_id: str = "",
    engine_sha: str = "",
    wrapper_sha: str = "",
    severity_counts: dict[str, int] | None = None,
    pubkey_fingerprint: str = "",
    generated_at: str = "",
) -> str:
    """Render a customer-facing PDF cover page.

    Layout:
      ┌───────────────────────────────────┐
      │ [logo] JELLEO · Autonomous audit  │
      │                                   │
      │ ─ <eyebrow>                       │
      │ <Big report title>                │
      │ Customer · Cycle · Window         │
      │ [5-cell severity strip]           │
      │                                   │
      │ ─────────────                     │
      │ Receipt fingerprint               │
      │ Verification command              │
      └───────────────────────────────────┘
    """
    sc = severity_counts or {}
    # Truncate the pubkey for display while keeping enough to be recognizable.
    pk = (pubkey_fingerprint or "").strip()
    if len(pk) > 96:
        pk_display = pk[:48] + "…" + pk[-24:]
    else:
        pk_display = pk or "(see jelleo.com/keys/)"

    cycle_row = (
        f'<div class="label">Cycle</div><div class="value"><code>{cycle_id}</code></div>'
        if cycle_id else ""
    )
    engine_row = (
        f'<div class="label">Engine SHA</div><div class="value"><code>{engine_sha}</code></div>'
        if engine_sha else ""
    )
    wrapper_row = (
        f'<div class="label">Wrapper SHA</div><div class="value"><code>{wrapper_sha}</code></div>'
        if wrapper_sha else ""
    )

    return f"""
    <section class="cover">
      <div class="cover-logo">
        <span class="cover-wordmark">{PRODUCT_NAME}</span>
        <span class="cover-tagline">{TAGLINE}</span>
      </div>

      <div class="cover-hero">
        <div class="cover-eyebrow">Audit report · {window_label}</div>
        <h1 class="cover-title">{report_title} <span class="accent">{target_name}.</span></h1>

        <div class="cover-meta-grid">
          <div class="label">Customer</div><div class="value">{target_name}</div>
          <div class="label">Window</div><div class="value">{window_label}</div>
          {cycle_row}
          {engine_row}
          {wrapper_row}
          <div class="label">Generated</div><div class="value"><code>{generated_at}</code></div>
        </div>

        <div class="cover-summary">
          <div class="cover-summary-cell crit"><div class="num">{sc.get('Critical', 0)}</div><span class="label">Critical</span></div>
          <div class="cover-summary-cell high"><div class="num">{sc.get('High', 0)}</div><span class="label">High</span></div>
          <div class="cover-summary-cell med"><div class="num">{sc.get('Medium', 0)}</div><span class="label">Medium</span></div>
          <div class="cover-summary-cell low"><div class="num">{sc.get('Low', 0)}</div><span class="label">Low</span></div>
          <div class="cover-summary-cell"><div class="num">{sc.get('Info', 0)}</div><span class="label">Info</span></div>
        </div>
      </div>

      <div class="cover-bottom">
        <div class="cover-receipt">
          <div class="header">Signed · Ed25519</div>
          <div class="pubkey">{pk_display}</div>
          <div class="verify">
            verify with <code>audit-pipeline sign verify &lt;file&gt; &lt;file&gt;.sig --pubkey jelleo.ed25519.pub</code><br>
            public key at <a href="{PUBLIC_KEY_URL}">{PUBLIC_KEY_URL}</a>
          </div>
        </div>
        <div class="cover-meta-block">
          <strong>{PRODUCT_NAME}</strong> · The underwriting layer for Solana DeFi.<br>
          Methodology: <a href="https://jelleo.com/methodology.html">jelleo.com/methodology.html</a><br>
          Disclosure: <a href="https://jelleo.com/security.html">jelleo.com/security.html</a><br>
          Apache-2.0 · v0.1 · <a href="mailto:security@jelleo.com">security@jelleo.com</a>
        </div>
      </div>
    </section>
    """
