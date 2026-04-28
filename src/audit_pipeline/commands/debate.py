"""`audit-pipeline debate` — render a challenger prompt for adversarial verdict review.

The Layer 1.5 step. Takes a first agent's verdict + evidence on a hypothesis
and renders a CHALLENGER prompt whose job is to find any hole in the
proposer's reasoning. The challenger is the methodological fix for the
F7-style failure mode where a single agent returns FALSE/HIGH and is wrong.
"""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.utils import render_placeholders

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
@click.pass_context
def debate_cmd(
    ctx: click.Context,
    hypothesis_id: str,
    proposer_verdict: str,
    hypothesis_claim: str | None,
    hypotheses_file: str | None,
    target_file: str | None,
    output: Path | None,
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

    console.print(
        Panel.fit(
            f"[bold]Debate prompt rendered[/bold]\n\n"
            f"Hypothesis:  {hypothesis_id}\n"
            f"Proposer:    {Path(proposer_verdict).name}\n"
            f"Challenger:  {out_path}\n\n"
            f"Send this to a fresh agent (NOT the same one that produced the\n"
            f"proposer verdict — adversarial integrity requires a clean reader).\n"
            f"Save the response as [cyan]{output}/{hypothesis_id}_challenger_response.md[/cyan].",
            title="Layer 1.5 - Adversarial debate",
        )
    )


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
