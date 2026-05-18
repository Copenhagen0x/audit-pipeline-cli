"""`audit-pipeline report` — HTML report generator from findings DB.

Two reports:
  cycle  : single hunt-cycle report with executive summary
  weekly : rolling N-day summary across all cycles for a target

Pure stdlib (no Jinja, no Flask) — emits a self-contained HTML file
using the shared Jelleo design system (audit_pipeline.branding).
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.branding import (
    CSS,
    cover_page_html,
    footer_html,
    read_pubkey_fingerprint,
    topbar_html,
)
from audit_pipeline.commands.sign import SignError, default_key_path, sign_file
from audit_pipeline.db import open_findings_db
from audit_pipeline.severity import DEFINITIONS, Severity

console = Console()


def _render_html_to_pdf(html_path: Path) -> Path | None:
    """Render an HTML report to PDF via headless Chromium/Chrome.

    Returns the PDF path on success, None if no working browser is found.
    Non-fatal — caller decides whether to surface.

    Iterates candidates in order of reliability. On Ubuntu 22.04 the apt
    'chromium-browser' is a wrapper around the snap, which is sandboxed
    by AppArmor — `--print-to-pdf` reports success but the file never
    materializes outside the snap's view. So we try google-chrome (deb)
    first, validate the output file actually exists, fall through if not.
    """
    candidates = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    )
    pdf_path = html_path.with_suffix(".pdf")
    # Disclosure audit Defect 11 (LOW→MED on VPS as-root): only pass
    # ``--no-sandbox`` when explicitly opted in via env. Default off —
    # the cycle HTML contains user-controlled content (PoC excerpts,
    # finding titles) and a browser RCE shouldn't escalate to whatever
    # uid the publish pipeline runs under.
    allow_no_sandbox = (os.environ.get("JELLEO_CHROME_NO_SANDBOX") or "") == "1"
    for cmd in candidates:
        if not shutil.which(cmd):
            continue
        try:
            if pdf_path.exists():
                pdf_path.unlink()
            args = [
                cmd,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                f"file://{html_path.resolve()}",
            ]
            if allow_no_sandbox:
                args.insert(3, "--no-sandbox")
            subprocess.run(
                args,
                capture_output=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path
    return None


def _auto_sign(workspace: Path, report_path: Path, sign_enabled: bool) -> None:
    """Sign a generated report if signing is enabled and a key exists.

    Failures are warnings, not errors — a missing key should not block
    report generation. Customers without keys still get the HTML; the
    .sig file appears next to the report only when a key is configured.

    Key resolution walks UP from the workspace:
      1. ``<workspace>/keys/jelleo.ed25519``       (workspace-local)
      2. ``<workspace>/../keys/jelleo.ed25519``    (eval-suite level)
      3. ``<workspace>/../../keys/jelleo.ed25519`` (audit-runs root)

    The audit-suite organizes workspaces as
    ``audit_runs/<suite>/workspaces/<target>/`` so the suite-level key
    is two parents up. This lets one key sign every workspace under a
    given eval suite without per-workspace duplication.
    """
    if not sign_enabled:
        return
    candidates = [
        default_key_path(workspace),
        workspace.parent / "keys" / "jelleo.ed25519",
        workspace.parent.parent / "keys" / "jelleo.ed25519",
    ]
    key_path = next((p for p in candidates if p.exists()), None)
    if key_path is None:
        console.print(
            f"[yellow]auto-sign skipped:[/yellow] no key at any of "
            f"{', '.join(str(p) for p in candidates)}. "
            f"Run [cyan]audit-pipeline sign keygen[/cyan] to enable signed receipts."
        )
        return
    try:
        sig_path = sign_file(report_path, key_path)
    except SignError as e:
        console.print(f"[yellow]auto-sign failed:[/yellow] {e}")
        return
    console.print(f"[green]signed[/green]    {sig_path} (key: {key_path})")


@click.group(name="report")
def report_cmd() -> None:
    """Generate HTML reports from the findings DB."""


@report_cmd.command(name="cycle")
@click.option("--cycle-id", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the generated report with the workspace's Ed25519 key.")
@click.option("--pdf/--no-pdf", default=False, show_default=True,
              help="Also render the HTML to PDF via chromium-headless and sign the PDF.")
@click.option("--public/--full", "public", default=True, show_default=True,
              help="Filter findings to disclosed/fixed/verified/rejected only "
                   "(default: --public). Confirmed-but-not-disclosed findings are "
                   "EXCLUDED from --public reports — they're a pre-disclosure leak. "
                   "Use --full for customer-private cycle reports that include "
                   "in-progress findings (the manifest gate handles those separately).")
@click.option("--draft/--no-draft", default=True, show_default=True,
              help="Mark the report with a 'DRAFT — NOT FOR DISTRIBUTION' "
                   "banner on the cover. Defaults to --draft so intermediate "
                   "review copies are clearly labeled; pass --no-draft for the "
                   "final publishable version.")
@click.pass_context
def cycle_report(
    ctx: click.Context, cycle_id: str, output: Path | None, sign: bool, pdf: bool,
    public: bool, draft: bool,
) -> None:
    """Generate an HTML report for a single hunt cycle."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)

    all_findings = [f for f in db.list_findings(limit=1000) if f.get("cycle_id") == cycle_id]
    if not all_findings:
        raise click.ClickException(f"No findings for cycle {cycle_id}")

    if public:
        findings = [f for f in all_findings if (f.get("status") or "") in PUBLIC_STATUSES]
    else:
        findings = all_findings

    cycles = db.list_cycles()
    cycle = next((c for c in cycles if c["cycle_id"] == cycle_id), None)
    target_id = cycle["target_id"] if cycle else findings[0]["target_id"]
    target = next((t for t in db.list_targets() if t["id"] == target_id), {"name": "?"})

    out = output or (workspace / "hunts" / cycle_id / "hunt_report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    pubkey = read_pubkey_fingerprint(workspace)
    out.write_text(
        _render_cycle_html(target, cycle, findings, pubkey,
                           workspace=workspace, public=public, draft=draft),
        encoding="utf-8",
    )
    console.print(f"[green]wrote[/green] {out}")
    _auto_sign(workspace, out, sign)

    if pdf:
        pdf_path = _render_html_to_pdf(out)
        if pdf_path:
            console.print(f"[green]rendered[/green] {pdf_path}")
            _auto_sign(workspace, pdf_path, sign)
        else:
            console.print("[yellow]chromium not available — PDF skipped[/yellow]")


@report_cmd.command(name="weekly")
@click.option("--target", required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--days", type=int, default=7, show_default=True)
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the generated report with the workspace's Ed25519 key.")
@click.option("--pdf/--no-pdf", default=False, show_default=True,
              help="Also render the HTML to PDF via chromium-headless and sign the PDF.")
@click.option("--public/--full", "public", default=True, show_default=True,
              help="Filter findings to disclosed/fixed/verified/rejected only "
                   "(default: --public). Use --full for customer-private weekly "
                   "digests that include in-progress findings.")
@click.pass_context
def weekly_report(
    ctx: click.Context, target: str, output: Path | None, days: int, sign: bool, pdf: bool,
    public: bool,
) -> None:
    """Rolling N-day summary across all cycles for one target."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    t = db.get_target(target)
    if not t:
        raise click.ClickException(f"Target '{target}' not found in DB")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cycles = [c for c in db.list_cycles(target_id=t["id"], limit=500)
              if (c.get("started_at") or "") >= cutoff]
    findings = [f for f in db.list_findings(target_id=t["id"], limit=1000)
                if (f.get("created_at") or "") >= cutoff]
    if public:
        findings = [f for f in findings if (f.get("status") or "") in PUBLIC_STATUSES]

    out = output or (workspace / "reports" / f"{target}_weekly_{datetime.now(timezone.utc):%Y%m%d}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    pubkey = read_pubkey_fingerprint(workspace)
    out.write_text(
        _render_weekly_html(t, cycles, findings, days, pubkey,
                            workspace=workspace, public=public),
        encoding="utf-8",
    )
    console.print(f"[green]wrote[/green] {out}")
    _auto_sign(workspace, out, sign)

    if pdf:
        pdf_path = _render_html_to_pdf(out)
        if pdf_path:
            console.print(f"[green]rendered[/green] {pdf_path}")
            _auto_sign(workspace, pdf_path, sign)
        else:
            console.print("[yellow]chromium not available — PDF skipped[/yellow]")


# ---------------------------------------------------------------------------
# HTML render helpers
# ---------------------------------------------------------------------------


def _sev_counts(findings: list[dict]) -> dict[str, int]:
    by = {s.value: 0 for s in Severity}
    for f in findings:
        s = f.get("severity")
        if s in by:
            by[s] += 1
    return by


# Findings only count as "real" once they've moved through the lifecycle
# beyond raw recon. A new/triaged verdict is just an LLM opinion; a
# confirmed/disclosed/fixed/verified finding has PoC backing, debate
# promotion, or human review behind it. Cover-page headline numbers
# show the real bucket only — full counts go in a separate breakdown.
REAL_STATUSES = {"confirmed", "disclosed", "fixed", "verified"}

# Statuses safe to expose on the *public* cycle archive
# (api.jelleo.com/cycles/<id>/cycle.html). `confirmed` is intentionally
# EXCLUDED — a confirmed finding has fired a PoC against the live target
# but the disclosure PR may not yet be filed; publishing it before
# disclosure is a pre-disclosure leak. `rejected` is fine to include
# (the engine itself decided the verdict was a false positive).
# `closed_not_planned` is also fine to include — distinct terminal state
# meaning the maintainer reviewed and closed upstream as won't-fix /
# by-design (engine was correct that the path exists; upstream chose
# not to address). Customer-private cycle reports (--full) include
# everything so the customer behind the token gate sees their
# in-progress state.
#
# POST-AUDIT FIX (2026-05-12): added closed_not_planned to the public set.
# Previously it was added to lifecycle.py but downstream consumers
# (this set, dashboard PUBLIC_STATUSES, narrative filter) still only
# knew about REJECTED — closed_not_planned findings were being filtered
# out of public archives despite being legitimately disclosable.
PUBLIC_STATUSES = {
    "disclosed", "fixed", "verified", "rejected", "closed_not_planned",
}


def _real_severity_counts(findings: list[dict]) -> dict[str, int]:
    """Severity counts limited to confirmed/disclosed/fixed/verified findings.

    new = unreviewed LLM verdict (could be hallucination)
    triaged = human looked but no PoC yet
    rejected = false positive

    None of those are "real findings" for customer-facing display.
    """
    return _sev_counts([f for f in findings if (f.get("status") or "") in REAL_STATUSES])


def _status_breakdown(findings: list[dict]) -> dict[str, int]:
    """Count of findings per lifecycle status (for the breakdown line)."""
    out: dict[str, int] = {}
    for f in findings:
        s = (f.get("status") or "unknown")
        out[s] = out.get(s, 0) + 1
    return out


def _sev_bar(counts: dict[str, int]) -> str:
    total = max(1, sum(counts.values()))
    if sum(counts.values()) == 0:
        return ""
    return f"""
    <div class="sev-bar">
      <span class="b-critical" style="width:{counts['Critical']/total*100:.1f}%"></span>
      <span class="b-high"     style="width:{counts['High']/total*100:.1f}%"></span>
      <span class="b-medium"   style="width:{counts['Medium']/total*100:.1f}%"></span>
      <span class="b-low"      style="width:{counts['Low']/total*100:.1f}%"></span>
      <span class="b-info"     style="width:{counts['Info']/total*100:.1f}%"></span>
    </div>
    <div class="sev-bar-legend">
      <span><i style="background:var(--critical)"></i>Critical {counts['Critical']}</span>
      <span><i style="background:var(--high)"></i>High {counts['High']}</span>
      <span><i style="background:var(--medium)"></i>Medium {counts['Medium']}</span>
      <span><i style="background:var(--low)"></i>Low {counts['Low']}</span>
      <span><i style="background:var(--info)"></i>Info {counts['Info']}</span>
    </div>"""


def _findings_table(findings: list[dict]) -> str:
    if not findings:
        return '<div class="empty">No findings in this scope.</div>'
    sev_order = {s.value: i for i, s in enumerate(Severity)}
    rows = []
    for f in sorted(findings, key=lambda x: sev_order.get(x.get("severity", "Info"), 99)):
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        status = (f.get("status") or "?").lower()
        rows.append(f"""
        <tr>
          <td><span class="sev {sev_cls}">{sev.value}</span></td>
          <td><code>{html.escape(f.get('hypothesis_id', '?'))}</code></td>
          <td style="max-width:520px">{html.escape((f.get('title') or '')[:160])}</td>
          <td>{html.escape(f.get('verdict','?'))} <span style="color:var(--text-3)">/ {html.escape(f.get('confidence','?'))}</span></td>
          <td><span class="status-pill {status}">{html.escape(status)}</span></td>
          <td>{'<span style="color:var(--ok)">✓ fired</span>' if f.get('poc_fired') else '<span style="color:var(--text-3)">—</span>'}</td>
        </tr>""")
    return f"""
    <table>
      <thead><tr>
        <th>Severity</th><th>Hypothesis</th><th>Title</th>
        <th>Verdict</th><th>Status</th><th>PoC</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _table_of_contents(
    findings: list[dict],
    *,
    workspace: Path | None = None,
) -> str:
    """Print-style TOC linking every finding by anchor id.

    Sort is identical to _findings_writeup so the TOC numbering matches
    the FINDING NN/NN banners. Renders nothing if there are no findings.
    """
    if not findings:
        return ""
    # TOC must mirror what _findings_writeup actually renders — i.e.
    # only REAL_STATUSES findings (the same filter applied there).
    # Otherwise we'd link to anchors that the body never emits.
    real_findings = [f for f in findings if (f.get("status") or "") in REAL_STATUSES]
    if not real_findings:
        return ""
    sev_order = {s.value: i for i, s in enumerate(Severity)}
    sorted_findings = sorted(
        real_findings,
        key=lambda x: sev_order.get(x.get("severity", "Info"), 99),
    )
    # Match _findings_writeup's heading derivation so TOC reads the same
    # short descriptive title (e.g. "Missing auth check on transfer_admin")
    # rather than the truncated claim prose.
    hyp_library = _load_hypothesis_library(workspace) if workspace else {}

    lis = []
    for idx, f in enumerate(sorted_findings, 1):
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        hyp_id = f.get("hypothesis_id") or ""
        bug_class = f.get("bug_class") or "—"
        hyp_yaml = hyp_library.get(hyp_id, {}) if hyp_id else {}
        engine_function = (hyp_yaml.get("engine_function") or "").strip()
        display_title = _short_finding_title(
            bug_class=bug_class,
            engine_function=engine_function,
            hypothesis_id=hyp_id,
            hyp_yaml=hyp_yaml,
            db_title=f.get("title"),
        )
        # Strip backticks from TOC entries (no nested <code> inside <a>);
        # the function name stays inline.
        toc_text = display_title.replace("`", "")
        lis.append(
            f'<li>'
            f'<span class="toc-num">{idx:02d}</span>'
            f'<span class="toc-sev"><span class="sev {sev_cls}">{sev.value}</span></span>'
            f'<a href="#finding-{idx:02d}">{html.escape(toc_text)}</a>'
            f'<span class="toc-class">{html.escape(bug_class)}</span>'
            f'</li>'
        )
    return (
        '<div class="toc">'
        '<h2 style="margin-bottom:14px">01 &mdash; Per-finding analysis &middot; contents</h2>'
        '<p style="color:var(--text-3);font-size:11px;margin:0 0 14px">'
        'Each finding below begins on its own page. Numbering matches the '
        '<code>FINDING NN / NN</code> banner in the body. Click any row to jump.'
        '</p>'
        f'<ol>{"".join(lis)}</ol>'
        '</div>'
    )


def _detect_language(workspace: Path, cycle_id: str) -> str:
    """Return ``"aptos"`` (Move), ``"solidity"`` (EVM), or ``"solana"`` (default).

    Detection order (cheap → expensive):
      1. ``workspace/formal/<lang>/*`` or ``workspace/fuzz/<lang>/*`` or
         ``workspace/tests/<lang>/*`` exists
      2. ``hunts/<cycle>/hunt.log.jsonl`` contains an event with the language tag
      3. Default to ``"solana"`` (back-compat for the existing renderer)
    """
    for lang in ("aptos", "solidity"):
        if (workspace / "formal" / lang).is_dir():
            return lang
        if (workspace / "fuzz" / lang).is_dir():
            return lang
        if (workspace / "tests" / lang).is_dir():
            return lang
    log = workspace / "hunts" / cycle_id / "hunt.log.jsonl"
    if log.is_file():
        try:
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[:500]:
                if '"language": "aptos"' in line or '"language":"aptos"' in line:
                    return "aptos"
                if '"language": "solidity"' in line or '"language":"solidity"' in line:
                    return "solidity"
        except OSError:
            pass
    return "solana"


def _artifact_paths_section(workspace: Path, cycle_id: str) -> str:
    """Table of audit-artifact paths so a reviewer can verify the work.

    Lists every file or directory the report references — PoCs, formal
    specs, fuzz harnesses, bundles, merkle root, signed reports —
    relative to the workspace root. Existence-checked so absent
    artifacts render dimmed with a "(missing)" marker.

    Per-language branching (2026-05-15): Solana cycles render with
    Solana paths (`hunts/<cycle>/poc/test_*.rs`, `hunts/<cycle>/kani`,
    `hunts/<cycle>/litesvm`, `hunts/<cycle>/bundles/*`). Aptos cycles
    keep the legacy Move-prover / fuzz directory layout. Without this
    branch the appendix on a Solana cycle still claimed Aptos paths.

    Entry: ``(label, displayed_path, abs_path, fallback_note)`` —
    fallback_note replaces the "(absent in this cycle)" marker when
    the artifact lives outside the workspace by design (e.g. the
    Ed25519 public key is platform-wide, served from jelleo.com/keys/).
    """
    cycle_dir = workspace / "hunts" / cycle_id
    language = _detect_language(workspace, cycle_id)
    is_aptos = language == "aptos"
    is_solidity = language == "solidity"

    # Resolve the actual findings.db that hunt.py wrote to: prefer
    # workspace-local, then the parent ``workspaces/`` dir, then the
    # eval-suite root (where the shared DB lives for OSec cycles).
    # Use absolute paths because ``Path(".").parent == Path(".")`` —
    # relative-path traversal silently no-ops, which made the artifacts
    # table render "absent in this cycle" even when the customer DB
    # existed two levels up.
    abs_workspace = workspace.resolve()
    findings_db_candidates = [
        abs_workspace / "findings.db",
        abs_workspace.parent / "findings.db",
        abs_workspace.parent.parent / "findings.db",
    ]
    findings_db_path = next(
        (p for p in findings_db_candidates if p.is_file()),
        abs_workspace / "findings.db",
    )

    entries: list[tuple[str, str, Path, str]] = [
        ("Cycle summary (manifest of every step)",
         "hunts/<cycle>/hunt_summary.json",
         cycle_dir / "hunt_summary.json", ""),
        ("Per-step event log",
         "hunts/<cycle>/hunt.log.jsonl",
         cycle_dir / "hunt.log.jsonl", ""),
        ("Layer 2.5 triage verdicts",
         "hunts/<cycle>/triage.jsonl",
         cycle_dir / "triage.jsonl", ""),
    ]

    if is_aptos:
        entries.extend([
            ("Layer 2 PoC sources (Move)",
             "tests/aptos/test_<slug>.move",
             workspace / "tests" / "aptos", ""),
            ("Layer 2 PoC run logs",
             "hunts/<cycle>/poc/runlog_<slug>.log",
             cycle_dir / "poc", ""),
            ("Layer 3 Move Prover specs",
             "formal/aptos/spec_<slug>_invariant.move",
             workspace / "formal" / "aptos", ""),
            ("Layer 4 property-fuzz harnesses",
             "fuzz/aptos/property_<slug>.move",
             workspace / "fuzz" / "aptos", ""),
        ])
    elif is_solidity:
        entries.extend([
            ("Layer 2 PoC sources (Solidity)",
             "tests/solidity/test_<slug>.t.sol",
             workspace / "tests" / "solidity", ""),
            ("Layer 2 PoC run logs",
             "hunts/<cycle>/poc/forge_<slug>.log",
             cycle_dir / "poc", ""),
            ("Layer 3 Halmos harnesses + verdicts",
             "formal/solidity/halmos_<slug>.log",
             workspace / "formal" / "solidity", ""),
            ("Layer 4 forge invariant / fuzz harnesses",
             "fuzz/solidity/forge_<slug>.t.sol",
             workspace / "fuzz" / "solidity", ""),
        ])
    else:
        # Solana cycle artifacts. The isolated L3 (Kani) and L4
        # (LiteSVM) runners shipped 2026-05-15 stash their sidecar
        # workspaces under ``hunts/<cycle>/kani/<slug>/`` and
        # ``hunts/<cycle>/litesvm/<slug>/``.
        entries.extend([
            ("Layer 2 PoC sources (Rust)",
             "hunts/<cycle>/poc/test_<slug>.rs",
             cycle_dir / "poc", ""),
            ("Layer 2 PoC run logs",
             "hunts/<cycle>/poc/cargo_<slug>.log",
             cycle_dir / "poc", ""),
            ("Layer 3 Kani harnesses + verdicts",
             "hunts/<cycle>/kani/<slug>/",
             cycle_dir / "kani", ""),
            ("Layer 4 LiteSVM exploit tests",
             "hunts/<cycle>/litesvm/<slug>/",
             cycle_dir / "litesvm", ""),
        ])

    # Disclosure bundles are language-agnostic; both Aptos and Solana
    # cycles now ship bundles under hunts/<cycle>/bundles/ (the older
    # recon/bundles/ path is legacy Percolator).
    bundles_path = cycle_dir / "bundles"
    if not bundles_path.is_dir():
        bundles_path = workspace / "recon" / "bundles"
        bundle_rel = "recon/bundles/<finding_id>/"
    else:
        bundle_rel = "hunts/<cycle>/bundles/<finding_id>/"

    entries.extend([
        ("Layer P3 fix bundles (patch.diff + evidence/ + manifest.json)",
         bundle_rel,
         bundles_path, ""),
        ("Narrative writeups (per finding)",
         "hunts/<cycle>/narratives/<hyp_id>.md",
         cycle_dir / "narratives", ""),
        ("Cycle Merkle root (tamper-evidence)",
         "hunts/<cycle>/merkle.json",
         cycle_dir / "merkle.json", ""),
        ("Findings DB (SQLite)",
         "findings.db",
         findings_db_path, ""),
        ("Ed25519 public key for receipt verification",
         "https://jelleo.com/keys/jelleo.ed25519.pub",
         # Resolve to the first existing location: workspace → eval-suite
         # → percolator-live → bundled. Caller only needs the file to
         # exist somewhere; the cover already prints the canonical URL.
         next(
             (p for p in (
                 workspace / "keys" / "jelleo.ed25519.pub",
                 workspace.parent / "keys" / "jelleo.ed25519.pub",
                 workspace.parent.parent / "keys" / "jelleo.ed25519.pub",
             ) if p.is_file()),
             workspace / "keys" / "jelleo.ed25519.pub",
         ),
         "(platform-wide key &mdash; served from jelleo.com/keys/)"),
    ])
    rows = []
    for label, rel, abs_path, fallback_note in entries:
        exists = abs_path.exists() or bool(fallback_note)
        cell_style = "" if exists else 'style="color:var(--text-3)"'
        suffix = ""
        if not abs_path.exists():
            if fallback_note:
                suffix = (
                    ' <span style="color:var(--text-3);font-size:10.5px">'
                    + fallback_note + '</span>'
                )
            else:
                suffix = (
                    ' <span style="color:var(--text-3);font-size:10.5px">'
                    '(absent in this cycle)</span>'
                )
        rows.append(
            f'<tr {cell_style}>'
            f'<td>{html.escape(label)}</td>'
            f'<td><code>{html.escape(rel)}</code>{suffix}</td>'
            f'</tr>'
        )
    return (
        '<table style="margin-top:8px">'
        '<thead><tr><th style="width:50%">Artifact</th><th>Path (relative to workspace)</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )


def _executive_summary_section(
    target_name: str,
    cycle: dict | None,
    findings: list[dict],
    real_counts: dict[str, int],
    language: str,
    protocol_label: str,
    workspace: Path | None = None,
) -> str:
    """One-paragraph executive summary opening the report.

    Renders before the TOC so the reader has the headline numbers and
    one sentence of framing before diving into the per-finding section.
    Pulls the cycle date from the cycle dict so the prose has the
    actual audit window.
    """
    cycle_id = (cycle or {}).get("cycle_id", "")
    started_at = (cycle or {}).get("started_at", "")
    # Pull a human-readable date from cycle_id (UTC `YYYYMMDD-HHMMSS`).
    date_label = ""
    if cycle_id and len(cycle_id) >= 8:
        try:
            from datetime import datetime as _dt
            dt = _dt.strptime(cycle_id[:8], "%Y%m%d")
            date_label = dt.strftime("%B %d, %Y")
        except ValueError:
            date_label = cycle_id[:10]
    elif started_at:
        date_label = started_at[:10]

    real_findings = [f for f in findings if (f.get("status") or "") in REAL_STATUSES]
    n_crit = real_counts.get("Critical", 0)
    n_high = real_counts.get("High", 0)
    n_med = real_counts.get("Medium", 0)
    n_low = real_counts.get("Low", 0)

    def _grammatical_count(n: int, singular: str, plural: str) -> str:
        if n == 1:
            return f"1 {singular}"
        return f"{n} {plural}"

    sev_phrase_parts = []
    for n, sing, plur in (
        (n_crit, "Critical", "Critical"),
        (n_high, "High", "High"),
        (n_med, "Medium", "Medium"),
        (n_low, "Low", "Low"),
    ):
        if n:
            sev_phrase_parts.append(_grammatical_count(n, sing, plur))
    if sev_phrase_parts:
        if len(sev_phrase_parts) == 1:
            sev_phrase = sev_phrase_parts[0] + " finding" + (
                "s" if real_findings and len(real_findings) != 1 else ""
            )
        else:
            sev_phrase = (
                ", ".join(sev_phrase_parts[:-1]) + " and "
                + sev_phrase_parts[-1] + " findings"
            )
    else:
        sev_phrase = "no confirmed findings"

    # Short descriptive titles for each finding. Use the same generator
    # as the per-finding heading + TOC so all three places read the same
    # short, professional phrase ("Missing auth check on transfer_admin"
    # / "Permissionless emergency_drain drains the vault"). Sidesteps
    # the mid-word truncation problem that the YAML-claim-based version
    # had, and avoids html-escape issues with type-parameter syntax in
    # backtick-quoted code (`borrow_global_mut<T>(addr)`).
    hyp_library = _load_hypothesis_library(workspace) if workspace else {}
    titles = []
    for f in real_findings:
        hid = f.get("hypothesis_id") or ""
        bc = f.get("bug_class") or ""
        hyp_yaml = hyp_library.get(hid, {}) if hid else {}
        ef = (hyp_yaml.get("engine_function") or "").strip()
        short = _short_finding_title(
            db_title=f.get("title"),
            bug_class=bc, engine_function=ef,
            hypothesis_id=hid, hyp_yaml=hyp_yaml,
        )
        titles.append(short)
    findings_summary = ""
    if titles:
        # Render titles with backticks as <code>. Joins with semicolons
        # and prefixes with "(a) / (b) / (c)" so the inline list reads
        # cleanly even on long lines.
        rendered_titles = [_render_inline_backticks(t) for t in titles]
        if len(titles) == 1:
            findings_summary = f" The finding documents: {rendered_titles[0]}."
        elif len(titles) <= 4:
            findings_summary = " The findings document: " + "; ".join(
                f"({chr(ord('a') + i)}) {t}" for i, t in enumerate(rendered_titles)
            ) + "."

    if language == "aptos":
        formal_phrase = "an authored Move Prover spec (Boogie/Z3/CVC5 not deployed on this VPS)"
        fuzz_phrase = "a property-based <code>aptos move test</code> reproduction"
        poc_phrase = "a Move-VM-executed"
    elif language == "solidity":
        formal_phrase = "a Halmos symbolic-execution check where the formal layer ran"
        fuzz_phrase = "a forge fuzz / invariant reproduction"
        poc_phrase = "a forge-test"
    else:
        formal_phrase = "a Kani-bounded model-checker proof where the formal layer ran"
        fuzz_phrase = "an on-chain BPF reproduction through LiteSVM"
        poc_phrase = "an engine-direct"
    framing = (
        f"This report documents the results of an autonomous {protocol_label} audit "
        f"cycle run by Jelleo against the <code>{html.escape(target_name)}</code> "
        f"workspace on {html.escape(date_label) if date_label else 'the date noted on the cover'}. "
        f"The cycle identified {sev_phrase} after Layer 2.5 triage and root-cause "
        f"clustering.{findings_summary} Each finding includes "
        f"{poc_phrase} proof-of-concept, {formal_phrase}, {fuzz_phrase}, "
        f"and an LLM-authored structural fix patch."
    )

    return (
        '<section style="margin:32px 0 24px;padding:20px 22px;'
        'background:var(--surface);border:1px solid var(--rule);'
        'border-radius:6px;border-left:3px solid var(--amber)">'
        '<h2 style="margin:0 0 12px;font-size:11px;letter-spacing:.22em;'
        'text-transform:uppercase;color:var(--amber);border:none;padding:0">'
        '00 &mdash; Executive summary'
        '</h2>'
        f'<p style="margin:0;color:var(--text);font-size:13px;line-height:1.7">'
        f'{framing}</p>'
        '</section>'
    )


def _scope_section(
    workspace: Path | None,
    target_name: str,
    cycle: dict | None,
    language: str,
    protocol_label: str,
) -> str:
    """Scope of work — engine repo, commit hash, target files, exclusions.

    Lists every source file in the engine's `sources/` directory (Move)
    or `src/` directory (Solana) so the reader sees exactly which code
    was in scope. Also captures the cycle's hypothesis library so the
    "what we tested for" surface is documented.
    """
    if not workspace or not cycle:
        return ""
    engine_sha_full = (cycle.get("engine_sha") or "").strip()
    engine_sha_short = engine_sha_full[:10] if engine_sha_full else "—"

    # Locate the engine source tree. Prefer the legacy ``workspace/engine``
    # symlink (Percolator), fall back to the workspace.json's
    # ``engine.local`` relative path (OSec eval cycles point at
    # ``../../../../ottersec-eval/repos/<target>``).
    import json as _json_scope
    engine_root = workspace / "engine"
    if not engine_root.is_dir():
        try:
            _ws_cfg = _json_scope.loads(
                (workspace / "workspace.json").read_text(encoding="utf-8")
            )
        except (OSError, _json_scope.JSONDecodeError):
            _ws_cfg = {}
        _engine_rel = (_ws_cfg.get("engine") or {}).get("local")
        if _engine_rel:
            engine_root = (workspace / _engine_rel).resolve()

    sources: list[str] = []
    if language == "aptos":
        for sub in ("sources",):
            d = engine_root / sub
            if d.is_dir():
                sources.extend(
                    str(p.relative_to(engine_root))
                    for p in sorted(d.glob("*.move"))
                )
    elif language == "solidity":
        # Solidity workspace: src/*.sol at engine root (Foundry convention).
        for sub in ("src",):
            d = engine_root / sub
            if d.is_dir():
                sources.extend(
                    str(p.relative_to(engine_root))
                    for p in sorted(d.glob("*.sol"))
                )
    else:
        # Percolator workspace: src/*.rs at engine root.
        for sub in ("src",):
            d = engine_root / sub
            if d.is_dir():
                sources.extend(
                    str(p.relative_to(engine_root))
                    for p in sorted(d.glob("*.rs"))
                )
        # Anchor workspace: programs/<name>/src/*.rs for each program.
        programs_dir = engine_root / "programs"
        if programs_dir.is_dir():
            for prog in sorted(programs_dir.iterdir()):
                if prog.is_dir() and (prog / "Cargo.toml").is_file():
                    src_dir = prog / "src"
                    if src_dir.is_dir():
                        sources.extend(
                            str(p.relative_to(engine_root))
                            for p in sorted(src_dir.glob("*.rs"))
                        )
    if not sources:
        sources = ["(engine source enumeration unavailable in this build)"]

    # Hypothesis library size — prefer the cycle-specific count (from
    # hunt_summary.json or the cycle's hunt.log.jsonl). Avoids the
    # union-of-all-bundled-libraries count which can read as e.g. 954.
    cycle_id_for_lib = (cycle or {}).get("cycle_id") if cycle else None
    n_hyps_in_library = (
        _cycle_hypothesis_library_size(workspace, cycle_id_for_lib)
        if cycle_id_for_lib else None
    )
    if n_hyps_in_library is None:
        # Final fallback: union library size
        n_hyps_in_library = len(_load_hypothesis_library(workspace))

    files_rows = "".join(
        f'<tr><td><code>{html.escape(s)}</code></td></tr>' for s in sources
    )
    return f"""
  <h2>00.1 &mdash; Scope</h2>
  <table>
    <thead><tr><th colspan="2">In-scope source set</th></tr></thead>
    <tbody>
      <tr><td style="width:160px;color:var(--text-3)">Target workspace</td>
          <td><code>{html.escape(target_name)}</code></td></tr>
      <tr><td style="color:var(--text-3)">Protocol</td>
          <td>{("Move smart-contract framework (Aptos)" if language == "aptos" else ("Solidity smart contracts (EVM / Foundry)" if language == "solidity" else "Solana BPF program"))}</td></tr>
      <tr><td style="color:var(--text-3)">Engine commit</td>
          <td><code>{html.escape(engine_sha_short)}</code> {('<span style="color:var(--text-3);font-size:11px">(' + html.escape(engine_sha_full) + ')</span>') if engine_sha_full and len(engine_sha_full) > 10 else ''}</td></tr>
      <tr><td style="color:var(--text-3)">Source files</td>
          <td><table style="border:none;margin:0;background:none"><tbody>{files_rows}</tbody></table></td></tr>
      <tr><td style="color:var(--text-3)">Hypothesis library</td>
          <td>{n_hyps_in_library} invariant claim(s) covering authorization, arithmetic safety, accounting consistency, capability handling, event auditability, and oracle / time freshness</td></tr>
      <tr><td style="color:var(--text-3)">Out of scope</td>
          <td style="color:var(--text-2)">Off-chain components (indexers, frontends, oracles); deployment scripts; framework / standard-library code; dependencies pinned in <code>{("Move.toml" if language == "aptos" else ("foundry.toml" if language == "solidity" else "Cargo.toml"))}</code> beyond their declared interfaces.</td></tr>
    </tbody>
  </table>
"""


def _load_hypothesis_library(workspace: Path) -> dict[str, dict]:
    """Pull the full hypothesis library that drove this workspace.

    The DB stores a 120-char-truncated ``title`` (the first segment of
    each hypothesis's ``claim:`` field). For audit-quality rendering we
    need the FULL claim prose. We re-load it from the YAML at render
    time. Caller can look up ``hypothesis_id`` and pull the untruncated
    claim, plus the ``rationale`` / ``severity`` / ``engine_function`` /
    ``target_file`` metadata that the DB also doesn't carry.

    Resolution order (first-match wins per hypothesis_id, so per-cycle
    libraries override bundled templates):
      1. ``workspace/hypotheses.yaml``       (legacy cycle-local copy)
      2. ``workspace/hypotheses/*.yaml``     (per-cycle library directory)
      3. ``src/audit_pipeline/templates/hypotheses/*.yaml``  (bundled)

    Returns ``{hypothesis_id: hyp_dict, ...}``. Empty dict if nothing
    resolves — caller falls back to the DB title.

    NOTE on counting: this dict is the UNION across all loaded files,
    so its length is NOT the size of the cycle's specific library. For
    cycle-specific counts use ``_cycle_hypothesis_library_size()``.
    """
    import yaml as _yaml
    candidates: list[Path] = []
    direct = workspace / "hypotheses.yaml"
    if direct.is_file():
        candidates.append(direct)
    hyp_dir = workspace / "hypotheses"
    if hyp_dir.is_dir():
        candidates.extend(sorted(hyp_dir.glob("*.yaml")))
    # Bundled templates as fallback
    try:
        import audit_pipeline
        pkg_root = Path(audit_pipeline.__file__).resolve().parent
        templates = pkg_root / "templates" / "hypotheses"
        if templates.is_dir():
            candidates.extend(sorted(templates.glob("*.yaml")))
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, dict] = {}
    for path in candidates:
        try:
            data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, _yaml.YAMLError):
            continue
        for h in (data.get("hypotheses") or []):
            if isinstance(h, dict) and h.get("id"):
                # First file wins per hypothesis_id (workspace-local
                # copies override bundled templates).
                out.setdefault(h["id"], h)
    return out


def _cycle_hypothesis_library_size(
    workspace: Path, cycle_id: str,
) -> int | None:
    """Return the size of the SPECIFIC hypothesis library this cycle used.

    Strategy:
      1. If ``hunt_summary.json`` recorded ``n_hypotheses``, use that.
      2. Else: count hypothesis_ids that appeared in the cycle's hunt
         log events (any event that has a ``hypothesis_id`` field).
      3. Else: None (caller falls back to the union library size).

    Avoids the inflated "954 hypotheses" we got from blindly counting
    every YAML in the bundled templates directory.
    """
    import json as _json
    summary = workspace / "hunts" / cycle_id / "hunt_summary.json"
    if summary.is_file():
        try:
            s = _json.loads(summary.read_text(encoding="utf-8"))
            n = s.get("n_hypotheses")
            if isinstance(n, int) and n > 0:
                return n
        except (OSError, ValueError):
            pass
    log = workspace / "hunts" / cycle_id / "hunt.log.jsonl"
    if log.is_file():
        seen: set[str] = set()
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                if '"hypothesis_id"' not in line:
                    continue
                try:
                    ev = _json.loads(line)
                except ValueError:
                    continue
                hid = ev.get("hypothesis_id")
                if hid:
                    seen.add(hid)
        except OSError:
            pass
        if seen:
            return len(seen)
    return None


# Bug-class → short descriptive title template. The `{fn}` placeholder
# is replaced with the engine_function (in backticks for code styling).
# Falls back to a title-cased bug_class when no template matches.
#
# These render as the per-finding section heading (h3), replacing the
# previous truncated-claim heading that ended with "…". Each title is a
# complete thought of 5–9 words so the reader can name the finding at a
# glance without reading the full invariant block.
_BUG_CLASS_TITLES: dict[str, str] = {
    # Authorization / access-control family
    "borrow-global-no-auth":   "Missing auth check on `{fn}`",
    "acl-bypass-entry":        "ACL bypass via direct `{fn}` entry",
    "missing-signer-check":    "Missing signer check on `{fn}`",
    "signer-not-bound-to-resource": "Signer not bound to resource in `{fn}`",
    "treasury-drain":          "Permissionless `{fn}` drains the vault",
    "cap-leak":                "Privileged capability leak via `{fn}`",
    "friend-module-trust":     "Friend-module trust assumption violated in `{fn}`",
    "resource-double-move":    "Resource double-move in `{fn}`",
    "module-publisher-confusion": "Publisher / `init_module` confusion in `{fn}`",
    "fee-receiver-unauthorized": "Unauthorized fee-receiver mutation in `{fn}`",

    # Arithmetic / accounting family
    "u64-overflow-arith":      "Unchecked u64 arithmetic overflow in `{fn}`",
    "u64-underflow-dos":       "u64 underflow → DoS in `{fn}`",
    "fixed-point-precision-drop": "Fixed-point precision drop in `{fn}`",
    "cast-truncation":         "Cast truncation loses bits in `{fn}`",
    "divide-by-zero":          "Division-by-zero in `{fn}`",
    "rounding-direction":      "Rounding direction favors attacker in `{fn}`",
    "liquidation-bonus-overflow": "Liquidation bonus overflow in `{fn}`",
    "lending-interest-accrual-overflow": "Interest accrual overflow in `{fn}`",
    "fee-percent-bound":       "Fee percent exceeds safe bound in `{fn}`",
    "total-supply-divergence": "Total-supply accounting divergence in `{fn}`",

    # State / reentrancy / lifecycle family
    "stake-double-claim":      "Stake reward double-claim in `{fn}` (no cursor advance)",
    "missing-pause-check":     "Missing pause-check on `{fn}`",
    "asymmetric-pause":        "Asymmetric pause enforcement in `{fn}`",
    "share-inflation-first-depositor": "First-depositor share inflation in `{fn}`",
    "share-donation-inflation": "Share-donation inflation attack on `{fn}`",
    "missing-slippage":        "Missing slippage parameter on `{fn}`",
    "no-deadline-on-permit":   "Permit has no deadline in `{fn}`",
    "withdraw-delay-bypass":   "Withdraw-delay bypass in `{fn}`",
    "auction-bid-after-end":   "Auction bid accepted after end in `{fn}`",
    "auction-settle-no-winner": "Auction settles with no winner in `{fn}`",
    "type-argument-confusion": "Type-argument confusion in `{fn}`",
    "resource-leak":           "Resource leak in `{fn}`",
    "governance-flashloan-vote": "Flash-loan vote in governance `{fn}`",
    "proposal-replay":         "Proposal replay in `{fn}`",
    "liquidation-no-min-amount": "Liquidation has no min-amount guard in `{fn}`",

    # Oracle family
    "oracle-staleness":        "Oracle staleness accepted by `{fn}`",
    "oracle-zero-price":       "Oracle zero-price accepted by `{fn}`",
    "oracle-decimals-mismatch": "Oracle decimals mismatch in `{fn}`",

    # Auditability
    "event-emit-missing":      "Silent state mutation in `{fn}` (no event)",

    # F7 / Percolator legacy classes (kept for Solana reports)
    "insurance-counter-vault-divergence": "Insurance / vault accounting divergence in `{fn}`",
    "implicit_invariant":      "Implicit invariant violated in `{fn}`",
    "invariant_property":      "Invariant property violated in `{fn}`",

    # Solana / Anchor PDA family (uppercased acronym)
    "predictable-pda":         "Predictable PDA from attacker-controlled seed in `{fn}`",
    "pda-not-canonical":       "PDA not canonical in `{fn}` (missing seeds / bump)",
}


def _render_inline_backticks(text: str) -> str:
    """Markdown-style backticks → <code>…</code>, with the rest escaped.

    Used to render the descriptive finding title where backtick-wrapped
    function names should typeset as inline code. Single-pass: split on
    backtick boundaries, escape each segment, wrap odd-indexed segments
    in <code>.
    """
    parts = text.split("`")
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<code>{html.escape(seg)}</code>")
        else:
            out.append(html.escape(seg))
    return "".join(out)


def _short_finding_title(
    *,
    bug_class: str,
    engine_function: str,
    hypothesis_id: str,
    hyp_yaml: dict | None = None,
    db_title: str | None = None,
) -> str:
    """Build a 5-9 word descriptive heading for a finding.

    Resolution order:
      1. ``hyp_yaml["title"]`` — if the hypothesis library author wrote
         an explicit short title (currently unused; reserved for future
         per-hyp overrides).
      2. ``_BUG_CLASS_TITLES[bug_class].format(fn=engine_function)`` —
         the curated template table above.
      3. Title-cased ``bug_class`` + " in `<engine_function>`" — generic
         fallback for unknown bug classes.
      4. The bare hypothesis_id — last resort when no metadata is available.
    """
    # 1. Operator-curated DB title wins (e.g. backfilled for aptos-large
    #    findings where the hypothesis library has no engine_function).
    if db_title and db_title.strip() and db_title.strip() != "—":
        return db_title.strip()
    yaml_title = (hyp_yaml or {}).get("title") if hyp_yaml else None
    if yaml_title:
        return str(yaml_title).strip()
    bc = (bug_class or "").strip().lower()
    fn = (engine_function or "").strip()
    template = _BUG_CLASS_TITLES.get(bc)
    if template and fn:
        return template.format(fn=fn)
    if template:
        # template uses {fn} but we have no engine_function — strip the placeholder
        return template.replace(" on `{fn}`", "").replace(" in `{fn}`", "").replace("`{fn}`", "").strip()
    if bc:
        humanized = bc.replace("-", " ").replace("_", " ").strip()
        # Capitalize the first letter only — lower-case the rest for natural prose
        humanized = humanized[:1].upper() + humanized[1:] if humanized else ""
        if fn:
            return f"{humanized} in `{fn}`"
        return humanized
    return hypothesis_id or "Finding"


# Bug-class → (impact, recommendation) prose. Keyed by the slug used in
# the hypothesis library `bug_class:` field. Falls back to a generic
# severity-tier description when the bug_class is unknown.
#
# Each entry is two short paragraphs:
#   - Impact:        what the bug lets an attacker do, in user-facing terms
#   - Recommendation: structural fix direction (not the literal patch —
#                    the patch diff is already in the L-P3 section)
_BUG_CLASS_PROSE: dict[str, tuple[str, str]] = {
    "borrow-global-no-auth": (
        "Any signer can call the privileged function and overwrite the resource "
        "without proving they are the current admin or capability holder. For a "
        "treasury / admin-cap module this is full protocol takeover: an attacker "
        "becomes admin in one transaction with zero preconditions beyond holding "
        "an Aptos account.",
        "Gate every entry that performs `borrow_global_mut<T>(addr)` on a "
        "privileged resource through `access_control::assert_admin(...)` (or the "
        "equivalent capability check) BEFORE the mutation. Pass the actual "
        "signer's address into the check — `signer::address_of(caller)` — so an "
        "attacker calling with a non-admin signer aborts before the borrow.",
    ),
    "treasury-drain": (
        "An unprivileged signer can withdraw arbitrary amounts from the protocol's "
        "vault. No admin check, no rate limit, no time-lock: the attacker passes "
        "the desired amount and receives the funds. The vault's internal accounting "
        "(`total_deposits`) is also not updated, so on-chain dashboards continue "
        "to show the pre-drain balance until the next deposit/withdraw rebalance.",
        "Add `access_control::assert_admin(signer::address_of(invoker))` at the "
        "top of every treasury-withdrawal entry, and update the vault's "
        "`total_deposits` counter in the same transaction as the coin extraction. "
        "For higher assurance, require a time-lock or multisig signer for "
        "emergency drains rather than a single-step admin call.",
    ),
    "acl-bypass-entry": (
        "A second public entry function performs the same privileged mutation as "
        "the gated entry but skips the access-control check. Even when the canonical "
        "entry is properly auth-gated, the bypass entry provides a route past the "
        "ACL. This is the multi-entry analogue of the single-entry missing-auth bug.",
        "Audit every `public entry fun` against the access-control matrix. Functions "
        "that mutate gated resources must call `assert_admin` (or the relevant "
        "capability accessor) on entry. Where multiple entries share a mutation, "
        "extract a private helper that performs the check + mutation, and have "
        "the public entries delegate.",
    ),
    "cap-leak": (
        "A capability with `store` ability is granted under a structurally "
        "unbounded issuance plan — the cap can be cloned, persisted in a public "
        "resource, or handed to per-user storage. Once leaked, the capability "
        "grants admin permanently and cannot be revoked without code changes.",
        "Issue privileged capabilities under a finite, intentional plan: one "
        "capability per admin slot, destroyed on rotation. Avoid `store` on "
        "capability types unless the storage path is also access-gated. Prefer "
        "non-storable witness types where the use site can verify the caller "
        "directly.",
    ),
    "event-emit-missing": (
        "State-mutating entries do not emit events. On-chain auditability is "
        "degraded — observers (indexers, monitoring, governance) cannot reconstruct "
        "who performed which mutation and when. In combination with other findings "
        "(e.g. silent admin takeover) this prevents downstream detection.",
        "Emit a typed event from every state-mutating entry. Events should record "
        "the actor (`signer::address_of(caller)`), the affected resource address, "
        "and the relevant before/after fields. Combine with `#[event]` typed struct "
        "definitions so off-chain tooling can decode the events with the deployed "
        "ABI.",
    ),
    "u64-overflow-arith": (
        "Arithmetic on a `u64` balance or counter that can grow under attacker "
        "control aborts on overflow at the Move VM level. While Aptos halts the "
        "transaction (no silent wrap), the abort is a denial-of-service vector — "
        "once the counter saturates, every subsequent call on the affected path "
        "is bricked until a counter-reset mechanism is invoked.",
        "Widen the arithmetic into `u128` for the accumulation step, check the "
        "result against `u64::MAX` before casting back, and abort with a typed "
        "error code (not the VM's generic arithmetic error). For long-running "
        "counters consider periodic rebalancing or a u256 representation.",
    ),
    "stake-double-claim": (
        "A reward-claim path computes the user's accrued reward from a stored "
        "snapshot (`last_claim`) but does not advance the snapshot atomically with "
        "the payout. The user can call the claim function repeatedly in the same "
        "block and accumulate the per-period reward multiple times. The protocol "
        "pays out N× the entitled amount.",
        "After computing the reward, advance the user's `last_claim` cursor by "
        "`period_count * SECONDS_PER_PERIOD` BEFORE updating `accumulated`. The "
        "cursor advancement must happen in the same transaction (and the same "
        "function) as the reward computation, not deferred to a separate "
        "settlement step.",
    ),
    # ----- Anchor / Solana bug classes (added 2026-05-15) -----
    "anchor_account_validation_signer_missing": (
        "A privileged account field is declared as `AccountInfo<'info>` instead of "
        "`Signer<'info>`. Anchor performs no signature check on AccountInfo, so "
        "any caller who knows the privileged pubkey can pass it without holding "
        "the private key. Combined with a downstream pubkey-equality guard, this "
        "lets anyone impersonate the privileged role and execute the gated "
        "instruction — typically a full drain of whatever vault the role controls.",
        "Change the field to `Signer<'info>` so the Solana runtime verifies the "
        "ed25519 signature before the instruction body executes. Pair it with "
        "an `#[account(address = ... @ ErrCode)]` constraint to enforce the "
        "pubkey match at the framework level. Drop the body-level pubkey-equality "
        "branch once the framework-level checks are in place — they become dead "
        "code.",
    ),
    "anchor_account_validation_has_one_missing": (
        "An account on a privileged instruction is declared without a "
        "`has_one = <field>` constraint binding it to a signer or stored "
        "authority. Any caller can pass an arbitrary pubkey in that slot; the "
        "instruction body then trusts the unauthenticated value and (typically) "
        "transfers funds to it via `invoke_signed`. This is a permissionless "
        "drain of whatever vault the instruction controls.",
        "Add `has_one = <authority_field> @ ErrCode::Unauthorized` to the "
        "account constraint. Make the authority field a `Signer<'info>` so a "
        "real signature is required. Anchor will then enforce both the "
        "signature and the pubkey-equality with the stored authority at the "
        "framework level, before the instruction body runs.",
    ),
    "anchor_account_validation_seeds_missing": (
        "A program-owned account on a privileged instruction is declared "
        "without `seeds = [...]` + `bump = ...` constraints. Anchor does not "
        "derive or check the account's PDA; the caller can pass any "
        "`Account<'info, T>` with the right discriminator, including one "
        "belonging to a different user. The instruction then reads/writes the "
        "wrong user's state. For reward-claim flows this means an attacker "
        "claims arbitrary users' accrued rewards into the attacker's wallet.",
        "Add `seeds = [b\"<tag>\", <signer>.key().as_ref()]` and `bump = "
        "<account>.bump` to the account constraint so Anchor derives the "
        "canonical PDA from the signer's key. Optionally also add `has_one = "
        "<signer>` for defense in depth. The framework rejects any account "
        "whose address doesn't match the derived PDA.",
    ),
    "state_machine_clock_monotonicity": (
        "A state-machine update reads `Clock::get()?.unix_timestamp` and writes "
        "the value back into a stored timestamp field without checking that "
        "the new value is monotonically greater than the prior one. A "
        "`saturating_sub`-based elapsed-time calculation silently clamps to "
        "zero on regression but the program still overwrites the checkpoint, "
        "permanently lowering it. A subsequent legitimate update then "
        "computes elapsed-time across the regression window, paying out "
        "inflated rewards.",
        "Reject any call where `current_time < stored_timestamp` via "
        "`require!(current_time >= stored_timestamp, ErrCode::ClockRegression)` "
        "BEFORE computing elapsed-time and BEFORE writing the new checkpoint. "
        "Add `ClockRegression` to the error enum. On mainnet validator clocks "
        "are practically monotonic; the guard is defense-in-depth against "
        "test/dev environments and any future runtime change that loosens "
        "monotonicity.",
    ),
    "anchor_close_constraint_missing": (
        "An instruction marks an account as completed/finalised but does not "
        "use Anchor's `close = <receiver>` constraint. The account stays "
        "on-chain forever with its discriminator intact: rent reserves are "
        "permanently locked, the `(maker, id)` tuple can never be reused "
        "because Anchor's `init` refuses to recreate an existing PDA, and "
        "the stale state is readable by any future instruction that "
        "references the PDA.",
        "Add `close = <receiver>` to the account constraint in the instruction "
        "that finalises the lifecycle. Anchor will return the rent to "
        "`<receiver>` and set the discriminator to "
        "`CLOSED_ACCOUNT_DISCRIMINATOR` (eight 0xFF bytes) atomically, "
        "freeing the address for future reuse and preventing revival-via-"
        "realloc attacks.",
    ),
    "arithmetic_saturating_sub_silent_clamp": (
        "A balance-or-counter update uses `saturating_sub` instead of "
        "`checked_sub`. On underflow the value silently clamps to zero with "
        "no error, allowing the on-chain counter to drift out of sync with "
        "the actual lamports balance. Direct exploitability requires another "
        "path to the over-withdrawal (e.g. an admin-auth bypass), but the "
        "silent counter desync also breaks integrators who treat the counter "
        "as authoritative for off-chain accounting.",
        "Replace `saturating_sub` with `.checked_sub(amount).ok_or(ErrCode::"
        "Underflow)?` and add `Underflow` to the error enum so any "
        "inconsistency aborts loudly. Alternatively, remove the counter "
        "entirely if it duplicates information already trackable via the "
        "vault's lamports balance.",
    ),
    "build_hygiene_workspace_member_missing_manifest": (
        "The root `Cargo.toml` declares a workspace member that has no "
        "`Cargo.toml` of its own. `cargo build` and `cargo test` both abort "
        "with `error: failed to load manifest for workspace member` on a "
        "fresh clone. Downstream auditors, CI pipelines, and integrators "
        "cannot build the repo as shipped — a reproducibility / supply-chain "
        "hygiene defect rather than a runtime vulnerability.",
        "Either remove the empty member from `members = [...]` (recommended "
        "if the directory contains script-style integration tests with their "
        "own invocation), or add a real `Cargo.toml` to the member crate so "
        "Cargo can load it as a workspace member. Verify with `cargo build` "
        "on a fresh clone before tagging the next release.",
    ),
}


def _impact_and_recommendation(
    *, bug_class: str, hyp_yaml: dict, severity: Severity,
) -> tuple[str, str]:
    """Return (impact_text, recommendation_text) for a finding.

    Prefers the YAML's per-hypothesis ``impact`` + ``recommendation`` fields
    if the library author wrote them. Falls back to the bug-class lookup
    table above. Falls back to a generic severity-tier description if
    bug_class is unknown.
    """
    yaml_impact = (hyp_yaml or {}).get("impact")
    yaml_rec = (hyp_yaml or {}).get("recommendation")
    if yaml_impact and yaml_rec:
        return str(yaml_impact).strip(), str(yaml_rec).strip()

    bc = (bug_class or "").strip().lower()
    prose = _BUG_CLASS_PROSE.get(bc)
    if prose:
        impact, rec = prose
        return yaml_impact or impact, yaml_rec or rec

    # Generic fallback by severity
    sev_v = severity.value if hasattr(severity, "value") else str(severity)
    generic_impact = {
        "Critical": "Direct loss of user funds or full protocol takeover with no "
                    "meaningful preconditions.",
        "High":     "Significant loss of user funds or invariant violation under "
                    "realistic preconditions.",
        "Medium":   "Hardening issue or invariant violation requiring a privileged "
                    "signer / improbable state.",
        "Low":      "Minor issue with no plausible path to fund loss.",
        "Info":     "Informational. No security impact.",
    }.get(sev_v, "")
    generic_rec = (
        "Audit the affected code path against the invariant stated above and "
        "apply the structural fix proposed in the patch diff below."
    )
    return yaml_impact or generic_impact, yaml_rec or generic_rec


def _load_triage_clusters(workspace: Path, cycle_id: str) -> dict[str, list[str]]:
    """Build a hyp_id → cluster members lookup from triage.jsonl.

    Used to render the "Represents N hypotheses" chip on cluster
    representatives. Every member maps to the SAME list (so non-reps
    also know the cluster id), but only the representative's section
    actually appears in the report (non-reps are TRIAGED, filtered by
    REAL_STATUSES).

    Returns ``{hyp_id: [member_ids, ...]}`` where the cluster
    representative's hyp_id keys the list. Empty dict if triage.jsonl
    is absent or unparseable.
    """
    import json as _json
    out: dict[str, list[str]] = {}
    log = workspace / "hunts" / cycle_id / "triage.jsonl"
    if not log.is_file():
        return out
    by_cluster: dict[str, list[str]] = {}
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if r.get("classification") != "STRONG":
                continue
            cid = r.get("cluster_id")
            hid = r.get("hyp_id")
            if not cid or not hid:
                continue
            by_cluster.setdefault(cid, []).append(hid)
    except OSError:
        return out
    # Map every member → the cluster's member list, with the rep first.
    for cid, members in by_cluster.items():
        ordered = [cid] + [m for m in members if m != cid]
        for hid in members:
            out[hid] = ordered
    return out


def _aptos_counterexample_excerpt(workspace: Path, cycle_id: str, hyp_slug: str) -> str:
    """Pull the Move Prover counterexample state from formal logs.

    Move Prover's `--diagnostics` output includes lines like:
        Counterexample found: ...
        Error trace: ...
    We surface a small excerpt so the report shows the actual state
    that violated the spec, not just "✓ counterexample found".

    Returns "" if no usable excerpt is available — caller renders only
    the one-line verdict in that case.
    """
    candidates = [
        workspace / "formal" / "aptos" / f"aptos_move_prove_{hyp_slug}.log",
        workspace / "hunts" / cycle_id / "formal" / f"prove_{hyp_slug}.log",
    ]
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            text = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Look for the diagnostics block
        lines = text.splitlines()
        keep: list[str] = []
        in_trace = False
        for ln in lines:
            low = ln.lower()
            if "counterexample" in low or "error trace" in low:
                in_trace = True
            if in_trace:
                keep.append(ln.rstrip())
                if len(keep) >= 18:
                    keep.append("    // …trace truncated for brevity")
                    break
        if keep:
            return "\n".join(keep)
    return ""


def _hunt_funnel_section(
    workspace: Path,
    cycle_id: str,
    findings: list[dict],
) -> str:
    """Render the layered-classification funnel for this cycle.

    Numbers:
      - hypotheses_tested      ← hunt_summary.json["n_candidates"]
      - poc_fires              ← hunt_summary.json["n_poc_fired"]
      - triage_strong          ← triage.jsonl count where classification=="STRONG"
      - triage_soft / false / lost
      - root_cause_clusters    ← distinct cluster_ids among STRONG fires
      - confirmed_findings     ← DB count where status="confirmed" for this cycle

    Each row is rendered as a horizontal stage with arrow + label + count,
    so the reader can trace 40 hypotheses → 2 unique root causes without
    asking "where did the other 38 go".
    """
    import json as _json
    cycle_dir = workspace / "hunts" / cycle_id
    summary_path = cycle_dir / "hunt_summary.json"
    triage_jsonl = cycle_dir / "triage.jsonl"

    n_hypotheses = "?"
    n_fires = "?"
    total_cost_usd: float | None = None
    elapsed_seconds: float | None = None
    started_at_iso: str | None = None
    if summary_path.is_file():
        try:
            s = _json.loads(summary_path.read_text(encoding="utf-8"))
            # Use n_hypotheses (total library size tested) as the funnel
            # base, NOT n_candidates (a narrower subset). The funnel math
            # only works if base = fires + non-fires, and the DB shows
            # exactly n_hypotheses findings per cycle. n_candidates is an
            # internal dispatch count that doesn't account for all rows.
            n_hypotheses = s.get("n_hypotheses") or s.get("n_candidates") or "?"
            n_fires = s.get("n_poc_fired", "?")
            total_cost_usd = s.get("total_cost_usd")
            elapsed_seconds = s.get("elapsed_seconds")
            started_at_iso = s.get("started_at")
        except (OSError, ValueError):
            pass

    # Wall-clock fallback: if elapsed_seconds is missing, zero, or
    # implausibly short (resume bug: the timer resets on each --resume-cycle
    # so a multi-hour Solidity hunt can report 0.1s), derive it from the
    # FIRST→LAST event timestamps in hunt.log.jsonl. That captures total
    # wall-clock across resumes.
    if (not elapsed_seconds or elapsed_seconds < 60) and (cycle_dir / "hunt.log.jsonl").is_file():
        try:
            from datetime import datetime as _dt, timezone as _tz
            first_ts: str | None = None
            last_ts: str | None = None
            for line in (cycle_dir / "hunt.log.jsonl").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    ev = _json.loads(line)
                except (ValueError, _json.JSONDecodeError):
                    continue
                ts = ev.get("ts")
                # hunt.log.jsonl mostly stores ISO-8601 strings, but a few
                # older entries used epoch floats. Coerce to string only when
                # it's already string-shaped — skip the float entries (the
                # ISO neighbours are enough to bracket the cycle).
                if not isinstance(ts, str) or not ts:
                    continue
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            if first_ts and last_ts:
                t0 = _dt.fromisoformat(first_ts.replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(last_ts.replace("Z", "+00:00"))
                delta = (t1 - t0).total_seconds()
                if delta > 0:
                    elapsed_seconds = delta
        except (ValueError, OSError):
            pass
    # Last-resort fallback: started_at -> latest disk mtime
    if (not elapsed_seconds or elapsed_seconds < 60) and started_at_iso:
        try:
            from datetime import datetime as _dt, timezone as _tz
            t0 = _dt.fromisoformat(started_at_iso.replace("Z", "+00:00"))
            try:
                latest_mt = max(
                    p.stat().st_mtime for p in cycle_dir.rglob("*")
                    if p.is_file()
                )
                t1 = _dt.fromtimestamp(latest_mt, tz=_tz.utc)
            except (OSError, ValueError):
                t1 = _dt.now(tz=_tz.utc)
            delta = (t1 - t0).total_seconds()
            if delta > 0:
                elapsed_seconds = delta
        except (ValueError, OSError):
            pass

    counts = {"STRONG": 0, "SOFT": 0, "FALSE": 0, "LOST": 0}
    clusters: set[str] = set()
    if triage_jsonl.is_file():
        try:
            for line in triage_jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = _json.loads(line)
                except ValueError:
                    continue
                cls = r.get("classification") or ""
                if cls in counts:
                    counts[cls] += 1
                if cls == "STRONG" and r.get("cluster_id"):
                    clusters.add(r["cluster_id"])
        except OSError:
            pass

    # Count anything that's progressed to a publishable lifecycle state
    # (confirmed → disclosed → fixed → verified). The funnel cell labels
    # this "Confirmed · reach this report" — any of those four states
    # have reached the public surface for THIS cycle.
    _funnel_states = {"confirmed", "disclosed", "fixed", "verified"}
    n_confirmed = sum(
        1 for f in findings
        if (f.get("status") or "") in _funnel_states
        and (f.get("cycle_id") or "") == cycle_id
    )

    # Where did the non-firing hypotheses go? Walk findings DB by status
    # for hypotheses in this cycle that didn't fire. This gives the reader
    # a complete accounting of the "24 → 7 fires" delta.
    non_fire_breakdown: dict[str, int] = {}
    for f in findings:
        if (f.get("cycle_id") or "") != cycle_id:
            continue
        if f.get("poc_fired"):
            continue
        st = f.get("status") or "unknown"
        non_fire_breakdown[st] = non_fire_breakdown.get(st, 0) + 1

    def _cell(label: str, value: str, note: str = "") -> str:
        note_html = (
            f'<div style="font-size:10px;color:var(--text-3);margin-top:4px;'
            f'font-family:var(--mono);letter-spacing:.06em">{html.escape(note)}</div>'
            if note else ""
        )
        return (
            f'<div style="flex:1 1 0;text-align:center;padding:14px 10px;'
            f'border:1px solid var(--rule);border-radius:6px;background:var(--surface)">'
            f'<div style="font-size:11px;color:var(--text-3);'
            f'text-transform:uppercase;letter-spacing:.18em;font-family:var(--mono)">'
            f'{html.escape(label)}</div>'
            f'<div style="font-size:26px;font-weight:700;color:var(--ink);'
            f'margin-top:6px;font-variant-numeric:tabular-nums">{html.escape(str(value))}</div>'
            f'{note_html}</div>'
        )

    def _arrow() -> str:
        return (
            '<div style="display:flex;align-items:center;justify-content:center;'
            'padding:0 4px;color:var(--ink-3);font-size:18px;'
            'font-family:var(--mono)">&rarr;</div>'
        )

    # Bundle-verification override: when the L2.5 triage judge mis-classifies
    # a real bug (typical Solidity / forge case where the LLM judge can't
    # parse forge JSON output and defaults to FALSE), but the bundle's
    # 5-gate verifier later confirmed the patch defuses the bug, the bundle
    # evidence supersedes the L2.5 verdict. Use n_confirmed as the floor
    # for the STRONG cell and add an explanatory annotation so the reader
    # understands the discrepancy.
    strong_display = max(counts["STRONG"], n_confirmed)
    override_n = max(0, n_confirmed - counts["STRONG"])
    if override_n > 0:
        triage_note = (
            f"STRONG {counts['STRONG']} · SOFT {counts['SOFT']} · "
            f"FALSE {counts['FALSE']} · LOST {counts['LOST']} "
            f"(+ {override_n} promoted on bundle-verifier evidence)"
        )
    else:
        triage_note = (
            f"STRONG {counts['STRONG']} · SOFT {counts['SOFT']} · "
            f"FALSE {counts['FALSE']} · LOST {counts['LOST']}"
        )
    fires_note = (
        f"{counts['STRONG'] + counts['SOFT'] + counts['FALSE'] + counts['LOST']} "
        "fires triaged"
    ) if any(counts.values()) else "test aborted in target module"
    funnel = (
        '<div style="display:flex;gap:0;margin:20px 0 12px;align-items:stretch">'
        + _cell("Hypotheses", str(n_hypotheses), "from class library")
        + _arrow()
        + _cell("PoC fires", str(n_fires), fires_note)
        + _arrow()
        + _cell("STRONG", str(strong_display), triage_note)
        + _arrow()
        + _cell(
            "Root causes",
            # Curator's confirmed count takes precedence over triage's raw
            # cluster count. Triage clusters by (engine_function, target_file)
            # at L2.5, but the operator's later disposition (rejected /
            # withdrawn / out-of-scope) can override membership — so when
            # the two diverge, show the curated number to match the §01
            # body and the cover-page summary.
            str(n_confirmed or len(clusters)),
            f"{strong_display} STRONG → {n_confirmed or len(clusters)} after curation",
        )
        + _arrow()
        + _cell("Confirmed", str(n_confirmed), "reach this report")
        + '</div>'
    )

    # Where the non-firing hypotheses went
    non_fire_total = sum(non_fire_breakdown.values())
    non_fire_rows = ""
    if non_fire_total:
        ordered = sorted(non_fire_breakdown.items(), key=lambda x: -x[1])
        noun = "hypothesis" if non_fire_total == 1 else "hypotheses"
        verb = "was" if non_fire_total == 1 else "were"
        non_fire_rows = (
            '<p style="color:var(--text-3);font-size:11.5px;margin:6px 0 14px;'
            'line-height:1.55">'
            '<strong style="color:var(--text-3);'
            'font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;'
            'font-family:var(--mono);margin-right:8px">Non-fire accounting</strong>'
            + (
                f"{non_fire_total} {noun} {verb} tested but the PoC did not fire — "
                + ", ".join(
                    f'{c}× <code>{html.escape(st)}</code>' for st, c in ordered
                ) + ". "
                "These are hypotheses where Layer 1 / Layer 1.5 returned a verdict "
                "but the Layer 2 PoC author either declined to produce a test "
                "(no plausible attack) or the test ran without an abort in the "
                "target module."
            )
            + '</p>'
        )

    # Wall-clock pill row (cost intentionally omitted)
    cost_elapsed = ""
    if elapsed_seconds is not None and elapsed_seconds >= 1:
        hours = int(elapsed_seconds // 3600)
        mins = int((elapsed_seconds % 3600) // 60)
        secs = int(elapsed_seconds % 60)
        if hours > 0:
            elapsed_str = f"{hours}h {mins}m {secs}s"
        else:
            elapsed_str = f"{mins}m {secs}s"
        cost_elapsed = (
            '<p style="color:var(--text-3);font-size:11.5px;margin:6px 0 14px;'
            'font-family:var(--mono);letter-spacing:.06em">'
            '<strong style="color:var(--text-3);font-size:10.5px;'
            'letter-spacing:.12em;text-transform:uppercase;margin-right:8px">'
            'Cycle wall-clock</strong>'
            f'<code>{elapsed_str}</code>'
            '</p>'
        )

    caption = (
        '<p style="color:var(--text-3);font-size:11px;margin:4px 0 18px;'
        'font-family:var(--mono);letter-spacing:.04em">'
        '&sect; B.1 &mdash; Cycle funnel. Hypotheses tested &rarr; PoC fires '
        '&rarr; Layer 2.5 judge filters out artifactual / mis-invariant fires '
        '&rarr; surviving STRONG fires cluster by code site '
        '&rarr; cluster representatives become published findings.'
        '</p>'
    )
    return funnel + non_fire_rows + cost_elapsed + caption


def _aptos_layer_results_from_log(
    workspace: Path, cycle_id: str,
) -> dict[str, dict[str, dict]]:
    """Walk hunt.log.jsonl and collect the LATEST L3/L4 adapter result per hyp.

    Returns ``{ "l3": { hyp_id: {...} }, "l4": { hyp_id: {...} } }``.
    Later events overwrite earlier ones, so a hyp that was re-fired keeps
    its newest verdict. Used by the Aptos branch of ``_findings_writeup``.
    """
    import json as _json
    out: dict[str, dict[str, dict]] = {"l3": {}, "l4": {}}
    log = workspace / "hunts" / cycle_id / "hunt.log.jsonl"
    if not log.is_file():
        return out
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                ev = _json.loads(line)
            except Exception:
                continue
            event = ev.get("event") or ""
            hyp = ev.get("hypothesis_id") or ""
            if not hyp:
                continue
            if event == "l3_adapter_done":
                out["l3"][hyp] = ev
            elif event == "l4_adapter_done":
                out["l4"][hyp] = ev
    except OSError:
        pass
    return out


def _findings_writeup(
    findings: list[dict],
    workspace: Path | None,
    cycle_id: str,
) -> str:
    """OtterSec-style per-finding analysis. For each finding renders a
    full section with description, impact, root cause, code excerpts from
    the L2 PoC, L3 (Kani / Move Prover) status, L4 (BPF / aptos-move-test)
    reproduction status with witness, P3 patch diff, and verification
    gates.

    Branches on protocol language:
      * ``solana`` — original Kani / LiteSVM paths
      * ``aptos``  — Move Prover spec + property-fuzz + Move PoC paths
    """
    if not findings or not workspace:
        return ""
    import json as _json

    language = _detect_language(workspace, cycle_id)
    cycle_dir = workspace / "hunts" / cycle_id
    poc_dir = cycle_dir / "poc"
    litesvm_dir = cycle_dir / "litesvm"
    # Bundles moved from the legacy ``workspace/recon/bundles/`` (the
    # Percolator-era P3 location) to the per-cycle
    # ``hunts/<cycle>/bundles/`` slot as of 2026-05-15. Prefer the
    # per-cycle path when it exists; fall back to the legacy location
    # for cycles that haven't been remastered.
    _hunt_bundles = cycle_dir / "bundles"
    bundles_dir = _hunt_bundles if _hunt_bundles.is_dir() else (
        workspace / "recon" / "bundles"
    )
    # Aptos workspaces keep the PoC source under workspace/tests/aptos/
    # (not the per-cycle poc/ dir, which only stores runlogs). Resolve once.
    aptos_tests_dir = workspace / "tests" / "aptos"
    aptos_specs_dir = workspace / "formal" / "aptos"
    aptos_fuzz_dir = workspace / "fuzz" / "aptos"
    # Solidity workspaces mirror the layout under */solidity/
    solidity_tests_dir = workspace / "tests" / "solidity"
    solidity_formal_dir = workspace / "formal" / "solidity"
    solidity_fuzz_dir = workspace / "fuzz" / "solidity"
    aptos_layer_results = (
        _aptos_layer_results_from_log(workspace, cycle_id)
        if language in ("aptos", "solidity") else {"l3": {}, "l4": {}}
    )

    # Full claims from the hypothesis library (DB stores claim[:120]
    # truncated; we want the full prose for the Invariant block).
    hyp_library = _load_hypothesis_library(workspace)
    # Build a hyp_id → (cluster_id, [member_ids]) map from triage.jsonl
    # so cluster representatives can render the duplicate-coverage chip.
    triage_clusters = _load_triage_clusters(workspace, cycle_id)

    # Per-finding analysis is reserved for findings that have material
    # signal (confirmed PoC, or already moved through the disclosure
    # pipeline). `new` / `triaged` findings are unreviewed LLM verdicts
    # that should not appear in a customer-facing per-finding writeup
    # — they'd dilute the report with hallucinations that the cycle's
    # confirmation step didn't promote. Statuses kept: REAL_STATUSES.
    real_findings = [f for f in findings if (f.get("status") or "") in REAL_STATUSES]
    sev_order = {s.value: i for i, s in enumerate(Severity)}
    sorted_findings = sorted(
        real_findings,
        key=lambda x: sev_order.get(x.get("severity", "Info"), 99),
    )

    sections = []
    for idx, f in enumerate(sorted_findings, 1):
        hyp_id = f.get("hypothesis_id") or "?"
        hyp_slug = hyp_id.lower().replace("-", "_")
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        # Prefer the YAML's untruncated `claim:` for the invariant block;
        # fall back to the DB title when the cycle ran against a
        # hypothesis library that's no longer on disk.
        hyp_yaml = hyp_library.get(hyp_id, {})
        full_claim = (
            (hyp_yaml.get("claim") or "").strip()
            or (f.get("title") or hyp_id).strip()
        )
        # Collapse internal whitespace (YAML folded-style block keeps
        # single-space joins, but residual newlines confuse downstream
        # consumers).
        full_claim = re.sub(r"\s+", " ", full_claim)

        bug_class = f.get("bug_class") or "unknown"
        finding_id = f.get("id")
        cluster_members = triage_clusters.get(hyp_id, [])

        # Heading title: a short, descriptive, COMPLETE phrase derived
        # from bug_class + engine_function via the curated template
        # table. No longer the truncated-with-ellipsis first sentence of
        # the claim — that was redundant with the Invariant block below
        # and read as a broken thought.
        engine_function = (hyp_yaml.get("engine_function") or "").strip()
        title = _short_finding_title(
            bug_class=bug_class,
            engine_function=engine_function,
            hypothesis_id=hyp_id,
            hyp_yaml=hyp_yaml,
            db_title=f.get("title"),
        )

        # ── L2 PoC excerpt (language-dependent file extension + body shape) ──
        l2_excerpt = ""
        l2_lang = "rust"

        def _cap_with_firing_tail(body: str, head: int = 16, tail: int = 12) -> str:
            """Cap the PoC body but ALWAYS preserve the firing assertion.

            The naive cap-at-N rule cuts before the `assert!(...)` that
            actually fires the bug, leaving the reader to infer the
            exploit from setup alone. We split into head + tail so the
            reader sees the attack setup AND the firing assertion, with
            a marker between them.

            Multi-line `assert!(\\n   x == y,\\n   E_BUG_HIT\\n);` shapes
            are common in Move — we extend the tail past the firing
            line through the next matching `);` so the full statement
            survives.

            If the body fits in head+tail lines, return it as-is.
            """
            parts = body.splitlines()
            if len(parts) <= head + tail:
                return body
            # Find the LAST assert!() or abort opener. Then walk forward
            # to find its closing `);` so multi-line asserts survive.
            firing_idx = -1
            for i, ln in enumerate(parts):
                s = ln.lstrip()
                if s.startswith(("assert!", "abort ", "abort(")):
                    firing_idx = i
            if firing_idx < 0:
                firing_idx = len(parts) - 1
            # Extend through the next `);` line if the firing line itself
            # didn't end the statement.
            end_idx = firing_idx
            firing_line = parts[firing_idx].rstrip()
            if not firing_line.endswith((");", ");//", "); ")):
                for j in range(firing_idx + 1, min(firing_idx + 12, len(parts))):
                    end_idx = j
                    if parts[j].rstrip().endswith((");", ");//", "); ")):
                        break
            tail_start = max(head + 1, end_idx - tail + 2)
            tail_end = min(len(parts), end_idx + 2)
            head_part = parts[:head]
            tail_part = parts[tail_start:tail_end]
            return (
                "\n".join(head_part)
                + "\n        // …setup truncated for brevity…\n"
                + "\n".join(tail_part)
            )

        if language == "aptos":
            # Move PoCs live at workspace/tests/aptos/test_<slug>.move
            move_poc = aptos_tests_dir / f"test_{hyp_slug}.move"
            if move_poc.is_file():
                try:
                    pt = move_poc.read_text(encoding="utf-8", errors="replace")
                    # Pull the `fun test_<slug>` body. Move tests use `#[test(...)]`
                    # then `fun <name>(<args>) { ... }` — the same shape works.
                    m = re.search(
                        r"(#\[test[^\]]*\]\s*\n\s*fun\s+[A-Za-z0-9_]+\s*[^\{]*\{.*?\n\s*\})",
                        pt, re.DOTALL,
                    )
                    l2_excerpt = _cap_with_firing_tail(m.group(1) if m else pt)
                except OSError:
                    pass
            l2_lang = "move"  # honest label; Prism may not highlight (no `move` grammar)
        elif language == "solidity":
            sol_poc = solidity_tests_dir / f"test_{hyp_slug}.t.sol"
            if sol_poc.is_file():
                try:
                    pt = sol_poc.read_text(encoding="utf-8", errors="replace")
                    # Pull the firing test function body. Solidity Foundry tests
                    # use `function test_<name>(...) public { ... }`.
                    m = re.search(
                        r"(function\s+test_[A-Za-z0-9_]+\s*\([^)]*\)\s*public[^\{]*\{.*?\n\s{0,8}\})",
                        pt, re.DOTALL,
                    )
                    l2_excerpt = _cap_with_firing_tail(m.group(1) if m else pt)
                except OSError:
                    pass
            l2_lang = "solidity"
        else:
            # Prefer the Layer 4 LiteSVM test source over the Layer 2 PoC
            # excerpt when present. Rationale: L2 PoCs are LLM-authored against
            # the hypothesis claim and can drift to a different function or
            # leave template placeholders unfilled (sol-medium cycle observed:
            # SOL39 / SOL40 L2 PoCs were unfilled templates; SOL30's L2 PoC
            # cited the wrong file; SOL6's L2 PoC cited vault_router while the
            # patch targets oracle_board). The L4 LiteSVM tests are hand-
            # verified against the patched .so for every confirmed finding
            # (14/14 PATCH_KILLS_EXPLOIT), so they're the authoritative
            # exploit demonstration for the customer report.
            litesvm_path = (cycle_dir / "litesvm" / hyp_slug
                            / f"test_{hyp_slug}_litesvm.rs")
            poc_path = poc_dir / f"test_{hyp_slug}.rs"
            source_text = None
            if litesvm_path.is_file():
                try:
                    source_text = litesvm_path.read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    source_text = None
            if not source_text and poc_path.is_file():
                try:
                    source_text = poc_path.read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    source_text = None
            if source_text:
                # Pull the test function body — match `#[test]\npub? fn test_<slug>`.
                m = re.search(
                    r"(#\[test\][^\n]*\n(?:pub\s+)?fn[^\n]+(?:_fires|_litesvm)[^\{]*\{.*?\n\})",
                    source_text, re.DOTALL,
                )
                if m:
                    l2_excerpt = _cap_with_firing_tail(m.group(1))
                else:
                    l2_excerpt = _cap_with_firing_tail(source_text)

        # ── L3 verification status (Kani for Solana / Move Prover for Aptos / Halmos for Solidity) ──
        l3_status = "—"
        if language == "aptos":
            l3_status = (
                "n/a for Move on this VPS — Boogie/Z3/CVC5 not deployed; "
                "L2 PoC + L4 property test serve as primary evidence"
            )
        elif language == "solidity":
            ev = aptos_layer_results.get("l3", {}).get(hyp_id)
            if ev:
                if ev.get("counterexample") is True:
                    raw_reason = (ev.get("reason") or "").strip()
                    # The reason often contains "Halmos found counterexample: "
                    # followed by raw hex parameter values that can run hundreds
                    # of chars and break mid-number when capped. Pull just the
                    # bug-confirming claim — the full counterexample lives in
                    # the .halmos.log artifact under formal/solidity/.
                    if "Halmos found counterexample" in raw_reason:
                        # Cut at the first concrete value's word boundary —
                        # e.g. "p_caller_address = 0x80000000..." — and keep
                        # at most ~80 chars of the witness so the line breaks
                        # cleanly. The witness is illustrative; the artifact
                        # holds the full SMT model.
                        l3_status = (
                            "✓ Halmos counterexample found "
                            "(bug confirmed by symbolic execution; full SMT "
                            "witness in formal/solidity/halmos_<slug>.log)."
                        )
                    else:
                        cap = raw_reason[:180]
                        l3_status = (
                            "✓ Halmos counterexample found "
                            "(bug confirmed by symbolic execution)."
                            + (f" {cap}" if cap else "")
                        )
                elif ev.get("proved") is True:
                    l3_status = (
                        "Halmos verified the patched invariant within bounded depth "
                        "(no counterexample within solver budget — symbolic safety)."
                    )
                elif ev.get("compile_error"):
                    l3_status = (
                        "Inconclusive — Halmos harness failed to compile "
                        "(L2 PoC + L4 forge fuzz remain the primary signal)."
                    )
                else:
                    reason = (ev.get("reason") or "")[:200]
                    l3_status = (
                        "Halmos inconclusive (timeout / solver budget exhausted)"
                        + (f": {reason}" if reason else "")
                    )
            else:
                l3_status = (
                    "Not run for this hypothesis — L2 PoC + L4 forge fuzz are the primary signal."
                )
        else:
            # Modern adapter writes per-slug subdir; legacy Percolator
            # path lives directly under kani/. Try both.
            kani_log_candidates = (
                cycle_dir / "kani" / hyp_slug / f"proof_{hyp_slug}.log",
                cycle_dir / "kani" / f"cargo_kani_{hyp_slug}_invariant.log",
            )
            kani_log = next(
                (p for p in kani_log_candidates if p.is_file()),
                None,
            )
            if kani_log is not None:
                try:
                    kt = kani_log.read_text(encoding="utf-8", errors="replace")
                    if "Verification:- FAILED" in kt or "VERIFICATION:- FAILED" in kt:
                        l3_status = "✓ Counterexample found (bug confirmed by symbolic execution)"
                    elif "Verification:- SUCCESSFUL" in kt or "VERIFICATION:- SUCCESSFUL" in kt:
                        l3_status = (
                            "Kani's bounded check did not find a counterexample. "
                            "This means the bug requires inputs outside the model's "
                            "depth / size limits (NOT that the bug is absent — "
                            "Layer 2 PoC firing remains authoritative)."
                        )
                    else:
                        l3_status = "Inconclusive (timeout / out of memory)"
                except OSError:
                    pass

        # ── L4 reproduction (LiteSVM/BPF for Solana, aptos-move-test for Aptos, forge fuzz for Solidity) ──
        l4_status = "—"
        l4_witness = ""
        if language == "aptos":
            ev = aptos_layer_results.get("l4", {}).get(hyp_id)
            if not ev:
                l4_status = (
                    "Not run — L4 property-fuzz stage was skipped for this "
                    "hypothesis (L2 PoC is the authoritative bug signal)"
                )
            if ev:
                if ev.get("crash_found") is True and ev.get("n_fail", 0) > 0:
                    l4_status = (
                        "✓ Property fuzz aborted — inverted-assertion fired "
                        "(bug demonstrably reachable from a Move property test)"
                    )
                    l4_witness = (ev.get("reason") or "")[:500]
                elif ev.get("crash_found") is True and ev.get("n_pass", 0) > 0:
                    l4_status = (
                        "✓ Property fuzz ran the attacker scenario end-to-end without abort "
                        "— bug-exploit reproduces cleanly, attacker's predicted gain confirmed"
                    )
                    l4_witness = (ev.get("reason") or "")[:500]
                elif ev.get("compile_error"):
                    l4_status = (
                        "Inconclusive (LLM-authored property test failed to compile; "
                        "L2 PoC remains the authoritative bug signal)"
                    )
                elif ev.get("ran_clean"):
                    l4_status = "Property fuzz ran clean — no PASS/FAIL markers (no signal)"
                else:
                    l4_status = "Inconclusive (Move test runner did not report a parseable verdict)"
        elif language == "solidity":
            ev = aptos_layer_results.get("l4", {}).get(hyp_id)
            if not ev:
                l4_status = (
                    "Not run — L4 forge fuzz / invariant stage was skipped for this "
                    "hypothesis (L2 PoC is the authoritative bug signal)"
                )
            else:
                if ev.get("crash_found") is True:
                    l4_status = (
                        "✓ forge fuzz / invariant fired — bug demonstrably reachable "
                        "from a property-based test."
                    )
                    l4_witness = (ev.get("reason") or "")[:500]
                elif ev.get("compile_error"):
                    l4_status = (
                        "Inconclusive (LLM-authored forge fuzz harness failed to compile; "
                        "L2 PoC remains the authoritative bug signal)"
                    )
                elif ev.get("ran_clean"):
                    l4_status = (
                        "forge fuzz ran clean — no counterexample within budget "
                        "(L2 PoC remains authoritative)."
                    )
                else:
                    reason = (ev.get("reason") or "")[:200]
                    l4_status = (
                        "Inconclusive (forge runner did not report a parseable verdict)"
                        + (f": {reason}" if reason else "")
                    )
        else:
            # Anchor L4 adapter writes per-hyp logs to
            #   hunts/<cycle>/litesvm/<slug>/test_<slug>_litesvm.log  (FINAL)
            #   hunts/<cycle>/litesvm/<slug>/test_<slug>_litesvm.attemptN.log  (per attempt)
            # The legacy Percolator path was hunts/<cycle>/litesvm/cargo_litesvm_<slug>*.log
            # Prefer the FINAL log; only fall back to attempts if the final
            # is absent. The earlier glob `test_*_litesvm*.log` was sorted
            # alphabetically and picked `attempt1.log` first (which is the
            # broken-compile attempt) — its `break` after the conditions
            # meant the FINAL log was never read.
            l4_candidates: list[Path] = []
            final_log = (litesvm_dir / hyp_slug
                         / f"test_{hyp_slug}_litesvm.log")
            if final_log.is_file():
                l4_candidates.append(final_log)
            else:
                l4_candidates.extend(
                    (litesvm_dir / hyp_slug).glob(
                        f"test_{hyp_slug}_litesvm*.log")
                )
            l4_candidates.extend(
                litesvm_dir.glob(f"cargo_litesvm_{hyp_slug}*.log"))
            for litesvm_log in l4_candidates:
                try:
                    lt = litesvm_log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                # Bug-witness phrase set covers the Anchor-isolated L4
                # runner's panic format ("SOL34: exploit tx should have
                # been rejected", "post-patch invariant violated") plus
                # the legacy Percolator "BUG ... CONFIRMED" format.
                _bug_witness = any(
                    p in lt for p in (
                        "post-patch invariant violated",
                        "BUG WITNESS:",
                        "exploit tx should have been rejected",
                        "exploit should have been rejected",
                        "state diverged",
                        "silently capped",
                        "silently returns",
                        "silently set",
                        "writes a SMALLER timestamp",
                        "corrupting time-accounting",
                    )
                ) or ("BUG" in lt and "CONFIRMED" in lt)
                # Also accept ``<HYP_ID>: <something>`` panic-message
                # prefixes — the Anchor LiteSVM author uses
                # ``SOL13: saturating_sub silently capped ...``,
                # ``SOL28: when current_time < last_claim_ts ...``,
                # ``F1: ...`` etc. The hyp_id in the renderer may be
                # the F-finding ID (F1, F4) or the original L2 hyp
                # (SOL13, SOL28) depending on whether the L4 sidecar
                # was symlinked or built directly; recognise both.
                if not _bug_witness and re.search(
                    r"^[A-Z]{1,5}\d+[A-Z0-9-]*:\s+\S",
                    lt,
                    re.MULTILINE,
                ):
                    _bug_witness = True
                _legacy_panic_at = f"panicked at tests/litesvm_{hyp_slug[:40]}" in lt
                _modern_panic_at = (
                    f"panicked at test_{hyp_slug[:60]}" in lt
                )

                if _bug_witness or _legacy_panic_at or _modern_panic_at:
                    l4_status = "✓ Reproduced through deployed BPF instructions"
                    # Pull a witness line — prefer the panic body that
                    # carries the bug-witness phrase, falling back to the
                    # legacy CONFIRMED/FIRES/DETECTED keywords.
                    panic_idx = lt.find("panicked at")
                    if panic_idx >= 0:
                        # The line immediately AFTER `panicked at ...:N:M:`
                        # is the assertion message.
                        nl = lt.find("\n", panic_idx)
                        if nl >= 0:
                            msg_start = nl + 1
                            msg_end = lt.find("\n", msg_start)
                            if msg_end < 0:
                                msg_end = msg_start + 500
                            _raw = lt[msg_start:msg_end].strip()
                            # Drop verbose runtime ``Error: ...`` debug-print tail
                            # if present (some authors append ``Error: {:?}``).
                            _err_split = _raw.find(" Error: ")
                            if _err_split > 80:
                                _raw = _raw[:_err_split].rstrip()
                            l4_witness = _raw[:600]
                    if not l4_witness:
                        for line in lt.splitlines():
                            if "BUG" in line and any(
                                k in line for k in ("CONFIRMED", "FIRES", "DETECTED", "WITNESS")
                            ):
                                l4_witness = line.strip()[:500]
                                break
                elif "test result: ok" in lt:
                    l4_status = (
                        "Not reproduced (wrapper-side defenses caught it OR "
                        "test setup didn't reach buggy state)"
                    )
                else:
                    l4_status = "Inconclusive"
                break

        # P3 patch + verification
        p3_patch = ""
        p3_gates = []
        p3_rationale = ""
        if finding_id:
            bundle_dir = bundles_dir / str(finding_id)
            patch_p = bundle_dir / "patch.diff"
            verif_p = bundle_dir / "verification.json"
            meta_p = bundle_dir / "bundle_meta.json"
            if patch_p.is_file():
                try:
                    raw = patch_p.read_text(encoding="utf-8", errors="replace")
                    parts = raw.splitlines()
                    # Strip the leading shell-command preamble emitted by
                    # ``diff -ruN`` (the first line is a verbatim copy of
                    # the diff invocation, including absolute sandbox
                    # paths that are noise in the customer report). Drop
                    # any line that doesn't start with ``-``, ``+``, ``@@``,
                    # ``---``, ``+++`` or a space (the legal first chars
                    # of a unified diff hunk).
                    _diff_first_char = re.compile(r"^(?:diff\s+[-]ruN|=|\?)\s")
                    _kept_parts: list[str] = []
                    _saw_hunk_header = False
                    for ln in parts:
                        if _diff_first_char.match(ln):
                            continue
                        if not _saw_hunk_header and ln.startswith("diff "):
                            continue
                        # Keep ``---``/``+++`` file headers, ``@@`` hunk
                        # headers, and ``-``/``+``/`` `` body lines.
                        _kept_parts.append(ln)
                        if ln.startswith("@@"):
                            _saw_hunk_header = True
                    parts = _kept_parts
                    # Show the FULL patch in the report. Bundle deliverables
                    # are the whole point — truncating the patch turns the
                    # "fix" section into a teaser. Long-line wrap is handled
                    # by CSS `white-space: pre-wrap` on .code-block below.
                    p3_patch = "\n".join(parts) if parts else raw
                except OSError:
                    pass
            if verif_p.is_file():
                try:
                    v = _json.loads(verif_p.read_text(encoding="utf-8"))
                    for g_name, g_data in v.get("gates", {}).items():
                        passed = g_data.get("passed")
                        icon = "✓" if passed is True else ("✗" if passed is False else "⏭")
                        # Allow up to 400 chars so per-finding gate explanations
                        # don't truncate mid-word in the rendered table.
                        reason = (g_data.get("reason") or "")[:400]
                        # Relabel the row name so the gate description matches the
                        # actual symbolic-verification / BPF tool for non-Solana cycles.
                        if language == "aptos" and g_name == "kani_proof_holds":
                            display_name = "move_prover_proof_holds"
                        elif language == "solidity" and g_name == "kani_proof_holds":
                            display_name = "halmos_proof_holds"
                        elif language == "solidity" and g_name == "litesvm_exploit_neutralized":
                            display_name = "forge_invariant_neutralized"
                        else:
                            display_name = g_name
                        p3_gates.append((icon, display_name, reason))
                except Exception:
                    pass
            if meta_p.is_file():
                try:
                    m = _json.loads(meta_p.read_text(encoding="utf-8"))
                    p3_rationale = (m.get("rationale") or "")[:400]
                except Exception:
                    pass

        # Render — L2 + P3 are the "code spread" half (page B). L3/L4/gates
        # are the "executive summary" half (page A) with bumped font.
        # Header reflects the actual source of the code block: when the L4
        # LiteSVM test is present (Solana) it's substituted in (it's the
        # authoritative exploit reproduction); for Aptos / older cycles the
        # L2 engine PoC is what gets rendered. Naming the header by what's
        # actually shown avoids the mislabeled "Layer 2" content that's
        # really an L4 LiteSVM test.
        litesvm_present = (
            language != "aptos"
            and (cycle_dir / "litesvm" / hyp_slug
                 / f"test_{hyp_slug}_litesvm.rs").is_file()
        )
        l2_header = (
            "Layer 4 — LiteSVM exploit reproduction (test source)"
            if litesvm_present
            else "Layer 2 — Concrete proof of concept (engine-direct)"
        )
        l2_section = (
            f'<h4 class="page-break-before">{l2_header}</h4>'
            f'<pre class="code-block code-tight"><code class="language-{l2_lang}">{html.escape(l2_excerpt)}</code></pre>'
        ) if l2_excerpt else (
            '<h4 class="page-break-before">Layer 2 — Concrete proof of concept</h4>'
            '<p style="color:var(--text-3)">No PoC source on file</p>'
        )

        if language == "aptos":
            l4_label = "Layer 4 — Property fuzz (aptos move test)"
        elif language == "solidity":
            l4_label = "Layer 4 — Forge fuzz / invariant"
        else:
            l4_label = "Layer 4 — On-chain BPF reproduction"
        l4_section = (
            f'<h4>{l4_label}</h4>'
            f'<p class="finding-prose">{html.escape(l4_status)}</p>'
            + (f'<pre class="code-block witness"><code>{html.escape(l4_witness)}</code></pre>' if l4_witness else "")
        )

        gates_section = ""
        if p3_gates:
            n_applicable = sum(1 for icon, _n, _r in p3_gates if icon != "⏭")
            n_total = len(p3_gates)
            n_passed = sum(1 for icon, _n, _r in p3_gates if icon == "✓")
            if n_applicable == n_total:
                lead = (
                    f"Result of running the proposed patch through Jelleo&rsquo;s "
                    f"{n_total}-gate verifier"
                )
            else:
                lead = (
                    f"Result of running the proposed patch through Jelleo&rsquo;s "
                    f"{n_total}-gate verifier "
                    f"({n_applicable} gates applicable for this language, "
                    f"{n_total - n_applicable} marked n/a)"
                )
            rows = "".join(
                f'<tr><td style="text-align:center;width:32px">{html.escape(icon)}</td>'
                f'<td><code>{html.escape(name)}</code></td>'
                f'<td style="color:var(--text-2)">{html.escape(reason)}</td></tr>'
                for icon, name, reason in p3_gates
            )
            gates_section = (
                '<h4>Verification gates &mdash; post-patch machine checks</h4>'
                '<p style="color:var(--text-3);font-size:11.5px;margin:-4px 0 10px">'
                f'{lead}: '
                'syntactic well-formedness, single-function scope, PoC fails pre-patch, '
                'PoC passes post-patch, existing tests still compile/pass.'
                '</p>'
                f'<table class="gates-table">{rows}</table>'
            )

        # Patch section keeps only rationale + diff now — gates promoted out
        p3_section = (
            '<h4>Layer P3 — Proposed structural fix (patch diff)</h4>'
            + (f'<p style="color:var(--text-2)">{html.escape(p3_rationale)}</p>'
               if p3_rationale else "")
            + (f'<pre class="code-block"><code class="language-diff">{html.escape(p3_patch)}</code></pre>' if p3_patch else
               '<p style="color:var(--text-3)">No patch authored yet</p>')
        )

        if language == "aptos":
            l3_label = "Layer 3 — Symbolic verification (Move Prover)"
        elif language == "solidity":
            l3_label = "Layer 3 — Symbolic verification (Halmos)"
        else:
            l3_label = "Layer 3 — Symbolic verification (Kani)"

        # Invariant block — the full untruncated claim from the YAML
        # (no longer relying on DB's claim[:120]).
        description_para = ""
        if full_claim and full_claim != title:
            description_para = (
                f'<p class="finding-prose" style="color:var(--text-2);'
                f'margin:-4px 0 14px;font-size:12.5px;line-height:1.55">'
                f'<strong style="color:var(--text-3);'
                f'font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;'
                f'font-family:var(--mono);margin-right:8px">Invariant</strong>'
                f'{html.escape(full_claim)}</p>'
            )

        # Cluster disclosure chip — if this finding's hyp_id is the
        # cluster representative for >1 STRONG fires, list the duplicate
        # hypothesis IDs it covers. Without this, the reader has no way
        # to know that 4 STRONG fires collapsed to 1 finding.
        cluster_chip = ""
        if len(cluster_members) > 1:
            # Exclude the rep itself from the duplicates list — the
            # cluster_members list ordering is not guaranteed, so we
            # filter by hypothesis_id rather than slicing [1:].
            covered = ", ".join(m for m in cluster_members if m != hyp_id)
            cluster_chip = (
                f'<p class="finding-prose" style="color:var(--text-2);'
                f'margin:-2px 0 14px;font-size:12px;line-height:1.55;'
                f'padding:8px 12px;background:var(--surface);'
                f'border:1px solid var(--rule);border-radius:4px">'
                f'<strong style="color:var(--text-3);'
                f'font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;'
                f'font-family:var(--mono);margin-right:8px">Cluster</strong>'
                f'This finding represents {len(cluster_members)} hypotheses that '
                f'converged on the same code-site root cause. The cluster '
                f'representative is <code>{html.escape(hyp_id)}</code>; '
                f'co-occurring duplicates: <code>{html.escape(covered)}</code>. '
                f'Each duplicate produced an independent STRONG-classified PoC fire '
                f'against the same engine function — see §B for the clustering rule.</p>'
            )

        # Impact + Recommendation prose. The bug_class drives a short
        # impact statement so the reader gets the "what does this mean
        # for users / funds" framing OSec / ToB / Zellic reports have.
        impact_text, recommendation_text = _impact_and_recommendation(
            bug_class=bug_class, hyp_yaml=hyp_yaml, severity=sev,
        )
        impact_section = (
            f'<h4>Impact</h4>'
            f'<p class="finding-prose">{html.escape(impact_text)}</p>'
        ) if impact_text else ""
        recommendation_section = (
            f'<h4>Recommendation</h4>'
            f'<p class="finding-prose">{html.escape(recommendation_text)}</p>'
        ) if recommendation_text else ""

        # Move Prover counterexample excerpt (Aptos only). Surfaces the
        # actual state assignment that violated the spec, not just the
        # one-line verdict.
        l3_excerpt = ""
        if language == "aptos":
            l3_excerpt = _aptos_counterexample_excerpt(workspace, cycle_id, hyp_slug)
        l3_excerpt_block = (
            f'<pre class="code-block code-tight"><code>{html.escape(l3_excerpt)}</code></pre>'
            if l3_excerpt else ""
        )

        sections.append(f"""
        <section class="finding" id="finding-{idx:02d}">
          <div class="finding-banner">
            <div class="finding-banner-num">FINDING {idx:02d} <span class="muted">/ {len(sorted_findings)}</span></div>
            <div class="finding-banner-meta">
              <span class="sev {sev_cls}">{sev.value}</span>
              <code class="finding-hyp">{html.escape(hyp_id)}</code>
              <code class="finding-class">{html.escape(bug_class)}</code>
            </div>
          </div>
          <h3 class="finding-title">{_render_inline_backticks(title)}</h3>
          {description_para}
          {cluster_chip}

          {impact_section}

          <h4>{l3_label}</h4>
          <p class="finding-prose">{html.escape(l3_status)}</p>
          {l3_excerpt_block}

          {l4_section}

          {recommendation_section}

          {gates_section}

          {l2_section}

          {p3_section}
        </section>
        """)

    return "".join(sections)


def _fix_bundle_section(
    workspace: Path,
    findings: list[dict],
    public: bool,
) -> str:
    """P3 Item 13: cycle-scoped fix-bundle activity block.

    --public mode: counters only (matches pre-disclosure rule).
    --full mode: per-finding table with bundle status + verification + authz.
    """
    import json as _json
    finding_ids = {f.get("id") for f in findings if f.get("id") is not None}
    if not finding_ids:
        return ""

    bdir = workspace / "recon" / "bundles"
    if not bdir.is_dir():
        return ""

    # Use the same short-title generator as the per-finding heading + TOC
    # so the §03 table reads "Missing auth check on transfer_admin" rather
    # than the DB-truncated claim "Every privileged capability with `store`
    # ability is given out under a finite, intentional".
    hyp_library_for_bundles = _load_hypothesis_library(workspace)

    rows: list[dict] = []
    counts: dict[str, int] = {}
    for f in findings:
        fid = f.get("id")
        if fid is None:
            continue
        mp = bdir / str(fid) / "meta.json"
        if not mp.is_file():
            continue
        try:
            m = _json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        status = m.get("status") or "drafted"
        counts[status] = counts.get(status, 0) + 1
        gates_passed = ""
        vp = bdir / str(fid) / "verification.json"
        if vp.is_file():
            try:
                v = _json.loads(vp.read_text(encoding="utf-8"))
                n_pass = sum(1 for g in (v.get("gates") or {}).values() if g.get("passed") is True)
                n_total = len(v.get("gates") or {})
                gates_passed = f"{n_pass}/{n_total}"
            except Exception:
                pass
        ap = bdir / str(fid) / "authorization.json"
        hid = f.get("hypothesis_id") or ""
        bc = f.get("bug_class") or ""
        hyp_yaml_row = hyp_library_for_bundles.get(hid, {}) if hid else {}
        ef = (hyp_yaml_row.get("engine_function") or "").strip()
        short = _short_finding_title(
            db_title=f.get("title") if isinstance(f, dict) else None,
            bug_class=bc, engine_function=ef,
            hypothesis_id=hid, hyp_yaml=hyp_yaml_row,
        )
        # Strip backticks for plain-text cell rendering; the function
        # name still reads inline ("Missing auth check on transfer_admin").
        rows.append({
            "id":       fid,
            "title":    short.replace("`", ""),
            "hyp":      hid,
            "status":   status,
            "gates":    gates_passed,
            "authorized": ap.is_file(),
        })

    if not rows:
        return ""

    counter_html = (
        '<div class="kpi-grid">'
        f'<div class="kpi"><div class="label">Findings with bundle</div>'
        f'<div class="value">{len(rows)}</div></div>'
        f'<div class="kpi"><div class="label">Verified</div>'
        f'<div class="value">{counts.get("verified", 0) + counts.get("authorized", 0) + counts.get("pr-opened", 0) + counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        f'<div class="kpi"><div class="label">Authorized</div>'
        f'<div class="value">{counts.get("authorized", 0) + counts.get("pr-opened", 0) + counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        f'<div class="kpi"><div class="label">PRs opened</div>'
        f'<div class="value">{counts.get("pr-opened", 0) + counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        f'<div class="kpi"><div class="label">Merged</div>'
        f'<div class="value">{counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        '</div>'
    )

    if public:
        return f"""
  <h2>03 &mdash; Fix-bundle activity</h2>
  <p style="color:var(--text-2)">
    Confirmed findings flow into the fix-bundle pipeline: LLM-drafted patch +
    machine verification + operator-typed authorization + upstream PR. Per-
    finding detail is suppressed in public reports (pre-disclosure rule).
    The engine never auto-opens upstream PRs — every PR Jelleo opens was
    authorized by the operator personally.
  </p>
  {counter_html}
"""

    # Build a hyp_id → finding_status map so we can annotate each row
    # with the FINDING-level state (confirmed / triaged / new), not just
    # the bundle-level state. Without this the reader sees seven rows of
    # `status: drafted` and can't tell which bundles back the published
    # findings vs which back triaged duplicates vs which back FALSE-
    # classified fires.
    finding_status_by_hyp: dict[str, str] = {}
    for f in findings:
        hid = f.get("hypothesis_id") or ""
        if hid:
            finding_status_by_hyp[hid] = (f.get("status") or "?")

    # Pull triage classification from triage.jsonl so we can label each
    # row's role: cluster-rep / duplicate / FALSE / SOFT.
    triage_classification: dict[str, str] = {}
    triage_clusters_local: dict[str, list[str]] = {}
    cycle_id_for_triage = ""
    # Best-effort cycle_id extraction from the first finding row (all share
    # the cycle in cycle_report mode).
    for f in findings:
        if f.get("cycle_id"):
            cycle_id_for_triage = str(f["cycle_id"])
            break
    if cycle_id_for_triage:
        triage_jsonl = workspace / "hunts" / cycle_id_for_triage / "triage.jsonl"
        if triage_jsonl.is_file():
            by_cluster: dict[str, list[str]] = {}
            try:
                for line in triage_jsonl.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rrec = _json.loads(line)
                    except ValueError:
                        continue
                    hid = rrec.get("hyp_id")
                    cls = rrec.get("classification")
                    cid = rrec.get("cluster_id")
                    if hid and cls:
                        triage_classification[hid] = cls
                    if cls == "STRONG" and cid and hid:
                        by_cluster.setdefault(cid, []).append(hid)
            except OSError:
                pass
            triage_clusters_local = by_cluster

    def _role_for(hyp_id: str, finding_status: str) -> str:
        # Curator override: when an operator has explicitly rejected a finding
        # (false-positive, out-of-scope, withdrawn), that supersedes whatever
        # Layer 2.5 triage said about it. Without this, a STRONG-triaged
        # finding that was later withdrawn keeps showing as 'confirmed' in the
        # bundle activity table, which contradicts the curated DB state.
        if finding_status == "rejected":
            return "rejected"
        # Verification override: when the bundle's 5-gate verifier passed
        # (poc_fails_pre_patch + poc_passes_post_patch + tests_pass_post_patch)
        # AND the finding's lifecycle status is in REAL_STATUSES (confirmed /
        # disclosed / fixed / verified), the triage classification is stale —
        # the empirical patch-fuses-bug evidence supersedes the L2.5 LLM
        # judge. Without this, a fully-verified bug that L2.5 mis-classified
        # as FALSE renders as "FALSE — artifactual fire" in §03 while
        # appearing as a confirmed bug in §01, which is internally
        # contradictory and torches the report's credibility.
        if finding_status in REAL_STATUSES:
            return "confirmed"
        cls = triage_classification.get(hyp_id, "")
        if cls == "STRONG":
            # Is it the cluster representative?
            for cid, members in triage_clusters_local.items():
                if hyp_id in members:
                    if hyp_id == cid:
                        if len(members) > 1:
                            return f"cluster rep ({len(members)} hyps)"
                        return "confirmed"
                    # Member but not the named rep. If the named rep was
                    # rejected/withdrawn (not in confirmed findings), THIS
                    # member is the surviving rep — promote to cluster rep
                    # rather than rendering as a misleading "duplicate of
                    # <rejected_id>" that contradicts §01.
                    rep_status = finding_status_by_hyp.get(cid, "")
                    if rep_status in ("rejected", ""):
                        if len(members) > 1:
                            return f"cluster rep ({len(members)} hyps)"
                        return "confirmed"
                    return f"duplicate of {cid}"
        if cls == "SOFT":
            return "SOFT — wrong invariant"
        if cls == "FALSE":
            return "FALSE — artifactual fire"
        if cls == "LOST":
            return "LOST — couldn't classify"
        return finding_status or "—"

    # Sort: confirmed reps first, then dups, then SOFT/FALSE last.
    def _sort_key(r):
        role = _role_for(r["hyp"], finding_status_by_hyp.get(r["hyp"], ""))
        if role.startswith("cluster rep") or role == "confirmed":
            return (0, r["hyp"])
        if role.startswith("duplicate"):
            return (1, r["hyp"])
        return (2, r["hyp"])

    body_rows: list[str] = []
    for r in sorted(rows, key=_sort_key):
        authz_mark = "&#x2713;" if r["authorized"] else "&middot;"
        finding_status = finding_status_by_hyp.get(r["hyp"], "")
        role = _role_for(r["hyp"], finding_status)
        role_color = {
            "confirmed": "var(--critical)",
        }.get(role.split(" ")[0], "var(--text-3)")
        # Special-case the visual emphasis for the "cluster rep" / plain
        # "confirmed" rows so they stand out from triaged duplicates.
        if role.startswith("cluster rep") or role == "confirmed":
            role_color = "var(--critical)"
        body_rows.append(
            f"<tr>"
            f"<td><code>{r['id']}</code></td>"
            f"<td><code>{html.escape(r['hyp'])}</code></td>"
            f"<td style=\"color:var(--text-2)\">{html.escape(r['title'])}</td>"
            f"<td style=\"color:{role_color};font-family:var(--mono);font-size:11.5px\">"
            f"{html.escape(role)}</td>"
            f"<td><code>{html.escape(r['status'])}</code></td>"
            f"<td style=\"text-align:right\">{html.escape(r['gates'])}</td>"
            f"<td style=\"text-align:center\">{authz_mark}</td>"
            f"</tr>"
        )
    table = (
        '<table><thead><tr>'
        '<th>id</th><th>hypothesis</th><th>title</th>'
        '<th>role</th><th>bundle status</th>'
        '<th style="text-align:right">gates</th>'
        '<th style="text-align:center">authz</th>'
        '</tr></thead><tbody>' + "".join(body_rows) + '</tbody></table>'
    )

    # Replace the original "Findings with bundle" headline — it's
    # misleading when triaged duplicates + FALSE fires also get bundles.
    confirmed_with_bundle = sum(
        1 for r in rows
        if (finding_status_by_hyp.get(r["hyp"]) or "") == "confirmed"
    )
    advisory_with_bundle = len(rows) - confirmed_with_bundle
    refined_counters = (
        '<div class="kpi-grid">'
        f'<div class="kpi"><div class="label">Confirmed-finding bundles</div>'
        f'<div class="value">{confirmed_with_bundle}</div></div>'
        f'<div class="kpi"><div class="label">Advisory bundles</div>'
        f'<div class="value">{advisory_with_bundle}</div>'
        f'<div class="delta">duplicates + SOFT + FALSE retained for audit trail</div></div>'
        f'<div class="kpi"><div class="label">Verified</div>'
        f'<div class="value">{counts.get("verified", 0) + counts.get("authorized", 0) + counts.get("pr-opened", 0) + counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        f'<div class="kpi"><div class="label">Authorized</div>'
        f'<div class="value">{counts.get("authorized", 0) + counts.get("pr-opened", 0) + counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        f'<div class="kpi"><div class="label">Merged</div>'
        f'<div class="value">{counts.get("merged", 0) + counts.get("fixed", 0)}</div></div>'
        '</div>'
    )

    return f"""
  <h2>03 &mdash; Fix-bundle activity</h2>
  <p style="color:var(--text-2)">
    Per-finding fix-bundle pipeline state. Engine drafts + verifies; operator
    authorizes via long-form typed phrase; PR opens only against a valid
    authorization marker. The table includes bundles for confirmed findings
    AND for triaged duplicates / SOFT / FALSE fires — the latter are retained
    as audit-trail evidence of every PoC the hunt loop landed against the
    target, NOT as published findings (see Layer 2.5 gating in §B).
  </p>
  {refined_counters}
  {table}
"""


def _propagation_section(
    workspace: Path,
    findings: list[dict],
    public: bool,
) -> str:
    """P2 H29: cycle-scoped propagation activity block.

    Counts what propagation work fired for findings in *this* cycle:
      * sibling YAMLs derived (one per confirmed finding)
      * cross-protocol propagation reports written
      * Layer-1 dispatches queued
      * chain pages rendered

    --public mode shows counters only (no finding IDs / titles, since
    confirmed-but-not-disclosed leakage is forbidden in public reports).
    --full mode lists each finding with its propagation footprint.
    """
    finding_ids = {f.get("id") for f in findings if f.get("id") is not None}
    if not finding_ids:
        return ""

    derived_dir = workspace / "derived"
    autofire_dir = workspace / "recon" / "propagate" / "auto-fire"
    chains_dir = workspace / "recon" / "propagate" / "chains"
    queue_dir = workspace / "recon" / "propagate" / "scheduled"

    # Map finding_id → propagation activity
    per_finding: dict[int, dict] = {}
    for f in findings:
        fid = f.get("id")
        if fid is None:
            continue
        slug = (f.get("hypothesis_id") or f"finding-{fid}").replace("/", "-")
        sib_count = 0
        if derived_dir.is_dir():
            sib_path = derived_dir / f"{slug}-siblings.yaml"
            if sib_path.is_file():
                try:
                    import yaml as _y
                    doc = _y.safe_load(sib_path.read_text(encoding="utf-8")) or {}
                    sib_count = len(doc.get("hypotheses") or [])
                except Exception:
                    sib_count = 0
        report_count = 0
        if autofire_dir.is_dir():
            report_count = len(list(autofire_dir.glob(f"propagation_finding_{fid}_*.md")))
        chain_present = chains_dir.is_dir() and (chains_dir / f"{fid}.html").is_file()
        queue_count = 0
        if queue_dir.is_dir():
            queue_count = len(list(queue_dir.glob(f"{fid}-*.json")))
        if sib_count or report_count or chain_present or queue_count:
            per_finding[fid] = {
                "siblings": sib_count, "reports": report_count,
                "chain": chain_present, "queue": queue_count,
            }

    if not per_finding:
        return ""

    total_sibs = sum(v["siblings"] for v in per_finding.values())
    total_reports = sum(v["reports"] for v in per_finding.values())
    total_chains = sum(1 for v in per_finding.values() if v["chain"])
    total_queue = sum(v["queue"] for v in per_finding.values())

    counters_html = (
        '<div class="kpi-grid">'
        f'<div class="kpi"><div class="label">Findings with propagation</div>'
        f'<div class="value">{len(per_finding)}</div></div>'
        f'<div class="kpi"><div class="label">Siblings derived</div>'
        f'<div class="value">{total_sibs}</div></div>'
        f'<div class="kpi"><div class="label">Propagation reports</div>'
        f'<div class="value">{total_reports}</div></div>'
        f'<div class="kpi"><div class="label">Chain pages</div>'
        f'<div class="value">{total_chains}</div></div>'
        f'<div class="kpi"><div class="label">Layer-1 queued</div>'
        f'<div class="value">{total_queue}</div></div>'
        '</div>'
    )

    if public:
        # Public mode: counters only — naming a finding's propagation
        # activity reveals it's confirmed even if it's not disclosed yet.
        return f"""
  <h2>02 &mdash; Propagation activity</h2>
  <p style="color:var(--text-2)">
    Confirmed findings auto-fire two follow-on stages: structural sibling
    derivation (LLM-emitted hypotheses about adjacent invariants) and a
    cross-protocol corpus sweep using regex + AST signatures. Counters below
    are cycle-scoped; per-finding detail is suppressed in public reports
    (pre-disclosure rule).
  </p>
  {counters_html}
"""

    # Full mode: per-finding rows for the customer-private report.
    rows: list[str] = []
    for f in findings:
        fid = f.get("id")
        if fid not in per_finding:
            continue
        v = per_finding[fid]
        title = html.escape((f.get("title") or "")[:90])
        hyp = html.escape(f.get("hypothesis_id") or "")
        rows.append(
            f"<tr>"
            f"<td><code>{fid}</code></td>"
            f"<td><code>{hyp}</code></td>"
            f"<td style=\"color:var(--text-2)\">{title}</td>"
            f"<td style=\"text-align:right\">{v['siblings']}</td>"
            f"<td style=\"text-align:right\">{v['reports']}</td>"
            f"<td style=\"text-align:center\">{'✓' if v['chain'] else ''}</td>"
            f"<td style=\"text-align:right\">{v['queue']}</td>"
            f"</tr>"
        )
    table = (
        '<table><thead><tr>'
        '<th>id</th><th>hypothesis</th><th>title</th>'
        '<th style="text-align:right">siblings</th>'
        '<th style="text-align:right">reports</th>'
        '<th style="text-align:center">chain</th>'
        '<th style="text-align:right">queued</th>'
        '</tr></thead><tbody>'
        + "".join(rows) +
        '</tbody></table>'
    )

    return f"""
  <h2>02 &mdash; Propagation activity</h2>
  <p style="color:var(--text-2)">
    Per-finding propagation footprint for this cycle. Each confirmed finding
    auto-fires sibling derivation + cross-protocol corpus sweep + chain page.
  </p>
  {counters_html}
  {table}
"""


def _render_cycle_html(
    target: dict,
    cycle: dict | None,
    findings: list[dict],
    pubkey_fingerprint: str = "",
    *,
    workspace: Path | None = None,
    public: bool = True,
    draft: bool = True,
) -> str:
    target_name = html.escape(target.get("name", "?"))
    cycle_id = html.escape(cycle.get("cycle_id", "?") if cycle else "?")
    engine_sha = html.escape((cycle.get("engine_sha") or "?")[:10] if cycle else "?")
    wrapper_sha = html.escape((cycle.get("wrapper_sha") or "?")[:10] if cycle else "?")
    started = html.escape(cycle.get("started_at", "?") if cycle else "?")

    # Detect the workspace's protocol language so cover + footer can use
    # the right tagline ("Solana" vs "Aptos"). Reuses the same heuristic
    # as `_findings_writeup` — keeps the report self-consistent.
    cycle_id_raw = cycle.get("cycle_id", "") if cycle else ""
    language = _detect_language(workspace, cycle_id_raw) if workspace else "solana"
    if language == "aptos":
        protocol_label = "Aptos"
    elif language == "solidity":
        protocol_label = "Solidity"
    else:
        protocol_label = "Solana"

    counts = _sev_counts(findings)              # full counts (all statuses)
    real_counts = _real_severity_counts(findings)  # confirmed/disclosed/fixed/verified only
    sb = _status_breakdown(findings)
    n_confirmed = sum(1 for f in findings if f.get("status") == "confirmed")

    # P4 Y0 — load the cycle's Merkle root if a sidecar exists. Surfaced
    # in the cover-page meta and in section A so any reader can recompute
    # against the published DB rows and detect tampering.
    # Defensive: validate the sidecar's cycle_id matches THIS cycle so an
    # accidentally-misplaced merkle.json (operator copy/paste error) doesn't
    # produce a confidently-wrong displayed hash.
    cycle_merkle_root_hex = ""
    if workspace and cycle and cycle.get("cycle_id"):
        try:
            import json as _json
            mp = workspace / "hunts" / cycle["cycle_id"] / "merkle.json"
            if mp.is_file():
                parsed = _json.loads(mp.read_text(encoding="utf-8"))
                if parsed.get("cycle_id") == cycle["cycle_id"]:
                    cycle_merkle_root_hex = parsed.get("merkle_root", "")
        except Exception:
            cycle_merkle_root_hex = ""

    # Status banner reflects real findings only — 50 'new' verdicts
    # shouldn't trigger a "Critical" red status when 0 of them are confirmed.
    if real_counts["Critical"] > 0:
        status_label, status_class = f"{real_counts['Critical']} Critical confirmed · disclosure pending", "critical"
    elif real_counts["High"] > 0:
        status_label, status_class = f"{real_counts['High']} High confirmed · review pending", "warn"
    else:
        status_label, status_class = "Cycle complete · no confirmed Critical/High", "ok"

    # Build a human-readable audit-date label from the cycle's
    # YYYYMMDD-HHMMSS id. Falls back to ``started_at`` if available.
    audit_date_label = ""
    raw_cycle_id = cycle.get("cycle_id", "") if cycle else ""
    if raw_cycle_id and len(raw_cycle_id) >= 8:
        try:
            dt = datetime.strptime(raw_cycle_id[:8], "%Y%m%d")
            audit_date_label = dt.strftime("%B %d, %Y")
        except ValueError:
            pass
    if not audit_date_label and started and started != "?":
        audit_date_label = started[:10]

    # Aptos / Move workspaces don't have a separate wrapper repo, so the
    # wrapper SHA equals the engine SHA. Hide the duplicate row.
    show_wrapper = language == "solana"

    cover = cover_page_html(
        target_name=target_name,
        report_title="",  # title now reads as just the target name (cleaner)
        window_label=f"cycle {cycle_id}",
        cycle_id=cycle_id,
        engine_sha=engine_sha,
        wrapper_sha=wrapper_sha,
        severity_counts=real_counts,        # real findings only on the headline
        status_breakdown=sb,                # full pipeline state context
        pubkey_fingerprint=pubkey_fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        protocol_label=protocol_label,
        audit_date_label=audit_date_label,
        show_wrapper_sha=show_wrapper,
        draft=draft,
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · {target_name} · cycle {cycle_id}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
<script>
  window.addEventListener('DOMContentLoaded', function () {{
    if (window.Prism && Prism.plugins && Prism.plugins.autoloader) {{
      Prism.plugins.autoloader.languages_path =
        'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/';
    }}
  }});
</script>
<style>{CSS}
/* jelleo.com nav parity — print stylesheet hides this so PDF output stays clean */
.jelleo-topnav {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 32px;
  border-bottom: 1px solid rgba(245,243,237,0.08);
  background: rgba(5,5,4,0.85);
  backdrop-filter: blur(10px);
  font-family: 'Inter', -apple-system, sans-serif;
  position: sticky; top: 0; z-index: 50;
}}
.jelleo-topnav a {{ text-decoration: none; }}
.jelleo-topnav .logo {{
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600; font-size: 14px; letter-spacing: 0.04em;
  color: #f5f3ed; text-transform: lowercase;
}}
.jelleo-topnav .logo:hover {{ color: #f5b800; }}
.jelleo-topnav .links {{ display: flex; gap: 24px; font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }}
.jelleo-topnav .links a {{ color: rgba(245,243,237,0.46); }}
.jelleo-topnav .links a:hover {{ color: #f5f3ed; }}
@media (max-width: 640px) {{ .jelleo-topnav .links {{ display: none; }} }}
@media print {{ .jelleo-topnav {{ display: none !important; }} }}

/* ============================== FINDING SECTIONS ============================== */
section.finding {{
  margin: 56px 0 0;
  padding: 0 0 36px;
  border-bottom: 1px solid var(--rule);
}}
.finding-banner {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
  background: linear-gradient(90deg, rgba(245,184,0,0.10), rgba(245,184,0,0.02) 50%, transparent);
  border-left: 4px solid var(--amber);
  padding: 16px 22px;
  margin: 0 0 18px -4px;
  border-radius: 0 6px 6px 0;
}}
.finding-banner-num {{
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 22px;
  letter-spacing: 0.18em;
  color: var(--amber);
  text-shadow: 0 0 12px rgba(245,184,0,0.35);
}}
.finding-banner-num .muted {{ color: rgba(245,184,0,0.45); font-weight: 500; font-size: 18px; }}
.finding-banner-meta {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.finding-hyp {{ color: var(--amber) !important; border-color: rgba(245,184,0,0.3) !important; font-weight: 600; }}
.finding-class {{ color: var(--ink-3) !important; font-size: 11px !important; }}
.finding-title {{
  font-size: 19px;
  line-height: 1.4;
  margin: 12px 0 24px;
  color: var(--ink);
  font-weight: 600;
}}
section.finding h4 {{
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--amber);
  margin: 18px 0 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--rule);
}}
/* "Page A" prose under L3 / L4: bump readability so this half of the
   finding looks complete and intentional, not an afterthought. */
section.finding p.finding-prose {{
  font-size: 14px;
  line-height: 1.55;
  margin: 6px 0 4px;
  color: var(--ink);
}}
/* Hard page break — Layer 2 PoC always begins page B */
section.finding h4.page-break-before {{
  page-break-before: always;
  break-before: page;
}}
/* Code blocks: keep the second-half pages dense; same wrap rules as the
   parent so PoC snippets don't bleed off the right edge in PDF either. */
pre.code-block.code-tight {{
  font-size: 10.5px;
  line-height: 1.45;
  padding: 10px 14px;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
  overflow-x: hidden;
}}

/* Code blocks: wrap long lines so nothing falls off the right edge in
   the PDF (overflow-x: auto worked on screen but the printer can't
   scroll — lines were silently cut off in shipped reports). */
pre.code-block {{
  background: #0d0c0a;
  border: 1px solid rgba(245,184,0,0.18);
  border-radius: 6px;
  padding: 14px 18px;
  margin: 12px 0 18px;
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-wrap: anywhere;
  overflow-x: hidden;
  page-break-inside: auto;
  break-inside: auto;
}}
pre.code-block code {{
  white-space: inherit;
  word-break: inherit;
  overflow-wrap: inherit;
}}
pre.code-block.witness {{
  background: rgba(74,222,128,0.06);
  border-color: rgba(74,222,128,0.32);
  color: #a5e8b8;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: hidden;
}}

/* Diff syntax (line-prefix coloring) */
pre code.language-diff {{ color: var(--ink-2); }}
pre code.language-diff .token.inserted, pre code.language-diff .inserted {{ color: #4ade80; background: rgba(74,222,128,0.09); }}
pre code.language-diff .token.deleted, pre code.language-diff .deleted {{ color: #ef4444; background: rgba(239,68,68,0.09); }}

/* Rust syntax tokens — Prism.js will inject these classes */
pre code.language-rust .token.keyword {{ color: #f5b800; font-weight: 600; }}
pre code.language-rust .token.string  {{ color: #4ade80; }}
pre code.language-rust .token.comment {{ color: rgba(245,243,237,0.42); font-style: italic; }}
pre code.language-rust .token.function {{ color: #ffce4a; }}
pre code.language-rust .token.number {{ color: #60a5fa; }}
pre code.language-rust .token.macro, pre code.language-rust .token.attribute {{ color: #c084fc; }}

/* ============================== TABLE OF CONTENTS ============================== */
.toc {{
  margin: 32px 0 48px;
  padding: 24px 28px;
  background: rgba(245,184,0,0.04);
  border: 1px solid rgba(245,184,0,0.18);
  border-radius: 8px;
}}
@media print {{ .toc {{ page-break-before: always; break-before: page; }} }}
/* Verification gates: dense, top-of-page friendly */
.gates-table {{ font-size: 11.5px; }}
.gates-table td {{ padding: 5px 10px; vertical-align: top; }}
.toc h2 {{ margin-top: 0 !important; border-bottom: none !important; padding-bottom: 0 !important; }}
.toc ol {{ counter-reset: toc; padding-left: 0; list-style: none; column-count: 1; margin: 0; }}
.toc li {{
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 3px 0;
  border-bottom: 1px dotted rgba(245,243,237,0.10);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10.5px;
  line-height: 1.4;
}}
.toc li .toc-num {{ color: var(--amber); font-weight: 600; min-width: 28px; }}
.toc li .toc-sev {{ min-width: 64px; }}
.toc li .toc-sev .sev {{ font-size: 9px !important; padding: 1px 6px !important; }}
.toc li a {{ color: var(--ink); border: none; flex: 1; }}
.toc li .toc-class {{ color: var(--ink-3); font-size: 9.5px; max-width: 200px; text-align: right; }}

/* ============================== PRINT / PAGE LAYOUT ============================== */
@page {{
  size: Letter;
  margin: 0.6in 0.55in;
  @bottom-center {{
    content: "Page " counter(page) " of " counter(pages);
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    color: rgba(245,243,237,0.42);
  }}
  @bottom-right {{
    content: "JELLEO · cycle {cycle_id}";
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    color: rgba(245,184,0,0.55);
  }}
}}
@media print {{
  /* Each finding starts on a fresh page — but only between siblings so the
     first finding doesn't collide with the TOC's natural page break and
     produce an empty page in between. */
  section.finding + section.finding {{ page-break-before: always; break-before: page; }}
  /* The FIRST finding still wants a fresh page (after TOC card) */
  .toc + section.finding,
  .toc ~ section.finding:first-of-type {{
    page-break-before: always; break-before: page;
  }}
  /* Banners and TOC rows must never split. Code blocks may split if they
     exceed a single page — page-break-inside:avoid on a 60-line diff would
     push it forward and leave the prior page mostly empty. */
  .finding-banner, .toc li, .gates-table tr {{
    page-break-inside: avoid;
    break-inside: avoid;
  }}
  pre.code-block.witness {{ page-break-inside: avoid; break-inside: avoid; }}
  /* page-break-inside: auto so a too-tall block can still split rather
     than be pushed to its own page (which produces a 3rd orphan finding
     page). With caps at 24/16 the combined L2+P3 block normally fits. */
  pre.code-block {{ page-break-inside: auto; break-inside: auto; }}
  h2, h3, h4 {{ page-break-after: avoid; break-after: avoid-page; }}
  /* Cover always its own page */
  .cover-page {{ page-break-after: always; break-after: page; }}
}}
</style>
</head><body>

<nav class="jelleo-topnav">
  <a href="https://jelleo.com" class="logo">jelleo</a>
  <div class="links">
    <a href="https://jelleo.com/protocols/">Protocols</a>
    <a href="https://jelleo.com/methodology.html">Methodology</a>
    <a href="https://jelleo.com/cycles/">Cycles</a>
    <a href="https://jelleo.com/status/">Status</a>
    <a href="https://jelleo.com/security.html">Security</a>
  </div>
</nav>

{topbar_html(status_label, status_class, tagline=f"Autonomous {protocol_label} audit")}

{cover}

<div class="shell">

  {_executive_summary_section(target_name, cycle, findings, real_counts, language, protocol_label, workspace=workspace)}

  {_scope_section(workspace, target_name, cycle, language, protocol_label)}

  {_table_of_contents(findings, workspace=workspace)}

  {_findings_writeup(findings, workspace, cycle_id) if workspace else ""}

  {_propagation_section(workspace, findings, public) if workspace else ""}
  {_fix_bundle_section(workspace, findings, public) if workspace else ""}

  <h2>A &mdash; Severity rubric</h2>
  <table style="page-break-inside:avoid; break-inside:avoid;">
    <thead><tr><th style="width:120px">Tier</th><th>Definition</th></tr></thead>
    <tbody>{''.join(
        f'<tr><td><span class="sev {s.value.lower()}">{s.value}</span></td>'
        f'<td style="color:var(--text-2)">{html.escape(DEFINITIONS[s])}</td></tr>'
        for s in Severity
    )}</tbody>
  </table>

  <h2>B &mdash; Methodology</h2>

  <h3 style="font-size:13px;margin:18px 0 6px;color:var(--text-2)">Layer overview</h3>
  <table>
    <thead><tr><th style="width:130px">Layer</th><th>Function</th></tr></thead>
    <tbody>
      <tr><td><code>Layer 1</code></td>
          <td style="color:var(--text-2)">Multi-agent recon. For each hypothesis, parallel LLM agents read the engine source and return a TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE verdict with confidence + per-agent grounding.</td></tr>
      <tr><td><code>Layer 1.5</code></td>
          <td style="color:var(--text-2)">Adversarial debate. Contested verdicts (NEEDS_L2 or split verdicts) are promoted through a single-round attacker / defender debate, with a separate judge resolving the final verdict.</td></tr>
      <tr><td><code>Layer 2</code></td>
          <td style="color:var(--text-2)">Concrete proof-of-concept. An inverted-assertion test is authored in {("Move and run via <code>aptos move test</code>" if language == "aptos" else ("Solidity and run via <code>forge test</code>" if language == "solidity" else "Rust and run via <code>cargo test</code>"))}. The test &quot;fires&quot; iff an abort with a custom error code originates in the target module (not stdlib / setup).</td></tr>
      <tr><td><code>Layer 2.5</code></td>
          <td style="color:var(--text-2)">Triage. An LLM judge classifies each fire as <code>STRONG</code> (real bug), <code>SOFT</code> (wrong invariant), <code>FALSE</code> (artifactual abort), or <code>LOST</code> (signal missing). STRONG fires are clustered by (engine_function, target_file) so the same code-site bug under multiple hypothesis IDs collapses to one root cause.</td></tr>
      <tr><td><code>Layer 3</code></td>
          <td style="color:var(--text-2)">Symbolic verification. {("Move Prover with Boogie + Z3 / CVC5 backends. The spec asserts the violated invariant; the prover either finds a counterexample (bug confirmed by SMT) or proves the invariant holds within the spec's bounded model." if language == "aptos" else ("Halmos symbolic execution with Z3 backend. An LLM-authored harness encodes the violated invariant as a <code>check_*</code> function; Halmos either finds a concrete counterexample (bug confirmed by SMT) or proves the invariant holds within bounded depth." if language == "solidity" else "Kani-based bounded model checking. The harness asserts the violated invariant; Kani either finds a counterexample within the bounded depth or proves safety."))}</td></tr>
      <tr><td><code>Layer 4</code></td>
          <td style="color:var(--text-2)">{("Property-based fuzzing via <code>aptos move test</code>. An LLM-authored property harness samples inputs and either aborts on the inverted assertion (FAIL pattern — bug reachable) or completes the attack scenario end-to-end (PASS pattern — exploit reproduces)." if language == "aptos" else ("Property-based fuzzing + invariant testing via <code>forge test</code>. An LLM-authored harness uses Foundry's fuzz / invariant runner — either a counterexample fires the inverted assertion (bug reachable) or the harness completes the attack scenario end-to-end." if language == "solidity" else "On-chain BPF reproduction. The Solana program is deployed into LiteSVM and the PoC re-executed through the deployed instructions, confirming the wrapper-side defenses don't catch the bug."))}</td></tr>
      <tr><td><code>Layer P3</code></td>
          <td style="color:var(--text-2)">Fix-bundle pipeline. The LLM authors a structural patch against the confirmed root cause and verifies it through a 6-gate machine check (well-formed diff, single-function scope, PoC fails pre-patch, PoC passes post-patch, existing tests still pass, and a language-specific symbolic/runtime check — Kani for Solana, Move Prover for Aptos, Halmos for Solidity). Two gates auto-skip when the language doesn&rsquo;t apply. Operator authorization is required before any upstream PR is opened.</td></tr>
    </tbody>
  </table>

  <h3 style="font-size:13px;margin:24px 0 6px;color:var(--text-2)">Cycle execution</h3>
  <p style="color:var(--text-2)">
    This cycle was produced by Jelleo's continuous, hypothesis-driven {protocol_label} audit loop.
    Every finding originates as a falsifiable invariant claim from a per-protocol
    hypothesis library, dispatched to Layer 1 multi-agent recon, promoted on
    contested verdicts via Layer 1.5 adversarial debate, and confirmed empirically
    through {"a Layer 2 <code>aptos move test</code> proof-of-concept" if language == "aptos" else ("a Layer 2 <code>forge test</code> proof-of-concept" if language == "solidity" else "a Layer 2 <code>cargo test</code> proof-of-concept")}.
    Layer 2.5 triage classifies each fire as
    <code>STRONG</code> / <code>SOFT</code> / <code>FALSE</code> / <code>LOST</code>;
    only STRONG cluster representatives advance to <code>confirmed</code> and
    appear in §01 above. SOFT and STRONG duplicates land in <code>triaged</code>;
    FALSE fires return to <code>new</code>. Lifecycle:
    <code>new &rarr; triaged &rarr; confirmed &rarr; disclosed &rarr; fixed &rarr; verified</code>.
    Every cycle is signed Ed25519 against the platform key — see the cover-page receipt.
  </p>
  {_hunt_funnel_section(workspace, cycle_id, findings) if workspace else ""}

  <h2>C &mdash; Audit artifacts</h2>
  <p style="color:var(--text-2)">
    All cycle artifacts are persisted on disk and verifiable independently of
    this report. The table below lists the canonical paths under the cycle workspace
    so a reviewer can re-execute every layer or recompute the cycle Merkle root.
  </p>
  {_artifact_paths_section(workspace, cycle_id) if workspace else ""}

  <h2>D &mdash; Disclaimers</h2>
  <p style="color:var(--text-2)">
    Findings in this report reflect the state of the engine source at the commit
    hash on the cover page. Subsequent changes to the codebase are not analyzed.
    The report is not a guarantee of code correctness or security: it documents
    invariants that fired (or held) under the hypothesis library applied during
    this cycle. Out-of-scope items are listed in §00.1 (Scope).
  </p>
  <p style="color:var(--text-2)">
    §03 reflects bundle-level state. A row is treated as a confirmed finding when
    the bundle&rsquo;s machine verification gates (PoC fails pre-patch + PoC passes
    post-patch + tests still pass) all hold, even if the Layer 2.5 LLM judge
    initially classified the fire as <code>SOFT</code> / <code>FALSE</code> /
    <code>LOST</code> — the verifier&rsquo;s empirical patch-defuses-bug evidence
    supersedes the judge. Rows that did not reach a confirmed lifecycle state are
    retained in §03 as audit-trail evidence but are not published findings; the
    authoritative set is whatever appears in §01.
  </p>
  <p style="color:var(--text-2)">
    Communication channel: <a href="mailto:security@jelleo.com">security@jelleo.com</a>
    (PGP key on <a href="https://jelleo.com/security.html">jelleo.com/security.html</a>).
    Coordinated disclosure follows the timeline published in our security policy;
    pre-disclosure leak protections are enforced at the report level (the
    <code>--public</code> renderer suppresses confirmed-but-not-disclosed findings).
  </p>

  <p style="color:var(--text-3);font-size:11.5px;margin-top:24px">
    Methodology spec: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/docs/methodology">docs/methodology/</a>
    &middot;
    Live reference: <a href="https://jelleo.com/methodology.html">jelleo.com/methodology.html</a>
    &middot;
    Source: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli">github.com/Copenhagen0x/audit-pipeline-cli</a>
  </p>

  {footer_html(extra=f"Cycle {cycle_id}", protocol_label=protocol_label)}

</div>
</body></html>"""


def _render_weekly_html(
    target: dict, cycles: list[dict], findings: list[dict], days: int,
    pubkey_fingerprint: str = "",
    *,
    workspace: Path | None = None,
    public: bool = True,
) -> str:
    target_name = html.escape(target.get("name", "?"))
    counts = _sev_counts(findings)              # full counts (all statuses)
    real_counts = _real_severity_counts(findings)  # confirmed/disclosed/fixed/verified only
    sb = _status_breakdown(findings)
    total_confirmed = sum(int(c.get("n_confirmed") or 0) for c in cycles)

    if real_counts["Critical"] > 0:
        status_label, status_class = f"{real_counts['Critical']} Critical confirmed", "critical"
    elif real_counts["High"] > 0:
        status_label, status_class = f"{real_counts['High']} High confirmed", "warn"
    else:
        status_label, status_class = f"Active · {days}-day window", "ok"

    # Window label: e.g. "24-hour rollup" / "7-day rollup" / "30-day rollup"
    if days == 1:
        window_label = "24-hour rollup"
        report_title = "24-hour audit ·"
    elif days <= 7:
        window_label = f"{days}-day rollup"
        report_title = f"{days}-day audit ·"
    else:
        window_label = f"{days}-day rollup"
        report_title = "Monthly audit ·"

    most_recent_cycle = sorted(
        cycles, key=lambda x: x.get("started_at") or "", reverse=True
    )[0] if cycles else None

    cover = cover_page_html(
        target_name=target_name,
        report_title=report_title,
        window_label=window_label,
        cycle_id="",
        engine_sha=(most_recent_cycle.get("engine_sha") or "")[:10] if most_recent_cycle else "",
        wrapper_sha=(most_recent_cycle.get("wrapper_sha") or "")[:10] if most_recent_cycle else "",
        severity_counts=real_counts,        # real findings only on the headline
        status_breakdown=sb,                # full pipeline state context
        pubkey_fingerprint=pubkey_fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    cycle_rows = []
    for c in sorted(cycles, key=lambda x: x.get("started_at") or "", reverse=True):
        cycle_rows.append(f"""
        <tr>
          <td><code>{html.escape(c.get('cycle_id', '?'))}</code></td>
          <td class="mono" style="color:var(--text-2)">{html.escape(c.get('started_at', '?'))}</td>
          <td><code>{html.escape((c.get('engine_sha') or '?')[:10])}</code></td>
          <td class="num">{c.get('n_dispatched', 0)}</td>
          <td class="num">{c.get('n_confirmed', 0)}</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JELLEO · {target_name} · {days}-day report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
<script>
  window.addEventListener('DOMContentLoaded', function () {{
    if (window.Prism && Prism.plugins && Prism.plugins.autoloader) {{
      Prism.plugins.autoloader.languages_path =
        'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/';
    }}
  }});
</script>
<style>{CSS}</style>
</head><body>

{topbar_html(status_label, status_class)}

{cover}

<div class="shell">

  <h1>{target_name} · {days}-day audit summary</h1>
  <p class="subhead">{datetime.now(timezone.utc).isoformat(timespec='minutes')} · rolling window</p>

  <div class="kpi-grid">
    <div class="kpi {'danger' if counts['Critical'] else 'ok'}">
      <div class="label">Critical</div><div class="value">{counts['Critical']}</div></div>
    <div class="kpi {'warn' if counts['High'] else 'ok'}">
      <div class="label">High</div><div class="value">{counts['High']}</div></div>
    <div class="kpi"><div class="label">Medium</div><div class="value">{counts['Medium']}</div></div>
    <div class="kpi"><div class="label">Hunt cycles</div><div class="value">{len(cycles)}</div></div>
    <div class="kpi"><div class="label">Confirmed</div><div class="value">{total_confirmed}</div></div>
  </div>

  {_sev_bar(counts)}

  {_propagation_section(workspace, findings, public) if workspace else ""}
  {_fix_bundle_section(workspace, findings, public) if workspace else ""}

  <h2>Severity rubric</h2>
  <table>
    <thead><tr><th style="width:120px">Tier</th><th>Definition</th></tr></thead>
    <tbody>{''.join(
        f'<tr><td><span class="sev {s.value.lower()}">{s.value}</span></td>'
        f'<td style="color:var(--text-2)">{html.escape(DEFINITIONS[s])}</td></tr>'
        for s in Severity
    )}</tbody>
  </table>

  <h2>Hunt cycles ({len(cycles)})</h2>
  <table>
    <thead><tr>
      <th>Cycle</th><th>Started (UTC)</th><th>Engine SHA</th>
      <th class="num">Dispatched</th><th class="num">Confirmed</th>
    </tr></thead>
    <tbody>{''.join(cycle_rows) or '<tr><td colspan="5" class="empty">No cycles in window.</td></tr>'}</tbody>
  </table>

  <h2>Findings ({len(findings)})</h2>
  {_findings_table(findings)}

  {footer_html(extra=f"{days}-day rolling")}

</div>
</body></html>"""
