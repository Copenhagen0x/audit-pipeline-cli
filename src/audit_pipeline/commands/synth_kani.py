"""`audit-pipeline synth-kani` — Kani harness from a natural-language invariant.

Layer 2.5 / Layer 3 author. Two modes:

  render mode (default): writes a Kani-authoring prompt for the user to
    feed to their LLM manually. Best for interactive use.

  --auto mode: actually calls Claude, generates the harness, runs
    `cargo check`, feeds compile errors back into the conversation,
    iterates up to --max-iterations times until the harness compiles.
    Optionally runs `cargo kani` and reports the verdict.

Auto mode collapses the highest-skill barrier in the pipeline (writing Kani
that actually compiles) into "describe what you want in English, get a
verified proof attempt back."
"""

import json
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
from audit_pipeline.utils.rust_compile import (
    cargo_kani,
    cargo_kani_codegen_check,
    extract_rust_code_block,
)

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
@click.option(
    "--auto",
    is_flag=True,
    help=(
        "Actually call the LLM, generate harness, run `cargo check`, "
        "feed errors back, and iterate until it compiles. Requires "
        "ANTHROPIC_API_KEY to be set."
    ),
)
@click.option(
    "--max-iterations",
    type=int,
    default=4,
    show_default=True,
    help="Max compile-fix-retry rounds in --auto mode",
)
@click.option(
    "--run-kani",
    is_flag=True,
    help=(
        "After the harness compiles in --auto mode, run `cargo kani` and "
        "report the verdict. May take 10-30 minutes per harness."
    ),
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
    auto: bool,
    max_iterations: int,
    run_kani: bool,
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
    engine_source_full = ""
    if engine_source and Path(engine_source).exists():
        engine_source_text = Path(engine_source).read_text(encoding="utf-8", errors="replace")
        function_signature = _extract_function_signature(
            engine_source_text,
            engine_function,
        )
        # Include the FULL engine source so the LLM can read actual struct
        # field names + helper signatures, not invent them from the function
        # signature alone. Same fix pattern as Layer 2 poc_llm.py — without
        # this the LLM hallucinates fields like `matured_pos_tot` when the
        # real one is `pnl_matured_pos_tot`, then cargo kani fails to build.
        engine_source_full = (
            f"### FULL ENGINE FILE `{Path(engine_source).name}` "
            f"(authoritative struct + helper definitions)\n"
            f"```rust\n{engine_source_text}\n```"
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
        ENGINE_SOURCE_FULL=engine_source_full,
        HARNESS_NAME=harness_name,
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

    if not auto:
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
                f"  [cyan]cargo kani --tests --features test --harness {harness_name}[/cyan]\n\n"
                f"Tip: pass [cyan]--auto[/cyan] to run the full compile-fix-retry loop\n"
                f"     end-to-end (requires ANTHROPIC_API_KEY).",
                title="Layer 2.5 / 3 - NL to Kani synthesis (render mode)",
            )
        )
        return

    # ---------- AUTO MODE ----------
    if not is_available():
        raise click.ClickException(
            "--auto mode requires ANTHROPIC_API_KEY in your environment "
            "AND the anthropic SDK installed. Set the key or omit --auto."
        )

    # Resolve engine dir for cargo check
    engine_dir = _resolve_engine_dir(workspace)
    if engine_dir is None:
        raise click.ClickException(
            "--auto mode needs to run `cargo check` against the engine, "
            "but no engine source dir was found in workspace.json. Either "
            "run `audit-pipeline init` with a clone, or pass --engine-source."
        )

    harness_path = engine_dir / "tests" / f"{harness_name}.rs"
    transcript_path = output / f"{harness_name}_iteration_log.md"

    console.print(
        Panel.fit(
            f"[bold]NL-to-Kani synthesis (AUTO MODE)[/bold]\n\n"
            f"Invariant:   {invariant!r}\n"
            f"Function:    {engine_function}\n"
            f"Mode:        {mode.upper()}\n"
            f"Harness:     {harness_name}\n"
            f"Engine dir:  {engine_dir}\n"
            f"Will write:  {harness_path}\n"
            f"Max rounds:  {max_iterations}\n"
            f"Run kani:    {run_kani}\n",
            title="Layer 2.5 / 3 - NL to Kani synthesis",
        )
    )

    transcript: list[str] = [
        "# NL-to-Kani auto synthesis log\n",
        f"- Invariant: `{invariant}`",
        f"- Function: `{engine_function}`",
        f"- Mode: `{mode}`",
        f"- Max iterations: {max_iterations}",
        "",
    ]

    # Round 1: initial generation
    console.print("[bold]Round 1[/bold] — generating initial harness...")
    try:
        response = complete(rendered)
    except LLMUnavailable as e:
        raise click.ClickException(str(e))
    transcript.append("## Round 1 — initial generation\n")
    transcript.append(f"_Tokens: in={response.input_tokens:,}, out={response.output_tokens:,}_\n")

    code = extract_rust_code_block(response.text)
    if code is None:
        transcript.append("**FAILED**: no Rust code block in response\n")
        transcript_path.write_text("\n".join(transcript), encoding="utf-8")
        raise click.ClickException(
            "LLM response did not contain a Rust code block. "
            f"See {transcript_path} for the raw response."
        )
    harness_path.parent.mkdir(parents=True, exist_ok=True)
    code = _strip_kani_unwind(code)
    harness_path.write_text(code, encoding="utf-8")
    transcript.append(f"Wrote {harness_path}\n")

    # Compile-fix-retry loop
    iteration = 1
    last_check: object = None
    while iteration <= max_iterations:
        console.print(f"  Compiling [cyan]{harness_path.name}[/cyan]...")
        # Use cargo kani --only-codegen instead of cargo check. Kani harnesses
        # are gated by `#![cfg(kani)]` so cargo check skips them silently and
        # always reports OK — the LLM never sees its compile errors. Earlier
        # attempts to add `--cfg kani` to cargo check failed because the
        # `kani` crate (which provides kani::any/assume) is only injected by
        # cargo kani's own rustc wrapper. --only-codegen does the full
        # kani-aware compile but stops before symbolic execution.
        check = cargo_kani_codegen_check(engine_dir, harness_name)
        last_check = check
        transcript.append(f"### cargo check (round {iteration})\n")
        transcript.append(f"- ok: {check.ok}")
        transcript.append(f"- errors: {len(check.errors)}")
        transcript.append(f"- warnings: {check.warnings_count}\n")

        if check.ok:
            console.print(
                f"  [green]Compiles![/green] (after {iteration} round(s))"
            )
            break

        if iteration == max_iterations:
            console.print(
                f"  [red]Hit max iterations ({max_iterations}) without "
                f"clean compile.[/red]"
            )
            break

        iteration += 1
        console.print(
            f"  [yellow]{len(check.errors)} compile error(s); asking "
            f"LLM to fix... (round {iteration})[/yellow]"
        )
        fix_prompt = _build_fix_prompt(rendered, code, check.errors)
        try:
            response = complete(fix_prompt)
        except LLMUnavailable as e:
            raise click.ClickException(str(e))
        transcript.append(f"## Round {iteration} — fix attempt\n")
        transcript.append(
            f"_Tokens: in={response.input_tokens:,}, out={response.output_tokens:,}_\n"
        )

        new_code = extract_rust_code_block(response.text)
        if new_code is None:
            transcript.append("**FAILED**: no Rust code block in fix response\n")
            console.print(
                "  [red]LLM fix response had no code block. Stopping.[/red]"
            )
            break
        code = _strip_kani_unwind(new_code)
        harness_path.write_text(code, encoding="utf-8")
        transcript.append(f"Rewrote {harness_path}\n")

    transcript_path.write_text("\n".join(transcript), encoding="utf-8")

    if not last_check or not last_check.ok:
        raise click.ClickException(
            f"Harness did not compile after {iteration} round(s). "
            f"See {transcript_path} for the iteration log and "
            f"{harness_path} for the latest harness."
        )

    if not run_kani:
        console.print(
            f"\n[bold green]Done.[/bold green] Harness compiles. "
            f"Run `cargo kani --tests --features test --harness {harness_name}` "
            f"to verify, or re-run with --run-kani to do it now."
        )
        return

    # Optional: actually run cargo kani
    console.print(f"\n[bold]Running cargo kani --harness {harness_name}...[/bold]")
    console.print("[dim](this can take 10-30 minutes; output streamed below)[/dim]")
    kani = cargo_kani(engine_dir, harness_name)
    transcript.append("\n## cargo kani verdict\n")
    transcript.append(f"- verdict: **{kani.verdict}**")
    transcript.append(f"- ok: {kani.ok}\n")
    transcript_path.write_text("\n".join(transcript), encoding="utf-8")

    color = {
        "PASS": "green",
        "FAIL": "red",
        "TIMEOUT": "yellow",
        "UNKNOWN": "dim",
    }.get(kani.verdict, "dim")
    console.print(f"[bold {color}]Kani verdict: {kani.verdict}[/bold {color}]")
    console.print(f"Iteration log: {transcript_path}")


_KANI_UNWIND_RE = re.compile(
    r"^\s*#\[kani::unwind\([^)]*\)\]\s*\n?", re.MULTILINE
)


def _strip_kani_unwind(code: str) -> str:
    """Strip ALL `#[kani::unwind(N)]` attributes from harness source.

    Per-function unwind attributes OVERRIDE `--default-unwind` at cargo
    kani invocation. Cycle 20260511's L3 verdicts were trivially
    SUCCESSFUL because templates shipped `#[kani::unwind(8)]` and the
    LLM was instructed to emit `#[kani::unwind(128)]` — both bounds too
    tight to find the bug regardless of dispatcher's --default-unwind.
    The v2 dispatcher (`deploy/dispatch_layer3_v2.py:533,563`) stripped
    these but the in-pipeline path (`synth-kani --auto`) did not, so
    every clean cycle reintroduced the bug.

    This helper centralizes the strip so both write sites (initial
    author + fix-loop rewrite) share the same regex.
    """
    if not code:
        return code
    return _KANI_UNWIND_RE.sub("", code)


def _resolve_engine_dir(workspace: Path) -> Path | None:
    """Return engine dir from workspace.json, or None if not configured."""
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text())
    candidate = workspace / config["engine"]["local"]
    if not (candidate / "Cargo.toml").exists():
        return None
    return candidate


def _build_fix_prompt(
    original_prompt: str,
    failing_code: str,
    errors: list[str],
) -> str:
    """Build a follow-up prompt that asks the LLM to fix compile errors."""
    err_section = "\n\n---\n\n".join(errors[:6])  # cap at 6 errors to fit context
    # Include the FULL original prompt (which now contains the full engine
    # source after the 2026-05-12 fix). Without this, the LLM fix attempt
    # is blind to actual struct field names and re-invents wrong ones —
    # exactly the bug that made all 4 retry rounds fail. Sonnet 4.6 has a
    # 200K context window so the full ~150K-token engine source still fits.
    return f"""You previously generated this Kani harness in response to the
original synthesis request. The harness did NOT compile. Fix it.

# Original synthesis request (for context — INCLUDES THE FULL ENGINE SOURCE)

{original_prompt}

# Your previous harness (FAILING)

```rust
{failing_code}
```

# Compile errors from `cargo check --tests --features test`

```
{err_section}
```

Generate a CORRECTED Kani harness. Output ONLY the Rust source in a
```rust fenced block. No explanatory prose outside the code block.
Preserve the harness function name and signature exactly. Address every
error above; do not skip any.
"""


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
