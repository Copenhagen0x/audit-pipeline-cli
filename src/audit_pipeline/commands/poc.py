"""`audit-pipeline poc` — instantiate a Layer-2 PoC test scaffold."""

import json
from pathlib import Path

import click
from rich.console import Console

console = Console()


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
    type=click.Choice(["engine_native_poc"]),
    default="engine_native_poc",
    show_default=True,
    help="Template to instantiate",
)
@click.option(
    "--engine-function",
    required=True,
    help="Engine function under test (e.g. advance_profit_warmup)",
)
@click.option(
    "--expected-panic-msg",
    default=None,
    help="Expected panic message (for #[should_panic]); omit for non-panic tests",
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
    output: Path | None,
) -> None:
    """Instantiate a Layer-2 PoC test scaffold from a template.

    The output is a Rust file at tests/engine/test_<finding>.rs with the
    template's placeholders replaced by your specific values. You then
    edit the file to add the witness state setup specific to your finding.
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

    # Replace placeholders
    content = content.replace("<FINDING_NAME>", finding)
    content = content.replace("<engine_function_name>", engine_function)
    if expected_panic_msg:
        content = content.replace("<EXPECTED_PANIC_MSG>", expected_panic_msg)
    else:
        content = content.replace("<EXPECTED_PANIC_MSG>", "TODO: insert exact panic message after first run")

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
    console.print(f"  2. Run: [cyan]cargo test --features test --test test_{finding}[/cyan]")
    console.print(
        f"  3. If panic message differs from expected, update #[should_panic(expected = ...)] "
        f"with the actual message"
    )
