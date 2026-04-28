"""`audit-pipeline hunt` — autonomous bug-hunt loop.

This is the production entrypoint that turns the methodology into a
working hunter. It orchestrates the full Layer 1 → 1.5 → 2 chain
without human intervention:

    1. Run `recon --auto` against a hypothesis file
    2. Parse verdicts from recon_summary.json
    3. For every verdict that's contested or TRUE/HIGH, dispatch
       a `debate --auto` adversarial second-opinion
    4. For every finding still TRUE/HIGH after debate, scaffold a
       Layer-2 state-conservation PoC and run `cargo test`
    5. Write a structured hunt_summary.json + a Markdown report
    6. Optionally POST a webhook on findings (Slack / Discord)

Designed to be triggered by `watch --on-update`, so every new commit
on the target repo gets a full audit pass without you doing anything.

Required:
  - ANTHROPIC_API_KEY in env
  - A hypotheses.yaml in the workspace
  - The target repo cloned at the workspace's pinned SHA
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from audit_pipeline.utils import is_available

console = Console()


@click.command(name="hunt")
@click.option(
    "--hypotheses",
    "-h",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "YAML file describing hypotheses (defaults to "
        "<workspace>/hypotheses.yaml)"
    ),
)
@click.option(
    "--budget-cap-usd",
    type=float,
    default=10.0,
    show_default=True,
    help="Total Claude spend cap for one hunt cycle",
)
@click.option(
    "--max-concurrent",
    type=int,
    default=4,
    show_default=True,
    help="Parallel agents in --auto modes",
)
@click.option(
    "--skip-debate",
    is_flag=True,
    help="Skip the adversarial debate step (faster but lower quality)",
)
@click.option(
    "--skip-poc",
    is_flag=True,
    help="Skip Layer-2 PoC scaffolding + cargo test (faster, no empirical proof)",
)
@click.option(
    "--webhook-url",
    default=None,
    envvar="HUNT_WEBHOOK_URL",
    help=(
        "Slack/Discord webhook URL to POST findings to (or set "
        "HUNT_WEBHOOK_URL env var)"
    ),
)
@click.option(
    "--engine-only/--engine-and-wrapper",
    default=True,
    show_default=True,
    help="Whether the hypotheses target only the engine (faster) or both",
)
@click.pass_context
def hunt_cmd(
    ctx: click.Context,
    hypotheses: str | None,
    budget_cap_usd: float,
    max_concurrent: int,
    skip_debate: bool,
    skip_poc: bool,
    webhook_url: str | None,
    engine_only: bool,
) -> None:
    """Autonomous full hunt cycle: recon → debate → PoC → report.

    Designed for `watch --on-update "audit-pipeline hunt"` — every commit
    triggers a full audit pass automatically.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init`."
        )
    config = json.loads(config_path.read_text())

    if hypotheses is None:
        candidate = workspace / "hypotheses.yaml"
        if not candidate.exists():
            raise click.ClickException(
                f"No --hypotheses passed and {candidate} not found. "
                "Either pass --hypotheses or drop a hypotheses.yaml in the workspace."
            )
        hypotheses = str(candidate)

    if not is_available():
        raise click.ClickException(
            "ANTHROPIC_API_KEY required for hunt. Set it and re-run."
        )

    hunts_dir = workspace / "hunts"
    hunts_dir.mkdir(parents=True, exist_ok=True)
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cycle_dir = hunts_dir / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel.fit(
            f"[bold]Hunt cycle [cyan]{cycle_id}[/cyan][/bold]\n\n"
            f"Workspace:    {workspace}\n"
            f"Engine SHA:   {config['engine']['sha'][:10]}\n"
            f"Wrapper SHA:  {config['wrapper']['sha'][:10]}\n"
            f"Hypotheses:   {hypotheses}\n"
            f"Budget cap:   ${budget_cap_usd:.2f}\n"
            f"Concurrent:   {max_concurrent}\n"
            f"Skip debate:  {skip_debate}\n"
            f"Skip PoC:     {skip_poc}\n"
            f"Webhook:      {'configured' if webhook_url else '(none)'}\n",
            title="Sentinel — autonomous hunt",
        )
    )

    cycle_log: list[dict[str, Any]] = []
    started_at = time.time()
    total_cost = 0.0

    def log(event: str, **fields: Any) -> None:
        rec = {"ts": _now(), "event": event, **fields}
        cycle_log.append(rec)
        with (cycle_dir / "hunt.log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    log("hunt_start", engine_sha=config["engine"]["sha"], wrapper_sha=config["wrapper"]["sha"])

    # ---------- Layer 1: recon --auto ----------
    console.print()
    console.print("[bold]Layer 1 — multi-agent recon[/bold]")
    recon_out = cycle_dir / "recon"
    recon_out.mkdir(parents=True, exist_ok=True)

    rc = _run(
        [
            "audit-pipeline", "--workspace", str(workspace),
            "recon",
            "--hypotheses", hypotheses,
            "--output", str(recon_out),
            "--auto",
            "--max-concurrent", str(max_concurrent),
            "--budget-cap-usd", str(budget_cap_usd),
        ]
    )
    log("layer1_done", returncode=rc)

    summary_path = recon_out / "recon_summary.json"
    if not summary_path.exists():
        log("layer1_no_summary", path=str(summary_path))
        console.print(f"[red]Layer 1 did not produce a summary at {summary_path}[/red]")
        return

    summary = json.loads(summary_path.read_text())
    total_cost += float(summary.get("total_cost_usd", 0.0))
    verdicts: list[dict[str, Any]] = summary.get("verdicts", [])

    candidates = [v for v in verdicts if v.get("verdict") in ("TRUE", "NEEDS_LAYER_2_TO_DECIDE")]
    contested_for_debate = [
        v for v in verdicts
        if v.get("verdict") == "FALSE" and v.get("confidence") == "HIGH"
    ]

    log(
        "layer1_summary",
        n_total=len(verdicts),
        n_true=sum(1 for v in verdicts if v.get("verdict") == "TRUE"),
        n_needs_l2=sum(1 for v in verdicts if v.get("verdict") == "NEEDS_LAYER_2_TO_DECIDE"),
        n_false_high=len(contested_for_debate),
        cost_layer1=summary.get("total_cost_usd"),
    )

    # ---------- Layer 1.5: debate (on contested) ----------
    debate_results: dict[str, dict[str, Any]] = {}
    if not skip_debate and contested_for_debate:
        console.print()
        console.print(
            f"[bold]Layer 1.5 — adversarial debate on "
            f"{len(contested_for_debate)} FALSE/HIGH verdicts[/bold]"
        )
        debate_out = cycle_dir / "debate"
        debate_out.mkdir(parents=True, exist_ok=True)

        for v in contested_for_debate:
            hyp_id = v["hypothesis_id"]
            proposer_path = recon_out / f"{hyp_id}_response.md"
            if not proposer_path.exists():
                continue
            rc = _run(
                [
                    "audit-pipeline", "--workspace", str(workspace),
                    "debate",
                    "--hypothesis-id", hyp_id,
                    "--proposer-verdict", str(proposer_path),
                    "--hypotheses-file", hypotheses,
                    "--output", str(debate_out),
                    "--auto",
                ]
            )
            challenger_resp = debate_out / f"{hyp_id}_challenger_response.md"
            verdict_changed = False
            if challenger_resp.exists():
                txt = challenger_resp.read_text(encoding="utf-8", errors="replace").upper()
                if "DISAGREE" in txt or "NEEDS_LAYER_2" in txt:
                    verdict_changed = True
                    candidates.append(v)  # promote to PoC pipeline
            debate_results[hyp_id] = {
                "returncode": rc,
                "challenger_response_path": str(challenger_resp),
                "promoted_to_layer2": verdict_changed,
            }
            log(
                "debate_one",
                hypothesis_id=hyp_id,
                returncode=rc,
                promoted=verdict_changed,
            )

    # ---------- Layer 2: PoC scaffold + cargo test ----------
    poc_results: dict[str, dict[str, Any]] = {}
    if not skip_poc and candidates:
        console.print()
        console.print(
            f"[bold]Layer 2 — empirical PoC scaffolding on "
            f"{len(candidates)} candidates[/bold]"
        )
        poc_out = cycle_dir / "poc"
        poc_out.mkdir(parents=True, exist_ok=True)
        engine_dir = workspace / config["engine"]["local"]

        for v in candidates:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            # Default to state-conservation PoC for invariant-class hypotheses
            rc = _run(
                [
                    "audit-pipeline", "--workspace", str(workspace),
                    "poc",
                    "--finding", finding_name,
                    "--template", "engine_state_conservation_poc",
                    "--engine-function", "absorb_protocol_loss",
                    "--invariant-description",
                    f"hunt-cycle {cycle_id} candidate {hyp_id}",
                    "--output", str(poc_out),
                ]
            )
            scaffold_path = poc_out / f"test_{finding_name}.rs"
            poc_results[hyp_id] = {
                "scaffold_path": str(scaffold_path),
                "scaffold_rc": rc,
                "compile_test_rc": None,
                "fired": False,
            }
            log("poc_scaffold", hypothesis_id=hyp_id, finding=finding_name, returncode=rc)

            # Try to compile + run the scaffold against the engine (best effort)
            if scaffold_path.exists() and (engine_dir / "Cargo.toml").exists():
                test_dest = engine_dir / "tests" / f"test_{finding_name}.rs"
                try:
                    test_dest.write_text(
                        scaffold_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    cargo_rc = _run(
                        [
                            "cargo", "test", "--features", "test", "--test",
                            f"test_{finding_name}",
                        ],
                        cwd=engine_dir,
                        timeout=300,
                    )
                    poc_results[hyp_id]["compile_test_rc"] = cargo_rc
                    # `cargo test` returns non-zero when an assertion fails — that's
                    # exactly what a state-conservation PoC FIRING looks like.
                    poc_results[hyp_id]["fired"] = (cargo_rc != 0)
                    log(
                        "poc_test_run",
                        hypothesis_id=hyp_id,
                        cargo_rc=cargo_rc,
                        fired=poc_results[hyp_id]["fired"],
                    )
                except Exception as e:  # noqa: BLE001
                    log("poc_test_error", hypothesis_id=hyp_id, error=str(e))

    # ---------- Synthesis: build the cycle report ----------
    elapsed = time.time() - started_at

    confirmed: list[dict[str, Any]] = []
    for v in candidates:
        hyp_id = v["hypothesis_id"]
        poc = poc_results.get(hyp_id, {})
        if poc.get("fired"):
            confirmed.append({
                "hypothesis_id": hyp_id,
                "verdict": v.get("verdict"),
                "confidence": v.get("confidence"),
                "poc": poc,
            })

    cycle_summary = {
        "schema": "audit-pipeline.hunt.v1",
        "cycle_id": cycle_id,
        "workspace": str(workspace),
        "engine_sha": config["engine"]["sha"],
        "wrapper_sha": config["wrapper"]["sha"],
        "started_at": _now_from(started_at),
        "elapsed_seconds": round(elapsed, 1),
        "total_cost_usd": round(total_cost, 4),
        "n_hypotheses": len(verdicts),
        "n_candidates": len(candidates),
        "n_debate_runs": len(debate_results),
        "n_poc_scaffolded": len(poc_results),
        "n_poc_fired": sum(1 for r in poc_results.values() if r.get("fired")),
        "n_confirmed": len(confirmed),
        "confirmed": confirmed,
        "verdicts": verdicts,
        "debate": debate_results,
        "poc": poc_results,
    }
    summary_path = cycle_dir / "hunt_summary.json"
    summary_path.write_text(json.dumps(cycle_summary, indent=2), encoding="utf-8")

    # Markdown report
    md_lines = _format_markdown_report(cycle_summary)
    (cycle_dir / "hunt_report.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # Print summary
    console.print()
    table = Table(title=f"Hunt cycle {cycle_id} — done in {elapsed:.0f}s")
    table.add_column("Metric")
    table.add_column("Value", style="bold")
    table.add_row("Hypotheses dispatched", str(len(verdicts)))
    table.add_row("Layer 2 candidates", str(len(candidates)))
    table.add_row("PoCs scaffolded", str(len(poc_results)))
    table.add_row("PoCs that fired", str(cycle_summary["n_poc_fired"]))
    table.add_row("Confirmed findings", str(len(confirmed)))
    table.add_row("Total cost (USD)", f"${total_cost:.3f}")
    console.print(table)

    if confirmed:
        console.print()
        console.print(
            f"[bold red]🚨 {len(confirmed)} confirmed finding(s) this cycle:[/bold red]"
        )
        for f in confirmed:
            console.print(f"  - {f['hypothesis_id']}: {f.get('verdict')}/{f.get('confidence')}")

    console.print()
    console.print(f"Cycle artifacts: [cyan]{cycle_dir}[/cyan]")
    log(
        "hunt_done",
        elapsed=elapsed,
        n_confirmed=len(confirmed),
        cost=total_cost,
    )

    # Webhook on findings
    if webhook_url and confirmed:
        try:
            _post_webhook(webhook_url, cycle_summary)
            log("webhook_sent", url=webhook_url[:50])
        except Exception as e:  # noqa: BLE001
            log("webhook_failed", error=str(e))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_from(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800) -> int:
    """Run a subprocess, stream output, return exit code."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124  # standard timeout exit code
    except FileNotFoundError as e:
        sys.stderr.write(f"command not found: {e}\n")
        return 127


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "hunt_finding"


def _post_webhook(url: str, summary: dict[str, Any]) -> None:
    confirmed = summary.get("confirmed", [])
    cycle_id = summary.get("cycle_id")
    msg_lines = [
        f"🚨 *Sentinel hunt cycle {cycle_id}* — {len(confirmed)} confirmed finding(s)",
        f"Engine SHA: `{summary.get('engine_sha', '?')[:10]}`",
        f"Wrapper SHA: `{summary.get('wrapper_sha', '?')[:10]}`",
        f"Cost: ${summary.get('total_cost_usd', 0):.3f}",
        "",
    ]
    for f in confirmed:
        msg_lines.append(
            f"• `{f['hypothesis_id']}` — {f.get('verdict')}/{f.get('confidence')}"
        )
    payload = {"text": "\n".join(msg_lines)}
    requests.post(url, json=payload, timeout=15)


def _format_markdown_report(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"# Hunt cycle `{summary['cycle_id']}`",
        "",
        f"- **Workspace:** `{summary['workspace']}`",
        f"- **Engine SHA:** `{summary['engine_sha'][:10]}`",
        f"- **Wrapper SHA:** `{summary['wrapper_sha'][:10]}`",
        f"- **Started:** {summary['started_at']}",
        f"- **Elapsed:** {summary['elapsed_seconds']}s",
        f"- **Total cost:** ${summary['total_cost_usd']}",
        "",
        "## Summary",
        "",
        f"- Hypotheses dispatched: **{summary['n_hypotheses']}**",
        f"- Layer 2 candidates: **{summary['n_candidates']}**",
        f"- PoCs scaffolded: **{summary['n_poc_scaffolded']}**",
        f"- PoCs that fired: **{summary['n_poc_fired']}**",
        f"- Confirmed findings: **{summary['n_confirmed']}**",
        "",
    ]

    if summary["confirmed"]:
        lines.append("## Confirmed findings")
        lines.append("")
        for f in summary["confirmed"]:
            lines.append(
                f"### `{f['hypothesis_id']}` — {f.get('verdict')}/{f.get('confidence')}"
            )
            lines.append("")
            poc = f.get("poc", {})
            lines.append(f"- PoC scaffold: `{poc.get('scaffold_path')}`")
            lines.append(f"- cargo test exit code: `{poc.get('compile_test_rc')}` (non-zero = PoC fired)")
            lines.append("")
    else:
        lines.append("## No confirmed findings this cycle")
        lines.append("")
        lines.append("_All hypotheses returned FALSE / their PoCs did not fire._")
        lines.append("")

    lines.append("## Verdict table")
    lines.append("")
    lines.append("| Hypothesis | Verdict | Confidence |")
    lines.append("|---|---|---|")
    for v in summary["verdicts"]:
        lines.append(
            f"| `{v.get('hypothesis_id')}` | {v.get('verdict', 'ERROR')} | {v.get('confidence', '-')} |"
        )

    return lines
