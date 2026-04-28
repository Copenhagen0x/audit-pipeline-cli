"""`audit-pipeline spec-check` — render a spec ↔ code consistency analysis prompt.

Layer 0: the pre-hypothesis step. Takes a target's spec doc + main source
file and renders a prompt for an agent to surface every place the spec
disagrees with the code. Each gap becomes a Layer 1 hypothesis candidate.

This is the methodology behind F7 — the spec said insurance absorption
preserves the haircut residual, the code didn't. Automating that gap
detection turns spec drift into a first-class finding source.
"""

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.utils import render_placeholders

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
@click.pass_context
def spec_check_cmd(
    ctx: click.Context,
    spec: str,
    code: str,
    output: Path | None,
    name: str | None,
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

    console.print(
        Panel.fit(
            f"[bold]Spec/code gap analysis prompt rendered[/bold]\n\n"
            f"Spec:    {spec_path} ({spec_lines:,} lines, {len(spec_content):,} chars)\n"
            f"Code:    {code_path} ({code_lines:,} lines, {len(code_content):,} chars)\n"
            f"Prompt:  {out_path}\n\n"
            f"Send to your LLM. Save the response as\n"
            f"[cyan]{output}/{name}_response.md[/cyan].\n\n"
            f"Each gap in the response is a candidate hypothesis for "
            f"`audit-pipeline recon`.",
            title="Layer 0 - Spec vs code gap analysis",
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
