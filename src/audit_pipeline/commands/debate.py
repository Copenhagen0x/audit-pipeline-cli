"""`audit-pipeline debate` — adversarial second-opinion on a Layer-1 verdict.

Two modes:

  render mode (default): writes a challenger prompt for the user to feed to
    their LLM manually.

  --auto mode: actually calls Claude to play the challenger role, writes
    BOTH prompt and response, and surfaces the challenger's verdict so the
    user can compare it side-by-side with the proposer's. Optionally runs
    a second round (proposer rebuttal) if --rounds > 1.

Methodological fix for the F7-style failure mode where a single agent
returns FALSE/HIGH and is wrong because it trusted a doc comment or
collapsed multiple call paths.
"""

import re
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.utils import (
    LLMUnavailable,
    complete,
    is_available,
    render_placeholders,
)
from audit_pipeline.utils.github_snapshot import GitHubSnapshot, SnapshotDownloadError

console = Console()


@click.command(name="debate")
@click.option(
    "--hypothesis-id",
    "-i",
    required=True,
    help="ID of the hypothesis under debate (matches recon prompt filename prefix)",
)
@click.option(
    "--proposer-verdict",
    "-v",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a markdown file containing the proposer agent's verdict + evidence",
)
@click.option(
    "--hypothesis-claim",
    "-c",
    default=None,
    help="One-sentence claim of the hypothesis (auto-derived if --hypotheses-file is set)",
)
@click.option(
    "--hypotheses-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Optional: path to hypotheses.yaml (used to auto-derive claim/target from --hypothesis-id)",
)
@click.option(
    "--target-file",
    "-t",
    default=None,
    help="Target source file (auto-derived if --hypotheses-file is set)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir (defaults to <workspace>/recon/debate/)",
)
@click.option(
    "--auto",
    is_flag=True,
    help=(
        "Actually call Claude to play the challenger role, write the "
        "response, and surface the challenger's verdict side-by-side "
        "with the proposer's. Requires ANTHROPIC_API_KEY."
    ),
)
@click.option(
    "--source-repo",
    default=None,
    help=(
        "GitHub repo (owner/repo) to read source from via an ephemeral "
        "snapshot. Not strictly required for debate (which only reads the "
        "proposer verdict + a template), but accepted for parity with the "
        "rest of the pipeline so callers can invoke debate with the same "
        "snapshot-mode flags."
    ),
)
@click.option(
    "--source-sha",
    default=None,
    help=(
        "Specific commit SHA to pin the snapshot to (requires --source-repo). "
        "If omitted, uses the default branch HEAD at download time."
    ),
)
@click.pass_context
def debate_cmd(
    ctx: click.Context,
    hypothesis_id: str,
    proposer_verdict: str,
    hypothesis_claim: str | None,
    hypotheses_file: str | None,
    target_file: str | None,
    output: Path | None,
    auto: bool,
    source_repo: str | None,
    source_sha: str | None,
) -> None:
    """Render an adversarial CHALLENGER prompt for a hypothesis verdict.

    Use this after a first agent has returned a verdict on a hypothesis.
    The challenger's prompt is shaped to find holes in the proposer's
    reasoning — the methodological fix for single-agent over-confidence.

    Workflow:
      1. Run `audit-pipeline recon` to render Layer 1 prompts
      2. Send a hypothesis prompt to your LLM, save the response as
         `recon/<hyp-id>_response.md`
      3. Run `audit-pipeline debate -i <hyp-id> -v recon/<hyp-id>_response.md`
      4. Send the challenger prompt to your LLM, save as
         `recon/debate/<hyp-id>_challenger_response.md`
      5. Compare the two verdicts. Stalemate -> escalate to Layer 2.
    """
    workspace = Path(ctx.obj["workspace"])

    # Validate snapshot args
    if source_sha and not source_repo:
        raise click.ClickException(
            "--source-sha requires --source-repo to be set."
        )

    # ---------- Source-mode resolution ----------
    # Debate doesn't read engine source directly (it only consumes the
    # proposer verdict markdown + a template), but if the caller passed
    # --source-repo we still open the snapshot for the duration of the
    # command so that any future template / grounding hooks see the same
    # ephemeral source tree the rest of the cycle is using.
    if source_repo:
        try:
            snap_cm = GitHubSnapshot(source_repo, source_sha)
        except SnapshotDownloadError as e:
            raise click.ClickException(f"snapshot init failed: {e}")
        snap = snap_cm.__enter__()
        console.print(
            f"  [cyan]Reading from snapshot {snap.repo_slug}@"
            f"{snap.resolved_sha[:7]}[/cyan] ({snap.workspace})"
        )
    else:
        snap_cm = None
        snap = None
        console.print(
            f"  [cyan]Reading from local workspace {workspace}[/cyan]"
        )

    try:
        return _debate_body(
            ctx=ctx,
            workspace=workspace,
            hypothesis_id=hypothesis_id,
            proposer_verdict=proposer_verdict,
            hypothesis_claim=hypothesis_claim,
            hypotheses_file=hypotheses_file,
            target_file=target_file,
            output=output,
            auto=auto,
        )
    finally:
        if snap_cm is not None:
            snap_cm.__exit__(None, None, None)


def _debate_body(
    *,
    ctx: click.Context,
    workspace: Path,
    hypothesis_id: str,
    proposer_verdict: str,
    hypothesis_claim: str | None,
    hypotheses_file: str | None,
    target_file: str | None,
    output: Path | None,
    auto: bool,
) -> None:
    """Body of debate, factored out so the snapshot lifecycle wraps it cleanly."""
    if output is None:
        output = workspace / "recon" / "debate"
    output.mkdir(parents=True, exist_ok=True)

    # Auto-derive claim + target_file from hypotheses.yaml if provided
    if hypotheses_file and (hypothesis_claim is None or target_file is None):
        import yaml
        with open(hypotheses_file) as f:
            hyp_data = yaml.safe_load(f)
        for h in hyp_data.get("hypotheses", []):
            if h["id"] == hypothesis_id:
                if hypothesis_claim is None:
                    hypothesis_claim = h.get("claim", "(see hypothesis brief)")
                if target_file is None:
                    target_file = h.get("target_file", "(see hypothesis brief)")
                break

    if hypothesis_claim is None:
        hypothesis_claim = "(claim not specified — see proposer verdict)"
    if target_file is None:
        target_file = "(target file not specified)"

    # Read the proposer's verdict + evidence
    proposer_text = Path(proposer_verdict).read_text(encoding="utf-8", errors="replace")

    # Locate the challenger prompt template
    from audit_pipeline import __file__ as pkg_init
    template_path = (
        Path(pkg_init).parent / "templates" / "agent_prompts" / "13_adversarial_debate.md"
    )
    if not template_path.exists():
        raise click.ClickException(f"Challenger prompt template missing at {template_path}")

    template = template_path.read_text(encoding="utf-8")

    rendered = render_placeholders(
        template,
        HYPOTHESIS_ID=hypothesis_id,
        HYPOTHESIS_CLAIM=hypothesis_claim,
        TARGET_FILE=target_file,
        PROPOSER_VERDICT=_extract_verdict_section(proposer_text),
        PROPOSER_EVIDENCE=proposer_text,
    )

    out_path = output / f"{hypothesis_id}_challenger.md"
    out_path.write_text(rendered, encoding="utf-8")

    if not auto:
        console.print(
            Panel.fit(
                f"[bold]Debate prompt rendered (render mode)[/bold]\n\n"
                f"Hypothesis:  {hypothesis_id}\n"
                f"Proposer:    {Path(proposer_verdict).name}\n"
                f"Challenger:  {out_path}\n\n"
                f"Send this to a fresh agent (NOT the same one that produced the\n"
                f"proposer verdict — adversarial integrity requires a clean reader).\n"
                f"Save the response as [cyan]{output}/{hypothesis_id}_challenger_response.md[/cyan].\n\n"
                f"Tip: pass [cyan]--auto[/cyan] to dispatch the challenger via\n"
                f"     Claude API and get the response written automatically.",
                title="Layer 1.5 - Adversarial debate",
            )
        )
        return

    # ---------- AUTO MODE ----------
    if not is_available():
        raise click.ClickException(
            "--auto requires ANTHROPIC_API_KEY in your environment. "
            "Either set the key or omit --auto."
        )

    console.print(
        f"[bold]Dispatching challenger via Claude...[/bold]\n"
        f"  Hypothesis:  {hypothesis_id}\n"
        f"  Proposer:    {Path(proposer_verdict).name}\n"
    )
    try:
        response = complete(rendered)
    except LLMUnavailable as e:
        raise click.ClickException(str(e))

    response_path = output / f"{hypothesis_id}_challenger_response.md"
    response_path.write_text(response.text, encoding="utf-8")

    challenger_verdict = _extract_challenger_verdict(response.text)
    proposer_verdict_text = _extract_verdict_section(proposer_text).strip()

    console.print(
        Panel.fit(
            f"[bold]Debate complete[/bold]\n\n"
            f"Hypothesis:  {hypothesis_id}\n\n"
            f"[bold]Proposer verdict:[/bold]\n  {_first_line(proposer_verdict_text)}\n\n"
            f"[bold]Challenger verdict:[/bold]\n  {challenger_verdict}\n\n"
            f"[bold]Disagree?[/bold] "
            f"{'[red]YES — escalate to Layer 2 PoC[/red]' if _verdicts_disagree(proposer_verdict_text, challenger_verdict) else '[green]No — verdicts converge[/green]'}\n\n"
            f"Tokens: in={response.input_tokens:,}, out={response.output_tokens:,}\n"
            f"Full challenger response: {response_path}",
            title="Layer 1.5 - Adversarial debate (auto mode)",
        )
    )


def _extract_challenger_verdict(text: str) -> str:
    """Pull the AGREE/DISAGREE/NEEDS_LAYER_2 line from a challenger response."""
    m = re.search(
        r"^##\s*Verdict\s*\n+(.+?)(?:\n##|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return "(verdict not found in response)"
    return _first_line(m.group(1).strip())


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def _verdicts_disagree(proposer: str, challenger: str) -> bool:
    """Heuristic: do the proposer and challenger verdicts disagree?

    Looks for TRUE/FALSE/AGREE/DISAGREE tokens. If unclear, defaults to
    True (escalate) — better to over-investigate than miss a finding.
    """
    p = proposer.upper()
    c = challenger.upper()
    if "DISAGREE" in c or "NEEDS_LAYER_2" in c:
        return True
    if "AGREE" in c and "DISAGREE" not in c:
        return False
    # Fallback: check for opposite TRUE/FALSE
    p_true = "TRUE" in p and "FALSE" not in p
    c_false = "FALSE" in c and "TRUE" not in c
    if p_true and c_false:
        return True
    return False


def _extract_verdict_section(text: str) -> str:
    """Extract just the '## Verdict' section from a markdown agent response.

    Falls back to first 500 chars if no Verdict section is found.
    """
    lines = text.splitlines()
    in_verdict = False
    out_lines: list[str] = []
    for line in lines:
        if line.lower().strip().startswith("## verdict"):
            in_verdict = True
            out_lines.append(line)
            continue
        if in_verdict and line.startswith("## ") and "verdict" not in line.lower():
            break
        if in_verdict:
            out_lines.append(line)

    if out_lines:
        return "\n".join(out_lines).strip()
    return text[:500] + ("..." if len(text) > 500 else "")
