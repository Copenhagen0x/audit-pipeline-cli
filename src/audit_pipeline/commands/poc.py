"""`audit-pipeline poc` — instantiate a Layer-2 PoC test scaffold."""

from pathlib import Path

import click
from rich.console import Console

console = Console()


TEMPLATE_CHOICES = ["engine_native_poc", "engine_state_conservation_poc"]


@click.command(name="poc")
@click.option(
    "--finding",
    "-f",
    required=True,
    help="Short snake_case identifier for the finding (e.g. bug3_trade_open_overflow)",
)
@click.option(
    "--template",
    "-t",
    type=click.Choice(TEMPLATE_CHOICES),
    default="engine_native_poc",
    show_default=True,
    help=(
        "Template to instantiate. "
        "engine_native_poc = #[should_panic] for crash-class bugs. "
        "engine_state_conservation_poc = before/after invariant check for "
        "silent state-corruption bugs (e.g. F7-style residual growth)."
    ),
)
@click.option(
    "--engine-function",
    required=True,
    help="Engine function under test (e.g. advance_profit_warmup)",
)
@click.option(
    "--expected-panic-msg",
    default=None,
    help="Expected panic message (engine_native_poc only); omit for non-panic tests",
)
@click.option(
    "--invariant-description",
    default=None,
    help=(
        "One-line description of the conservation rule "
        "(engine_state_conservation_poc only)."
    ),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir (defaults to <workspace>/tests/engine/)",
)
@click.pass_context
def poc_cmd(
    ctx: click.Context,
    finding: str,
    template: str,
    engine_function: str,
    expected_panic_msg: str | None,
    invariant_description: str | None,
    output: Path | None,
) -> None:
    """Instantiate a Layer-2 PoC test scaffold from a template.

    The output is a Rust file at tests/engine/test_<finding>.rs with the
    template's placeholders replaced by your specific values. You then
    edit the file to add the witness state setup specific to your finding.

    Two templates are available:

      engine_native_poc  - For crash/panic-class bugs (overflow, unwrap,
                           divide-by-zero). Uses #[should_panic].

      engine_state_conservation_poc - For silent state-corruption bugs
                                      where the call returns Ok(()) but a
                                      conservation invariant is violated
                                      (e.g. F7 residual growth on insurance
                                      absorption). Uses before/after
                                      invariant comparison.
    """
    workspace = Path(ctx.obj["workspace"])

    if output is None:
        output = workspace / "tests" / "engine"
    output.mkdir(parents=True, exist_ok=True)

    # Locate bundled template
    from audit_pipeline import __file__ as pkg_init
    template_path = Path(pkg_init).parent / "templates" / f"{template}.rs.template"

    if not template_path.exists():
        raise click.ClickException(f"Template missing at {template_path}")

    content = template_path.read_text()

    # Replace placeholders shared across templates
    content = content.replace("<FINDING_NAME>", finding)
    content = content.replace("<engine_function_name>", engine_function)

    # Template-specific placeholders
    if template == "engine_native_poc":
        if expected_panic_msg:
            content = content.replace("<EXPECTED_PANIC_MSG>", expected_panic_msg)
        else:
            content = content.replace(
                "<EXPECTED_PANIC_MSG>",
                "TODO: insert exact panic message after first run",
            )
    elif template == "engine_state_conservation_poc":
        desc = invariant_description or (
            "TODO: describe the conservation rule (e.g. 'residual = vault - "
            "(c_tot + insurance) is preserved across the call')"
        )
        content = content.replace("<INVARIANT_DESCRIPTION>", desc)

    out_path = output / f"test_{finding}.rs"
    if out_path.exists():
        raise click.ClickException(
            f"{out_path} already exists. Refusing to overwrite. "
            f"Delete the existing file first OR use a different --finding name."
        )

    out_path.write_text(content)
    console.print(f"[green]Wrote {out_path}[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Edit the witness state setup to your finding's specifics")
    if template == "engine_state_conservation_poc":
        console.print(
            "  2. Adapt the `invariant` closure to your conservation formula"
        )
        console.print(
            f"  3. Run: [cyan]cargo test --features test --test test_{finding}[/cyan]"
        )
        console.print(
            "  4. The conservation test should FAIL (bug confirmed); the "
            "sanity test should pass."
        )
    else:
        console.print(
            f"  2. Run: [cyan]cargo test --features test --test test_{finding}[/cyan]"
        )
        console.print(
            "  3. If panic message differs from expected, update "
            "#[should_panic(expected = ...)] with the actual message"
        )
