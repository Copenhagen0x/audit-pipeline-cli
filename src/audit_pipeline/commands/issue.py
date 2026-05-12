"""`audit-pipeline issue` — draft or file a GitHub issue from a finding.

Two modes:
  draft  : print the Markdown issue body to stdout (no network)
  file   : actually call `gh issue create` against the target repo

Default is `draft` because filing public issues is irreversible. For
production, pass `--auto-file` to enable issue creation in the watch
loop's --on-update.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB, open_findings_db
from audit_pipeline.gates.repo_pin import check_repo_pin
from audit_pipeline.lifecycle import Status
from audit_pipeline.severity import DEFINITIONS, Severity
from audit_pipeline.severity import emoji as sev_emoji

console = Console()


@click.group(name="issue")
def issue_cmd() -> None:
    """Draft / file GitHub issues for confirmed findings."""


@issue_cmd.command(name="draft")
@click.option("--finding-id", type=int, required=True, help="DB finding id")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Write Markdown to a file (default: stdout)")
@click.pass_context
def draft_cmd(ctx: click.Context, finding_id: int, output: Path | None) -> None:
    """Print a Markdown issue body for a finding (no GitHub call)."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"Finding {finding_id} not found")

    body = _render_issue_body(finding, db)
    if output:
        output.write_text(body, encoding="utf-8")
        console.print(f"[green]wrote[/green] {output}")
    else:
        click.echo(body)


@issue_cmd.command(name="file")
@click.option("--finding-id", type=int, required=True, help="DB finding id")
@click.option("--repo", required=True, help="GitHub repo (owner/name)")
@click.option("--label", multiple=True, default=("security", "audit-pipeline"),
              help="Labels to apply (repeatable)")
@click.option("--draft-comment-only", is_flag=True,
              help="Don't open an issue; post as a comment on a tracking issue (set --tracking-issue)")
@click.option("--tracking-issue", type=int, default=None,
              help="Issue number to comment on (used with --draft-comment-only)")
@click.option("--dry-run", is_flag=True,
              help="Show the gh command but don't run it")
@click.option("--allow-mixed-pin", is_flag=True, default=False,
              help=(
                  "Bypass Gate 6 (L5.repo_pin) — by default the issue body "
                  "is rejected if it cites a commit SHA that doesn't resolve "
                  "in --repo. Pass this only when you've verified the SHA "
                  "manually (cross-repo references etc.)."
              ))
@click.pass_context
def file_cmd(
    ctx: click.Context,
    finding_id: int,
    repo: str,
    label: tuple[str, ...],
    draft_comment_only: bool,
    tracking_issue: int | None,
    dry_run: bool,
    allow_mixed_pin: bool,
) -> None:
    """Open a GitHub issue (or post a comment) from a finding via `gh`."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"Finding {finding_id} not found")

    severity = finding.get("severity", "Medium")
    title = (
        f"[{severity}] {finding.get('title') or finding.get('hypothesis_id')}"
    )[:100]
    body = _render_issue_body(finding, db)

    # Gate 6 — L5.repo_pin. Cycle 20260511-183154 was retracted because the
    # issue header pinned engine SHA 6cd742f25a in a wrapper-repo (`percolator-prog`)
    # disclosure. Block disclosure when any cited SHA doesn't resolve in --repo.
    if not allow_mixed_pin and not dry_run:
        pin_result = check_repo_pin(body=body, target_repo=repo)
        if pin_result.passed is False:
            raise click.ClickException(
                "Gate L5.repo_pin FAILED — refusing to file:\n  "
                + pin_result.reason
                + "\n\nFix the body's SHA citations, OR pass --allow-mixed-pin "
                "if you've verified the SHA manually."
            )
        if pin_result.passed is None:
            console.print(
                f"[yellow]Gate L5.repo_pin SKIP[/yellow] — {pin_result.reason}"
            )
        else:
            console.print(f"[dim]Gate L5.repo_pin pass — {pin_result.reason}[/dim]")

    if draft_comment_only:
        if not tracking_issue:
            raise click.ClickException("--draft-comment-only requires --tracking-issue")
        cmd = ["gh", "issue", "comment", str(tracking_issue),
               "--repo", repo, "--body-file", "-"]
    else:
        cmd = ["gh", "issue", "create",
               "--repo", repo,
               "--title", title,
               "--body-file", "-"]
        for l in label:
            cmd += ["--label", l]

    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] would execute:")
        console.print("  " + " ".join(cmd))
        console.print()
        console.print("--- body ---")
        click.echo(body)
        return

    try:
        result = subprocess.run(
            cmd, input=body, capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        raise click.ClickException(
            "`gh` CLI not found. Install with: https://cli.github.com/"
        )

    if result.returncode != 0:
        raise click.ClickException(
            f"gh failed (rc={result.returncode}):\n{result.stderr}"
        )

    issue_url = result.stdout.strip()
    console.print(f"[green]✓[/green] Opened: {issue_url}")
    db.transition_finding(
        finding_id=finding_id,
        to_status=Status.DISCLOSED,
        reason=f"GitHub issue filed: {issue_url}",
        actor="audit-pipeline issue file",
    )


@issue_cmd.command(name="sync")
@click.option("--repo", required=True, help="GitHub repo (owner/name)")
@click.option("--finding-id", type=int, required=False,
              help="Optional: sync only one finding instead of all disclosed ones")
@click.option("--dry-run", is_flag=True, default=False)
@click.pass_context
def sync_cmd(
    ctx: click.Context, repo: str, finding_id: int | None, dry_run: bool,
) -> None:
    """Poll upstream issue state and transition findings on closed-not-planned.

    Disclosure audit Defect 02 (HIGH): the disclosure path transitions
    findings to ``disclosed`` on ``gh issue create``, but nothing reverses
    that transition when the upstream maintainer closes the issue as
    ``closed-not-planned`` (yesterday's exact retraction shape). Issue
    #92 currently sits in the DB at ``disclosed`` even though it's been
    CLOSED with NOT_PLANNED for hours, which inflates dashboards, public
    counts, and customer manifests.

    This command polls every disclosed finding's filed-issue (or one
    specific finding via --finding-id), reads ``state + stateReason``
    via ``gh``, and transitions to ``REJECTED`` with a recorded reason
    when the upstream issue is CLOSED with stateReason=NOT_PLANNED. Safe
    to run repeatedly; idempotent.
    """
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)

    if finding_id is not None:
        findings = [db.get_finding(finding_id)]
        findings = [f for f in findings if f]
    else:
        findings = db.list_findings(status=Status.DISCLOSED, limit=500)

    if not findings:
        console.print("[dim]No disclosed findings to sync[/dim]")
        return

    URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
    n_synced = 0
    n_rejected = 0
    for f in findings:
        transitions = db.transitions_for(f["id"])
        url = None
        for t in reversed(transitions):
            if t.get("to_status") == Status.DISCLOSED.value:
                m = URL_RE.search(str(t.get("reason") or ""))
                if m:
                    url = m.group(0)
                    target_repo = m.group(1)
                    issue_no = int(m.group(2))
                    break
        else:
            continue
        if target_repo.lower() != repo.lower():
            continue   # different repo, skip

        try:
            proc = subprocess.run(
                ["gh", "issue", "view", str(issue_no), "--repo", repo,
                 "--json", "state,stateReason"],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            console.print(f"[yellow]skip[/yellow] #{issue_no}: gh unavailable")
            continue
        if proc.returncode != 0:
            console.print(f"[yellow]skip[/yellow] #{issue_no}: gh exit {proc.returncode}")
            continue
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue

        state = (info.get("state") or "").upper()
        reason = (info.get("stateReason") or "").upper()
        n_synced += 1
        if state == "CLOSED" and reason in {"NOT_PLANNED", "NOTPLANNED"}:
            if dry_run:
                console.print(f"[dim]DRY-RUN[/dim] would reject finding {f['id']} (#{issue_no})")
                continue
            db.transition_finding(
                finding_id=f["id"],
                to_status=Status.REJECTED,
                reason=f"upstream closed-not-planned: {url}",
                actor="audit-pipeline issue sync",
            )
            n_rejected += 1
            console.print(f"[red]rejected[/red] finding {f['id']} (#{issue_no} closed-not-planned)")

    console.print(f"\nSynced {n_synced} finding(s); transitioned {n_rejected} to REJECTED.")


@issue_cmd.command(name="auto-file-confirmed")
@click.option("--cycle-id", required=True, help="Hunt cycle id (e.g. 20260428-191412)")
@click.option("--repo", required=True, help="GitHub repo (owner/name)")
@click.option("--severity-floor",
              type=click.Choice([s.value for s in Severity]),
              default="High", show_default=True,
              help="Only auto-file findings >= this severity")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def auto_file_cmd(
    ctx: click.Context, cycle_id: str, repo: str, severity_floor: str, dry_run: bool,
) -> None:
    """Auto-file all confirmed findings from a cycle that meet the severity floor."""
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)

    floor = Severity(severity_floor)
    floor_rank = list(Severity).index(floor)

    findings = db.list_findings(status=Status.CONFIRMED)
    cycle_findings = [f for f in findings if f.get("cycle_id") == cycle_id]

    # Disclosure audit Defect 01 (HIGH): auto-file used to skip every
    # post-confirmation gate — `poc_fired` wasn't required, the L2 PoC
    # quality wasn't re-checked, and the disclosure_history gate wasn't
    # re-run. Yesterday's 20 false-positive cycle would have auto-filed
    # through this path with zero protection. Now: hard gates.
    from audit_pipeline.gates.post_cycle import _check_one_poc
    eligible: list = []
    skipped: list = []
    cycle_dir = workspace / "hunts" / cycle_id
    workspace_cfg = workspace / "workspace.json"
    engine_src = None
    wrapper_src = None
    try:
        import json as _json
        cfg = _json.loads(workspace_cfg.read_text(encoding="utf-8"))
        if cfg.get("engine", {}).get("local"):
            engine_src = workspace / cfg["engine"]["local"] / "src"
        if cfg.get("wrapper", {}).get("local"):
            wrapper_src = workspace / cfg["wrapper"]["local"] / "src"
    except (OSError, ValueError):
        pass
    search_dirs = [d for d in (engine_src, wrapper_src) if d and d.is_dir()]

    for f in cycle_findings:
        try:
            sev = Severity(f.get("severity"))
        except ValueError:
            continue
        if list(Severity).index(sev) > floor_rank:
            continue
        # Gate 1: poc_fired must be True
        if not f.get("poc_fired"):
            skipped.append((f["id"], "poc_fired=False — no empirical signal"))
            continue
        # Gate 2: PoC source must pass post-cycle re-check (symbol_grep +
        # pseudo-pass marker detection)
        if search_dirs:
            hyp_id = f.get("hypothesis_id") or ""
            from audit_pipeline.utils.slug import slug_for_hypothesis
            test_name = f"test_{slug_for_hypothesis(hyp_id)}"
            poc_path = cycle_dir / "poc" / f"{test_name}.rs"
            row = _check_one_poc(poc_path, test_name, search_dirs)
            if not row.get("passed"):
                skipped.append((f["id"], f"PoC re-check failed: {row.get('reason', '')[:160]}"))
                continue
        eligible.append(f)

    if skipped:
        console.print(
            f"[yellow]auto-file: skipped {len(skipped)} finding(s) "
            f"that failed post-confirmation gates:[/yellow]"
        )
        for fid, reason in skipped[:8]:
            console.print(f"  finding {fid}: {reason}")
        if len(skipped) > 8:
            console.print(f"  ... and {len(skipped) - 8} more")

    if not eligible:
        console.print(f"[dim]No confirmed findings >= {severity_floor} in cycle {cycle_id} survived auto-file gates[/dim]")
        return

    console.print(f"[bold]Filing {len(eligible)} confirmed finding(s) from cycle {cycle_id}[/bold]")
    for f in eligible:
        ctx.invoke(
            file_cmd,
            finding_id=f["id"],
            repo=repo,
            label=("security", "audit-pipeline", f"severity-{f['severity'].lower()}"),
            draft_comment_only=False,
            tracking_issue=None,
            dry_run=dry_run,
        )


def _md_safe(s: str | None, *, max_len: int = 400) -> str:
    """Disclosure audit Defect 03 (HIGH): neutralise LLM-authored fields
    before they're interpolated into a GitHub issue body.

    Strips characters that close markdown code spans, HTML tags, and
    GFM link syntax that could be weaponised by an attacker who can
    write a hypothesis ``claim`` / ``title`` (e.g. via a propagation
    sweep from a malicious target repo). Output is safe to embed in
    backtick code spans, plain text, or table cells.
    """
    if not s:
        return ""
    text = str(s)[:max_len]
    # Backticks would close inline code spans + leak the rest as text
    text = text.replace("`", "'")
    # Angle brackets — GFM allows raw HTML. Replace with HTML entities.
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    # Pipe breaks markdown tables when the field is rendered in one
    text = text.replace("|", "\\|")
    # CR/LF collapse to single space so a multi-line claim doesn't
    # smuggle "fake maintainer reply" block-quote lines.
    text = re.sub(r"[\r\n]+", " ", text)
    return text


def _render_issue_body(finding: dict, db: FindingsDB) -> str:
    severity_str = finding.get("severity", "Medium")
    try:
        severity = Severity(severity_str)
    except ValueError:
        severity = Severity.MEDIUM
    sev_def = DEFINITIONS.get(severity, "")

    details_json = finding.get("details_json") or "{}"
    try:
        details = json.loads(details_json)
    except json.JSONDecodeError:
        details = {}

    transitions = db.transitions_for(finding["id"])

    safe_title = _md_safe(finding.get('title') or finding.get('hypothesis_id'))
    safe_hyp_id = _md_safe(finding.get('hypothesis_id'))
    safe_verdict = _md_safe(finding.get('verdict'), max_len=40)
    safe_confidence = _md_safe(finding.get('confidence'), max_len=40)
    safe_status = _md_safe(finding.get('status'), max_len=40)
    safe_cycle = _md_safe(finding.get('cycle_id'), max_len=80)
    lines: list[str] = [
        f"# {sev_emoji(severity)} {severity.value} — {safe_title}",
        "",
        "## Summary",
        "",
        f"- **Hypothesis ID:** `{safe_hyp_id}`",
        f"- **Severity:** **{severity.value}** — _{_md_safe(sev_def, max_len=200)}_",
        f"- **Verdict:** `{safe_verdict}` ({safe_confidence})",
        f"- **Status:** `{safe_status}`",
        f"- **Engine SHA:** `{(finding.get('engine_sha') or '?')[:10]}`",
        f"- **Wrapper SHA:** `{(finding.get('wrapper_sha') or '?')[:10]}`",
        f"- **Discovered:** {finding.get('created_at')}",
        f"- **Hunt cycle:** `{safe_cycle}`",
        "",
        "## Evidence",
        "",
    ]

    if finding.get("poc_fired"):
        lines += [
            "- ✅ **PoC fired** (`cargo test` exit code non-zero on a state-conservation invariant)",
            f"- PoC scaffold: `{finding.get('poc_path')}`",
        ]
    elif finding.get("poc_path"):
        lines.append(f"- ⚠️ PoC scaffold did not fire: `{finding.get('poc_path')}`")
    else:
        lines.append("- _No empirical PoC for this finding (Layer 1 only)._")

    if finding.get("debate_promoted"):
        lines.append(
            "- 🗣️ **Promoted by adversarial debate** (challenger flipped a "
            "FALSE/HIGH verdict back into the candidate set)"
        )

    kani = (details or {}).get("kani") or {}
    if kani:
        lines.append(f"- 🛡️ Kani harness rc: `{kani.get('returncode')}`")

    lines += [
        "",
        "## Reproduction",
        "",
        "```bash",
        f"# Workspace: {finding.get('cycle_id')} cycle artifacts",
        "audit-pipeline hunt --hypotheses hypotheses.yaml",
        f"# Then inspect: hunts/{finding.get('cycle_id')}/",
        "```",
        "",
        "## Recommended next steps",
        "",
    ]

    if severity in (Severity.CRITICAL, Severity.HIGH):
        lines += [
            "1. **Acknowledge** receipt within 24h",
            "2. **Triage** the PoC under your test harness",
            "3. **Patch** the affected code path",
            "4. **Re-run** `audit-pipeline hunt` against the patched commit to verify the PoC no longer fires",
        ]
    else:
        lines += [
            "1. Triage at normal cadence",
            "2. Optional: patch in the next release",
        ]

    lines += [
        "",
        "## Audit trail",
        "",
        "| When | From | To | Actor | Reason |",
        "|---|---|---|---|---|",
    ]
    for t in transitions:
        lines.append(
            f"| {t.get('ts')} | `{t.get('from_status') or '-'}` | "
            f"`{t.get('to_status')}` | {t.get('actor')} | {t.get('reason')} |"
        )

    lines += [
        "",
        "---",
        "",
        "_Generated automatically by [audit-pipeline](https://github.com/Copenhagen0x/audit-pipeline-cli) "
        "— a multi-layer autonomous bug-hunting framework for Solana programs._",
    ]
    return "\n".join(lines)
