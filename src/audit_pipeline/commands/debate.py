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
@click.option(
    "--challenger-model",
    default=None,
    help=(
        "L1.5 audit Defect — debate independence. The challenger normally "
        "uses the same model as recon (DEFAULT_MODEL), defeating the point "
        "of an adversarial second opinion. Pass a different model "
        "(e.g. ``claude-opus-4-5``) so a same-mistake bias in one model "
        "doesn't propagate into the debate."
    ),
)
@click.option(
    "--redact-proposer-evidence/--full-proposer-evidence",
    default=False,
    show_default=True,
    help=(
        "Pass only the proposer's verdict line (not the line citations + "
        "code snippets the proposer pulled) to the challenger. Forces the "
        "challenger to re-derive evidence rather than rubber-stamp the "
        "proposer's reads."
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
    challenger_model: str | None,
    redact_proposer_evidence: bool,
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
            challenger_model=challenger_model,
            redact_proposer_evidence=redact_proposer_evidence,
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
    challenger_model: str | None = None,
    redact_proposer_evidence: bool = False,
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

    # L1.5 audit Defect — independence. When --redact-proposer-evidence is
    # set, only the verdict line goes to the challenger so the challenger
    # has to re-derive evidence rather than skim+rubber-stamp.
    if redact_proposer_evidence:
        proposer_evidence_for_challenger = (
            "(redacted — challenger must re-derive evidence independently. "
            "Only the proposer's final verdict line is below.)"
        )
    else:
        proposer_evidence_for_challenger = proposer_text

    rendered = render_placeholders(
        template,
        HYPOTHESIS_ID=hypothesis_id,
        HYPOTHESIS_CLAIM=hypothesis_claim,
        TARGET_FILE=target_file,
        # POST-AUDIT FIX: pass redacted=True so the verdict section is
        # collapsed to a single line under redaction. Otherwise a verbose
        # proposer can write "TRUE / HIGH because foo.rs:42..." inside
        # the verdict block and leak evidence through this placeholder.
        PROPOSER_VERDICT=_extract_verdict_section(proposer_text, redacted=redact_proposer_evidence),
        PROPOSER_EVIDENCE=proposer_evidence_for_challenger,
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
        if challenger_model:
            response = complete(rendered, model=challenger_model)
        else:
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

    POST-AUDIT FIX: the previous `return False` fallback contradicted
    the docstring's "defaults to True" promise. A garbage / unparseable
    challenger response silently downgraded to "no disagreement" — i.e.
    rubber-stamped the proposer. Now correctly returns True on unclear
    output so the cycle escalates to Layer 2 PoC.
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
    # Unclear / no parseable verdict tokens — err toward escalation per
    # the "over-investigate beats miss-a-finding" invariant.
    return True


def _extract_verdict_section(text: str, redacted: bool = False) -> str:
    """Extract just the '## Verdict' section from a markdown agent response.

    When ``redacted=True`` (the L1.5 independence mode), strip everything
    except a SINGLE LINE containing the verdict tokens (TRUE / FALSE /
    INCONCLUSIVE + confidence). A proposer who wrote inline reasoning
    inside the verdict block can otherwise leak evidence through this
    placeholder even though the operator passed --redact-proposer-evidence.

    Falls back to a very small slice (200 chars) when no Verdict header
    is present; the previous 500-char fallback occasionally pulled in
    the entire evidence section verbatim.
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

    if not out_lines:
        # No `## Verdict` header. Look for a single-line verdict pattern
        # ("TRUE / HIGH", "FALSE / LOW", etc) anywhere in the text. If
        # found, return only that line — never a freeform prose slice.
        #
        # POST-AUDIT FIX (2026-05-12): re.IGNORECASE so lowercase
        # `true / high` style verdicts also match — agents sometimes
        # downcase by accident and we want to surface them.
        import re as _re
        m = _re.search(
            r"\b(TRUE|FALSE|INCONCLUSIVE|NEEDS_LAYER_2(?:_TO_DECIDE)?|AGREE|DISAGREE)\b\s*/\s*\b(HIGH|MEDIUM|LOW)\b",
            text,
            _re.IGNORECASE,
        )
        if m:
            return m.group(0)
        # Last resort: a tight 200-char slice (was 500 — too much room
        # for the proposer's evidence to leak into the challenger prompt).
        return text[:200] + ("..." if len(text) > 200 else "")

    section = "\n".join(out_lines).strip()
    if not redacted:
        return section

    # Redacted mode: keep ONLY a verdict-shaped line found inside the
    # verdict section. Strips any "TRUE / HIGH because the haircut
    # residual at line 1684 grows by..." style smuggling.
    #
    # POST-AUDIT FIX (2026-05-12 re-audit catch):
    #   1. Skip fenced code blocks — a proposer could plant a fake
    #      `FALSE / LOW` bait verdict inside ```fenced``` ahead of
    #      the real one to spoof the challenger. Lines inside
    #      ```...``` (or ~~~...~~~) are now ignored.
    #   2. Prefer the LAST verdict-shaped line found, not the first.
    #      LLM agents commonly write a quick "draft: TRUE / HIGH"
    #      thought line ahead of the final "Verdict: FALSE / LOW"
    #      line; the last occurrence is the one we should trust.
    #   3. re.IGNORECASE for the verdict tokens, same rationale as
    #      the no-header branch above.
    import re as _re
    in_fence = False
    candidate: str | None = None
    for ln in section.splitlines():
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _re.search(
            r"\b(TRUE|FALSE|INCONCLUSIVE|NEEDS_LAYER_2(?:_TO_DECIDE)?|AGREE|DISAGREE)\b\s*/\s*\b(HIGH|MEDIUM|LOW)\b",
            ln,
            _re.IGNORECASE,
        )
        if m:
            candidate = m.group(0)
    if candidate is not None:
        return candidate
    # Header present but no parseable verdict line. Return the header
    # itself only (no body), so we don't leak the body.
    return "## Verdict\n(unparseable verdict — challenger should treat as INCONCLUSIVE)"
