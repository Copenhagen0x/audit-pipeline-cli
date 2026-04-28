"""Sentinel design system — shared CSS and chrome for HTML reports + dashboard.

Single source of truth for the product's visual identity. Both
`commands/dashboard.py` and `commands/report.py` import from here.

Aesthetic: dark, minimal, high-contrast typography. Inter where
available, system fallback otherwise. Inspired by Linear/Vercel
dashboards.
"""

from __future__ import annotations

PRODUCT_NAME = "SENTINEL"
TAGLINE = "Autonomous Solana audit"


CSS = """
:root {
  --bg:           #0a0a0a;
  --surface:      #111111;
  --surface-2:   #161616;
  --border:       #1f1f1f;
  --border-2:     #2a2a2a;
  --text:         #fafafa;
  --text-2:       #a1a1aa;
  --text-3:       #71717a;
  --accent:       #3b82f6;
  --ok:           #10b981;
  --warn:         #f59e0b;
  --critical:     #ef4444;
  --high:         #f97316;
  --medium:       #eab308;
  --low:          #3b82f6;
  --info:         #6b7280;
}

* { box-sizing: border-box; }

html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-feature-settings: 'cv02','cv03','cv04','cv11';
}

a { color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border-2); }
a:hover { border-bottom-color: var(--text); }

code, pre, .mono {
  font-family: 'JetBrains Mono', 'SF Mono', 'Cascadia Code', Menlo, Monaco, Consolas, monospace;
  font-size: .88em;
}

code {
  background: var(--surface);
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  color: var(--text-2);
}

/* Layout */

.shell {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 32px 80px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 32px;
  border-bottom: 1px solid var(--border);
  background: rgba(10,10,10,.85);
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
  width: 28px; height: 28px;
  background: var(--text);
  -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 1.6 3 5v6.5c0 4.7 3.4 9.1 9 11 5.6-1.9 9-6.3 9-11V5l-9-3.4Z' fill='black'/></svg>") center/contain no-repeat;
          mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 1.6 3 5v6.5c0 4.7 3.4 9.1 9 11 5.6-1.9 9-6.3 9-11V5l-9-3.4Z' fill='black'/></svg>") center/contain no-repeat;
}
.brand .wordmark { font-weight: 700; letter-spacing: .18em; font-size: 14px; }
.brand .tagline { color: var(--text-3); font-size: 12px; letter-spacing: .04em; margin-left: 6px;
                  border-left: 1px solid var(--border-2); padding-left: 10px; }

.status {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: var(--text-2);
  text-transform: uppercase; letter-spacing: .08em;
}
.dot { width: 8px; height: 8px; border-radius: 50%; }
.dot.ok       { background: var(--ok);       box-shadow: 0 0 12px rgba(16,185,129,.6); }
.dot.warn     { background: var(--warn);     box-shadow: 0 0 12px rgba(245,158,11,.6); }
.dot.critical { background: var(--critical); box-shadow: 0 0 12px rgba(239,68,68,.6); }

h1 {
  font-size: 28px; font-weight: 700; letter-spacing: -0.02em;
  margin: 0 0 4px; color: var(--text);
}
h2 {
  font-size: 13px; font-weight: 600; letter-spacing: .12em;
  text-transform: uppercase; color: var(--text-3);
  margin: 36px 0 14px;
  padding-bottom: 10px; border-bottom: 1px solid var(--border);
}
h3 {
  font-size: 16px; font-weight: 600; margin: 0 0 6px; color: var(--text);
}

.subhead {
  color: var(--text-2);
  font-size: 14px;
  margin: 0 0 28px;
}

/* KPIs */

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin: 0 0 8px;
}

.kpi {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
  display: flex; flex-direction: column; gap: 8px;
  transition: border-color .15s ease;
}
.kpi:hover { border-color: var(--border-2); }
.kpi .label {
  font-size: 11px; color: var(--text-3);
  text-transform: uppercase; letter-spacing: .1em; font-weight: 600;
}
.kpi .value {
  font-size: 30px; font-weight: 700; line-height: 1;
  letter-spacing: -0.02em; color: var(--text);
  font-variant-numeric: tabular-nums;
}
.kpi.danger { border-color: rgba(239,68,68,.4); background: linear-gradient(180deg, rgba(239,68,68,.05), transparent); }
.kpi.danger .value { color: var(--critical); }
.kpi.warn   { border-color: rgba(249,115,22,.4); }
.kpi.warn   .value { color: var(--high); }
.kpi.ok     .value { color: var(--ok); }
.kpi .delta { font-size: 11px; color: var(--text-3); }

/* Cards / sections */

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 22px;
  margin: 0 0 12px;
}
.card h3 { margin-bottom: 10px; }
.card .meta { color: var(--text-3); font-size: 12px; }
.card .row { display: flex; justify-content: space-between; align-items: center; gap: 16px; }

/* Tables */

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  font-size: 13px;
}
th, td {
  padding: 12px 16px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
tbody tr:last-child td { border-bottom: none; }
th {
  background: var(--surface-2);
  font-weight: 600;
  color: var(--text-3);
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: .08em;
}
tr:hover td { background: var(--surface-2); }
td.num { font-variant-numeric: tabular-nums; text-align: right; }

/* Severity badges */

.sev {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  border: 1px solid transparent;
  font-variant-numeric: tabular-nums;
}
.sev.critical { background: rgba(239,68,68,.12); color: var(--critical); border-color: rgba(239,68,68,.3); }
.sev.high     { background: rgba(249,115,22,.12); color: var(--high);    border-color: rgba(249,115,22,.3); }
.sev.medium   { background: rgba(234,179,8,.12); color: var(--medium);   border-color: rgba(234,179,8,.3); }
.sev.low      { background: rgba(59,130,246,.1); color: var(--low);      border-color: rgba(59,130,246,.25); }
.sev.info     { background: rgba(107,114,128,.12); color: var(--info);   border-color: rgba(107,114,128,.3); }

.status-pill {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: .04em;
  background: var(--surface-2); color: var(--text-2);
  border: 1px solid var(--border);
}
.status-pill.confirmed { background: rgba(239,68,68,.1); color: var(--critical); border-color: rgba(239,68,68,.3); }
.status-pill.disclosed { background: rgba(59,130,246,.1); color: var(--low); border-color: rgba(59,130,246,.25); }
.status-pill.fixed     { background: rgba(16,185,129,.1); color: var(--ok); border-color: rgba(16,185,129,.3); }
.status-pill.verified  { background: rgba(16,185,129,.18); color: var(--ok); border-color: rgba(16,185,129,.4); }
.status-pill.rejected  { background: var(--surface-2); color: var(--text-3); }

/* Severity bar */

.sev-bar {
  display: flex; height: 8px; border-radius: 4px; overflow: hidden;
  background: var(--surface-2); border: 1px solid var(--border);
  margin: 12px 0 6px;
}
.sev-bar > span { display: block; height: 100%; }
.sev-bar .b-critical { background: var(--critical); }
.sev-bar .b-high     { background: var(--high); }
.sev-bar .b-medium   { background: var(--medium); }
.sev-bar .b-low      { background: var(--low); }
.sev-bar .b-info     { background: var(--info); }
.sev-bar-legend { display: flex; gap: 14px; font-size: 11px; color: var(--text-3); flex-wrap: wrap; }
.sev-bar-legend span { display: inline-flex; align-items: center; gap: 6px; }
.sev-bar-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; }

/* Empty state */
.empty {
  padding: 40px 24px;
  text-align: center;
  color: var(--text-3);
  background: var(--surface);
  border: 1px dashed var(--border-2);
  border-radius: 10px;
  font-size: 13px;
}

/* Footer */

.footer {
  margin-top: 60px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center;
  color: var(--text-3); font-size: 11px;
  letter-spacing: .04em;
}
.footer .muted { color: var(--text-3); }
"""


def topbar_html(status_label: str = "Active", status_class: str = "ok") -> str:
    """Render the sticky top bar with brand + status pill."""
    return f"""
    <div class="topbar">
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
      <span>SENTINEL &middot; Autonomous Solana audit platform</span>
      <span class="muted">{extra}</span>
    </div>
    """
