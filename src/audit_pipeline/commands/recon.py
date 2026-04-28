"""`audit-pipeline recon` — build Layer-1 multi-agent prompts from a hypothesis list.

Reads a hypotheses.yaml file, instantiates the appropriate agent prompt
template for each hypothesis, and writes ready-to-send prompts to disk.

The CLI does NOT spawn the agents itself — that's the user's job (via
their LLM of choice). The CLI's role is to assemble well-formed prompts
so the user only needs to copy-paste.
"""

import json
from pathlib import Path

import click
import yaml
from rich.console import Console

from audit_pipeline.utils import render_placeholders

console = Console()


HYPOTHESIS_CLASS_TO_TEMPLATE = {
    "implicit_invariant": "02_implicit_invariant_hunt.md",
    "arithmetic_overflow": "03_arithmetic_overflow_class_audit.md",
    "state_transition": "04_state_transition_completeness.md",
    "authorization": "05_authorization_chain_trace.md",
    "panic_site": "06_panic_site_enumeration.md",
    "reachability": "07_call_chain_reachability.md",
    "invariant_property": "08_invariant_property_definition.md",
}


@click.command(name="recon")
@click.option(
    "--hypotheses",
    "-h",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file describing hypotheses to investigate",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory for prompt files (defaults to <workspace>/recon/)",
)
@click.pass_context
def recon_cmd(ctx: click.Context, hypotheses: str, output: Path | None) -> None:
    """Build Layer-1 multi-agent recon prompts from a hypothesis list.

    The hypotheses YAML file should have:

      hypotheses:
        - id: H1
          class: implicit_invariant
          claim: "..."
          target_file: "..."
          target_lines: "..."
        - id: H2
          ...

    Output is one prompt file per hypothesis, ready to send to your LLM.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException(f"No workspace.json at {config_path}. Run `audit-pipeline init` first.")

    config = json.loads(config_path.read_text())

    # Default output dir
    if output is None:
        output = workspace / "recon"
    output.mkdir(parents=True, exist_ok=True)

    # Load hypotheses
    with open(hypotheses) as f:
        hyp_data = yaml.safe_load(f)

    if "hypotheses" not in hyp_data:
        raise click.ClickException(
            f"{hypotheses} must contain a top-level 'hypotheses' key with a list."
        )

    # Locate orientation prompt + class-specific prompts
    from audit_pipeline import __file__ as pkg_init
    prompts_dir = Path(pkg_init).parent / "templates" / "agent_prompts"

    orientation_path = prompts_dir / "00_orientation.md"
    if not orientation_path.exists():
        raise click.ClickException(f"Orientation prompt missing at {orientation_path}")

    orientation = orientation_path.read_text()

    # Generate per-hypothesis prompt files
    for hyp in hyp_data["hypotheses"]:
        hyp_id = hyp["id"]
        hyp_class = hyp.get("class", "implicit_invariant")

        if hyp_class not in HYPOTHESIS_CLASS_TO_TEMPLATE:
            console.print(f"[yellow]Warning:[/yellow] {hyp_id} has unknown class '{hyp_class}'; using implicit_invariant template.")
            hyp_class = "implicit_invariant"

        template_filename = HYPOTHESIS_CLASS_TO_TEMPLATE[hyp_class]
        template_path = prompts_dir / template_filename
        if not template_path.exists():
            raise click.ClickException(f"Template missing: {template_path}")

        template_content = template_path.read_text()

        # Substitute {KEY} placeholders in BOTH orientation and class template.
        # Class templates reference paths like {ENGINE_PATH} / {SPEC_PATH} that
        # the recon command needs to fill in alongside the orientation kwargs.
        local_engine_path = str(workspace / config["engine"]["local"])
        local_wrapper_path = str(workspace / config["wrapper"]["local"])
        substitutions = {
            "ENGINE_REPO_URL": config["engine"]["repo"],
            "ENGINE_SHA": config["engine"]["sha"],
            "WRAPPER_REPO_URL": config["wrapper"]["repo"],
            "WRAPPER_SHA": config["wrapper"]["sha"],
            "LOCAL_ENGINE_PATH": local_engine_path,
            "LOCAL_WRAPPER_PATH": local_wrapper_path,
            "ENGINE_PATH": local_engine_path,
            "WRAPPER_PATH": local_wrapper_path,
            "SPEC_PATH": str(Path(local_engine_path) / "spec.md"),
            "LIST_RELEVANT_CONSTANTS": hyp.get("relevant_constants", "(none specified)"),
            "LIST_RELEVANT_INSTRUCTIONS": hyp.get("relevant_instructions", "(none specified)"),
        }

        rendered_orientation = render_placeholders(orientation, **substitutions)
        rendered_class_template = render_placeholders(template_content, **substitutions)

        full_prompt = f"""{rendered_orientation}

---

{rendered_class_template}

---

# Specific hypothesis to investigate

ID:           {hyp_id}
Claim:        {hyp.get("claim", "(see hypothesis brief above)")}
Target file:  {hyp.get("target_file", "(see hypothesis brief above)")}
Target lines: {hyp.get("target_lines", "(see hypothesis brief above)")}
Notes:        {hyp.get("notes", "(none)")}
"""

        out_path = output / f"{hyp_id}_prompt.md"
        out_path.write_text(full_prompt)
        console.print(f"  [green]wrote[/green] {out_path}")

    console.print()
    console.print(f"[bold green]Built {len(hyp_data['hypotheses'])} prompts in {output}/[/bold green]")
    console.print(
        "  Send each prompt to your LLM (Claude with subagent dispatch, or equivalent)."
    )
    console.print(
        "  Save responses as <hyp-id>_response.md in the same directory for synthesis."
    )
