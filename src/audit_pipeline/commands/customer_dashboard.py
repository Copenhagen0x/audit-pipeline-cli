"""`audit-pipeline customer build-dashboard` — generate the customer's portal.

Every customer gets a dedicated portal at
``https://jelleo.com/customer/<id>/`` — token-gated, scoped to their
targets, identified by their logo + hero title in the chrome. The
visual identity (palette, typography, motion) is JELLEO'S, fixed across
every customer. The customer-specific surface is IDENTITY + COPY +
CONTENT SCOPE:

  * **Identity**: their logo on the nav, their name in the status badge
  * **Copy**: hero title (e.g. "OtterSec × Jelleo · Vendor Evaluation"),
    footer line, PDF watermark
  * **Content scope**: only their targets / findings / cycles appear

Each customer's portal is a **clone-and-patch** of the demo customer's
``index.html`` (the lobby) and ``full.html`` (the live Bridge view)
under ``website/deploy/customer/demo/``. The demo pages are the
canonical templates — every fix or polish we ship to the demo
propagates to every customer the next time the generator runs.

For multi-target customers (e.g. OtterSec with 12 evaluation repos),
the generator additionally:

  * Replaces the lobby's single-protocol findings table with a
    **12-repo grid** (one card per target, grouped by language)
  * Injects a **tab bar** at the top of the Bridge view so the
    operator can switch between targets without leaving the page;
    each tab caches its own state client-side so the switch is
    instant, not a reload
  * Routes SSE events through a ``repo_id`` filter on the client so
    one stream feeds all tabs

Standing behind every customer's portal is the **typed-key gate** at
``/customer/`` — operators type their token (e.g. ``ottersec``) to
land on their portal. The generator registers the customer's id in the
gate's known-tokens list as part of its output.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from audit_pipeline import customers as customers_mod
from audit_pipeline.db import open_findings_db

console = Console()


# ---------------------------------------------------------------------------
# Brand defaults — apply when the customer hasn't customized.
#
# Jelleo's visual identity (palette, typography, motion) is SHARED across
# every customer portal. The per-customer fields below are IDENTITY +
# COPY only. Each customer gets a nameplate on the door; nobody
# redecorates the building.
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "hero_title":    "audit dashboard",
    "footer_text":   "Powered by Jelleo · continuous Solana audit",
    "pdf_watermark": "",
}


# Finding statuses we display on a customer portal (matches CUSTOMER_STATUSES
# in dashboard.py). Pre-triage and rejected stay invisible.
_DISPLAY_STATUSES: frozenset[str] = frozenset({
    "triaged", "confirmed", "disclosed", "fixed", "verified",
    "closed_not_planned",
})


def _brand_for(entry: dict[str, Any]) -> dict[str, str]:
    """Pull branding from the customer entry, defaulting fields we don't have."""
    b = dict(_DEFAULTS)
    raw = entry.get("branding") or {}
    for k, v in raw.items():
        if v:
            b[k] = v
    if "hero_title" not in (entry.get("branding") or {}):
        b["hero_title"] = f"{entry.get('name', 'Customer')} · audit dashboard"
    return b


def _scoped_targets(db, customer: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter the DB's targets to those scoped to this customer."""
    raw = (customer.get("target_match") or "").strip().lower()
    targets = db.list_targets()
    if not raw:
        return []
    wanted = {tok.strip() for tok in re.split(r"[\s,;]+", raw) if tok.strip()}
    out: list[dict[str, Any]] = []
    for t in targets:
        name = (t.get("name") or "").lower()
        matched = False
        for tok in wanted:
            if tok.endswith("*"):
                if name.startswith(tok[:-1]):
                    matched = True
                    break
            elif name == tok:
                matched = True
                break
        if matched:
            out.append(t)
    # Sort by name so the grid is deterministic across re-renders.
    out.sort(key=lambda t: t.get("name") or "")
    return out


def _target_rollup(db, target: dict[str, Any]) -> dict[str, Any]:
    """Compute per-target stats for the lobby grid + the bridge tab badges."""
    target_id = int(target["id"])
    findings = [
        f for f in db.list_findings(target_id=target_id, limit=2000)
        if (f.get("status") or "").lower() in _DISPLAY_STATUSES
    ]
    sev = {
        "Critical": sum(1 for f in findings if f.get("severity") == "Critical"),
        "High":     sum(1 for f in findings if f.get("severity") == "High"),
        "Medium":   sum(1 for f in findings if f.get("severity") == "Medium"),
        "Low":      sum(1 for f in findings if f.get("severity") == "Low"),
        "Info":     sum(1 for f in findings if f.get("severity") == "Info"),
    }
    cycles = db.list_cycles(target_id=target_id, limit=5)
    last_cycle = cycles[0] if cycles else None
    status = "idle"
    if last_cycle:
        status = "scanned" if last_cycle.get("finished_at") else "scanning"
    return {
        "id":              target_id,
        "name":            target.get("name") or "",
        "status":          status,
        "n_findings":      len(findings),
        "severity_counts": sev,
        "last_cycle_id":   last_cycle["cycle_id"] if last_cycle else None,
        "last_cycle_at":   (
            last_cycle.get("finished_at") or last_cycle.get("started_at")
        ) if last_cycle else None,
        "engine_sha":      (last_cycle or {}).get("engine_sha", "")[:10],
    }


# ---------------------------------------------------------------------------
# Helpers to find the demo template + website root.
# ---------------------------------------------------------------------------

def _find_website_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a website/deploy/ directory."""
    cur = start.resolve()
    for _ in range(6):
        cand = cur / "website" / "deploy"
        if cand.is_dir():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# ---------------------------------------------------------------------------
# Identity substitutions — applied to BOTH lobby and bridge templates.
# ---------------------------------------------------------------------------


def _identity_substitutions(
    entry: dict[str, Any],
    brand: dict[str, str],
) -> list[tuple[str, str]]:
    """List of (find, replace) pairs that swap demo customer identity for ours.

    Each pair is run sequentially via str.replace. Order matters where
    one replacement subsumes another — keep more-specific strings first.
    """
    cid = entry["id"]
    name = entry.get("name", cid)
    hero = brand["hero_title"]
    return [
        # Customer status badge (top right of nav)
        ("Demo customer · token cus_demo", f"{name} · token {cid}"),
        ("Demo customer · cus_demo",       f"{name} · {cid}"),
        # Lobby header
        ("Continuous audit · Percolator", hero),
        ("Customer · <span class=\"name\">Percolator team</span>",
         f"Customer · <span class=\"name\">{name}</span>"),
        # Page <title>
        ("Customer dashboard · Jelleo", f"{name} · Jelleo"),
        ("Live audit · Jelleo",         f"{name} · Live · Jelleo"),
        # URL paths under /customer/demo/ → /customer/<id>/
        ("/customer/demo/", f"/customer/{cid}/"),
        ("api.jelleo.com/customer/demo/", f"api.jelleo.com/customer/{cid}/"),
        ("api.jelleo.com/events/demo",    f"api.jelleo.com/events/{cid}"),
    ]


def _apply_substitutions(
    html: str,
    subs: list[tuple[str, str]],
) -> str:
    out = html
    for find, replace in subs:
        out = out.replace(find, replace)
    return out


# ---------------------------------------------------------------------------
# Multi-target injection — only fires for customers with >1 target.
# ---------------------------------------------------------------------------


# Group prefixes (for OSec-style chain × size grids). The order here
# drives the tab/grid visual grouping. Unknown prefixes fall back to
# "Other".
_GROUP_ORDER = ["solana", "solidity", "c", "aptos", "evm", "move", "anchor"]


def _group_for(target_name: str) -> str:
    """Infer the group (language/chain) from the target name.

    Looks for known group tokens after the customer prefix. Falls back
    to 'other' so the grid still renders even on unanticipated names.
    """
    n = target_name.lower()
    for g in _GROUP_ORDER:
        # match "osec-solana-small", "ottersec-solana-small", "solana-small"
        if f"-{g}-" in n or n.startswith(f"{g}-") or f"-{g}" == n[-(len(g) + 1):]:
            return g
    return "other"


def _size_for(target_name: str) -> str:
    """Infer size token from name suffix (small/medium/large), fallback ''."""
    n = target_name.lower()
    for s in ("small", "medium", "large", "xl"):
        if n.endswith(f"-{s}"):
            return s
    return ""


# Sort key for targets within a group: small → medium → large → xl, then alpha.
# Alphabetic would put "large" before "medium" which reads backwards.
_SIZE_RANK = {"small": 0, "medium": 1, "large": 2, "xl": 3, "": 99}


def _target_sort_key(target_name: str) -> tuple[int, str]:
    return (_SIZE_RANK.get(_size_for(target_name), 99), target_name)


def _short_label(target_name: str) -> str:
    """Human-friendly short label for a tab chip.

    'osec-solana-small' → 'Solana · small'
    """
    group = _group_for(target_name).capitalize()
    size = _size_for(target_name)
    return f"{group} · {size}" if size else group


# Multi-target lobby grid — replaces the single-protocol findings table
# on the lobby. Rendered into HTML inline (no JS, server-side fill).
_LOBBY_TARGETS_TEMPLATE = """
<!-- ════════════════════════════════════════════════════════════════
     MULTI-TARGET GRID — injected for multi-target customers (vendor
     evals, multi-protocol engagements). Replaces the single-protocol
     findings table on the lobby. Each card links into the Bridge view
     with that target pre-selected via URL hash.
     ════════════════════════════════════════════════════════════════ -->
<style>
  .target-grid {{
    margin: 0 0 48px;
  }}
  .target-group {{
    margin-bottom: 32px;
  }}
  .target-group-label {{
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--ink-3);
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .target-group-label::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: var(--rule);
  }}
  .target-row {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
  }}
  .target-card-tile {{
    display: block;
    padding: 22px;
    border: 1px solid var(--rule);
    border-radius: 10px;
    background: var(--surface);
    color: inherit;
    text-decoration: none;
    transition: transform 0.2s ease, border-color 0.2s ease;
    position: relative;
  }}
  .target-card-tile:hover {{
    transform: translateY(-2px);
    border-color: var(--amber);
  }}
  .target-card-tile .name {{
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 8px;
  }}
  .target-card-tile .status-chip {{
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 2px;
    display: inline-block;
    margin-bottom: 14px;
  }}
  .target-card-tile .status-chip.idle     {{ background: rgba(245,243,237,0.06); color: var(--ink-3); }}
  .target-card-tile .status-chip.scanning {{ background: rgba(245,184,0,0.12); color: var(--amber); }}
  .target-card-tile .status-chip.scanned  {{ background: rgba(74,222,128,0.12); color: #4ade80; }}
  .target-card-tile .sev-mini {{
    display: flex;
    gap: 12px;
    font-family: var(--mono);
    font-size: 11px;
    align-items: baseline;
    margin-bottom: 10px;
  }}
  .target-card-tile .sev-mini .group {{
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
  }}
  .target-card-tile .sev-mini .n {{
    font-size: 15px;
    font-weight: 600;
    color: var(--ink);
  }}
  .target-card-tile .sev-mini .group.crit .n {{ color: #dc2626; }}
  .target-card-tile .sev-mini .group.high .n {{ color: #ea580c; }}
  .target-card-tile .sev-mini .group.med  .n {{ color: #ca8a04; }}
  .target-card-tile .sev-mini .group.low  .n {{ color: #2563eb; }}
  .target-card-tile .sev-mini .label {{
    font-size: 9px;
    color: var(--ink-3);
    letter-spacing: 0.10em;
    text-transform: uppercase;
  }}
  .target-card-tile .meta {{
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.06em;
  }}
  .target-card-tile .arrow {{
    position: absolute;
    top: 22px; right: 22px;
    font-family: var(--mono);
    color: var(--ink-3);
    transition: color 0.2s, transform 0.2s;
  }}
  .target-card-tile:hover .arrow {{
    color: var(--amber);
    transform: translateX(3px);
  }}
</style>
<div class="dash-section-label">01 · Targets</div>
<h2 class="dash-section-title">Engagement scope</h2>
<div class="dash-section-sub">{n_targets} target{plural} under continuous audit. Click any tile to open the Bridge view with that target pre-selected.</div>

<div class="target-grid">
{groups_html}
</div>
"""


_LOBBY_TARGET_CARD = """
<a class="target-card-tile" href="/customer/{cid}/full.html#{target_name}" aria-label="Open Bridge view for {target_name}">
  <span class="arrow">→</span>
  <div class="name">{short_label}</div>
  <span class="status-chip {status}">{status}</span>
  <div class="sev-mini">
    <span class="group crit"><span class="n">{n_crit}</span><span class="label">crit</span></span>
    <span class="group high"><span class="n">{n_high}</span><span class="label">high</span></span>
    <span class="group med"><span class="n">{n_med}</span><span class="label">med</span></span>
    <span class="group low"><span class="n">{n_low}</span><span class="label">low</span></span>
  </div>
  <div class="meta">{meta_line}</div>
</a>
""".strip()


def _render_lobby_target_grid(cid: str, rollups: list[dict[str, Any]]) -> str:
    """Render the multi-target lobby grid HTML."""
    # Group rollups by language/chain
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rollups:
        g = _group_for(r["name"])
        grouped.setdefault(g, []).append(r)

    # Order groups by canonical order, unknowns last
    ordered_groups = [g for g in _GROUP_ORDER if g in grouped] + [
        g for g in grouped if g not in _GROUP_ORDER
    ]

    groups_html_parts: list[str] = []
    for g in ordered_groups:
        cards: list[str] = []
        for r in sorted(grouped[g], key=lambda x: _target_sort_key(x["name"])):
            sev = r["severity_counts"]
            meta = (
                f'engine <span style="color:var(--ink-2); background:rgba(245,184,0,0.06); padding:1px 6px; border-radius:2px; border:1px solid rgba(245,184,0,0.18);">{r["engine_sha"]}</span>'
                if r["engine_sha"]
                else '<span style="color: var(--ink-4); font-style: italic;">no cycles yet</span>'
            )
            cards.append(_LOBBY_TARGET_CARD.format(
                cid=cid,
                target_name=r["name"],
                short_label=_short_label(r["name"]),
                status=r["status"],
                n_crit=sev["Critical"], n_high=sev["High"],
                n_med=sev["Medium"], n_low=sev["Low"],
                meta_line=meta,
            ))
        group_label = g.capitalize()
        groups_html_parts.append(f"""
<div class="target-group">
  <div class="target-group-label">{group_label}</div>
  <div class="target-row">
    {''.join(cards)}
  </div>
</div>
""".strip())

    return _LOBBY_TARGETS_TEMPLATE.format(
        n_targets=len(rollups),
        plural="s" if len(rollups) != 1 else "",
        groups_html="\n".join(groups_html_parts),
    )


# ---------------------------------------------------------------------------
# Bridge tab bar — injected for multi-target Bridge views.
# ---------------------------------------------------------------------------


_BRIDGE_TAB_BAR_STYLE = """
<style id="bridge-tab-bar-style">
  /* Multi-target tab bar — premium chip row that lives BETWEEN the health
     banner ("Cycle running — Jelleo is auditing right now") and the
     dashboard hero strip. Sticks to the top of the viewport once the
     operator scrolls past the health banner, so the active target chip
     is always visible while reviewing the Bridge content below. */
  .tab-bar {
    position: sticky;
    top: 86px;       /* below the fixed nav, immediately above bridge-header */
    z-index: 90;
    background: rgba(5,5,4,0.82);
    backdrop-filter: blur(20px) saturate(140%);
    -webkit-backdrop-filter: blur(20px) saturate(140%);
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    padding: 14px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: thin;
  }
  .tab-bar::-webkit-scrollbar { height: 6px; }
  .tab-bar::-webkit-scrollbar-track { background: transparent; }
  .tab-bar::-webkit-scrollbar-thumb { background: var(--rule); border-radius: 3px; }
  .tab-bar .label {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-right: 6px;
    flex-shrink: 0;
  }
  .tab-bar .tab-group {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-shrink: 0;
  }
  .tab-bar .tab-group + .tab-group::before {
    content: '';
    display: block;
    width: 1px; height: 22px;
    background: var(--rule);
    margin: 0 10px;
  }
  .tab-bar .tab {
    flex-shrink: 0;
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--ink-2);
    padding: 7px 14px;
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.06em;
    cursor: pointer;
    transition: background 0.16s ease, color 0.16s ease, border-color 0.16s ease;
    display: inline-flex;
    align-items: center;
    gap: 10px;
    white-space: nowrap;
  }
  .tab-bar .tab .group-name {
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    color: var(--ink);
  }
  .tab-bar .tab .group-sep {
    color: var(--ink-4);
    font-weight: 400;
  }
  .tab-bar .tab .size-name {
    color: var(--ink-2);
  }
  .tab-bar .tab:hover {
    border-color: var(--amber);
    color: var(--ink);
  }
  .tab-bar .tab:hover .size-name { color: var(--ink); }
  .tab-bar .tab.active {
    background: var(--amber);
    border-color: var(--amber);
    font-weight: 600;
  }
  .tab-bar .tab.active .group-name,
  .tab-bar .tab.active .group-sep,
  .tab-bar .tab.active .size-name {
    color: var(--bg);
  }
  .tab-bar .tab .badge {
    display: inline-block;
    min-width: 18px;
    text-align: center;
    padding: 1px 5px;
    border-radius: 8px;
    font-size: 9px;
    background: rgba(245,243,237,0.08);
    color: var(--ink-2);
    font-weight: 600;
  }
  .tab-bar .tab.active .badge {
    background: rgba(5,5,4,0.22);
    color: var(--bg);
  }
  .tab-bar .tab .badge.zero { opacity: 0.4; }

  /* The tab bar lives INSIDE main.bridge (after the health banner), so
     main.bridge keeps its standard 96px top padding (just clears the
     fixed nav). No extra space needed because the sticky tab bar takes
     vertical room only when scrolled past. */
</style>
"""


def _render_bridge_tab_bar(rollups: list[dict[str, Any]]) -> str:
    """Render the sticky multi-target tab bar that lives ABOVE the Bridge
    content but BELOW the health banner.

    Each tab is fully self-describing — "Solana · small", "Aptos · large",
    etc — so the operator can identify the active target without counting
    separators. Within each language group, sizes go small → medium → large.
    Subtle vertical dividers between language groups for visual rhythm.
    """
    # Group rollups
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rollups:
        grouped.setdefault(_group_for(r["name"]), []).append(r)
    ordered_groups = [g for g in _GROUP_ORDER if g in grouped] + [
        g for g in grouped if g not in _GROUP_ORDER
    ]

    parts: list[str] = ['<div class="tab-bar" id="tab-bar" role="tablist" aria-label="Audit targets">']
    parts.append('<span class="label">Targets</span>')
    for g in ordered_groups:
        parts.append('<div class="tab-group">')
        group_display = g.capitalize()
        for r in sorted(grouped[g], key=lambda x: _target_sort_key(x["name"])):
            n = r["n_findings"]
            badge_class = "badge" if n > 0 else "badge zero"
            size = _size_for(r["name"]) or g
            parts.append(
                f'<button class="tab" role="tab" data-target="{r["name"]}" '
                f'aria-controls="tab-panel-{r["name"]}" data-group="{g}" '
                f'title="{group_display} · {size}">'
                f'<span class="group-name">{group_display}</span>'
                f'<span class="group-sep">·</span>'
                f'<span class="size-name">{size}</span>'
                f'<span class="{badge_class}">{n}</span>'
                f'</button>'
            )
        parts.append('</div>')
    parts.append('</div>')
    return _BRIDGE_TAB_BAR_STYLE + "\n" + "\n".join(parts)


_BRIDGE_TAB_JS = """
<script>
/* Multi-target tab bar logic. One SSE stream feeds all tabs; each tab
   maintains its own state slice so switching is instant (no reload).
   Active tab persists in localStorage so refresh keeps your spot.
   Tab can also be selected via URL hash (#osec-solana-small). */
(function () {
  const TAB_BAR = document.getElementById('tab-bar');
  if (!TAB_BAR) return;

  const TARGETS = Array.from(TAB_BAR.querySelectorAll('.tab')).map(b => b.dataset.target);
  const STORAGE_KEY = 'jelleo:active-target:' + (window.location.pathname.split('/')[2] || 'unknown');

  function getInitialTarget() {
    const hash = window.location.hash.replace('#', '').trim();
    if (hash && TARGETS.indexOf(hash) >= 0) return hash;
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && TARGETS.indexOf(stored) >= 0) return stored;
    return TARGETS[0] || null;
  }

  function setActive(target) {
    if (!target) return;
    TAB_BAR.querySelectorAll('.tab').forEach(b => {
      b.classList.toggle('active', b.dataset.target === target);
      b.setAttribute('aria-selected', b.dataset.target === target ? 'true' : 'false');
    });
    // Expose globally so the Bridge state code can filter SSE events.
    window.JELLEO_ACTIVE_TARGET = target;
    // Dispatch a synthetic event so the Bridge JS can repaint.
    window.dispatchEvent(new CustomEvent('jelleo:target-switched', {detail: {target}}));
    try { localStorage.setItem(STORAGE_KEY, target); } catch (e) {}
    // Update URL hash without scrolling
    history.replaceState(null, '', '#' + target);
  }

  TAB_BAR.addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    setActive(tab.dataset.target);
  });

  // Initial selection
  setActive(getInitialTarget());

  // Keyboard navigation (← / →)
  window.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const active = window.JELLEO_ACTIVE_TARGET;
    const i = TARGETS.indexOf(active);
    if (e.key === 'ArrowRight' && i >= 0 && i < TARGETS.length - 1) setActive(TARGETS[i + 1]);
    if (e.key === 'ArrowLeft'  && i > 0) setActive(TARGETS[i - 1]);
  });
})();
</script>
"""


# ---------------------------------------------------------------------------
# Identity (logo + name) injection into the customer's nav.
# ---------------------------------------------------------------------------


def _inject_customer_logo(html: str, name: str, logo_src: str | None) -> str:
    """Replace the bare 'jelleo' wordmark with the customer's logo + name.

    On the customer portal, the top-left logo should show the CUSTOMER'S
    identity (their logo + name), with Jelleo as a small "powered by"
    attribution. The customer is who the portal is FOR; Jelleo is who
    BUILT it.

    Falls back to a text monogram when no logo is provided.
    """
    if logo_src:
        logo_html = f'<img src="{logo_src}" alt="{name} logo" style="max-height: 28px; width: auto;">'
    else:
        initials = "".join(w[0] for w in name.split()[:2]).upper()[:2] or "??"
        logo_html = (
            f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'width:32px;height:32px;background:var(--amber);color:var(--bg);'
            f'font-family:var(--mono);font-size:13px;font-weight:700;border-radius:6px;">'
            f'{initials}</span>'
        )

    # Find the demo's "jelleo" wordmark <a> and replace with customer mark
    # Pattern: <a href="/" class="nav-logo">jelleo</a>
    new_anchor = (
        f'<a href="/" class="nav-logo" aria-label="{name} portal" '
        f'style="display:inline-flex;align-items:center;gap:12px;">'
        f'{logo_html}'
        f'<span style="font-family:var(--font);font-weight:700;font-size:18px;'
        f'letter-spacing:-0.01em;color:var(--ink);">{name}</span>'
        f'<span style="font-family:var(--mono);font-size:10px;color:var(--ink-3);'
        f'letter-spacing:0.12em;text-transform:uppercase;border-left:1px solid var(--rule);'
        f'padding-left:12px;margin-left:4px;">Jelleo</span>'
        f'</a>'
    )
    return html.replace(
        '<a href="/" class="nav-logo">jelleo</a>',
        new_anchor,
    )


# ---------------------------------------------------------------------------
# Lobby patcher — clone demo's index.html + customize for this customer.
# ---------------------------------------------------------------------------


def _patch_lobby(
    template: str,
    entry: dict[str, Any],
    brand: dict[str, str],
    rollups: list[dict[str, Any]],
    logo_src: str | None,
) -> str:
    cid = entry["id"]
    name = entry.get("name", cid)

    html = _apply_substitutions(template, _identity_substitutions(entry, brand))
    html = _inject_customer_logo(html, name, logo_src)

    # For multi-target customers: REPLACE the findings section with the
    # multi-target grid. Specifically, swap out the "01 · Findings" block
    # and its table. The trick: find the section label start, find the
    # next dash-section-label, replace everything between with our grid.
    if len(rollups) > 1:
        grid_html = _render_lobby_target_grid(cid, rollups)
        # Find the start of the Findings section
        m = re.search(
            r'<!--\s*Findings\s*-->.*?(?=<!--\s*Cycle receipts)',
            html, re.DOTALL,
        )
        if m:
            html = html[:m.start()] + grid_html + "\n\n    " + html[m.end():]

    # Update footer text if customer customized it
    if brand.get("footer_text") and brand["footer_text"] != _DEFAULTS["footer_text"]:
        # The demo's footer has a paragraph in the brand block. Append
        # a customer footer line near the end of the footer if present.
        # (We don't replace the structural footer — we add a line.)
        html = html.replace(
            '<a href="/" class="nav-logo">jelleo</a>',
            '<a href="/" class="nav-logo">jelleo</a>',
            1,  # only the FIRST occurrence (in nav, not footer)
        )

    return html


# ---------------------------------------------------------------------------
# Bridge patcher — clone demo's full.html + customize.
# ---------------------------------------------------------------------------


def _patch_bridge(
    template: str,
    entry: dict[str, Any],
    brand: dict[str, str],
    rollups: list[dict[str, Any]],
    logo_src: str | None,
) -> str:
    name = entry.get("name", entry["id"])

    html = _apply_substitutions(template, _identity_substitutions(entry, brand))
    html = _inject_customer_logo(html, name, logo_src)

    # For multi-target: inject the tab bar INSIDE <main class="bridge">,
    # immediately AFTER the health banner and BEFORE the bridge-header
    # strip. This keeps the visual hierarchy:
    #   1. Fixed nav (logo + nav-center + sign-out)
    #   2. Health banner ("Cycle running — Jelleo is auditing right now")
    #   3. Tab bar (active target indicator + switcher)
    #   4. Bridge dashboard content (hero strip, hyp grid, finding cards, etc.)
    # Landmark: `<div class="bridge-header">` is the FIRST element after
    # the health banner. Inject the tab bar IMMEDIATELY BEFORE it.
    if len(rollups) > 1:
        tab_bar = _render_bridge_tab_bar(rollups) + "\n" + _BRIDGE_TAB_JS
        m = re.search(r'<div\s+class="bridge-header"', html)
        if m:
            # Walk backwards from `<div class="bridge-header">` to find
            # the start of its preceding comment or the previous tag's
            # close, so the injection lands on a clean line boundary.
            insert_at = m.start()
            # Trim back any leading whitespace on the bridge-header line
            # so our injection inherits the same indentation context.
            while insert_at > 0 and html[insert_at - 1] in (" ", "\t"):
                insert_at -= 1
            html = html[:insert_at] + tab_bar + "\n\n  " + html[insert_at:]

    return html


# ---------------------------------------------------------------------------
# Gate update — add this customer's id to the typed-key login form.
# ---------------------------------------------------------------------------


def _add_customer_to_gate(gate_html: str, customer_id: str) -> str:
    """Inject `customer_id` into the KNOWN Set on the typed-key gate.

    The gate at /customer/index.html has a JS line:
        const KNOWN = new Set(['demo']);
    We rewrite it to include the new id. Idempotent — re-adding the same
    id is a no-op.
    """
    pattern = re.compile(r"const KNOWN = new Set\(\[([^\]]*)\]\);")
    m = pattern.search(gate_html)
    if not m:
        return gate_html  # gate format changed; bail rather than break
    existing_raw = m.group(1)
    # Parse the existing entries (simple split, strip quotes)
    entries: list[str] = []
    for tok in existing_raw.split(","):
        tok = tok.strip().strip("'").strip('"')
        if tok:
            entries.append(tok)
    if customer_id not in entries:
        entries.append(customer_id)
    new_set = "const KNOWN = new Set([" + ", ".join(f"'{e}'" for e in entries) + "]);"
    return pattern.sub(new_set, gate_html, count=1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@click.command(name="build-dashboard")
@click.argument("customer_id")
@click.option("--output-root", type=click.Path(path_type=Path), default=None,
              help="Root dir where website/deploy/customer/<id>/ lives. "
                   "Default: auto-detect website/deploy/ near the workspace.")
@click.option("--template-customer", default="demo", show_default=True,
              help="Customer id whose index.html + full.html serve as the "
                   "templates to clone. Default: 'demo' (the canonical "
                   "polished pages live there).")
@click.option("--skip-gate-update", is_flag=True, default=False,
              help="Don't add this customer's id to the typed-key gate. "
                   "Useful for testing or when the gate is managed separately.")
@click.pass_context
def build_dashboard_cmd(
    ctx: click.Context,
    customer_id: str,
    output_root: Path | None,
    template_customer: str,
    skip_gate_update: bool,
) -> None:
    """Generate the customer's branded portal pages.

    Outputs to ``website/deploy/customer/<id>/``:

      \b
      * index.html       — lobby (multi-target grid for multi-target customers)
      * full.html        — Bridge view (with tab bar for multi-target)
      * customer-brand.css — currently empty; placeholder for future per-customer overrides
      * manifest.json    — data feed the dashboard hydrates from
      * logo.<ext>       — copy of the customer's logo (if present)

    Also patches ``website/deploy/customer/index.html`` to add this
    customer's id to the typed-key gate's KNOWN tokens — so customers
    can type their id at https://jelleo.com/customer/ to land on their
    portal. Pass --skip-gate-update to opt out.

    The Jelleo identity (palette, typography, motion) is preserved. The
    customer-specific surface is logo, hero title, footer, and content
    scope. Each customer gets a nameplate on the door; nobody
    redecorates the building.
    """
    workspace = Path(ctx.obj["workspace"])
    entry = customers_mod.get_customer(workspace, customer_id)
    if not entry:
        raise click.ClickException(f"customer '{customer_id}' is not registered")

    brand = _brand_for(entry)
    db = open_findings_db(workspace)
    targets = _scoped_targets(db, entry)
    rollups = [_target_rollup(db, t) for t in targets]

    # Locate website root + load templates
    website_root: Path | None
    if output_root is not None:
        # Caller-supplied output root — expect its parent is website/deploy
        if output_root.name == customer_id:
            customer_out = output_root
            website_root = output_root.parent.parent
        else:
            customer_out = output_root / customer_id
            website_root = output_root.parent
    else:
        website_root = _find_website_root(workspace)
        if website_root is None:
            raise click.ClickException(
                "could not auto-locate website/deploy/. Pass --output-root explicitly."
            )
        customer_out = website_root / "customer" / customer_id

    template_dir = website_root / "customer" / template_customer
    lobby_template_path = template_dir / "index.html"
    bridge_template_path = template_dir / "full.html"
    if not lobby_template_path.is_file() or not bridge_template_path.is_file():
        raise click.ClickException(
            f"template customer '{template_customer}' is missing "
            f"index.html or full.html under {template_dir}. "
            f"Pass --template-customer to point at a different one."
        )

    lobby_template = lobby_template_path.read_text(encoding="utf-8")
    bridge_template = bridge_template_path.read_text(encoding="utf-8")

    # Copy logo into output dir + compute the in-page src
    customer_out.mkdir(parents=True, exist_ok=True)
    logo_src: str | None = None
    raw_logo = (entry.get("branding") or {}).get("logo_path")
    if raw_logo:
        src = (workspace / raw_logo).resolve()
        if src.is_file():
            ext = src.suffix.lower() or ".svg"
            dst = customer_out / f"logo{ext}"
            shutil.copyfile(src, dst)
            logo_src = f"./logo{ext}"

    # Render + write the two pages
    lobby_html = _patch_lobby(lobby_template, entry, brand, rollups, logo_src)
    bridge_html = _patch_bridge(bridge_template, entry, brand, rollups, logo_src)
    (customer_out / "index.html").write_text(lobby_html, encoding="utf-8")
    (customer_out / "full.html").write_text(bridge_html, encoding="utf-8")

    # Manifest for the dashboard data feed
    manifest = {
        "customer": {
            "id":            customer_id,
            "name":          entry.get("name"),
            "protocol_name": entry.get("protocol_name"),
            "tier":          entry.get("tier"),
            "since":         entry.get("since"),
        },
        "branding": brand,
        "targets":   rollups,
        "totals": {
            "n_targets":   len(rollups),
            "n_findings":  sum(r["n_findings"] for r in rollups),
            "by_severity": {
                k: sum(r["severity_counts"][k] for r in rollups)
                for k in ("Critical", "High", "Medium", "Low", "Info")
            },
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (customer_out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )

    # Patch the typed-key gate so the operator can type the customer id
    if not skip_gate_update:
        gate_path = website_root / "customer" / "index.html"
        if gate_path.is_file():
            original = gate_path.read_text(encoding="utf-8")
            patched = _add_customer_to_gate(original, customer_id)
            if patched != original:
                gate_path.write_text(patched, encoding="utf-8")
                console.print(
                    f"[green]gate[/green] added '{customer_id}' to "
                    f"{gate_path.relative_to(website_root.parent)} "
                    f"(operators can now type the key at jelleo.com/customer/)"
                )

    # Summary
    console.print(f"[green]built[/green] portal for [bold]{customer_id}[/bold]")
    console.print(f"  output:    {customer_out}")
    console.print(f"  lobby:     {customer_out / 'index.html'}")
    console.print(f"  bridge:    {customer_out / 'full.html'}")
    console.print(f"  targets:   {len(rollups)}{' (multi-target view enabled)' if len(rollups) > 1 else ''}")
    console.print(f"  findings:  {sum(r['n_findings'] for r in rollups)}")
    if logo_src:
        console.print(f"  logo:      {logo_src}")
    else:
        console.print(f"  [dim]no logo — using text monogram as fallback[/dim]")
    console.print()
    console.print(
        f"[dim]Commit website/deploy/customer/{customer_id}/ (and the gate "
        f"customer/index.html if it changed) + redeploy Netlify to publish at:\n"
        f"  https://jelleo.com/customer/{customer_id}/[/dim]"
    )
