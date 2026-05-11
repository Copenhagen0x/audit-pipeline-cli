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
    complete,
    is_available,
    render_placeholders,
)
from audit_pipeline.utils.github_snapshot import GitHubSnapshot, SnapshotDownloadError

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
@click.option(
    "--source-repo",
    default=None,
    help=(
        "GitHub repo (owner/repo) to read source from via an ephemeral "
        "snapshot. If set, source code is downloaded fresh per run instead "
        "of reading from a local clone. Mutually exclusive with the "
        "default local-clone path resolved via workspace.json."
    ),
)
@click.option(
    "--source-sha",
    default=None,
    help=(
        "Specific commit SHA to pin the snapshot to (requires --source-repo). "
        "If omitted, uses the default branch HEAD at download time. Pinning "
        "is preferred for reproducibility."
    ),
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
    source_repo: str | None,
    source_sha: str | None,
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

    # Validate source-mode args
    if source_sha and not source_repo:
        raise click.ClickException(
            "--source-sha requires --source-repo to be set."
        )

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

    # ---------- Source-mode resolution ----------
    # If --source-repo is given, download an ephemeral snapshot for the
    # lifetime of this command and read source code from it. Otherwise
    # fall back to the local-clone path resolved via workspace.json
    # (legacy mode, preserves backward compat).
    if source_repo:
        try:
            snap_cm = GitHubSnapshot(source_repo, source_sha)
        except SnapshotDownloadError as e:
            raise click.ClickException(f"snapshot init failed: {e}")
    else:
        snap_cm = None

    with (snap_cm if snap_cm is not None else _NoSnapshot()) as snap:
        if snap_cm is not None:
            engine_root = snap.workspace
            wrapper_root = snap.workspace  # snapshot is a single repo; wrapper unused
            console.print(
                f"  [cyan]Reading from snapshot {snap.repo_slug}@"
                f"{snap.resolved_sha[:7]}[/cyan] ({engine_root})"
            )
        else:
            engine_root = workspace / config["engine"]["local"]
            wrapper_root = workspace / config["wrapper"]["local"]
            console.print(
                f"  [cyan]Reading from local workspace {engine_root}[/cyan]"
            )

        return _recon_body(
            ctx=ctx,
            workspace=workspace,
            config=config,
            engine_root=engine_root,
            wrapper_root=wrapper_root,
            hypotheses=hypotheses,
            hyp_data=hyp_data,
            output=output,
            auto=auto,
            max_concurrent=max_concurrent,
            budget_cap_usd=budget_cap_usd,
            refinement_rounds=refinement_rounds,
            ground_code=ground_code,
            code_max_lines=code_max_lines,
            snap_sha=snap.resolved_sha if snap_cm is not None else None,
        )


class _NoSnapshot:
    """No-op context manager so the snapshot/non-snapshot branches share a body."""
    def __enter__(self):
        return None
    def __exit__(self, *a):
        return False


def _recon_body(
    *,
    ctx: click.Context,
    workspace: Path,
    config: dict,
    engine_root: Path,
    wrapper_root: Path,
    hypotheses: str,
    hyp_data: dict,
    output: Path,
    auto: bool,
    max_concurrent: int,
    budget_cap_usd: float,
    refinement_rounds: int,
    ground_code: bool,
    code_max_lines: int,
    snap_sha: str | None,
) -> None:
    """Body of recon, factored out so snapshot vs local-clone modes share it."""

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

        local_engine_path = str(engine_root)
        local_wrapper_path = str(wrapper_root)
        substitutions = {
            "ENGINE_REPO_URL": config["engine"]["repo"],
            "ENGINE_SHA": snap_sha or config["engine"]["sha"],
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

    # Resume support: if a previous recon left behind <hyp_id>_response.md in
    # the output dir (e.g. cycle hit a timeout), load that response into the
    # results dict and skip the paid API call. Cost is preserved across the
    # stop/fix/resume protocol.
    n_resumed = 0
    for hyp_id, _prompt_text in rendered_prompts:
        existing = output / f"{hyp_id}_response.md"
        if existing.is_file() and existing.stat().st_size > 0:
            try:
                results[hyp_id] = {
                    "ok": True,
                    "text": existing.read_text(encoding="utf-8"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "n_rounds": 1,
                    "model": "resumed-from-disk",
                    "stop_reason": "resumed",
                }
                n_resumed += 1
            except OSError:
                pass  # fall through; will re-dispatch
    if n_resumed:
        console.print(
            f"[cyan]resume[/cyan] reloaded {n_resumed} existing response.md files; "
            f"will dispatch only the {len(rendered_prompts) - n_resumed} missing hyps"
        )

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        future_map = {}
        for hyp_id, prompt_text in rendered_prompts:
            if hyp_id in results:
                continue  # resumed from disk; don't re-pay
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
        "engine_sha": snap_sha or config.get("engine", {}).get("sha"),
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

    Strategy:
      1. Find the LAST `## Verdict` section (agents often write multiple
         while reasoning; only the final one is the actual verdict).
      2. Within that section, parse only the FIRST 800 chars after the
         header — that's where the verdict declaration lives. Looser
         matches against the full body produced false positives (e.g.
         "this is true for opening orders" leaking into the verdict).
      3. Use word-boundary regexes for TRUE/FALSE/NEEDS_LAYER_2 instead of
         loose substring contains.
    """
    import re

    # All `## Verdict` matches, take the last
    matches = list(re.finditer(
        r"(?im)^\s*##+\s*(?:final\s+)?verdict\b.*$",
        text,
    ))
    if not matches:
        return "UNKNOWN", "UNKNOWN"
    last = matches[-1]
    # Slice from the matched header to the next ## or end-of-text
    section_start = last.end()
    next_header = re.search(r"(?im)^\s*##+\s+\S", text[section_start:])
    section_end = section_start + next_header.start() if next_header else len(text)
    block = text[section_start:section_end][:1000].upper()

    # Verdict match — order matters: NEEDS_LAYER_2 wins over bare TRUE/FALSE.
    # FIX #5: tightened TRUE/FALSE matching. Legacy code fell through to bare
    # `\bTRUE\b` / `\bFALSE\b` substring match, which mis-classified responses
    # like "this assumption is TRUE for opening orders but FALSE for closes"
    # as TRUE verdicts. New order:
    #   1. Anchored `VERDICT: TRUE/FALSE` (preferred form per template)
    #   2. Token on its own line (`^\s*TRUE\s*$`) — emphasized verdict
    #   3. Strict tokens at top of block only (first 200 chars)
    verdict = "UNKNOWN"
    block_head = block[:200]
    if re.search(r"NEEDS[_\s]LAYER[_\s]2", block):
        verdict = "NEEDS_LAYER_2_TO_DECIDE"
    elif re.search(r"\bVERDICT\s*[:=\-]\s*TRUE\b", block):
        verdict = "TRUE"
    elif re.search(r"\bVERDICT\s*[:=\-]\s*FALSE\b", block):
        verdict = "FALSE"
    elif re.search(r"(?m)^\s*\*?\*?TRUE\*?\*?\s*$", block):
        verdict = "TRUE"
    elif re.search(r"(?m)^\s*\*?\*?FALSE\*?\*?\s*$", block):
        verdict = "FALSE"
    elif re.search(r"\bTRUE\b", block_head):
        verdict = "TRUE"
    elif re.search(r"\bFALSE\b", block_head):
        verdict = "FALSE"

    # Confidence — also word-boundary
    confidence = "UNKNOWN"
    for c in ("HIGH", "MED", "LOW"):
        if re.search(rf"\b{c}\b", block):
            confidence = c
            break

    return verdict, confidence
