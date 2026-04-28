"""`audit-pipeline narrative` — LLM-generated narrative finding write-ups.

For confirmed (or candidate) findings, generates a detailed audit-firm-shape
narrative writeup with reproduction steps, impact analysis, recommended fix,
and references. Output is Markdown suitable for direct attachment to a
GitHub issue, PDF report, or signed disclosure package.

Two modes:
  generate : produce the writeup, attach to the finding's `details_json`,
             optionally write to a file
  bulk     : generate writeups for every confirmed finding in a cycle (or all
             confirmed findings overall)
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.severity import DEFINITIONS, Severity
from audit_pipeline.utils import complete, is_available

console = Console()


@click.group(name="narrative")
def narrative_cmd() -> None:
    """Generate narrative finding write-ups (audit-firm-grade)."""


@narrative_cmd.command(name="generate")
@click.option("--finding-id", type=int, required=True)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Write Markdown to this file (default: <workspace>/findings/<id>.md)")
@click.option("--max-tokens", type=int, default=8192, show_default=True)
@click.pass_context
def generate_cmd(
    ctx: click.Context, finding_id: int, output: Path | None, max_tokens: int,
) -> None:
    """Generate a detailed narrative writeup for a single finding."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"Finding {finding_id} not found")
    if not is_available():
        raise click.ClickException("ANTHROPIC_API_KEY required.")

    text = _generate_narrative(finding, db, max_tokens)

    out = output or (workspace / "findings" / f"finding_{finding_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


@narrative_cmd.command(name="bulk")
@click.option("--cycle-id", default=None,
              help="Only narrate findings from this cycle (default: all confirmed/candidate findings)")
@click.option("--severity-floor",
              type=click.Choice([s.value for s in Severity]),
              default="Medium", show_default=True)
@click.option("--max-tokens", type=int, default=8192, show_default=True)
@click.pass_context
def bulk_cmd(
    ctx: click.Context, cycle_id: str | None, severity_floor: str, max_tokens: int,
) -> None:
    """Generate writeups for every finding meeting severity-floor (and cycle, if given)."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")
    if not is_available():
        raise click.ClickException("ANTHROPIC_API_KEY required.")

    floor_rank = list(Severity).index(Severity(severity_floor))
    findings = db.list_findings(limit=1000)
    eligible = []
    for f in findings:
        if cycle_id and f.get("cycle_id") != cycle_id:
            continue
        try:
            sev = Severity(f.get("severity", "Info"))
        except ValueError:
            continue
        if list(Severity).index(sev) > floor_rank:
            continue
        if f.get("status") == "rejected":
            continue
        eligible.append(f)

    if not eligible:
        console.print(f"[dim]No eligible findings (severity ≥ {severity_floor}).[/dim]")
        return

    console.print(f"[bold]Generating narratives for {len(eligible)} finding(s)...[/bold]")
    out_dir = workspace / "findings"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in eligible:
        text = _generate_narrative(f, db, max_tokens)
        out_path = out_dir / f"finding_{f['id']}.md"
        out_path.write_text(text, encoding="utf-8")
        console.print(
            f"  [green]✓[/green] {f['hypothesis_id']} — {f['severity']} → {out_path.name}"
        )


def _generate_narrative(finding: dict, db: FindingsDB, max_tokens: int) -> str:
    """Call Claude to produce an audit-firm-grade narrative writeup."""
    severity = finding.get("severity", "Medium")
    try:
        sev_enum = Severity(severity)
    except ValueError:
        sev_enum = Severity.MEDIUM
    sev_def = DEFINITIONS.get(sev_enum, "")
    transitions = db.transitions_for(finding["id"])
    details = {}
    try:
        details = json.loads(finding.get("details_json") or "{}")
    except json.JSONDecodeError:
        pass

    poc_path = finding.get("poc_path") or "(none)"
    poc_excerpt = "(no PoC scaffold available)"
    if finding.get("poc_path"):
        try:
            content = Path(finding["poc_path"]).read_text(encoding="utf-8")[:4000]
            poc_excerpt = content
        except OSError:
            pass

    prompt = f"""You are a senior security researcher producing an enterprise-grade audit finding writeup.
Your writeup will be attached to a signed disclosure package and potentially filed as a GitHub issue against the maintainer's repository.

# Source data

- Hypothesis ID: {finding.get('hypothesis_id')}
- Title (current): {finding.get('title')}
- Verdict: {finding.get('verdict')} ({finding.get('confidence')})
- Severity (auto-derived): {severity} — {sev_def}
- Lifecycle status: {finding.get('status')}
- Engine SHA: {(finding.get('engine_sha') or '?')[:10]}
- Wrapper SHA: {(finding.get('wrapper_sha') or '?')[:10]}
- PoC path: {poc_path}
- PoC fired (cargo test asserted): {bool(finding.get('poc_fired'))}
- Debate promoted: {bool(finding.get('debate_promoted'))}
- Hunt cycle: {finding.get('cycle_id')}
- Discovered: {finding.get('created_at')}

# Evidence

## Lifecycle audit trail
{json.dumps([{"from": t.get('from_status'), "to": t.get('to_status'), "reason": t.get('reason'), "ts": t.get('ts')} for t in transitions], indent=2)}

## Cycle artifact details
{json.dumps(details, indent=2)[:4000]}

## PoC scaffold (if any)
```rust
{poc_excerpt}
```

# Required output structure (Markdown)

Produce a finding writeup in EXACTLY this structure:

```
# [{severity}] <crisp 80-char descriptive title>

## Summary
<2-4 sentence executive summary: what the issue is, why it matters, what the impact is>

## Affected code
- File: <path>
- Lines: <range>
- Function(s): <name(s)>

## Description
<3-6 paragraphs of technical analysis. Walk through the relevant code paths.
Identify the invariant being violated (or potentially violated). Explain
WHY it matters. Reference specific functions, struct fields, and instructions.>

## Impact
<2-3 paragraphs: what an attacker (or accidental state) could cause. Quantify
if possible (fund loss bounds, instruction reachability, signer requirements).>

## Reproduction
<Step-by-step reproduction. Reference the PoC scaffold if available.
If no PoC fired, describe the conceptual reproduction path: which instructions
to call in what order with what state preconditions.>

## Recommended fix
<Concrete code-level fix. Specify which function(s) to modify, what the change
should look like (in pseudocode or Rust), and why this fix preserves the
invariant in question.>

## References
<Bullet list of related code locations, prior commits, similar bug classes
in other Solana programs, or relevant Solana SDK / spec documents.>
```

Be precise, technical, and concise. Match the tone of a top-tier audit-firm
writeup. NO marketing, NO platform-promotion. Pure technical analysis.
If certain fields cannot be determined from the evidence, say so explicitly
("affected lines could not be determined from cycle artifacts; manual review required").
"""

    resp = complete(prompt, max_tokens=max_tokens)
    return resp.text
