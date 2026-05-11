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
    7. POST a webhook on confirmed findings (Slack)

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

from audit_pipeline.db import open_findings_db
from audit_pipeline.lifecycle import from_hunt_outcome
from audit_pipeline.severity import derive_severity
from audit_pipeline.severity import emoji as sev_emoji
from audit_pipeline.utils import is_available
from audit_pipeline.utils.daily_cap import DailyCap
from audit_pipeline.utils.github_snapshot import GitHubSnapshot, SnapshotDownloadError

console = Console()


@click.command(name="hunt")
@click.option(
    "--hypotheses", "-h",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file describing hypotheses (defaults to <workspace>/hypotheses.yaml)",
)
@click.option(
    "--protocol-class",
    type=click.Choice(["perp_dex", "amm_cp", "clmm", "lending", "lst"], case_sensitive=False),
    default=None,
    help=(
        "Load the entire class library for the given protocol class (e.g. "
        "perp_dex loads perp_dex_*.yaml + percolator*.yaml from the bundled "
        "templates). Mutually exclusive with --hypotheses. The class library "
        "is filtered by the target's applies_to + scope_conditions before "
        "dispatch."
    ),
)
@click.option(
    "--diff-since-sha",
    default=None,
    envvar="AUDIT_DIFF_SINCE_SHA",
    help=(
        "Tier 2 #11 — diff-aware hunting. When set, hunt computes "
        "`git diff <SHA>..HEAD --name-only` against the engine repo and "
        "skips any hypothesis whose `target_file` is NOT in the diff. "
        "Hyps without a target_file always run (whole-protocol invariants). "
        "Typically passed by `watch --on-update` so commit-triggered cycles "
        "skip hyps for unchanged files (~5x cheaper)."
    ),
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
    type=float, default=0.0, show_default=True, envvar="AUDIT_DAILY_CAP_USD",
    help=(
        "Total Claude spend cap PER DAY across all cycles. Pass 0 (default) "
        "to disable the daily cap entirely — the per-cycle --budget-cap-usd "
        "is the only limit. Pass a positive value to enforce a daily ceiling "
        "(hunt aborts at start if today's spend + cycle budget would exceed)."
    ),
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
    "--skip-kani/--no-skip-kani", default=False, show_default=True,
    help=(
        "Skip Layer-3 Kani harness synthesis. Default OFF: every PoC-fired "
        "finding gets a Kani harness. Pass --skip-kani for faster cycles "
        "(e.g. smoke tests). VPS must have Kani installed."
    ),
)
@click.option(
    "--debate-scope",
    type=click.Choice(["false_high", "all_high", "all"]),
    default="all_high",
    show_default=True,
    help=(
        "Which verdicts to send to Layer 1.5 adversarial debate. "
        "false_high = only convergent rejections (legacy behavior, fast). "
        "all_high = HIGH confidence verdicts on BOTH sides (catches convergent "
        "acceptance too). all = every verdict (most rigorous, expensive)."
    ),
)
@click.option(
    "--poc-mode",
    type=click.Choice(["template", "llm"]),
    default="llm",
    show_default=True,
    help=(
        "Layer-2 PoC authoring mode. "
        "template = scaffold the F7 template with placeholders (fast, weak). "
        "llm = ask Claude to author a complete per-hyp PoC from source "
        "code + claim (slower, strong; default)."
    ),
)
@click.option(
    "--skip-propagate/--no-skip-propagate", default=False, show_default=True,
    help="Skip Pillar 2 cross-protocol propagation on Med+ confirmed findings.",
)
@click.option(
    "--skip-bundle/--no-skip-bundle", default=False, show_default=True,
    help="Skip Pillar 3 fix-bundle authoring on Med+ confirmed findings.",
)
@click.option(
    "--skip-merkle/--no-skip-merkle", default=False, show_default=True,
    help="Skip Pillar 4 Merkle attestation at end of cycle.",
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
    "--ground-code/--no-ground-code", default=True, show_default=True,
    help="Inject actual Rust source for hyp-named functions into agent prompts (defeats hallucinated reads)",
)
@click.option(
    "--webhook-url",
    default=None, envvar="HUNT_WEBHOOK_URL",
    help="Slack webhook URL to POST findings to (or HUNT_WEBHOOK_URL env)",
)
@click.option(
    "--engine-only/--engine-and-wrapper",
    default=True, show_default=True,
    help="Whether the hypotheses target only the engine (faster) or both",
)
@click.option(
    "--source-repo",
    default=None,
    help=(
        "GitHub repo (owner/repo) to snapshot for this cycle. When set, the "
        "engine source is downloaded fresh from upstream (no local clone) "
        "and child subcommands (recon, debate) are dispatched with the "
        "resolved SHA so every agent sees identical bytes. When omitted, "
        "hunt falls back to the legacy local-clone path "
        "(workspace/<engine.local>) — preserves backward compat."
    ),
)
@click.option(
    "--source-sha",
    default=None,
    help=(
        "Specific commit SHA to pin the snapshot to (requires --source-repo). "
        "If omitted, the snapshot resolves to the default branch HEAD at "
        "download time. Pinning is preferred for reproducibility within a "
        "cycle and across parallel agents."
    ),
)
@click.pass_context
def hunt_cmd(
    ctx: click.Context,
    hypotheses: str | None,
    protocol_class: str | None,
    diff_since_sha: str | None,
    target_name: str | None,
    budget_cap_usd: float,
    daily_cap_usd: float,
    max_concurrent: int,
    skip_debate: bool,
    skip_poc: bool,
    skip_kani: bool,
    debate_scope: str,
    poc_mode: str,
    skip_litesvm: bool,
    skip_propagate: bool,
    skip_bundle: bool,
    skip_merkle: bool,
    skip_narrative: bool,
    refinement_rounds: int,
    ground_code: bool,
    webhook_url: str | None,
    engine_only: bool,
    source_repo: str | None,
    source_sha: str | None,
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

    # Validate snapshot args
    if source_sha and not source_repo:
        raise click.ClickException(
            "--source-sha requires --source-repo to be set."
        )

    # --hypotheses and --protocol-class are mutually exclusive. If neither
    # is passed we fall back to <workspace>/hypotheses.yaml. If --protocol-class
    # is passed, materialize the merged class library to a temp file.
    if hypotheses and protocol_class:
        raise click.ClickException(
            "--hypotheses and --protocol-class are mutually exclusive."
        )
    if protocol_class:
        import tempfile

        import yaml as _yaml

        from audit_pipeline.scoping import load_class_library
        merged, files = load_class_library(protocol_class, extra_dirs=[workspace])
        console.print(
            f"  [cyan]protocol_class='{protocol_class}': {len(merged)} hyps from "
            f"{len(files)} file(s)[/cyan]"
        )
        # Write merged hypotheses to a temp yaml file for downstream commands
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        )
        _yaml.safe_dump({"hypotheses": merged}, tmp, sort_keys=False, allow_unicode=True)
        tmp.close()
        hypotheses = tmp.name
    elif hypotheses is None:
        candidate = workspace / "hypotheses.yaml"
        if not candidate.exists():
            raise click.ClickException(
                f"No --hypotheses or --protocol-class passed and {candidate} not found. "
                "Either pass --hypotheses, --protocol-class, or drop a hypotheses.yaml in the workspace."
            )
        hypotheses = str(candidate)

    # Tier 2 #11 — diff-aware hunting.
    # If --diff-since-sha is set, compute the diff against the engine repo
    # and filter the hypothesis library to only hyps targeting changed files.
    # Hyps without a target_file always run (whole-protocol invariants).
    if diff_since_sha:
        import tempfile

        import yaml as _yaml2

        from audit_pipeline.scoping import (
            changed_files_between,
            filter_hypotheses_by_diff,
            load_hypotheses,
        )
        engine_dir = workspace / config["engine"]["local"]
        changed = changed_files_between(engine_dir, diff_since_sha, "HEAD")
        if not changed:
            console.print(
                f"  [yellow]--diff-since-sha={diff_since_sha[:10]}: no diff "
                f"info from {engine_dir} — running full library[/yellow]"
            )
        else:
            console.print(
                f"  [cyan]diff vs {diff_since_sha[:10]}: {len(changed)} file(s) "
                f"changed[/cyan]"
            )
            hyps_in = load_hypotheses(Path(hypotheses))
            kept, skipped = filter_hypotheses_by_diff(hyps_in, changed)
            console.print(
                f"  [cyan]diff filter: {len(kept)} hyps kept, "
                f"{len(skipped)} skipped (~{round(100 * len(kept) / max(len(hyps_in), 1))}%"
                f" of library)[/cyan]"
            )
            tmp2 = tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            )
            _yaml2.safe_dump(
                {"hypotheses": kept}, tmp2, sort_keys=False, allow_unicode=True
            )
            tmp2.close()
            hypotheses = tmp2.name

    if not is_available():
        raise click.ClickException(
            "ANTHROPIC_API_KEY required for hunt. Set it and re-run."
        )

    target = target_name or config.get("name") or "default"

    # ---------- Source-mode resolution ----------
    # If --source-repo is given, hunt opens an ephemeral GitHub snapshot
    # ONCE for the whole cycle. The resolved SHA is forwarded to every
    # subprocess child (recon, debate) so they all see identical bytes.
    # When omitted, fall back to legacy local-clone path resolved via
    # workspace.json.
    if source_repo:
        try:
            snap_cm = GitHubSnapshot(source_repo, source_sha)
            snap = snap_cm.__enter__()
        except SnapshotDownloadError as e:
            raise click.ClickException(f"snapshot init failed: {e}")
        resolved_sha = snap.resolved_sha
        engine_dir_for_cargo = snap.workspace
        console.print(
            f"  [cyan]Reading from snapshot {snap.repo_slug}@"
            f"{resolved_sha[:7]}[/cyan] ({snap.workspace})"
        )
    else:
        snap_cm = None
        snap = None
        resolved_sha = config["engine"]["sha"]
        engine_dir_for_cargo = workspace / config["engine"]["local"]
        console.print(
            f"  [cyan]Reading from local workspace {engine_dir_for_cargo}[/cyan]"
        )

    try:
        _hunt_run(
            ctx=ctx,
            workspace=workspace,
            config=config,
            target=target,
            hypotheses=hypotheses,
            budget_cap_usd=budget_cap_usd,
            daily_cap_usd=daily_cap_usd,
            max_concurrent=max_concurrent,
            skip_debate=skip_debate,
            skip_poc=skip_poc,
            skip_kani=skip_kani,
            debate_scope=debate_scope,
            poc_mode=poc_mode,
            skip_litesvm=skip_litesvm,
            skip_propagate=skip_propagate,
            skip_bundle=skip_bundle,
            skip_merkle=skip_merkle,
            skip_narrative=skip_narrative,
            refinement_rounds=refinement_rounds,
            ground_code=ground_code,
            webhook_url=webhook_url,
            engine_only=engine_only,
            source_repo=source_repo,
            resolved_sha=resolved_sha,
            engine_dir_for_cargo=engine_dir_for_cargo,
        )
    finally:
        if snap_cm is not None:
            snap_cm.__exit__(None, None, None)


def _hunt_run(
    *,
    ctx: click.Context,
    workspace: Path,
    config: dict,
    target: str,
    hypotheses: str,
    budget_cap_usd: float,
    daily_cap_usd: float,
    max_concurrent: int,
    skip_debate: bool,
    skip_poc: bool,
    skip_kani: bool,
    debate_scope: str,
    poc_mode: str,
    skip_litesvm: bool,
    skip_propagate: bool,
    skip_bundle: bool,
    skip_merkle: bool,
    skip_narrative: bool,
    refinement_rounds: int,
    ground_code: bool,
    webhook_url: str | None,
    engine_only: bool,
    source_repo: str | None,
    resolved_sha: str,
    engine_dir_for_cargo: Path,
) -> None:
    """Hunt cycle body, factored out so the snapshot lifecycle wraps it cleanly."""

    # ---------- Daily cap check ----------
    daily_cap = DailyCap(workspace / ".daily_spend.json", daily_cap_usd)
    # Skip pre-flight check when daily cap is disabled (cap_usd <= 0).
    # DailyCap.unlimited semantics: remaining_today() returns math.inf.
    if not daily_cap.unlimited and daily_cap.remaining_today() < budget_cap_usd:
        raise click.ClickException(
            f"Daily cap exhausted: spent ${daily_cap.today_spend():.2f} of "
            f"${daily_cap_usd:.2f} today, only ${daily_cap.remaining_today():.2f} "
            f"left but cycle budget is ${budget_cap_usd:.2f}. Aborting."
        )

    # ---------- DB setup ----------
    db = open_findings_db(workspace)
    target_id = db.upsert_target(
        name=target,
        github_url=config.get("engine", {}).get("repo"),
        engine_repo=config.get("engine", {}).get("repo"),
        wrapper_repo=config.get("wrapper", {}).get("repo"),
        config=config,
    )

    hunts_dir = workspace / "hunts"
    hunts_dir.mkdir(parents=True, exist_ok=True)
    base_cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # When in snapshot mode, suffix with SHA short-form for pinning / traceability
    if source_repo and resolved_sha:
        cycle_id = f"{base_cycle_id}-{resolved_sha[:7]}"
    else:
        cycle_id = base_cycle_id
    cycle_dir = hunts_dir / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)

    db.insert_cycle(
        target_id=target_id,
        cycle_id=cycle_id,
        engine_sha=resolved_sha,
        wrapper_sha=config.get("wrapper", {}).get("sha"),
        summary_json_path=str(cycle_dir / "hunt_summary.json"),
    )

    source_mode_line = (
        f"Source mode:  snapshot ({source_repo}@{resolved_sha[:10]})"
        if source_repo
        else f"Source mode:  local clone ({engine_dir_for_cargo})"
    )

    console.print(
        Panel.fit(
            f"[bold]Hunt cycle [cyan]{cycle_id}[/cyan][/bold]  ({target})\n\n"
            f"Workspace:    {workspace}\n"
            f"{source_mode_line}\n"
            f"Engine SHA:   {resolved_sha[:10]}\n"
            f"Wrapper SHA:  {config['wrapper']['sha'][:10]}\n"
            f"Hypotheses:   {hypotheses}\n"
            f"Budget cap:   ${budget_cap_usd:.2f} (cycle) / "
            f"{'unlimited (no daily cap)' if daily_cap.unlimited else f'${daily_cap_usd:.2f} (day, ${daily_cap.remaining_today():.2f} left)'}\n"
            f"Concurrent:   {max_concurrent}\n"
            f"Skip debate:  {skip_debate}\n"
            f"Skip PoC:     {skip_poc}\n"
            f"Skip Kani:    {skip_kani}\n"
            f"Webhook:      {'configured' if webhook_url else '(none)'}\n",
            title="Jelleo - autonomous hunt",
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
        engine_sha=resolved_sha,
        wrapper_sha=config["wrapper"]["sha"],
        target=target,
        source_mode=("snapshot" if source_repo else "local"),
        source_repo=source_repo,
        daily_remaining_usd=daily_cap.remaining_today())

    # ---------- Layer 1: recon --auto ----------
    console.print()
    console.print("[bold]Layer 1 - multi-agent recon[/bold]")
    recon_out = cycle_dir / "recon"
    recon_out.mkdir(parents=True, exist_ok=True)

    # Cap recon spend at min(budget_cap, daily_remaining)
    recon_cap = min(budget_cap_usd, daily_cap.remaining_today())

    recon_argv = [
        _audit_pipeline_bin(), "--workspace", str(workspace),
        "recon",
        "--hypotheses", hypotheses,
        "--output", str(recon_out),
        "--auto",
        "--max-concurrent", str(max_concurrent),
        "--budget-cap-usd", str(recon_cap),
        "--refinement-rounds", str(refinement_rounds),
        "--ground-code" if ground_code else "--no-ground-code",
    ]
    if source_repo:
        recon_argv += ["--source-repo", source_repo, "--source-sha", resolved_sha]
    rc = _run(recon_argv)
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
    # FIX 6: Debate scope expanded. Legacy behavior only debated FALSE/HIGH
    # ("convergent rejection" — might be a missed bug). New default also debates
    # TRUE/HIGH ("convergent acceptance" — might be a false positive that
    # looked rigorous). debate_scope=all debates every verdict regardless of
    # confidence (most rigorous, expensive).
    if debate_scope == "false_high":
        contested_for_debate = [
            v for v in verdicts
            if v.get("verdict") == "FALSE" and v.get("confidence") == "HIGH"
        ]
    elif debate_scope == "all_high":
        contested_for_debate = [
            v for v in verdicts
            if v.get("confidence") == "HIGH"
            and v.get("verdict") in ("TRUE", "FALSE")
        ]
    else:  # "all"
        contested_for_debate = list(verdicts)

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
            debate_argv = [
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "debate",
                "--hypothesis-id", hyp_id,
                "--proposer-verdict", str(proposer_path),
                "--hypotheses-file", hypotheses,
                "--output", str(debate_out),
                "--auto",
            ]
            if source_repo:
                debate_argv += ["--source-repo", source_repo, "--source-sha", resolved_sha]
            rc = _run(debate_argv)
            challenger_resp = debate_out / f"{hyp_id}_challenger_response.md"
            verdict_changed = False
            demoted = False
            if challenger_resp.exists():
                txt = challenger_resp.read_text(encoding="utf-8", errors="replace").upper()
                proposer_verdict = v.get("verdict")
                if "DISAGREE" in txt or "NEEDS_LAYER_2" in txt:
                    if proposer_verdict == "FALSE":
                        # Challenger says the FALSE verdict is wrong — promote
                        # to Layer 2 candidates list (legacy behavior).
                        if v not in candidates:
                            candidates.append(v)
                            promoted_ids.add(hyp_id)
                            verdict_changed = True
                    elif proposer_verdict == "TRUE":
                        # FIX 6: Challenger refutes the TRUE verdict — demote
                        # from candidates list to avoid wasting Layer 2 cost on
                        # a likely false positive. Verdict will be recorded as
                        # TRUE but marked debate-demoted in the DB details.
                        candidates[:] = [c for c in candidates if c is not v]
                        demoted = True
            debate_results[hyp_id] = {
                "returncode": rc,
                "challenger_response_path": str(challenger_resp),
                "promoted_to_layer2": verdict_changed,
                "demoted_by_debate": demoted,
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
        # In snapshot mode, the engine source lives in the snapshot's temp
        # workspace; in legacy mode, it's the local clone resolved via
        # workspace.json. _hunt_run is given the right path either way.
        engine_dir = engine_dir_for_cargo

        for v in candidates:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            scaffold_path = poc_out / f"test_{finding_name}.rs"
            # FIX 1: Per-hyp LLM-authored PoC by default. Falls back to template
            # scaffolding if poc-llm fails. Template path remains for smoke /
            # legacy compatibility via --poc-mode template.
            if poc_mode == "llm":
                meta = hyp_meta.get(hyp_id, {})
                bug_class = meta.get("bug_class", "unknown")
                rc = _run([
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "poc-llm",
                    "--hypothesis-id", hyp_id,
                    "--hypotheses", hypotheses,
                    "--engine-root", str(engine_dir_for_cargo),
                    "--output", str(poc_out),
                ])
                # poc-llm bills its own API call — estimate $0.10-0.20 per hyp
                daily_cap.record_spend(0.15)
                total_cost += 0.15
                log("poc_llm_authored", hypothesis_id=hyp_id, finding=finding_name,
                    bug_class=bug_class, returncode=rc)
                # If LLM authoring failed, fall back to template scaffolding so
                # the chain doesn't break. The template-only fallback is the
                # legacy F7-shaped scaffold (weak but always produces a file).
                if rc != 0 or not scaffold_path.exists():
                    log("poc_llm_fallback_to_template", hypothesis_id=hyp_id)
                    engine_function = meta.get("engine_function", "absorb_protocol_loss")
                    rc = _run([
                        _audit_pipeline_bin(), "--workspace", str(workspace),
                        "poc",
                        "--finding", finding_name,
                        "--template", "engine_state_conservation_poc",
                        "--engine-function", engine_function,
                        "--invariant-description",
                        f"hunt-cycle {cycle_id} candidate {hyp_id}",
                        "--output", str(poc_out),
                    ])
            else:
                # Legacy template-only path. Reads engine_function from hyp meta
                # (FIX 2 added the field) instead of always falling back to F7.
                meta = hyp_meta.get(hyp_id, {})
                engine_function = meta.get("engine_function", "absorb_protocol_loss")
                rc = _run([
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "poc",
                    "--finding", finding_name,
                    "--template", "engine_state_conservation_poc",
                    "--engine-function", engine_function,
                    "--invariant-description",
                    f"hunt-cycle {cycle_id} candidate {hyp_id}",
                    "--output", str(poc_out),
                ])
            poc_results[hyp_id] = {
                "scaffold_path": str(scaffold_path),
                "scaffold_rc": rc,
                "compile_test_rc": None,
                "fired": False,
                "authoring_mode": poc_mode,
            }
            log("poc_scaffold", hypothesis_id=hyp_id, finding=finding_name,
                returncode=rc, mode=poc_mode)

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
            f"[bold]Layer 4 - LiteSVM exploit-chain authoring + dispatch on "
            f"{len(fired_for_litesvm)} PoC-fired findings[/bold]"
        )
        litesvm_out = cycle_dir / "litesvm"
        litesvm_out.mkdir(parents=True, exist_ok=True)
        # FIX 3: After authoring, actually invoke dispatch_litesvm.sh to run
        # the LiteSVM test. Pass the actual wrapper path (workspace config)
        # so the script does NOT depend on /tmp/audit symlinks. If vps.host
        # is null (running on the VPS itself), use "-" "-" to skip SSH.
        vps_host = config.get("vps", {}).get("host") or "-"
        vps_key = config.get("vps", {}).get("ssh_key") or "-"
        from audit_pipeline import __file__ as pkg_init_path
        dispatch_script = Path(pkg_init_path).parent / "scripts" / "dispatch_litesvm.sh"
        # The wrapper lives under the workspace at config.wrapper.local
        wrapper_dir = workspace / config.get("wrapper", {}).get("local", "target/wrapper")
        for v in fired_for_litesvm:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            rc_author = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "litesvm", "author",
                "--finding", finding_name,
                "--template", "litesvm_bound_analysis",
                "--output", str(litesvm_out),
            ])
            dispatch_rc: int | None = None
            if rc_author == 0 and dispatch_script.exists():
                # Test naming convention from litesvm.py: test_<finding>_bound_analysis
                test_name = f"test_{finding_name}_bound_analysis"
                dispatch_rc = _run(
                    [
                        "bash", str(dispatch_script),
                        vps_host, vps_key, test_name, str(wrapper_dir),
                    ],
                    timeout=600,
                )
                log("litesvm_dispatched", hypothesis_id=hyp_id,
                    test=test_name, returncode=dispatch_rc)
            litesvm_results[hyp_id] = {
                "returncode": rc_author,
                "dispatch_returncode": dispatch_rc,
                "scaffold_dir": str(litesvm_out),
            }
            log("litesvm_authored", hypothesis_id=hyp_id, returncode=rc_author)

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
            # FIX 4: synth-kani --auto authors the harness AND runs `cargo check`
            # to compile-verify. With --run-kani it ALSO runs `cargo kani` to
            # actually formally verify (5-30 min per harness; budget ~$0.50
            # API spend + compute time).
            rc = _run([
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "synth-kani",
                "--invariant", invariant,
                "--engine-function", engine_function,
                "--harness-name", f"{_slugify(hyp_id)}_invariant",
                "--output", str(kani_out),
                "--auto",
                "--run-kani",
            ], timeout=1800)  # 30 min per harness
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
            engine_sha=resolved_sha,
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
        "source_mode": "snapshot" if source_repo else "local",
        "source_repo": source_repo,
        "engine_sha": resolved_sha,
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

    # ---------- FIX 5: Chain P2 propagate + P3 bundle + P4 merkle ----------
    # Med+ confirmed findings (Critical / High / Medium) trigger:
    #  - P2 propagate search: sweep sibling protocols for the same bug class
    #  - P3 bundle draft + verify: author a fix patch and run verification
    #    gates. NEVER opens upstream PRs (per memory's HARD RULE on PR
    #    authorization — engine never auto-opens; user explicitly authorizes).
    #  - P4 merkle compute --sign: produce signed attestation for this cycle
    pillar_results: dict[str, Any] = {"propagate": {}, "bundle": {}, "merkle": None}
    med_plus_severities = {"Critical", "High", "Medium"}

    def _confirmed_med_plus() -> list[dict[str, Any]]:
        med_plus = []
        for c in confirmed:
            hyp_id = c["hypothesis_id"]
            meta = hyp_meta.get(hyp_id, {})
            sev = derive_severity(
                hypothesis_class=meta.get("class", "implicit_invariant"),
                verdict=c.get("verdict"),
                poc_fired=True,
                debate_promoted=hyp_id in promoted_ids,
                explicit=meta.get("severity"),
            )
            if sev.value in med_plus_severities:
                med_plus.append({**c, "severity": sev.value})
        return med_plus

    med_plus_findings = _confirmed_med_plus()

    # P2 needs a propagate corpus (built by `propagate init-corpus`). If the
    # corpus dir is missing, skip P2 gracefully instead of failing.
    propagate_corpus = workspace / "propagate_corpus"
    if med_plus_findings and not skip_propagate:
        if not propagate_corpus.is_dir():
            console.print(
                f"[yellow]Pillar 2 skipped — corpus not initialized at "
                f"{propagate_corpus}. Run `audit-pipeline propagate init-corpus "
                f"--output {propagate_corpus}` first.[/yellow]"
            )
            pillar_results["propagate"]["_skipped"] = "corpus not initialized"
        else:
            console.print()
            console.print(
                f"[bold]Pillar 2 - auto-fire propagate on "
                f"{len(med_plus_findings)} Med+ finding(s) across sibling "
                f"protocols[/bold]"
            )
            for c in med_plus_findings:
                hyp_id = c["hypothesis_id"]
                findings_for_hyp = [
                    f for f in db.list_findings(target_id=target_id, limit=200)
                    if f.get("cycle_id") == cycle_id and f.get("hypothesis_id") == hyp_id
                ]
                if not findings_for_hyp:
                    pillar_results["propagate"][hyp_id] = {"error": "finding not in db"}
                    continue
                finding_id = findings_for_hyp[0]["id"]
                rc = _run([
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "propagate", "auto-fire",
                    "--finding-id", str(finding_id),
                    "--corpus", str(propagate_corpus),
                ], timeout=600)
                pillar_results["propagate"][hyp_id] = {
                    "finding_id": finding_id, "returncode": rc,
                }
                log("propagate_done", hypothesis_id=hyp_id, finding_id=finding_id, rc=rc)
                daily_cap.record_spend(1.50)
                total_cost += 1.50

    if med_plus_findings and not skip_bundle:
        console.print()
        console.print(
            f"[bold]Pillar 3 - bundle draft + verify on "
            f"{len(med_plus_findings)} Med+ finding(s) "
            f"(does NOT open PRs)[/bold]"
        )
        for c in med_plus_findings:
            hyp_id = c["hypothesis_id"]
            findings_for_hyp = [
                f for f in db.list_findings(target_id=target_id, limit=200)
                if f.get("cycle_id") == cycle_id and f.get("hypothesis_id") == hyp_id
            ]
            if not findings_for_hyp:
                pillar_results["bundle"][hyp_id] = {"error": "finding not in db"}
                continue
            finding_id = findings_for_hyp[0]["id"]
            # bundle draft signature: takes finding_id as POSITIONAL arg.
            # Also pass engine-repo + target-file + poc-source-file +
            # poc-test-name so the bundle has everything `verify` needs.
            poc_info = poc_results.get(hyp_id, {})
            scaffold_path = poc_info.get("scaffold_path")
            poc_test_name = f"test_{_slugify(hyp_id)}"
            meta = hyp_meta.get(hyp_id, {})
            target_file_rel = meta.get("target_file", "src/percolator.rs")
            draft_argv = [
                _audit_pipeline_bin(), "--workspace", str(workspace),
                "bundle", "draft",
                str(finding_id),
                "--engine-repo", str(engine_dir_for_cargo),
                "--target-file", target_file_rel,
                "--poc-test-name", poc_test_name,
            ]
            if scaffold_path:
                draft_argv += ["--poc-source-file", scaffold_path]
            rc_draft = _run(draft_argv, timeout=900)
            rc_verify = None
            if rc_draft == 0:
                rc_verify = _run([
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "bundle", "verify",
                    str(finding_id),
                    "--engine-repo", str(engine_dir_for_cargo),
                    "--poc-test-name", poc_test_name,
                ], timeout=600)
            pillar_results["bundle"][hyp_id] = {
                "finding_id": finding_id,
                "draft_rc": rc_draft,
                "verify_rc": rc_verify,
            }
            log("bundle_done", hypothesis_id=hyp_id, finding_id=finding_id,
                draft_rc=rc_draft, verify_rc=rc_verify)
            daily_cap.record_spend(5.00)
            total_cost += 5.00

    # Pillar 4: always sign Merkle root for the cycle (cheap, no API spend)
    if not skip_merkle:
        console.print()
        console.print("[bold]Pillar 4 - compute + sign Merkle root for this cycle[/bold]")
        rc_merkle = _run([
            _audit_pipeline_bin(), "--workspace", str(workspace),
            "merkle", "compute", cycle_id, "--sign",
        ], timeout=120)
        pillar_results["merkle"] = {"cycle_id": cycle_id, "returncode": rc_merkle}
        log("merkle_signed", cycle_id=cycle_id, rc=rc_merkle)

    # Persist pillar results as a cycle artifact
    (cycle_dir / "pillars.json").write_text(
        json.dumps(pillar_results, indent=2), encoding="utf-8"
    )

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
        f"🚨 *Jelleo hunt cycle {cycle_id}* (`{target}`) — "
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
