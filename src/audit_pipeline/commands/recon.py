"""`audit-pipeline recon` — Layer-1 multi-agent code review.

Two modes:

  render mode (default): writes one prompt file per hypothesis for the
    user to feed to their LLM manually.

  --auto mode: actually dispatches N parallel Claude agents, one per
    hypothesis, saves each response to disk, parses verdicts, and emits
    a structured summary. Requires ANTHROPIC_API_KEY.

When --auto is on, this becomes the entry point of the autonomous hunt
loop: the watch daemon's --on-update can fire `recon --auto` on every
new commit, the synthesis script processes the verdicts, and downstream
PoC + Kani + disclosure flows handle whatever's confirmed.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.table import Table

from audit_pipeline.utils import (
    LLMResponse,
    LLMUnavailable,
    complete,
    is_available,
    render_placeholders,
)

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


# Approximate Sonnet 4.7 pricing as of build time. If pricing changes,
# update here. Used only for cost estimation in --auto mode.
COST_PER_INPUT_TOKEN = 3.0e-6   # $3 per 1M input tokens
COST_PER_OUTPUT_TOKEN = 15.0e-6  # $15 per 1M output tokens


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
@click.option(
    "--auto",
    is_flag=True,
    help=(
        "Actually dispatch Claude agents in parallel, save responses, "
        "parse verdicts. Requires ANTHROPIC_API_KEY in env."
    ),
)
@click.option(
    "--max-concurrent",
    type=int,
    default=4,
    show_default=True,
    help="Max parallel Claude API calls in --auto mode",
)
@click.option(
    "--budget-cap-usd",
    type=float,
    default=5.0,
    show_default=True,
    help=(
        "Hard cap on total Claude spend for this run (--auto mode). "
        "If estimated spend would exceed, agents skip silently."
    ),
)
@click.option(
    "--refinement-rounds",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Adversarial refinement rounds per hypothesis. 0=single-pass, "
        "1=challenge round (devil's-advocate self-critique then reconsider), "
        "2=double-challenge. Each round ~doubles per-hyp API spend."
    ),
)
@click.option(
    "--ground-code/--no-ground-code",
    default=True,
    show_default=True,
    help=(
        "Inject actual Rust source for the functions / patterns named in each "
        "hypothesis's `relevant_constants` and `relevant_instructions` fields. "
        "Defeats the agent-hallucinates-code-reads failure mode at the cost of "
        "more input tokens per call."
    ),
)
@click.option(
    "--code-max-lines",
    type=int, default=120, show_default=True,
    help="Max lines per extracted function in --ground-code mode",
)
@click.pass_context
def recon_cmd(
    ctx: click.Context,
    hypotheses: str,
    output: Path | None,
    auto: bool,
    max_concurrent: int,
    budget_cap_usd: float,
    refinement_rounds: int,
    ground_code: bool,
    code_max_lines: int,
) -> None:
    """Layer-1 multi-agent recon. Render prompts (default) or dispatch agents (--auto).

    The hypotheses YAML file should have:

      hypotheses:
        - id: H1
          class: implicit_invariant
          claim: "..."
          target_file: "..."
          target_lines: "..."
        - id: H2
          ...

    In default mode: writes <hyp-id>_prompt.md per hypothesis.
    In --auto mode: also writes <hyp-id>_response.md with the agent's
    output, plus a recon_summary.json aggregating all verdicts for the
    synthesis pipeline to consume.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )

    config = json.loads(config_path.read_text())

    if output is None:
        output = workspace / "recon"
    output.mkdir(parents=True, exist_ok=True)

    with open(hypotheses) as f:
        hyp_data = yaml.safe_load(f)

    if "hypotheses" not in hyp_data:
        raise click.ClickException(
            f"{hypotheses} must contain a top-level 'hypotheses' key with a list."
        )

    # Pre-flight check for --auto mode
    if auto and not is_available():
        raise click.ClickException(
            "--auto mode requires ANTHROPIC_API_KEY in your environment. "
            "Either set the key or omit --auto."
        )

    # Locate orientation prompt + class-specific prompts
    from audit_pipeline import __file__ as pkg_init
    prompts_dir = Path(pkg_init).parent / "templates" / "agent_prompts"

    orientation_path = prompts_dir / "00_orientation.md"
    if not orientation_path.exists():
        raise click.ClickException(f"Orientation prompt missing at {orientation_path}")

    orientation = orientation_path.read_text(encoding="utf-8", errors="replace")

    # Render prompts for every hypothesis (mode-independent)
    rendered_prompts: list[tuple[str, str]] = []  # (hyp_id, full_prompt_text)
    for hyp in hyp_data["hypotheses"]:
        hyp_id = hyp["id"]
        hyp_class = hyp.get("class", "implicit_invariant")

        if hyp_class not in HYPOTHESIS_CLASS_TO_TEMPLATE:
            console.print(
                f"[yellow]Warning:[/yellow] {hyp_id} has unknown class "
                f"'{hyp_class}'; using implicit_invariant template."
            )
            hyp_class = "implicit_invariant"

        template_filename = HYPOTHESIS_CLASS_TO_TEMPLATE[hyp_class]
        template_path = prompts_dir / template_filename
        if not template_path.exists():
            raise click.ClickException(f"Template missing: {template_path}")

        template_content = template_path.read_text(encoding="utf-8", errors="replace")

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
            "LIST_RELEVANT_CONSTANTS": hyp.get(
                "relevant_constants", "(none specified)"
            ),
            "LIST_RELEVANT_INSTRUCTIONS": hyp.get(
                "relevant_instructions", "(none specified)"
            ),
        }

        rendered_orientation = render_placeholders(orientation, **substitutions)
        rendered_class = render_placeholders(template_content, **substitutions)

        # Optional code-grounding: pull actual Rust source for the named
        # constants / instructions and inject into the prompt. Defeats the
        # agent-hallucinates-code-reads failure mode.
        code_section = ""
        if ground_code:
            from audit_pipeline.utils.code_extract import collect_grounded_code

            engine_src_dir = Path(local_engine_path) / "src"
            wrapper_src_dir = Path(local_wrapper_path) / "src"
            rs_files: list[Path] = []
            for d in (engine_src_dir, wrapper_src_dir):
                if d.exists():
                    rs_files.extend(sorted(d.glob("*.rs")))

            targets: list[str] = []
            for raw_block in (hyp.get("relevant_constants", ""), hyp.get("relevant_instructions", "")):
                if not isinstance(raw_block, str):
                    continue
                for line in raw_block.splitlines():
                    for token in line.replace(",", " ").split():
                        token = token.strip(" `'\"():")
                        if token and not token.startswith("("):
                            targets.append(token)

            grounded = collect_grounded_code(targets, rs_files, max_lines=code_max_lines)
            blocks = [v for v in grounded.values() if v]
            if blocks:
                code_section = (
                    "\n\n# CODE-GROUNDED CONTEXT (actual source bytes)\n\n"
                    "These are real source-code excerpts pulled from the workspace. "
                    "Cite specific line numbers from these excerpts in your verdict — "
                    "DO NOT invent code that is not in this section.\n\n"
                    + "\n\n".join(blocks)
                )

        full_prompt = f"""{rendered_orientation}

---

{rendered_class}

---

# Specific hypothesis to investigate

ID:           {hyp_id}
Claim:        {hyp.get("claim", "(see hypothesis brief above)")}
Target file:  {hyp.get("target_file", "(see hypothesis brief above)")}
Target lines: {hyp.get("target_lines", "(see hypothesis brief above)")}
Notes:        {hyp.get("notes", "(none)")}
{code_section}
"""

        out_path = output / f"{hyp_id}_prompt.md"
        out_path.write_text(full_prompt, encoding="utf-8")
        rendered_prompts.append((hyp_id, full_prompt))
        console.print(f"  [green]wrote[/green] {out_path}")

    console.print()
    console.print(
        f"[bold green]Built {len(rendered_prompts)} prompts in {output}/[/bold green]"
    )

    if not auto:
        console.print(
            "  Send each prompt to your LLM. Save responses as "
            "<hyp-id>_response.md in the same directory for synthesis."
        )
        console.print(
            "  Tip: pass [cyan]--auto[/cyan] to dispatch agents in parallel "
            "and skip the manual paste."
        )
        return

    # ---------- AUTO MODE ----------
    console.print()
    console.print(
        f"[bold]Dispatching {len(rendered_prompts)} agents (max {max_concurrent} "
        f"concurrent, budget cap ${budget_cap_usd:.2f})...[/bold]"
    )

    results: dict[str, dict[str, Any]] = {}
    total_in_tokens = 0
    total_out_tokens = 0
    total_cost = 0.0
    started = time.time()
    skipped_for_budget: list[str] = []

    def _dispatch_one(hyp_id: str, prompt_text: str) -> tuple[str, dict[str, Any]]:
        """Worker: dispatch one hypothesis to Claude with optional refinement rounds.

        Each refinement round runs a fresh API call asking the agent to
        play devil's advocate against its own prior verdict, then produce
        a final verdict considering both perspectives. The final verdict
        replaces earlier ones; all rounds' tokens are summed.
        """
        try:
            resp = complete(prompt_text)
            rounds_data = [{
                "text": resp.text,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
            }]

            for r in range(refinement_rounds):
                challenge_prompt = _build_challenge_prompt(
                    hyp_id, prompt_text, rounds_data[-1]["text"], r + 1,
                )
                challenge_resp = complete(challenge_prompt)
                rounds_data.append({
                    "text": challenge_resp.text,
                    "input_tokens": challenge_resp.input_tokens,
                    "output_tokens": challenge_resp.output_tokens,
                })
                resp = challenge_resp  # last round becomes the final verdict

            return hyp_id, {
                "ok": True,
                "text": rounds_data[-1]["text"],
                "input_tokens": sum(r["input_tokens"] for r in rounds_data),
                "output_tokens": sum(r["output_tokens"] for r in rounds_data),
                "n_rounds": len(rounds_data),
                "rounds": rounds_data,
                "model": resp.model,
                "stop_reason": resp.stop_reason,
            }
        except Exception as e:  # noqa: BLE001 — surface every failure as data
            return hyp_id, {"ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        future_map = {}
        for hyp_id, prompt_text in rendered_prompts:
            # Pre-check budget (rough estimate: assume max 30k tokens out per call)
            est_cost = (
                len(prompt_text) / 4 * COST_PER_INPUT_TOKEN
                + 30_000 * COST_PER_OUTPUT_TOKEN
            )
            if total_cost + est_cost > budget_cap_usd:
                skipped_for_budget.append(hyp_id)
                continue
            future_map[ex.submit(_dispatch_one, hyp_id, prompt_text)] = hyp_id

        for future in as_completed(future_map):
            hyp_id, result = future.result()
            results[hyp_id] = result
            if result.get("ok"):
                total_in_tokens += result["input_tokens"]
                total_out_tokens += result["output_tokens"]
                cost = (
                    result["input_tokens"] * COST_PER_INPUT_TOKEN
                    + result["output_tokens"] * COST_PER_OUTPUT_TOKEN
                )
                total_cost += cost
                # Write response to disk
                resp_path = output / f"{hyp_id}_response.md"
                resp_path.write_text(result["text"], encoding="utf-8")
                console.print(
                    f"  [green]✓[/green] {hyp_id}: "
                    f"{result['input_tokens']:,}in / {result['output_tokens']:,}out "
                    f"(${cost:.3f})"
                )
            else:
                console.print(
                    f"  [red]✗[/red] {hyp_id}: {result.get('error', 'unknown error')}"
                )

    elapsed = time.time() - started

    # Parse verdicts from each response
    verdict_summary: list[dict[str, Any]] = []
    for hyp_id, result in sorted(results.items()):
        record: dict[str, Any] = {"hypothesis_id": hyp_id}
        if not result.get("ok"):
            record["status"] = "ERROR"
            record["error"] = result.get("error")
        else:
            verdict, confidence = _parse_verdict(result["text"])
            record["status"] = "OK"
            record["verdict"] = verdict
            record["confidence"] = confidence
            record["input_tokens"] = result["input_tokens"]
            record["output_tokens"] = result["output_tokens"]
        verdict_summary.append(record)

    summary = {
        "schema": "audit-pipeline.recon.v1",
        "workspace": str(workspace),
        "engine_sha": config.get("engine", {}).get("sha"),
        "wrapper_sha": config.get("wrapper", {}).get("sha"),
        "n_hypotheses": len(rendered_prompts),
        "n_dispatched": len(results),
        "n_skipped_budget": len(skipped_for_budget),
        "skipped_for_budget": skipped_for_budget,
        "n_errors": sum(1 for r in results.values() if not r.get("ok")),
        "n_ok": sum(1 for r in results.values() if r.get("ok")),
        "total_input_tokens": total_in_tokens,
        "total_output_tokens": total_out_tokens,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_seconds": round(elapsed, 1),
        "verdicts": verdict_summary,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    summary_path = output / "recon_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Print summary table
    console.print()
    table = Table(title=f"Recon dispatch complete in {elapsed:.0f}s")
    table.add_column("Hypothesis", style="cyan")
    table.add_column("Verdict", style="bold")
    table.add_column("Confidence")
    table.add_column("Tokens")
    for r in verdict_summary:
        if r["status"] == "OK":
            verdict_color = {
                "TRUE": "[red]TRUE[/red]",
                "FALSE": "[green]FALSE[/green]",
                "NEEDS_LAYER_2_TO_DECIDE": "[yellow]NEEDS_L2[/yellow]",
                "UNKNOWN": "[dim]UNKNOWN[/dim]",
            }.get(r["verdict"], r["verdict"])
            table.add_row(
                r["hypothesis_id"],
                verdict_color,
                r["confidence"],
                f"{r['input_tokens']:,}/{r['output_tokens']:,}",
            )
        else:
            table.add_row(
                r["hypothesis_id"],
                "[red]ERROR[/red]",
                "-",
                r.get("error", "")[:30],
            )
    console.print(table)

    console.print()
    console.print(
        f"[bold]Spent:[/bold] ${total_cost:.3f} "
        f"({total_in_tokens:,}in / {total_out_tokens:,}out)"
    )
    if skipped_for_budget:
        console.print(
            f"[yellow]Skipped {len(skipped_for_budget)} for budget:[/yellow] "
            f"{', '.join(skipped_for_budget)}"
        )
    console.print(f"Summary: [cyan]{summary_path}[/cyan]")


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _build_challenge_prompt(
    hyp_id: str, original_prompt: str, prior_response: str, round_num: int,
) -> str:
    """Construct the devil's-advocate challenge prompt for a refinement round.

    The agent is asked to identify the strongest counter-argument against
    its own prior verdict, surface missed code paths, and then produce a
    final verdict that takes both perspectives into account.
    """
    return f"""You previously analyzed hypothesis `{hyp_id}` and produced this verdict + analysis:

---BEGIN PRIOR ANALYSIS---
{prior_response}
---END PRIOR ANALYSIS---

This is refinement round {round_num}. Now play devil's advocate against your own verdict.

Identify, specifically:
1. Code paths or call sites you may not have examined that could flip the conclusion.
2. Invariants asserted elsewhere in the codebase that contradict (or weaken) your prior reasoning.
3. Edge cases — adversarial inputs, race conditions, atomicity gaps, integer-edge values, replay sequences — that your prior analysis didn't address.
4. Assumptions in your prior reasoning that may not hold under all reachable states.

After surfacing the strongest counter-argument, reconsider the original hypothesis and produce your FINAL verdict. Be intellectually honest: if the devil's-advocate analysis flips your conclusion, change the verdict. If it strengthens your conclusion, raise the confidence.

Use the SAME output format as before, including a `## Verdict` section that ends with TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE plus HIGH / MED / LOW confidence.

Original hypothesis context (for reference, do not repeat in your output):
{original_prompt[-2000:]}
"""


def _parse_verdict(text: str) -> tuple[str, str]:
    """Extract (verdict, confidence) from an agent response.

    Looks for a "## Verdict" section header and parses the next non-empty
    line for TRUE/FALSE/NEEDS_LAYER_2_TO_DECIDE plus HIGH/MED/LOW.
    Falls back to (UNKNOWN, UNKNOWN) if the structure isn't found.
    """
    import re

    m = re.search(
        r"^##\s*Verdict\s*\n+(.+?)(?:\n##|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return "UNKNOWN", "UNKNOWN"

    block = m.group(1).strip().upper()
    verdict = "UNKNOWN"
    if "NEEDS_LAYER_2" in block:
        verdict = "NEEDS_LAYER_2_TO_DECIDE"
    elif " TRUE " in f" {block} " or block.startswith("TRUE"):
        verdict = "TRUE"
    elif " FALSE " in f" {block} " or block.startswith("FALSE"):
        verdict = "FALSE"

    confidence = "UNKNOWN"
    for c in ("HIGH", "MED", "LOW"):
        if c in block:
            confidence = c
            break

    return verdict, confidence
