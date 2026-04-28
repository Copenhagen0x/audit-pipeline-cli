"""`audit-pipeline synth-kani` — Kani harness from a natural-language invariant.

Layer 2.5 / Layer 3 author. Takes a sentence describing an invariant + the
target function, renders a prompt that asks an agent to produce a complete
Kani harness ready to compile and verify.

Collapses the highest-skill barrier in the pipeline (writing Kani) into
"describe what you want in English, get a proof attempt back."
"""

import json
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.utils import render_placeholders

console = Console()


@click.command(name="synth-kani")
@click.option(
    "--invariant",
    "-i",
    required=True,
    help="Natural-language description of the invariant to verify",
)
@click.option(
    "--engine-function",
    "-f",
    required=True,
    help="Engine function under test (e.g. absorb_protocol_loss)",
)
@click.option(
    "--harness-name",
    "-n",
    default=None,
    help="Snake_case name for the harness (defaults to <fn>_invariant)",
)
@click.option(
    "--engine-source",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to the engine source file (auto-derived from workspace if omitted)",
)
@click.option(
    "--mode",
    type=click.Choice(["safe", "cex"]),
    default="safe",
    show_default=True,
    help="safe = prove invariant holds; cex = prove invariant is violated",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir (defaults to <workspace>/recon/synth_kani/)",
)
@click.pass_context
def synth_kani_cmd(
    ctx: click.Context,
    invariant: str,
    engine_function: str,
    harness_name: str | None,
    engine_source: str | None,
    mode: str,
    output: Path | None,
) -> None:
    """Render a Kani-harness-from-invariant authoring prompt.

    Workflow:
      1. Run `audit-pipeline synth-kani -i "<invariant in english>" -f <fn>`
      2. Send the rendered prompt to your LLM
      3. Save the response (which contains a complete Rust harness) as
         `recon/synth_kani/<harness_name>.rs`
      4. Drop into the engine's tests/ dir and run:
         `cargo kani --tests --features test --harness <harness_name>`
      5. If verification fails to compile, feed the error back into the
         same conversation — the agent iterates on the harness until it
         compiles + verifies.

    The mode flag controls the assertion shape:
      safe = `assert!(invariant_holds);`     (expect PASS)
      cex  = `assert!(!invariant_holds);`    (expect VERIFICATION FAILED + CEX)
    """
    workspace = Path(ctx.obj["workspace"])

    if output is None:
        output = workspace / "recon" / "synth_kani"
    output.mkdir(parents=True, exist_ok=True)

    if harness_name is None:
        suffix = "_safe" if mode == "safe" else "_cex"
        harness_name = f"{engine_function}_invariant{suffix}"

    # Auto-locate engine source if omitted
    if engine_source is None:
        config_path = workspace / "workspace.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())
            candidate = (
                workspace / config["engine"]["local"] / "src" / "percolator.rs"
            )
            if candidate.exists():
                engine_source = str(candidate)

    function_signature = "(see engine source for signature)"
    if engine_source and Path(engine_source).exists():
        function_signature = _extract_function_signature(
            Path(engine_source).read_text(encoding="utf-8", errors="replace"),
            engine_function,
        )

    # Reasonable defaults for the constants block — user can edit later
    engine_constants = (
        "MAX_VAULT_TVL = 1e16\n"
        "MAX_ACCOUNT_POSITIVE_PNL = 1e32\n"
        "MAX_POSITION_ABS_Q = 1e14\n"
        "(extend with target-specific constants as needed)"
    )

    # Pull the structural reference template from the bundled templates dir
    from audit_pipeline import __file__ as pkg_init
    bundled_templates = Path(pkg_init).parent / "templates"
    structural_template_path = (
        bundled_templates / ("kani_safe_invariant.rs.template" if mode == "safe" else "kani_cex_panic_class.rs.template")
    )
    if not structural_template_path.exists():
        raise click.ClickException(
            f"Structural Kani template missing at {structural_template_path}"
        )
    kani_template_content = structural_template_path.read_text(encoding="utf-8")

    # Pull the synthesis prompt template
    template_path = (
        bundled_templates / "agent_prompts" / "15_kani_harness_from_invariant.md"
    )
    if not template_path.exists():
        raise click.ClickException(f"NL-Kani prompt template missing at {template_path}")

    template = template_path.read_text(encoding="utf-8")

    rendered = render_placeholders(
        template,
        INVARIANT_NL=invariant,
        ENGINE_PATH=engine_source or "(not specified)",
        ENGINE_FUNCTION=engine_function,
        FUNCTION_SIGNATURE=function_signature,
        ENGINE_CONSTANTS=engine_constants,
        KANI_TEMPLATE=kani_template_content,
    )

    out_path = output / f"{harness_name}_prompt.md"
    out_path.write_text(rendered, encoding="utf-8")

    # Also write the engine source helper if found, for the agent to grep
    if engine_source and Path(engine_source).exists():
        try:
            (output / f"{harness_name}_function_signature.txt").write_text(
                function_signature, encoding="utf-8"
            )
        except OSError:
            pass

    console.print(
        Panel.fit(
            f"[bold]NL-to-Kani synthesis prompt rendered[/bold]\n\n"
            f"Invariant:   {invariant!r}\n"
            f"Function:    {engine_function}\n"
            f"Mode:        {mode.upper()}\n"
            f"Harness:     {harness_name}\n"
            f"Prompt:      {out_path}\n\n"
            f"Send to your LLM. Save the harness body (Rust code in the\n"
            f"response) to:\n"
            f"  [cyan]{output}/{harness_name}.rs[/cyan]\n\n"
            f"Then run:\n"
            f"  [cyan]cargo kani --tests --features test --harness {harness_name}[/cyan]",
            title="Layer 2.5 / 3 - NL to Kani synthesis",
        )
    )


def _extract_function_signature(source: str, fn_name: str) -> str:
    """Extract the line(s) defining `fn <fn_name>(...)` from Rust source.

    Returns the signature lines up to and including the opening `{`. Falls
    back to a one-liner placeholder if the function is not found.
    """
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if f"fn {fn_name}(" in line or f"fn {fn_name} (" in line:
            sig_lines = [line]
            j = i
            while "{" not in sig_lines[-1] and j + 1 < len(lines):
                j += 1
                sig_lines.append(lines[j])
            return "\n".join(sig_lines).strip()
    return f"// fn {fn_name}(...) — signature not found in source"
