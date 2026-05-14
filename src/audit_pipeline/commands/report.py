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
    """
    if not sign_enabled:
        return
    key_path = default_key_path(workspace)
    if not key_path.exists():
        console.print(
            f"[yellow]auto-sign skipped:[/yellow] no key at {key_path}. "
            f"Run [cyan]audit-pipeline sign keygen[/cyan] to enable signed receipts."
        )
        return
    try:
        sig_path = sign_file(report_path, key_path)
    except SignError as e:
        console.print(f"[yellow]auto-sign failed:[/yellow] {e}")
        return
    console.print(f"[green]signed[/green]    {sig_path}")


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
@click.pass_context
def cycle_report(
    ctx: click.Context, cycle_id: str, output: Path | None, sign: bool, pdf: bool,
    public: bool,
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


def _table_of_contents(findings: list[dict]) -> str:
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
    lis = []
    for idx, f in enumerate(sorted_findings, 1):
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            sev = Severity.INFO
        sev_cls = sev.value.lower()
        title = (f.get("title") or f.get("hypothesis_id") or "?").strip()
        bug_class = f.get("bug_class") or "—"
        lis.append(
            f'<li>'
            f'<span class="toc-num">{idx:02d}</span>'
            f'<span class="toc-sev"><span class="sev {sev_cls}">{sev.value}</span></span>'
            f'<a href="#finding-{idx:02d}">{html.escape(title[:90])}</a>'
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
    """Return ``"aptos"`` if this workspace ran the Move stack, else ``"solana"``.

    Detection order (cheap → expensive):
      1. ``workspace/formal/aptos/*`` or ``workspace/fuzz/aptos/*`` exists
      2. ``hunts/<cycle>/hunt.log.jsonl`` contains an event with ``language: "aptos"``
      3. Default to ``"solana"`` (back-compat for the existing renderer)
    """
    if (workspace / "formal" / "aptos").is_dir():
        return "aptos"
    if (workspace / "fuzz" / "aptos").is_dir():
        return "aptos"
    if (workspace / "tests" / "aptos").is_dir():
        return "aptos"
    log = workspace / "hunts" / cycle_id / "hunt.log.jsonl"
    if log.is_file():
        try:
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[:500]:
                if '"language": "aptos"' in line or '"language":"aptos"' in line:
                    return "aptos"
        except OSError:
            pass
    return "solana"


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
    bundles_dir = workspace / "recon" / "bundles"
    # Aptos workspaces keep the PoC source under workspace/tests/aptos/
    # (not the per-cycle poc/ dir, which only stores runlogs). Resolve once.
    aptos_tests_dir = workspace / "tests" / "aptos"
    aptos_specs_dir = workspace / "formal" / "aptos"
    aptos_fuzz_dir = workspace / "fuzz" / "aptos"
    aptos_layer_results = (
        _aptos_layer_results_from_log(workspace, cycle_id)
        if language == "aptos" else {"l3": {}, "l4": {}}
    )

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
        # The DB stores the full hypothesis "claim" prose as title — useful
        # as a finding-description paragraph but too long for the section
        # heading. Truncate at the end of the first sentence (or at a word
        # boundary near 110 chars) so the banner reads cleanly; keep the
        # full text available below as the Invariant description paragraph.
        full_title = (f.get("title") or hyp_id).strip()
        first_sentence = re.split(r"(?<=[.!?])\s+", full_title, maxsplit=1)[0]
        if len(first_sentence) <= 110:
            title = first_sentence
        else:
            cut = full_title[:107]
            # Break on the last whitespace so we don't truncate mid-word.
            last_space = cut.rfind(" ")
            if last_space >= 80:
                cut = cut[:last_space]
            title = cut.rstrip(" ,;:") + "…"
        bug_class = f.get("bug_class") or "unknown"
        finding_id = f.get("id")

        # ── L2 PoC excerpt (language-dependent file extension + body shape) ──
        l2_excerpt = ""
        l2_lang = "rust"

        def _cap_lines(s: str, n: int = 24) -> str:
            # Cap at ~24 lines so L2 + P3 patch both fit on the
            # second-half page without splitting. Code-tight font
            # is 10.5px × 1.4 line-height; combined L2(24)+P3(16)
            # lands at ~750px, well under the 870px usable budget.
            parts = s.splitlines()
            if len(parts) <= n:
                return s
            return "\n".join(parts[:n]) + "\n    // …truncated for brevity"

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
                    l2_excerpt = _cap_lines(m.group(1) if m else pt)
                except OSError:
                    pass
            l2_lang = "rust"  # Prism doesn't ship `move`; rust highlight is closest
        else:
            poc_path = poc_dir / f"test_{hyp_slug}.rs"
            if poc_path.is_file():
                try:
                    poc_text = poc_path.read_text(encoding="utf-8", errors="replace")
                    # Pull the fires function body — start at `#[test]\nfn ..._fires`
                    m = re.search(
                        r"(#\[test\][^\n]*\nfn[^\n]+_fires[^\{]*\{.*?\n\})",
                        poc_text, re.DOTALL,
                    )
                    if m:
                        l2_excerpt = _cap_lines(m.group(1))
                    else:
                        l2_excerpt = _cap_lines(poc_text)
                except OSError:
                    pass

        # ── L3 verification status (Kani for Solana / Move Prover for Aptos) ──
        l3_status = "—"
        if language == "aptos":
            ev = aptos_layer_results.get("l3", {}).get(hyp_id)
            if ev:
                if ev.get("counterexample") is True:
                    l3_status = "✓ Move Prover found a counterexample (bug confirmed by symbolic execution)"
                elif ev.get("proved") is True:
                    l3_status = "Move Prover proved the invariant holds (no counterexample within the spec)"
                elif ev.get("compile_error"):
                    l3_status = "Spec inconclusive (Move Prover failed to compile the LLM-authored spec)"
                else:
                    l3_status = "Inconclusive (Move Prover infra error or unparseable verdict)"
        else:
            kani_log = cycle_dir / "kani" / f"cargo_kani_{hyp_slug}_invariant.log"
            if kani_log.is_file():
                try:
                    kt = kani_log.read_text(encoding="utf-8", errors="replace")
                    if "Verification:- FAILED" in kt or "VERIFICATION:- FAILED" in kt:
                        l3_status = "✓ Counterexample found (bug confirmed by symbolic execution)"
                    elif "Verification:- SUCCESSFUL" in kt or "VERIFICATION:- SUCCESSFUL" in kt:
                        l3_status = "Proved safe under small-model bounds (no counterexample within those constraints)"
                    else:
                        l3_status = "Inconclusive (timeout / out of memory)"
                except OSError:
                    pass

        # ── L4 reproduction (LiteSVM/BPF for Solana, aptos-move-test for Aptos) ──
        l4_status = "—"
        l4_witness = ""
        if language == "aptos":
            ev = aptos_layer_results.get("l4", {}).get(hyp_id)
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
                        "L2 PoC + L3 Move Prover remain the authoritative bug signals)"
                    )
                elif ev.get("ran_clean"):
                    l4_status = "Property fuzz ran clean — no PASS/FAIL markers (no signal)"
                else:
                    l4_status = "Inconclusive (Move test runner did not report a parseable verdict)"
        else:
            for litesvm_log in litesvm_dir.glob(f"cargo_litesvm_{hyp_slug}*.log"):
                try:
                    lt = litesvm_log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if (f"panicked at tests/litesvm_{hyp_slug[:40]}" in lt
                        or "BUG" in lt and "CONFIRMED" in lt):
                    l4_status = "✓ Reproduced through deployed BPF instructions"
                    # Extract the BUG ... CONFIRMED line as witness
                    for line in lt.splitlines():
                        if "BUG" in line and ("CONFIRMED" in line or "FIRES" in line or "DETECTED" in line):
                            l4_witness = line.strip()[:500]
                            break
                elif "test result: ok" in lt:
                    l4_status = "Not reproduced (wrapper-side defenses caught it OR test setup didn't reach buggy state)"
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
                    if len(parts) > 16:
                        p3_patch = "\n".join(parts[:16]) + "\n@@ …truncated for brevity @@"
                    else:
                        p3_patch = raw
                except OSError:
                    pass
            if verif_p.is_file():
                try:
                    v = _json.loads(verif_p.read_text(encoding="utf-8"))
                    for g_name, g_data in v.get("gates", {}).items():
                        passed = g_data.get("passed")
                        icon = "✓" if passed is True else ("✗" if passed is False else "⏭")
                        reason = (g_data.get("reason") or "")[:200]
                        p3_gates.append((icon, g_name, reason))
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
        l2_section = (
            f'<h4 class="page-break-before">Layer 2 — Concrete proof of concept (engine-direct)</h4>'
            f'<pre class="code-block code-tight"><code class="language-{l2_lang}">{html.escape(l2_excerpt)}</code></pre>'
        ) if l2_excerpt else (
            '<h4 class="page-break-before">Layer 2 — Concrete proof of concept</h4>'
            '<p style="color:var(--text-3)">No PoC source on file</p>'
        )

        l4_label = (
            "Layer 4 — Property fuzz (aptos move test)"
            if language == "aptos"
            else "Layer 4 — On-chain BPF reproduction"
        )
        l4_section = (
            f'<h4>{l4_label}</h4>'
            f'<p class="finding-prose">{html.escape(l4_status)}</p>'
            + (f'<pre class="code-block witness"><code>{html.escape(l4_witness)}</code></pre>' if l4_witness else "")
        )

        gates_section = ""
        if p3_gates:
            rows = "".join(
                f'<tr><td style="text-align:center;width:32px">{html.escape(icon)}</td>'
                f'<td><code>{html.escape(name)}</code></td>'
                f'<td style="color:var(--text-2)">{html.escape(reason)}</td></tr>'
                for icon, name, reason in p3_gates
            )
            gates_section = (
                '<h4>Verification gates &mdash; post-patch machine checks</h4>'
                '<p style="color:var(--text-3);font-size:11.5px;margin:-4px 0 10px">'
                'Result of running the proposed patch through Jelleo&rsquo;s 5-gate verifier '
                '(unsigned, syntactic well-formedness, single-function scope, no new deps, '
                'tests still compile/pass).'
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

        l3_label = (
            "Layer 3 — Symbolic verification (Move Prover)"
            if language == "aptos"
            else "Layer 3 — Symbolic verification (Kani)"
        )
        # If the full claim was truncated for the heading, surface the
        # full text as a finding-description paragraph below so the
        # reader still gets the invariant statement in context.
        description_para = ""
        if full_title != title:
            description_para = (
                f'<p class="finding-prose" style="color:var(--text-2);'
                f'margin:-4px 0 14px;font-size:12.5px;">'
                f'<strong style="color:var(--text-3);'
                f'font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;'
                f'font-family:var(--mono);margin-right:8px">Invariant</strong>'
                f'{html.escape(full_title)}</p>'
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
          <h3 class="finding-title">{html.escape(title)}</h3>
          {description_para}

          <h4>{l3_label}</h4>
          <p class="finding-prose">{html.escape(l3_status)}</p>

          {l4_section}

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
        rows.append({
            "id":       fid,
            "title":    (f.get("title") or "")[:90],
            "hyp":      f.get("hypothesis_id") or "",
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

    body_rows: list[str] = []
    for r in rows:
        authz_mark = "&#x2713;" if r["authorized"] else "&middot;"
        body_rows.append(
            f"<tr>"
            f"<td><code>{r['id']}</code></td>"
            f"<td><code>{html.escape(r['hyp'])}</code></td>"
            f"<td style=\"color:var(--text-2)\">{html.escape(r['title'])}</td>"
            f"<td><code>{html.escape(r['status'])}</code></td>"
            f"<td style=\"text-align:right\">{html.escape(r['gates'])}</td>"
            f"<td style=\"text-align:center\">{authz_mark}</td>"
            f"</tr>"
        )
    table = (
        '<table><thead><tr>'
        '<th>id</th><th>hypothesis</th><th>title</th>'
        '<th>status</th><th style="text-align:right">gates</th>'
        '<th style="text-align:center">authz</th>'
        '</tr></thead><tbody>' + "".join(body_rows) + '</tbody></table>'
    )

    return f"""
  <h2>03 &mdash; Fix-bundle activity</h2>
  <p style="color:var(--text-2)">
    Per-finding fix-bundle pipeline state. Engine drafts + verifies; operator
    authorizes via long-form typed phrase; PR opens only against valid
    authorization marker.
  </p>
  {counter_html}
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
    protocol_label = "Aptos" if language == "aptos" else "Solana"

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

    cover = cover_page_html(
        target_name=target_name,
        report_title="Hunt cycle ·",
        window_label=f"cycle {cycle_id}",
        cycle_id=cycle_id,
        engine_sha=engine_sha,
        wrapper_sha=wrapper_sha,
        severity_counts=real_counts,        # real findings only on the headline
        status_breakdown=sb,                # full pipeline state context
        pubkey_fingerprint=pubkey_fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        protocol_label=protocol_label,
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
/* Code blocks: keep the second-half pages dense */
pre.code-block.code-tight {{
  font-size: 10.5px;
  line-height: 1.45;
  padding: 10px 14px;
}}

/* Code blocks: prevent splitting across pages, give a card feel */
pre.code-block {{
  background: #0d0c0a;
  border: 1px solid rgba(245,184,0,0.18);
  border-radius: 6px;
  padding: 14px 18px;
  margin: 12px 0 18px;
  font-size: 11.5px;
  line-height: 1.55;
  overflow-x: auto;
  page-break-inside: avoid;
  break-inside: avoid;
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

{topbar_html(status_label, status_class)}

{cover}

<div class="shell">

  {_table_of_contents(findings)}

  {_findings_writeup(findings, workspace, cycle_id) if workspace else ""}

  {_propagation_section(workspace, findings, public) if workspace else ""}
  {_fix_bundle_section(workspace, findings, public) if workspace else ""}

  <h2>A &mdash; Severity rubric</h2>
  <table>
    <thead><tr><th style="width:120px">Tier</th><th>Definition</th></tr></thead>
    <tbody>{''.join(
        f'<tr><td><span class="sev {s.value.lower()}">{s.value}</span></td>'
        f'<td style="color:var(--text-2)">{html.escape(DEFINITIONS[s])}</td></tr>'
        for s in Severity
    )}</tbody>
  </table>

  <h2>B &mdash; Methodology</h2>
  <p style="color:var(--text-2)">
    This cycle was produced by Jelleo's continuous, hypothesis-driven Solana audit loop.
    Every finding originates as a falsifiable invariant claim from a per-protocol
    hypothesis library, dispatched to multi-agent recon (Layer 1), promoted on
    contested verdicts via adversarial debate (Layer 1.5), and confirmed empirically
    via a <code>cargo test</code> proof-of-concept (Layer 2) before transitioning to
    <code>confirmed</code>. Confirmed findings auto-fire structural sibling derivation
    and cross-protocol propagation hooks, then move through a restricted lifecycle
    (<code>new &rarr; triaged &rarr; confirmed &rarr; disclosed &rarr; fixed &rarr; verified</code>).
    Every cycle is signed Ed25519 against the platform key — see the cover-page receipt.
  </p>
  <p style="color:var(--text-2)">
    Full spec: <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/docs/methodology">docs/methodology/</a>
    (eleven sections, &sect;01&ndash;&sect;10) &middot;
    Live reference: <a href="https://jelleo.com/methodology.html">jelleo.com/methodology.html</a> &middot;
    Inaugural disclosure: <a href="https://github.com/aeyakovenko/percolator-prog/pull/39">aeyakovenko/percolator-prog#39</a> (F7, 2026-04)
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
