"""`audit-pipeline hunt-deep` — tool-using bug-hunting agent.

For each hypothesis, spawns a Claude agent equipped with `read_file`,
`grep`, and `find_function` tools. The agent iteratively explores the
codebase, cites specific line numbers, and produces a verdict grounded
in actual source bytes.

This is the upgrade from speculation/static-grounding to real bug-hunting.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from audit_pipeline.utils.llm_tools import run_tool_using_agent

console = Console()


SYSTEM_PROMPT = """You are a senior security auditor for Solana programs (specifically perpetual DEX engines).

You have three tools available:
- `read_file(path, start_line, end_line)` — read source files
- `grep(pattern, path)` — search for regex patterns
- `find_function(name)` — locate a Rust function definition

USE THE TOOLS. Do not speculate. Do not paraphrase code from memory. Every claim
about how the code behaves MUST be backed by a tool result you cite verbatim.

Your job is to investigate a single hypothesis (an invariant claim about the
codebase) and render a verdict:
- TRUE  — the invariant holds (positive safety attestation)
- FALSE — the invariant is violated (potential bug)
- NEEDS_LAYER_2_TO_DECIDE — code reading alone is insufficient; empirical PoC required

For FALSE verdicts (potential bugs), you MUST:
1. Cite the exact file path and line numbers
2. Explain the precondition that triggers the violation
3. Describe a concrete reproduction (instruction sequence, account state, slot timing)
4. Estimate severity: Critical / High / Medium / Low / Info

For TRUE verdicts (safety attestations):
1. Cite the specific code structure that enforces the invariant
2. Identify any edge cases that strain it but still hold

Be intellectually honest:
- If you cannot decide, say NEEDS_LAYER_2_TO_DECIDE — do not guess
- If a hypothesis as stated is over-specified or doesn't apply, say so
- Lead with EVIDENCE, not narrative

End your final response with a section titled exactly `## Verdict` containing
on its own line: `VERDICT: <TRUE|FALSE|NEEDS_LAYER_2_TO_DECIDE>` and
`CONFIDENCE: <HIGH|MED|LOW>`.

Workspace root for tools: <WORKSPACE_PATH>
Engine source: <ENGINE_PATH>/src/percolator.rs
Wrapper source: <WRAPPER_PATH>/src/percolator.rs
"""


@click.command(name="hunt-deep")
@click.option(
    "--hypotheses", "-h",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file (defaults to <workspace>/hypotheses.yaml)",
)
@click.option(
    "--filter-id", "-f", multiple=True,
    help="Only investigate these hypothesis IDs (repeatable)",
)
@click.option(
    "--max-concurrent", type=int, default=3, show_default=True,
    help="Concurrent tool-using agent loops",
)
@click.option(
    "--max-turns", type=int, default=20, show_default=True,
    help="Max tool-call turns per hypothesis",
)
@click.option(
    "--budget-cap-usd", type=float, default=30.0, show_default=True,
    help="Hard cap on total spend",
)
@click.option(
    "--output", "-o", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Output dir (default: <workspace>/hunt_deep/)",
)
@click.pass_context
def hunt_deep_cmd(
    ctx: click.Context,
    hypotheses: str | None,
    filter_id: tuple[str, ...],
    max_concurrent: int,
    max_turns: int,
    budget_cap_usd: float,
    output: Path | None,
) -> None:
    """Tool-using deep hunt — agents read code iteratively, cite line numbers."""
    workspace = Path(ctx.obj["workspace"])
    config = json.loads((workspace / "workspace.json").read_text())

    if hypotheses is None:
        hypotheses = str(workspace / "hypotheses.yaml")

    hyps = yaml.safe_load(Path(hypotheses).read_text())["hypotheses"]
    if filter_id:
        ids = set(filter_id)
        hyps = [h for h in hyps if h["id"] in ids]
    if not hyps:
        raise click.ClickException("No hypotheses to investigate")

    out = output or (workspace / "hunt_deep" / time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    out.mkdir(parents=True, exist_ok=True)

    engine_path = str(workspace / config["engine"]["local"])
    wrapper_path = str(workspace / config["wrapper"]["local"])
    system_prompt = (
        SYSTEM_PROMPT
        .replace("<WORKSPACE_PATH>", str(workspace))
        .replace("<ENGINE_PATH>", engine_path)
        .replace("<WRAPPER_PATH>", wrapper_path)
    )

    # Per-call cost estimate (Sonnet 4.6 pricing)
    COST_IN = 3e-6
    COST_OUT = 15e-6

    console.print(
        f"[bold]Tool-using deep hunt — {len(hyps)} hypotheses, "
        f"max_concurrent={max_concurrent}, budget_cap=${budget_cap_usd:.2f}[/bold]"
    )

    def worker(hyp: dict) -> tuple[str, dict]:
        hyp_id = hyp["id"]
        msg = (
            f"## Hypothesis to investigate\n\n"
            f"**ID:** {hyp_id}\n"
            f"**Class:** {hyp.get('class', '?')}\n"
            f"**Severity (proposed):** {hyp.get('severity', 'auto')}\n\n"
            f"**Claim:**\n{hyp.get('claim', '?')}\n\n"
            f"**Relevant identifiers from the YAML:**\n"
            f"- relevant_constants: {hyp.get('relevant_constants', '(none)')}\n"
            f"- relevant_instructions: {hyp.get('relevant_instructions', '(none)')}\n"
            f"- target_file: {hyp.get('target_file', '(none)')}\n\n"
            f"Begin by using `grep` to find call sites of the named identifiers, "
            f"then `read_file` or `find_function` on the most relevant. "
            f"Render your verdict with line citations."
        )
        try:
            result = run_tool_using_agent(
                workspace=workspace,
                system_prompt=system_prompt,
                initial_user_message=msg,
                max_turns=max_turns,
            )
            return hyp_id, {
                "ok": True,
                "text": result.text,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "n_turns": result.n_turns,
                "tool_calls": result.tool_calls,
                "stop_reason": result.stop_reason,
            }
        except Exception as e:  # noqa: BLE001
            return hyp_id, {"ok": False, "error": str(e)}

    started = time.time()
    total_cost = 0.0
    results: dict[str, dict] = {}
    skipped: list[str] = []

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        future_map = {}
        for hyp in hyps:
            # Pre-flight: rough estimate per hyp ~$1 (10 turns × 5K in × 2K out)
            est = 10 * 5000 * COST_IN + 10 * 2000 * COST_OUT
            if total_cost + est > budget_cap_usd:
                skipped.append(hyp["id"])
                continue
            future_map[ex.submit(worker, hyp)] = hyp["id"]

        for future in as_completed(future_map):
            hyp_id, result = future.result()
            results[hyp_id] = result
            if result.get("ok"):
                cost = (
                    result["input_tokens"] * COST_IN
                    + result["output_tokens"] * COST_OUT
                )
                total_cost += cost
                resp_path = out / f"{hyp_id}_response.md"
                resp_path.write_text(result["text"], encoding="utf-8")
                console.print(
                    f"  [green]✓[/green] {hyp_id}: "
                    f"{result['n_turns']} turns, "
                    f"{result['input_tokens']:,}in/{result['output_tokens']:,}out "
                    f"(${cost:.3f})"
                )
                # Save tool-call log
                (out / f"{hyp_id}_toolcalls.json").write_text(
                    json.dumps(result["tool_calls"], indent=2)
                )
            else:
                console.print(f"  [red]✗[/red] {hyp_id}: {result.get('error', '?')}")

    elapsed = time.time() - started

    # Parse verdicts
    from audit_pipeline.commands.recon import _parse_verdict
    summary_rows = []
    for hyp_id in sorted(results.keys()):
        r = results[hyp_id]
        if not r.get("ok"):
            summary_rows.append({"hypothesis_id": hyp_id, "status": "ERROR", "error": r.get("error")})
            continue
        verdict, confidence = _parse_verdict(r["text"])
        summary_rows.append({
            "hypothesis_id": hyp_id,
            "status": "OK",
            "verdict": verdict,
            "confidence": confidence,
            "n_turns": r["n_turns"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "n_tool_calls": len(r["tool_calls"]),
        })

    summary = {
        "schema": "audit-pipeline.hunt-deep.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "elapsed_seconds": round(elapsed, 1),
        "total_cost_usd": round(total_cost, 4),
        "n_dispatched": len(results),
        "n_skipped_budget": len(skipped),
        "skipped_ids": skipped,
        "verdicts": summary_rows,
    }
    summary_path = out / "hunt_deep_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    # Print
    console.print()
    table = Table(title=f"Tool-using deep hunt — {elapsed:.0f}s")
    table.add_column("Hypothesis", style="cyan")
    table.add_column("Verdict", style="bold")
    table.add_column("Conf")
    table.add_column("Turns")
    table.add_column("Tools")
    for r in summary_rows:
        if r.get("status") == "OK":
            color = {
                "TRUE": "[red]TRUE[/red]",
                "FALSE": "[green]FALSE[/green]",
                "NEEDS_LAYER_2_TO_DECIDE": "[yellow]NEEDS_L2[/yellow]",
                "UNKNOWN": "[dim]UNKNOWN[/dim]",
            }.get(r["verdict"], r["verdict"])
            table.add_row(r["hypothesis_id"], color, r["confidence"],
                          str(r["n_turns"]), str(r["n_tool_calls"]))
        else:
            table.add_row(r["hypothesis_id"], "[red]ERROR[/red]", "-", "-", "-")
    console.print(table)

    console.print()
    console.print(f"[bold]Spent:[/bold] ${total_cost:.3f} in {elapsed:.0f}s")
    if skipped:
        console.print(f"[yellow]Skipped {len(skipped)} for budget:[/yellow] {', '.join(skipped)}")
    console.print(f"Summary: [cyan]{summary_path}[/cyan]")
