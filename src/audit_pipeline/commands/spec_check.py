"""`audit-pipeline spec-check` — spec ↔ code consistency analysis.

Layer 0: the pre-hypothesis step. Two modes:

  render mode (default): writes a gap-analysis prompt for the user's LLM.

  --auto mode: calls Claude with the gap-analysis prompt, parses the
    response to extract structured spec-vs-code gaps, and writes a
    `hypotheses_from_gaps.yaml` file ready to feed straight into
    `audit-pipeline recon`. This turns "spec drift detection" from a
    multi-step manual workflow into a single command.
"""

import re
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from audit_pipeline.utils import (
    LLMUnavailable,
    complete,
    is_available,
    render_placeholders,
)

console = Console()

# How much of each file to inline in the prompt before truncating.
# Keep aggressive enough to fit even Sonnet's 1M context window comfortably
# alongside the prompt scaffolding, the spec, and an arbitrary code file.
MAX_SPEC_CHARS = 80_000
MAX_CODE_CHARS = 200_000


@click.command(name="spec-check")
@click.option(
    "--spec",
    "-s",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the spec markdown / text file",
)
@click.option(
    "--code",
    "-c",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the implementation source file (e.g. src/percolator.rs)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir (defaults to <workspace>/recon/spec_check/)",
)
@click.option(
    "--name",
    "-n",
    default=None,
    help="Output filename stem (defaults to <code-filename-stem>_gap_analysis)",
)
@click.option(
    "--auto",
    is_flag=True,
    help=(
        "Actually call Claude with the gap-analysis prompt, parse the "
        "structured response, and write hypotheses_from_gaps.yaml ready "
        "for `audit-pipeline recon`. Requires ANTHROPIC_API_KEY."
    ),
)
@click.pass_context
def spec_check_cmd(
    ctx: click.Context,
    spec: str,
    code: str,
    output: Path | None,
    name: str | None,
    auto: bool,
) -> None:
    """Render a spec ↔ code gap-analysis prompt.

    Reads SPEC and CODE, inlines them into the gap-analysis prompt template,
    writes the rendered prompt to disk for the user to feed to their LLM.

    The agent's response should be saved as <name>_response.md in the same
    output directory. Each gap in the response becomes a candidate
    hypothesis for `audit-pipeline recon` in the next phase.
    """
    workspace = Path(ctx.obj["workspace"])

    if output is None:
        output = workspace / "recon" / "spec_check"
    output.mkdir(parents=True, exist_ok=True)

    spec_path = Path(spec)
    code_path = Path(code)

    spec_content = spec_path.read_text(encoding="utf-8", errors="replace")
    code_content = code_path.read_text(encoding="utf-8", errors="replace")

    spec_truncated = _truncate_with_marker(spec_content, MAX_SPEC_CHARS, "spec")
    code_truncated = _truncate_with_marker(code_content, MAX_CODE_CHARS, "code")

    if name is None:
        name = f"{code_path.stem}_gap_analysis"

    # Locate the gap-analysis prompt template
    from audit_pipeline import __file__ as pkg_init
    template_path = (
        Path(pkg_init).parent / "templates" / "agent_prompts" / "14_spec_code_gap_analysis.md"
    )
    if not template_path.exists():
        raise click.ClickException(f"Gap-analysis prompt template missing at {template_path}")

    template = template_path.read_text(encoding="utf-8")

    rendered = render_placeholders(
        template,
        SPEC_PATH=str(spec_path),
        SPEC_CONTENT=spec_truncated,
        CODE_PATH=str(code_path),
        CODE_CONTENT=code_truncated,
    )

    out_path = output / f"{name}.md"
    out_path.write_text(rendered, encoding="utf-8")

    spec_lines = spec_content.count("\n") + 1
    code_lines = code_content.count("\n") + 1

    if not auto:
        console.print(
            Panel.fit(
                f"[bold]Spec/code gap analysis prompt rendered (render mode)[/bold]\n\n"
                f"Spec:    {spec_path} ({spec_lines:,} lines, {len(spec_content):,} chars)\n"
                f"Code:    {code_path} ({code_lines:,} lines, {len(code_content):,} chars)\n"
                f"Prompt:  {out_path}\n\n"
                f"Send to your LLM. Save the response as\n"
                f"[cyan]{output}/{name}_response.md[/cyan].\n\n"
                f"Tip: pass [cyan]--auto[/cyan] to dispatch via Claude API and\n"
                f"     get a hypotheses_from_gaps.yaml back (skip the manual paste).",
                title="Layer 0 - Spec vs code gap analysis",
            )
        )
        return

    # ---------- AUTO MODE ----------
    if not is_available():
        raise click.ClickException(
            "--auto requires ANTHROPIC_API_KEY in your environment. "
            "Set the key or omit --auto."
        )

    console.print(
        f"[bold]Dispatching gap analysis via Claude...[/bold]\n"
        f"  Spec:  {spec_path} ({spec_lines:,} lines)\n"
        f"  Code:  {code_path} ({code_lines:,} lines)\n"
    )
    try:
        response = complete(rendered, max_tokens=8192)
    except LLMUnavailable as e:
        raise click.ClickException(str(e))

    response_path = output / f"{name}_response.md"
    response_path.write_text(response.text, encoding="utf-8")

    # Parse the structured gap table from the response
    gaps = _parse_gap_table(response.text)
    summary = _parse_summary(response.text)

    # Write hypotheses_from_gaps.yaml — ready for `audit-pipeline recon`
    yaml_path = output / "hypotheses_from_gaps.yaml"
    hyp_data = {"hypotheses": []}
    for i, gap in enumerate(gaps, start=1):
        hyp_data["hypotheses"].append({
            "id": f"GAP{i:02d}-{_slugify(gap['summary'])}",
            "class": _gap_class_to_hypothesis_class(gap.get("gap_class", "DRIFT")),
            "claim": gap["summary"],
            "target_file": gap.get("code_location", str(code_path)),
            "target_lines": "(see gap analysis response)",
            "notes": (
                f"Spec drift candidate from spec-check auto-mode.\n"
                f"Spec claim: {gap.get('spec_claim', '(see response)')}\n"
                f"Severity guess: {gap.get('severity', 'UNKNOWN')}\n"
                f"Gap class: {gap.get('gap_class', 'DRIFT')}"
            ),
        })
    yaml_path.write_text(
        yaml.safe_dump(hyp_data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    # Display summary
    table = Table(title=f"Gaps surfaced ({len(gaps)} total)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Class", style="bold")
    table.add_column("Severity")
    table.add_column("Summary", style="cyan")

    for i, gap in enumerate(gaps[:10], start=1):
        sev = gap.get("severity", "?")
        sev_style = {
            "CRITICAL": "[bold red]CRITICAL[/bold red]",
            "HIGH": "[red]HIGH[/red]",
            "MED": "[yellow]MED[/yellow]",
            "LOW": "[dim]LOW[/dim]",
        }.get(sev.upper(), sev)
        table.add_row(str(i), gap.get("gap_class", "?"), sev_style, gap["summary"][:80])

    console.print(table)
    if summary:
        console.print(
            Panel.fit(
                summary,
                title="Summary from response",
            )
        )
    console.print(
        Panel.fit(
            f"Full response: [cyan]{response_path}[/cyan]\n"
            f"Hypotheses YAML: [cyan]{yaml_path}[/cyan]\n\n"
            f"Next step:\n"
            f"  [cyan]audit-pipeline recon -h {yaml_path}[/cyan]\n\n"
            f"Tokens: in={response.input_tokens:,}, out={response.output_tokens:,}",
            title="Done",
        )
    )


def _truncate_with_marker(text: str, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    omitted = len(text) - max_chars
    return (
        f"{head}\n\n"
        f"[... {omitted:,} chars of {label} truncated for prompt size ...]\n\n"
        f"{tail}"
    )


# ---------------------------------------------------------------------------
# Auto-mode response parsing
# ---------------------------------------------------------------------------


def _parse_gap_table(text: str) -> list[dict[str, str]]:
    """Extract the 'Gaps worth promoting to Layer 1 hypothesis' markdown table.

    Returns a list of dicts: {spec_claim, code_location, gap_class, severity, summary}.
    Falls back to empty list if the table isn't found (auto-mode still useful
    via the raw response file).
    """
    gaps: list[dict[str, str]] = []

    # Find the table: look for header row matching "Spec claim | Code location | Class | Severity | ... summary"
    table_pattern = re.compile(
        r"\|\s*Spec claim\s*\|\s*Code location\s*\|\s*Class\s*\|\s*Severity[^|]*\|\s*[^|]*summary[^|]*\|",
        re.IGNORECASE,
    )
    m = table_pattern.search(text)
    if not m:
        return gaps

    # Walk forward from the header, parsing pipe-delimited rows until blank or
    # next markdown heading
    lines = text[m.start():].splitlines()
    # Skip header + separator
    for raw in lines[2:]:
        if not raw.strip():
            break
        if raw.startswith("##") or raw.startswith("---"):
            break
        if not raw.startswith("|"):
            break
        cells = [c.strip() for c in raw.strip("|").split("|")]
        if len(cells) < 5:
            continue
        gaps.append({
            "spec_claim": cells[0],
            "code_location": cells[1],
            "gap_class": cells[2],
            "severity": cells[3],
            "summary": cells[4],
        })
    return gaps


def _parse_summary(text: str) -> str | None:
    """Pull the '## Summary' section block from the response, if present."""
    m = re.search(
        r"^##\s*Summary\s*\n+(.+?)(?:\n##|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return None
    return m.group(1).strip()


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(text: str) -> str:
    """Make a filesystem/yaml-safe ID from free text."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:40] if slug else "untitled"


def _gap_class_to_hypothesis_class(gap_class: str) -> str:
    """Map spec-check gap classes to recon hypothesis classes."""
    mapping = {
        "DRIFT": "implicit_invariant",
        "MISSING": "state_transition",
        "CONTRADICTION": "implicit_invariant",
        "MATCH": "implicit_invariant",  # shouldn't appear, but safe default
    }
    return mapping.get(gap_class.upper().strip(), "implicit_invariant")
