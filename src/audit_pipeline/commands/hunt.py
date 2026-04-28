"""`audit-pipeline hunt` — autonomous bug-hunt loop.

Production entrypoint. Orchestrates Layer 1 -> 1.5 -> 2 -> 3 (Kani) without
human intervention:

    1. Run `recon --auto` against a hypothesis file
    2. Parse verdicts from recon_summary.json
    3. For every verdict that's contested or TRUE/HIGH, dispatch
       `debate --auto` adversarial second-opinion
    4. For every finding still TRUE/HIGH after debate, scaffold a
       Layer-2 state-conservation PoC and run `cargo test`
    5. For every finding whose PoC fires, scaffold + run a Kani harness
       via `synth-kani --auto`
    6. Write every verdict into the findings DB with severity + lifecycle
       status; emit a cycle summary + Markdown report
    7. POST a webhook on confirmed findings (Slack / Discord)

Designed to be triggered by `watch --on-update`, so every new commit
on the target repo gets a full audit pass without you doing anything.

Required:
  - ANTHROPIC_API_KEY in env
  - A hypotheses.yaml in the workspace (or pass --hypotheses)
  - The target repo cloned at the workspace's pinned SHA
"""

from __future__ import annotations

import json
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

from audit_pipeline.db import FindingsDB
from audit_pipeline.lifecycle import Status, from_hunt_outcome
from audit_pipeline.severity import Severity, derive_severity, emoji as sev_emoji
from audit_pipeline.utils import is_available
from audit_pipeline.utils.daily_cap import DailyCap

console = Console()


@click.command(name="hunt")
@click.option(
    "--hypotheses", "-h",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file describing hypotheses (defaults to <workspace>/hypotheses.yaml)",
)
@click.option(
    "--target-name", "-t",
    default=None,
    help="Target name (defaults to workspace.json `name` or 'default')",
)
@click.option(
    "--budget-cap-usd",
    type=float, default=10.0, show_default=True,
    help="Total Claude spend cap for one hunt cycle",
)
@click.option(
    "--daily-cap-usd",
    type=float, default=50.0, show_default=True, envvar="AUDIT_DAILY_CAP_USD",
    help="Total Claude spend cap PER DAY across all cycles. Hunt aborts if exceeded.",
)
@click.option(
    "--max-concurrent",
    type=int, default=4, show_default=True,
    help="Parallel agents in --auto modes",
)
@click.option(
    "--skip-debate", is_flag=True,
    help="Skip the adversarial debate step (faster but lower quality)",
)
@click.option(
    "--skip-poc", is_flag=True,
    help="Skip Layer-2 PoC scaffolding + cargo test (faster, no empirical proof)",
)
@click.option(
    "--skip-kani", is_flag=True, default=True, show_default=True,
    help="Skip Layer-3 Kani harness synthesis (default ON: Kani is slow)",
)
@click.option(
    "--skip-litesvm", is_flag=True, default=False, show_default=True,
    help="Skip Layer-4 LiteSVM exploit-chain authoring on PoC-fired findings",
)
@click.option(
    "--skip-narrative", is_flag=True, default=False, show_default=True,
    help="Skip narrative writeup generation for confirmed findings",
)
@click.option(
    "--refinement-rounds", type=int, default=0, show_default=True,
    help="Adversarial refinement rounds in Layer 1 (0=fast, 1=balanced, 2=deep)",
)
@click.option(
    "--webhook-url",
    default=None, envvar="HUNT_WEBHOOK_URL",
    help="Slack/Discord webhook URL to POST findings to (or HUNT_WEBHOOK_URL env)",
)
@click.option(
    "--engine-only/--engine-and-wrapper",
    default=True, show_default=True,
    help="Whether the hypotheses target only the engine (faster) or both",
)
@click.pass_context
def hunt_cmd(
    ctx: click.Context,
    hypotheses: str | None,
    target_name: str | None,
    budget_cap_usd: float,
    daily_cap_usd: float,
    max_concurrent: int,
    skip_debate: bool,
    skip_poc: bool,
    skip_kani: bool,
    skip_litesvm: bool,
    skip_narrative: bool,
    refinement_rounds: int,
    webhook_url: str | None,
    engine_only: bool,
) -> None:
    """Autonomous full hunt cycle: recon -> debate -> PoC -> Kani -> report.

    Designed for `watch --on-update "audit-pipeline hunt"` - every commit
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

    target = target_name or config.get("name") or "default"

    # ---------- Daily cap check ----------
    daily_cap = DailyCap(workspace / ".daily_spend.json", daily_cap_usd)
    if daily_cap.remaining_today() < budget_cap_usd:
        raise click.ClickException(
            f"Daily cap exhausted: spent ${daily_cap.today_spend():.2f} of "
            f"${daily_cap_usd:.2f} today, only ${daily_cap.remaining_today():.2f} "
            f"left but cycle budget is ${budget_cap_usd:.2f}. Aborting."
        )

    # ---------- DB setup ----------
    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(
        name=target,
        github_url=config.get("engine", {}).get("repo"),
        engine_repo=config.get("engine", {}).get("repo"),
        wrapper_repo=config.get("wrapper", {}).get("repo"),
        config=config,
    )

    hunts_dir = workspace / "hunts"
    hunts_dir.mkdir(parents=True, exist_ok=True)
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cycle_dir = hunts_dir / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)

    db.insert_cycle(
        target_id=target_id,
        cycle_id=cycle_id,
        engine_sha=config.get("engine", {}).get("sha"),
        wrapper_sha=config.get("wrapper", {}).get("sha"),
        summary_json_path=str(cycle_dir / "hunt_summary.json"),
    )

    console.print(
        Panel.fit(
            f"[bold]Hunt cycle [cyan]{cycle_id}[/cyan][/bold]  ({target})\n\n"
            f"Workspace:    {workspace}\n"
            f"Engine SHA:   {config['engine']['sha'][:10]}\n"
            f"Wrapper SHA:  {config['wrapper']['sha'][:10]}\n"
            f"Hypotheses:   {hypotheses}\n"
            f"Budget cap:   ${budget_cap_usd:.2f} (cycle) / "
            f"${daily_cap_usd:.2f} (day, ${daily_cap.remaining_today():.2f} left)\n"
            f"Concurrent:   {max_concurrent}\n"
            f"Skip debate:  {skip_debate}\n"
            f"Skip PoC:     {skip_poc}\n"
            f"Skip Kani:    {skip_kani}\n"
            f"Webhook:      {'configured' if webhook_url else '(none)'}\n",
            title="Sentinel - autonomous hunt",
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

    log("hunt_start",
        engine_sha=config["engine"]["sha"],
        wrapper_sha=config["wrapper"]["sha"],
        target=target,
        daily_remaining_usd=daily_cap.remaining_today())

    # ---------- Layer 1: recon --auto ----------
    console.print()
    console.print("[bold]Layer 1 - multi-agent recon[/bold]")
    recon_out = cycle_dir / "recon"
    recon_out.mkdir(parents=True, exist_ok=True)

    # Cap recon spend at min(budget_cap, daily_remaining)
    recon_cap = min(budget_cap_usd, daily_cap.remaining_today())

    rc = _run([
        _audit_pipeline_bin(), "--workspace", str(workspace),
        "recon",
        "--hypotheses", hypotheses,
        "--output", str(recon_out),
        "--auto",
        "--max-concurrent", str(max_concurrent),
        "--budget-cap-usd", str(recon_cap),
        "--refinement-rounds", str(refinement_rounds),
    ])
    log("layer1_done", returncode=rc)

    summary_path = recon_out / "recon_summary.json"
    if not summary_path.exists():
        log("layer1_no_summary", path=str(summary_path))
        console.print(f"[red]Layer 1 did not produce a summary at {summary_path}[/red]")
        db.finish_cycle(cycle_id, n_dispatched=0, n_confirmed=0, total_cost_usd=0)
        return

    summary = json.loads(summary_path.read_text())
    layer1_cost = float(summary.get("total_cost_usd", 0.0))
    total_cost += layer1_cost
    daily_cap.record_spend(layer1_cost)
    verdicts: list[dict[str, Any]] = summary.get("verdicts", [])

    # Build a hyp_id -> hypothesis-meta map (need class for severity derivation)
    import yaml
    with open(hypotheses) as f:
        hyp_meta_list = yaml.safe_load(f).get("hypotheses", [])
    hyp_meta = {h["id"]: h for h in hyp_meta_list}

    candidates = [v for v in verdicts if v.get("verdict") in ("TRUE", "NEEDS_LAYER_2_TO_DECIDE")]
    contested_for_debate = [
        v for v in verdicts
        if v.get("verdict") == "FALSE" and v.get("confidence") == "HIGH"
    ]

    log("layer1_summary",
        n_total=len(verdicts),
        n_true=sum(1 for v in verdicts if v.get("verdict") == "TRUE"),
        n_needs_l2=sum(1 for v in verdicts if v.get("verdict") == "NEEDS_LAYER_2_TO_DECIDE"),
        n_false_high=len(contested_for_debate),
        cost_layer1=layer1_cost)

    promoted_ids: set[str] = set()  # hypotheses promoted by debate

    # ---------- Layer 1.5: debate (on contested) ----------
    debate_results: dict[str, dict[str, Any]] = {}
    if not skip_debate and contested_for_debate and daily_cap.remaining_today() > 0.50:
        console.print()
        console.print(
            f"[bold]Layer 1.5 - adversarial debate on "
            f"{len(contested_for_debate)} FALSE/HIGH verdicts[/bold]"
        )
        debate_out = cycle_dir / "debate"
        debate_out.mkdir(parents=True, exist_ok=True)

        for v in contested_for_debate:
            if daily_cap.remaining_today() < 0.50:
                log("debate_halted_daily_cap", spent=daily_cap.today_spend())
                break
            hyp_id = v["hypothesis_id"]
            proposer_path = recon_out / f"{hyp_id}_response.md"
            if not proposer_path.exists():
                continue
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "debate",
                "--hypothesis-id", hyp_id,
                "--proposer-verdict", str(proposer_path),
                "--hypotheses-file", hypotheses,
                "--output", str(debate_out),
                "--auto",
            ])
            challenger_resp = debate_out / f"{hyp_id}_challenger_response.md"
            verdict_changed = False
            if challenger_resp.exists():
                txt = challenger_resp.read_text(encoding="utf-8", errors="replace").upper()
                if "DISAGREE" in txt or "NEEDS_LAYER_2" in txt:
                    verdict_changed = True
                    candidates.append(v)
                    promoted_ids.add(hyp_id)
            debate_results[hyp_id] = {
                "returncode": rc,
                "challenger_response_path": str(challenger_resp),
                "promoted_to_layer2": verdict_changed,
            }
            log("debate_one", hypothesis_id=hyp_id, returncode=rc, promoted=verdict_changed)
            # debate ~= one Sonnet round; assume ~$0.20 average; record best-effort
            daily_cap.record_spend(0.20)
            total_cost += 0.20

    # ---------- Layer 2: PoC scaffold + cargo test ----------
    poc_results: dict[str, dict[str, Any]] = {}
    if not skip_poc and candidates:
        console.print()
        console.print(
            f"[bold]Layer 2 - empirical PoC scaffolding on "
            f"{len(candidates)} candidates[/bold]"
        )
        poc_out = cycle_dir / "poc"
        poc_out.mkdir(parents=True, exist_ok=True)
        engine_dir = workspace / config["engine"]["local"]

        for v in candidates:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "poc",
                "--finding", finding_name,
                "--template", "engine_state_conservation_poc",
                "--engine-function", "absorb_protocol_loss",
                "--invariant-description",
                f"hunt-cycle {cycle_id} candidate {hyp_id}",
                "--output", str(poc_out),
            ])
            scaffold_path = poc_out / f"test_{finding_name}.rs"
            poc_results[hyp_id] = {
                "scaffold_path": str(scaffold_path),
                "scaffold_rc": rc,
                "compile_test_rc": None,
                "fired": False,
            }
            log("poc_scaffold", hypothesis_id=hyp_id, finding=finding_name, returncode=rc)

            if scaffold_path.exists() and (engine_dir / "Cargo.toml").exists():
                test_dest = engine_dir / "tests" / f"test_{finding_name}.rs"
                try:
                    test_dest.write_text(
                        scaffold_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    cargo_rc = _run(
                        ["cargo", "test", "--features", "test", "--test",
                         f"test_{finding_name}"],
                        cwd=engine_dir, timeout=300,
                    )
                    poc_results[hyp_id]["compile_test_rc"] = cargo_rc
                    poc_results[hyp_id]["fired"] = (cargo_rc != 0)
                    log("poc_test_run", hypothesis_id=hyp_id, cargo_rc=cargo_rc,
                        fired=poc_results[hyp_id]["fired"])
                except Exception as e:  # noqa: BLE001
                    log("poc_test_error", hypothesis_id=hyp_id, error=str(e))

    # ---------- Layer 4: LiteSVM exploit-chain authoring (on PoC-fired) ----------
    # Pre-declare results dicts so synthesis works regardless of which layers ran
    kani_results: dict[str, dict[str, Any]] = {}
    litesvm_results: dict[str, dict[str, Any]] = {}
    narrative_results: dict[str, str] = {}
    fired_for_litesvm = [
        v for v in candidates
        if poc_results.get(v["hypothesis_id"], {}).get("fired")
    ]
    if not skip_litesvm and fired_for_litesvm:
        console.print()
        console.print(
            f"[bold]Layer 4 - LiteSVM exploit-chain authoring on "
            f"{len(fired_for_litesvm)} PoC-fired findings[/bold]"
        )
        litesvm_out = cycle_dir / "litesvm"
        litesvm_out.mkdir(parents=True, exist_ok=True)
        for v in fired_for_litesvm:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "litesvm", "author",
                "--finding", finding_name,
                "--template", "litesvm_bound_analysis",
                "--output", str(litesvm_out),
            ])
            litesvm_results[hyp_id] = {
                "returncode": rc,
                "scaffold_dir": str(litesvm_out),
            }
            log("litesvm_authored", hypothesis_id=hyp_id, returncode=rc)

    # ---------- Layer 3: Kani harness (only on PoC-fired) ----------
    fired_for_kani = [
        v for v in candidates
        if poc_results.get(v["hypothesis_id"], {}).get("fired")
    ]
    if not skip_kani and fired_for_kani and daily_cap.remaining_today() > 0.50:
        console.print()
        console.print(
            f"[bold]Layer 3 - Kani harness synthesis on "
            f"{len(fired_for_kani)} PoC-fired findings[/bold]"
        )
        kani_out = cycle_dir / "kani"
        kani_out.mkdir(parents=True, exist_ok=True)

        for v in fired_for_kani:
            if daily_cap.remaining_today() < 0.50:
                log("kani_halted_daily_cap", spent=daily_cap.today_spend())
                break
            hyp_id = v["hypothesis_id"]
            meta = hyp_meta.get(hyp_id, {})
            invariant = meta.get("claim", f"invariant for {hyp_id}")[:500]
            engine_function = meta.get("engine_function", "absorb_protocol_loss")
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "synth-kani",
                "--invariant", invariant,
                "--engine-function", engine_function,
                "--harness-name", f"{_slugify(hyp_id)}_invariant",
                "--output", str(kani_out),
                "--auto",
            ])
            kani_results[hyp_id] = {
                "returncode": rc,
                "harness_dir": str(kani_out),
            }
            log("kani_one", hypothesis_id=hyp_id, returncode=rc)
            # synth-kani is iterative; budget ~$0.50 per attempt
            daily_cap.record_spend(0.50)
            total_cost += 0.50

    # ---------- Synthesis: build the cycle report + write to DB ----------
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
                "kani": kani_results.get(hyp_id),
            })

    # Write every verdict (TRUE / NEEDS_L2 / FALSE) to the findings DB
    for v in verdicts:
        hyp_id = v["hypothesis_id"]
        meta = hyp_meta.get(hyp_id, {})
        verdict = v.get("verdict", "UNKNOWN")
        confidence = v.get("confidence", "UNKNOWN")
        debate_promoted = hyp_id in promoted_ids
        poc_fired = poc_results.get(hyp_id, {}).get("fired", False)
        sev = derive_severity(
            hypothesis_class=meta.get("class", "implicit_invariant"),
            verdict=verdict,
            poc_fired=poc_fired,
            debate_promoted=debate_promoted,
            explicit=meta.get("severity"),
        )
        status = from_hunt_outcome(verdict, debate_promoted, poc_fired)
        title = (meta.get("claim", hyp_id)[:120].replace("\n", " ")) or hyp_id

        db.upsert_finding(
            target_id=target_id,
            cycle_id=cycle_id,
            hypothesis_id=hyp_id,
            verdict=verdict,
            confidence=confidence,
            severity=sev,
            status=status,
            title=title,
            poc_path=poc_results.get(hyp_id, {}).get("scaffold_path"),
            poc_fired=poc_fired,
            debate_promoted=debate_promoted,
            engine_sha=config.get("engine", {}).get("sha"),
            wrapper_sha=config.get("wrapper", {}).get("sha"),
            details={
                "debate": debate_results.get(hyp_id),
                "kani": kani_results.get(hyp_id),
                "input_tokens": v.get("input_tokens"),
                "output_tokens": v.get("output_tokens"),
            },
        )

    cycle_summary = {
        "schema": "audit-pipeline.hunt.v1",
        "cycle_id": cycle_id,
        "target": target,
        "workspace": str(workspace),
        "engine_sha": config["engine"]["sha"],
        "wrapper_sha": config["wrapper"]["sha"],
        "started_at": _now_from(started_at),
        "elapsed_seconds": round(elapsed, 1),
        "total_cost_usd": round(total_cost, 4),
        "daily_spent_usd": round(daily_cap.today_spend(), 4),
        "daily_cap_usd": daily_cap_usd,
        "n_hypotheses": len(verdicts),
        "n_candidates": len(candidates),
        "n_debate_runs": len(debate_results),
        "n_poc_scaffolded": len(poc_results),
        "n_poc_fired": sum(1 for r in poc_results.values() if r.get("fired")),
        "n_kani_runs": len(kani_results),
        "n_litesvm_runs": len(litesvm_results),
        "n_narratives": len(narrative_results),
        "n_confirmed": len(confirmed),
        "confirmed": confirmed,
        "verdicts": verdicts,
        "debate": debate_results,
        "poc": poc_results,
        "kani": kani_results,
        "litesvm": litesvm_results,
        "narrative": narrative_results,
    }
    summary_path = cycle_dir / "hunt_summary.json"
    summary_path.write_text(json.dumps(cycle_summary, indent=2), encoding="utf-8")

    md_lines = _format_markdown_report(cycle_summary)
    (cycle_dir / "hunt_report.md").write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # ---------- Narrative writeups for confirmed findings ----------
    if (not skip_narrative) and confirmed and daily_cap.remaining_today() > 0.30:
        console.print()
        console.print(
            f"[bold]Generating narrative writeups for "
            f"{len(confirmed)} confirmed finding(s)[/bold]"
        )
        narrative_out = cycle_dir / "narratives"
        narrative_out.mkdir(parents=True, exist_ok=True)
        # Look up each finding's DB id
        for c in confirmed:
            if daily_cap.remaining_today() < 0.30:
                break
            hyp_id = c["hypothesis_id"]
            findings_for_hyp = [
                f for f in db.list_findings(target_id=target_id, limit=200)
                if f.get("cycle_id") == cycle_id and f.get("hypothesis_id") == hyp_id
            ]
            if not findings_for_hyp:
                continue
            finding_id = findings_for_hyp[0]["id"]
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "narrative", "generate",
                "--finding-id", str(finding_id),
                "--output", str(narrative_out / f"{hyp_id}.md"),
            ])
            narrative_results[hyp_id] = str(narrative_out / f"{hyp_id}.md")
            log("narrative_generated", hypothesis_id=hyp_id, returncode=rc)
            daily_cap.record_spend(0.10)
            total_cost += 0.10

    db.finish_cycle(
        cycle_id=cycle_id,
        n_dispatched=len(verdicts),
        n_confirmed=len(confirmed),
        total_cost_usd=total_cost,
    )

    # Print summary
    console.print()
    table = Table(title=f"Hunt cycle {cycle_id} - done in {elapsed:.0f}s")
    table.add_column("Metric")
    table.add_column("Value", style="bold")
    table.add_row("Hypotheses dispatched", str(len(verdicts)))
    table.add_row("Layer 2 candidates", str(len(candidates)))
    table.add_row("PoCs scaffolded", str(len(poc_results)))
    table.add_row("PoCs that fired", str(cycle_summary["n_poc_fired"]))
    table.add_row("Kani harnesses run", str(len(kani_results)))
    table.add_row("Confirmed findings", str(len(confirmed)))
    table.add_row("Cycle cost (USD)", f"${total_cost:.3f}")
    table.add_row("Daily spend (USD)", f"${daily_cap.today_spend():.3f} / ${daily_cap_usd}")
    console.print(table)

    if confirmed:
        console.print()
        console.print(
            f"[bold red]🚨 {len(confirmed)} confirmed finding(s) this cycle:[/bold red]"
        )
        for f in confirmed:
            hyp_id = f["hypothesis_id"]
            meta = hyp_meta.get(hyp_id, {})
            sev = derive_severity(
                hypothesis_class=meta.get("class", "implicit_invariant"),
                verdict=f.get("verdict"),
                poc_fired=True,
                debate_promoted=hyp_id in promoted_ids,
                explicit=meta.get("severity"),
            )
            console.print(f"  {sev_emoji(sev)} {sev.value}: {hyp_id}")

    console.print()
    console.print(f"Cycle artifacts: [cyan]{cycle_dir}[/cyan]")
    console.print(f"Findings DB:     [cyan]{workspace / 'findings.db'}[/cyan]")
    log("hunt_done", elapsed=elapsed, n_confirmed=len(confirmed), cost=total_cost)

    if webhook_url and confirmed:
        try:
            _post_webhook(webhook_url, cycle_summary, hyp_meta, promoted_ids)
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
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124
    except FileNotFoundError as e:
        sys.stderr.write(f"command not found: {e}\n")
        return 127


def _audit_pipeline_bin() -> str:
    """Resolve the audit-pipeline binary path (defeats PATH issues under systemd).

    Prefer sys.argv[0] (what the user actually invoked) so subprocess
    self-recursion uses the same install. Falls back to "audit-pipeline" on PATH.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and Path(argv0).name in ("audit-pipeline", "audit-pipeline.exe") and Path(argv0).exists():
        return str(Path(argv0).resolve())
    return "audit-pipeline"


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:60] or "hunt_finding"


def _post_webhook(
    url: str,
    summary: dict[str, Any],
    hyp_meta: dict[str, dict],
    promoted_ids: set[str],
) -> None:
    confirmed = summary.get("confirmed", [])
    cycle_id = summary.get("cycle_id")
    target = summary.get("target", "unknown")
    msg_lines = [
        f"🚨 *Sentinel hunt cycle {cycle_id}* (`{target}`) — "
        f"{len(confirmed)} confirmed finding(s)",
        f"Engine SHA: `{summary.get('engine_sha', '?')[:10]}`",
        f"Wrapper SHA: `{summary.get('wrapper_sha', '?')[:10]}`",
        f"Cost: ${summary.get('total_cost_usd', 0):.3f}  "
        f"(daily ${summary.get('daily_spent_usd', 0):.2f}/${summary.get('daily_cap_usd', 0):.0f})",
        "",
    ]
    for f in confirmed:
        hyp_id = f["hypothesis_id"]
        meta = hyp_meta.get(hyp_id, {})
        sev = derive_severity(
            hypothesis_class=meta.get("class", "implicit_invariant"),
            verdict=f.get("verdict"),
            poc_fired=True,
            debate_promoted=hyp_id in promoted_ids,
            explicit=meta.get("severity"),
        )
        msg_lines.append(
            f"• {sev_emoji(sev)} *{sev.value}* — `{hyp_id}` "
            f"({f.get('verdict')}/{f.get('confidence')})"
        )
    payload = {"text": "\n".join(msg_lines)}
    requests.post(url, json=payload, timeout=15)


def _format_markdown_report(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"# Hunt cycle `{summary['cycle_id']}` — `{summary.get('target', 'unknown')}`",
        "",
        f"- **Workspace:** `{summary['workspace']}`",
        f"- **Engine SHA:** `{summary['engine_sha'][:10]}`",
        f"- **Wrapper SHA:** `{summary['wrapper_sha'][:10]}`",
        f"- **Started:** {summary['started_at']}",
        f"- **Elapsed:** {summary['elapsed_seconds']}s",
        f"- **Cycle cost:** ${summary['total_cost_usd']}",
        f"- **Daily spend:** ${summary.get('daily_spent_usd', 0):.2f} / "
        f"${summary.get('daily_cap_usd', 0):.0f}",
        "",
        "## Summary",
        "",
        f"- Hypotheses dispatched: **{summary['n_hypotheses']}**",
        f"- Layer 2 candidates: **{summary['n_candidates']}**",
        f"- PoCs scaffolded: **{summary['n_poc_scaffolded']}**",
        f"- PoCs that fired: **{summary['n_poc_fired']}**",
        f"- Kani harnesses: **{summary.get('n_kani_runs', 0)}**",
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
            kani = f.get("kani")
            if kani:
                lines.append(f"- Kani harness rc: `{kani.get('returncode')}`")
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
