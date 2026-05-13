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
import os
import re
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
from audit_pipeline.gates.disclosure_history import (
    filter_hypotheses_by_disclosure_history,
)
from audit_pipeline.gates.freshness_gate import check_freshness
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
    type=float, default=1_000_000.0, show_default=True,
    help=(
        "Total Claude spend cap for one hunt cycle. Default is effectively "
        "unlimited ($1M) — caps were removed per operator request 2026-05-13 "
        "after $10/cycle defaults kept clipping real cycles mid-flight. "
        "Pass a positive value if you want an explicit cap."
    ),
)
@click.option(
    "--daily-cap-usd",
    type=float, default=0.0, show_default=True, envvar="AUDIT_DAILY_CAP_USD",
    help=(
        "Total Claude spend cap PER DAY across all cycles. Default 0 = "
        "no daily cap (the per-cycle --budget-cap-usd is the only limit, "
        "and that's also effectively unlimited by default)."
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
@click.option(
    "--resume-cycle",
    default=None,
    help=(
        "Resume a partial hunt cycle by ID (e.g. 20260511-032554). Reuses the "
        "existing <workspace>/hunts/<cycle-id>/ directory. Each layer's per-hyp "
        "loop checks for already-completed artifacts and skips them: "
        "Layer 1 skips entirely if recon_summary.json exists; Layer 1.5 / 2 / 3 "
        "/ 4 skip per-hyp where their output file is present. P2/P3/P4 always "
        "re-run (idempotent). Use this after Ctrl-C, OOM, or hunt.py crash to "
        "pick up where the prior cycle stopped without re-running paid work."
    ),
)
@click.option(
    "--auto-publish/--no-auto-publish",
    default=True,
    show_default=True,
    help=(
        "When the cycle finishes, run deploy/publish_cycle.sh: copies "
        "artifacts to the public examples/ directory + git push, publishes "
        "to /var/www/jelleo.com/cycles/<id>/, renders + signs PDF, and "
        "emails Critical/High confirmed findings via notifier.json. "
        "Silently no-ops if the script is not found on the host. Override "
        "the script path via the JELLEO_PUBLISH_SCRIPT env var."
    ),
)
@click.option(
    "--ignore-freshness", is_flag=True, default=False,
    help=(
        "Bypass Gate L0.freshness — by default the cycle refuses to start "
        "if workspace.json's pinned SHAs are stale beyond --max-stale-hours. "
        "Pass this to intentionally audit a frozen snapshot."
    ),
)
@click.option(
    "--max-stale-hours", type=float, default=6.0, show_default=True,
    help=(
        "Gate L0.freshness grace window in hours. 0 = strict (pinned must "
        "equal upstream HEAD exactly)."
    ),
)
@click.option(
    "--ignore-disclosure-history", is_flag=True, default=False,
    help=(
        "Bypass Gate L5.disclosure_history — by default hypotheses whose "
        "``prior_disclosure.decision`` is ``rejected`` (with no "
        "``revisit_justification``) or ``merged`` are filtered out of the "
        "cycle. Pass this to re-run every hypothesis regardless of history."
    ),
)
# L1 + L1.5 SOURCE-GROUNDING / INDEPENDENCE — these flags propagate into
# the recon + debate subprocess invocations. Default is ON for tool-using
# recon because the alternative (speculation-based single-shot) produced
# the cycle-20260511 retraction. Operators can disable for cheap CI runs.
@click.option(
    "--use-tools/--no-use-tools", default=True, show_default=True,
    help=(
        "Layer 1 recon: route each hypothesis through the tool-using "
        "agent loop (read_file/grep/find_function against live workspace) "
        "instead of single-shot completion. Defeats the speculation-recon "
        "hallucination that retracted cycle 20260511."
    ),
)
@click.option(
    "--tool-max-turns", type=int, default=12, show_default=True,
    help="Max tool-using turns per hypothesis (only used with --use-tools).",
)
@click.option(
    "--challenger-model", default=None,
    help=(
        "L1.5 debate independence: route the challenger through a "
        "different model than the proposer (e.g. ``claude-opus-4-5``). "
        "Defeats same-mistake bias when proposer + challenger share a "
        "backbone."
    ),
)
@click.option(
    "--redact-proposer-evidence/--full-proposer-evidence",
    default=True, show_default=True,
    help=(
        "L1.5 debate independence: pass only the proposer's verdict "
        "line (not the line citations + code) to the challenger. Forces "
        "independent re-derivation rather than rubber-stamp."
    ),
)
@click.option(
    "--triage-fires/--no-triage-fires", default=True, show_default=True,
    help=(
        "Layer 2.5: between Layer 2 (PoC) and Layer 3 (Kani), classify each "
        "PoC fire as STRONG / SOFT / FALSE / LOST and dispatch ONLY the "
        "STRONG cluster representatives to Layer 3. Saves ~$280 of "
        "wasted Kani+LiteSVM spend per false-fire-heavy cycle. Productized "
        "from the manual triage that collapsed cycle 20260511's 64 fires "
        "down to 7 STRONG / 4 root causes."
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
    resume_cycle: str | None,
    auto_publish: bool,
    ignore_freshness: bool,
    max_stale_hours: float,
    ignore_disclosure_history: bool,
    use_tools: bool,
    tool_max_turns: int,
    challenger_model: str | None,
    redact_proposer_evidence: bool,
    triage_fires: bool,
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

    # Phase 1h — language-aware multi-target dispatch.
    #
    # workspace.json may declare:
    #   * "language":   "solana" | "c" | "solidity" | "aptos"
    #   * "hyp_library": "path/to/<class>.yaml" — class library to load when
    #                    no --hypotheses / --protocol-class is passed.
    #   * "customer_id": "ottersec" | "percolator" | ... — selects which
    #                    customer-scoped findings.db to write into. Without
    #                    this, every OSec eval cell would write to its own
    #                    isolated per-workspace DB and the dashboard would
    #                    see no findings.
    #
    # Defaults preserve legacy Percolator behavior (language=solana, DB
    # local to workspace).
    config_language = str(config.get("language") or "solana").strip().lower()
    if config_language not in ("solana", "c", "solidity", "aptos"):
        raise click.ClickException(
            f"workspace.json `language` = {config_language!r} is not one of "
            "{solana, c, solidity, aptos}."
        )
    workspace_hyp_library = config.get("hyp_library") or None
    customer_id_from_workspace = config.get("customer_id") or None

    # Gate L0.freshness — refuse to start the cycle if upstream has moved
    # past our pinned SHA more than --max-stale-hours ago. Cycle 20260511-183154
    # was retracted in part because the wrapper clone was 3 commits behind
    # at cycle start, and one of those missing commits had already fixed
    # the bug class our PoC then "confirmed".
    #
    # Phase B self-audit Defect 04: previously this also skipped on
    # --resume-cycle, which is the EXACT scenario the gate exists to catch
    # (a stale cycle resumed hours/days later runs against code upstream
    # has already moved past). Now: always run the gate; the operator can
    # still pass --ignore-freshness if a frozen-snapshot audit is the
    # intent. Resume gets an extra warning line if upstream drifted.
    if not ignore_freshness:
        fresh = check_freshness(workspace=workspace, max_stale_hours=max_stale_hours)
        if fresh.passed is False:
            extra = (
                "\n\nThis is a RESUME path; the cycle's recorded engine_sha "
                "is from when the cycle originally started. Upstream may have "
                "moved past it. Re-run `audit-pipeline freshness --update` "
                "before resuming, OR pass --ignore-freshness if you "
                "intentionally want to finish the audit against the original "
                "snapshot."
            ) if resume_cycle else ""
            raise click.ClickException(
                f"Gate L0.freshness FAILED:\n  {fresh.reason}{extra}"
            )
        if fresh.passed is None:
            console.print(
                f"[yellow]Gate L0.freshness SKIP[/yellow] — {fresh.reason}"
            )
        else:
            console.print(f"[dim]Gate L0.freshness pass — {fresh.reason}[/dim]")

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
    # POST-AUDIT FIX: every NamedTemporaryFile(delete=False) below leaked
    # a /tmp YAML per cycle. Register atexit cleanup so each temp file
    # gets removed when the process exits (success, exception, signal).
    import atexit as _atexit_hunt
    import os as _os_hunt
    def _cleanup_temp_yaml(path: str) -> None:
        try:
            _os_hunt.unlink(path)
        except (OSError, FileNotFoundError):
            pass
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
        _atexit_hunt.register(_cleanup_temp_yaml, tmp.name)
    elif hypotheses is None:
        # Resolution order:
        #   1. CLI flag --hypotheses (already handled above)
        #   2. CLI flag --protocol-class (already handled above)
        #   3. workspace.json `hyp_library` (declared per-target language class)
        #   4. <workspace>/hypotheses.yaml (legacy)
        candidate_paths: list[Path] = []
        if workspace_hyp_library:
            # hyp_library may be relative to the audit-pipeline-cli package's
            # templates dir (`src/audit_pipeline/templates/...`) OR an absolute
            # path. Try both.
            hl_path = Path(workspace_hyp_library)
            if hl_path.is_absolute() and hl_path.exists():
                candidate_paths.append(hl_path)
            else:
                # Relative to the installed package
                from audit_pipeline import __file__ as _ap_pkg_path
                pkg_root = Path(_ap_pkg_path).resolve().parent.parent
                pkg_relative = pkg_root / workspace_hyp_library
                if pkg_relative.exists():
                    candidate_paths.append(pkg_relative)
                # Also try relative to repo root (one level above the
                # `src/audit_pipeline/...` package install location)
                src_relative = (pkg_root.parent / workspace_hyp_library)
                if src_relative.exists():
                    candidate_paths.append(src_relative)
                # Also try relative to workspace itself
                ws_relative = workspace / workspace_hyp_library
                if ws_relative.exists():
                    candidate_paths.append(ws_relative)
        candidate_paths.append(workspace / "hypotheses.yaml")

        chosen: Path | None = next((p for p in candidate_paths if p.exists()), None)
        if chosen is None:
            raise click.ClickException(
                "No --hypotheses or --protocol-class passed, and no library "
                f"found at any of: {', '.join(str(p) for p in candidate_paths)}.\n"
                "Either pass --hypotheses, --protocol-class, declare hyp_library "
                "in workspace.json, or drop hypotheses.yaml in the workspace."
            )
        hypotheses = str(chosen)
        console.print(
            f"  [cyan]Using hyp library: {chosen}[/cyan]"
        )

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
            _atexit_hunt.register(_cleanup_temp_yaml, tmp2.name)

    # Gate L5.disclosure_history — filter out hypotheses that restate
    # previously-disclosed-and-rejected patterns (without explicit
    # ``revisit_justification``), or whose underlying defect was already
    # merged upstream. Cycle 20260511-183154 re-derived 7 variants of the
    # PR #39 residual-conservation cluster because the pipeline had no
    # notion of disclosure history.
    if not ignore_disclosure_history:
        import tempfile as _tempfile3

        import yaml as _yaml3
        try:
            _hyp_doc = _yaml3.safe_load(Path(hypotheses).read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(
                f"Gate L5.disclosure_history: cannot read {hypotheses}: {e}"
            )
        _hyps_list = _hyp_doc.get("hypotheses") or []
        if _hyps_list:
            allowed, blocked = filter_hypotheses_by_disclosure_history(_hyps_list)
            if blocked:
                console.print(
                    f"  [yellow]Gate L5.disclosure_history: filtered "
                    f"{len(blocked)} of {len(_hyps_list)} hyps[/yellow]"
                )
                for h, gr in blocked[:8]:
                    icon = "skip" if gr.passed is None else "block"
                    console.print(
                        f"    [{icon}] {h.get('id','?')}: {gr.reason[:140]}"
                    )
                if len(blocked) > 8:
                    console.print(f"    ... and {len(blocked) - 8} more")
            tmp3 = _tempfile3.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            )
            _yaml3.safe_dump(
                {"hypotheses": allowed}, tmp3, sort_keys=False, allow_unicode=True,
            )
            tmp3.close()
            hypotheses = tmp3.name
            _atexit_hunt.register(_cleanup_temp_yaml, tmp3.name)

    # Orchestration audit Defect 01 (HIGH): `--engine-only` previously
    # was a NO-OP — declared, captured, never read. Now it actually
    # filters out wrapper-only hyps (target_file under wrapper/ OR
    # applies_to == ["wrapper"]).
    if engine_only:
        # POST-AUDIT FIX (2026-05-12 re-audit catch): split stdlib + 3p
        # imports onto separate lines (ruff I001) — tempfile is stdlib,
        # yaml is third-party so they must not share an import block.
        import tempfile as _tempfile4

        import yaml as _yaml4
        try:
            _doc = _yaml4.safe_load(Path(hypotheses).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            _doc = {}
        _hyps_in = _doc.get("hypotheses") or []
        def _is_engine_hyp(h: dict) -> bool:
            tgt = (h.get("target_file") or "")
            if tgt.startswith("target/wrapper/") or tgt.startswith("wrapper/src/"):
                return False
            applies = h.get("applies_to") or []
            if isinstance(applies, list) and len(applies) == 1 and "wrapper" in applies[0].lower():
                return False
            return True
        kept_eo = [h for h in _hyps_in if _is_engine_hyp(h)]
        dropped_eo = len(_hyps_in) - len(kept_eo)
        if dropped_eo > 0:
            console.print(
                f"  [cyan]--engine-only: dropped {dropped_eo} wrapper-only "
                f"hyps; {len(kept_eo)} remain[/cyan]"
            )
            tmp4 = _tempfile4.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            )
            _yaml4.safe_dump(
                {"hypotheses": kept_eo}, tmp4, sort_keys=False, allow_unicode=True,
            )
            tmp4.close()
            hypotheses = tmp4.name
            _atexit_hunt.register(_cleanup_temp_yaml, tmp4.name)

    if not is_available():
        raise click.ClickException(
            "ANTHROPIC_API_KEY required for hunt. Set it and re-run."
        )

    # workspace.json uses the key `target_name`; fall back to legacy `name`
    # for old workspaces. Without this, every cycle bound to the literal
    # string "default" and the demo customer's percolator-substring filter
    # silently hid them all.
    target = target_name or config.get("target_name") or config.get("name") or "default"

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
            resume_cycle=resume_cycle,
            auto_publish=auto_publish,
            use_tools=use_tools,
            tool_max_turns=tool_max_turns,
            challenger_model=challenger_model,
            redact_proposer_evidence=redact_proposer_evidence,
            triage_fires=triage_fires,
            language=config_language,
            customer_id=customer_id_from_workspace,
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
    resume_cycle: str | None = None,
    auto_publish: bool = True,
    use_tools: bool = True,
    tool_max_turns: int = 12,
    challenger_model: str | None = None,
    redact_proposer_evidence: bool = True,
    triage_fires: bool = True,
    language: str = "solana",
    customer_id: str | None = None,
) -> None:
    """Hunt cycle body, factored out so the snapshot lifecycle wraps it cleanly."""

    # ---------- Daily cap check ----------
    # Orchestration audit Defect 02 (HIGH): two `audit-pipeline hunt`
    # invocations against the same workspace previously raced on the
    # DB + DailyCap state. SQLite WAL (Batch 3) helps with reads, but
    # the right-shape fix is a workspace-level advisory lock so that
    # only one cycle runs per workspace at a time. Best-effort: drops
    # to a "no lock" warning on platforms without fcntl/msvcrt.
    _lock_handle = None
    try:
        lock_path = workspace / ".hunt.lock"
        _lock_handle = open(lock_path, "a+b")
        try:
            import fcntl
            try:
                fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise click.ClickException(
                    f"Another `audit-pipeline hunt` is already running on this "
                    f"workspace (locked via {lock_path}). Wait for it to finish, "
                    f"or remove the lock if you're sure no other run is active."
                ) from e
        except ImportError:
            try:
                import msvcrt
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise click.ClickException(
                    f"Another hunt holds {lock_path}. Wait or remove."
                ) from e
    except OSError:
        # Filesystem can't open lock file — proceed without lock with a warning.
        # POST-AUDIT FIX (2026-05-12): drop f-prefix on f-strings without
        # placeholders (ruff F541) — these are static rich-markup strings.
        console.print("[yellow]workspace lock unavailable; proceeding without"
                      " mutual exclusion[/yellow]")

    # POST-AUDIT FIX: register a cleanup that releases the lock on ANY
    # process exit (normal completion, exception, sys.exit, KeyboardInterrupt).
    # Previously the FD relied on GC + process death to release the kernel
    # lock; on msvcrt (Windows) the byte-range lock could survive abnormal
    # exit and block the next invocation with a confusing "Another hunt
    # holds .hunt.lock" message.
    if _lock_handle is not None:
        import atexit
        _close_handle = _lock_handle
        atexit.register(lambda h=_close_handle: (h.close() if not h.closed else None))

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
    #
    # PHASE 1h B1 fix: when workspace.json declares customer_id (OSec
    # eval, or any other multi-target customer), every cell's findings
    # need to flow into the SHARED customer-level findings.db that the
    # dashboard reads. Without this, each per-workspace DB is isolated
    # and the customer dashboard sees zero findings.
    #
    # Convention: the shared DB lives at
    #   <repo_root>/audit_runs/<customer_id>-eval/findings.db
    # which is the parent of the per-workspace dirs created by
    # ottersec-eval/setup_workspaces.py.
    db_workspace = workspace
    if customer_id:
        # Walk up: workspace is <repo>/audit_runs/<customer>-eval/workspaces/<cell>
        # parent.parent.parent gives us <repo>/audit_runs/<customer>-eval
        candidate = workspace.parent.parent
        if (candidate / "findings.db").exists() or candidate.name.endswith("-eval"):
            db_workspace = candidate
            console.print(
                f"  [cyan]customer_id={customer_id}: writing findings to "
                f"shared DB at {candidate / 'findings.db'}[/cyan]"
            )
    db = open_findings_db(db_workspace)
    target_id = db.upsert_target(
        name=target,
        github_url=config.get("engine", {}).get("repo"),
        engine_repo=config.get("engine", {}).get("repo"),
        wrapper_repo=config.get("wrapper", {}).get("repo"),
        config=config,
    )

    hunts_dir = workspace / "hunts"
    hunts_dir.mkdir(parents=True, exist_ok=True)

    if resume_cycle:
        # RESUME PATH: reuse the existing cycle dir + DB record. Per-layer
        # loops below check for already-completed artifacts and skip them.
        cycle_id = resume_cycle
        cycle_dir = hunts_dir / cycle_id
        if not cycle_dir.is_dir():
            raise click.ClickException(
                f"--resume-cycle {resume_cycle} requested but {cycle_dir} "
                f"does not exist. Available cycles: "
                f"{sorted(p.name for p in hunts_dir.iterdir() if p.is_dir())[-5:]}"
            )
        # REOPEN the cycle so the dashboard reflects "in progress" again.
        # Without this, a previously-completed cycle (hunt_summary.json
        # + DB finished_at set) being resumed left the dashboard showing
        # "all layers done" even as new L2 PoCs were being authored +
        # run. Operator caught this with: "why the fuck the waterfall
        # shows that all layers are done already".
        #
        # We:
        # 1. Clear DB finished_at so manifest reports in_progress
        # 2. Move (NOT delete) hunt_summary.json aside so the
        #    in_progress_cycle_progress phase detector goes back to
        #    "poc" / "kani" / etc. instead of "publishing". Keeping a
        #    backup means we don't lose the previous run's report if
        #    the resume itself fails before producing a new one.
        # 3. Move (NOT delete) the .publish-blocked sentinel for the
        #    same reason.
        try:
            db.reopen_cycle(cycle_id)
        except AttributeError:
            # Back-compat: older DB has no reopen_cycle(); patch via raw SQL.
            try:
                with db._conn() as _c:
                    _c.execute(
                        "UPDATE cycles SET finished_at = NULL "
                        "WHERE cycle_id = ?", (cycle_id,)
                    )
            except Exception:
                pass
        for stale in ("hunt_summary.json", ".publish-blocked", "retraction.json"):
            stale_path = cycle_dir / stale
            if stale_path.exists():
                backup = cycle_dir / (stale + ".pre-resume")
                try:
                    stale_path.rename(backup)
                except OSError:
                    pass
        # NB: `log()` is defined later in this function (closure over
        # cycle_log), so we cannot record into hunt.log.jsonl from here
        # — only operator-facing console.print is available. Acceptable
        # because the reopen is a one-shot diagnostic event and the
        # rest of the cycle's log will reflect the fresh phase entries.
        console.print(
            f"[cyan]Resume reopened cycle {cycle_id}: cleared "
            f"finished_at + moved hunt_summary.json aside[/cyan]"
        )
        # The DB already has this cycle inserted from the prior run; skip insert
        # (db.insert_cycle may not be idempotent — safer to omit).
    else:
        base_cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        # When in snapshot mode, suffix with SHA short-form for traceability
        if source_repo and resolved_sha:
            cycle_id = f"{base_cycle_id}-{resolved_sha[:7]}"
        else:
            cycle_id = base_cycle_id
        cycle_dir = hunts_dir / cycle_id
        # FIX C7: collision-safe cycle ID. If two cycles fire within the same
        # UTC second, append a 4-char random suffix.
        if cycle_dir.exists():
            import secrets
            cycle_id = f"{cycle_id}-{secrets.token_hex(2)}"
            cycle_dir = hunts_dir / cycle_id
        cycle_dir.mkdir(parents=True, exist_ok=True)

        db.insert_cycle(
            target_id=target_id,
            cycle_id=cycle_id,
            engine_sha=resolved_sha,
            wrapper_sha=config.get("wrapper", {}).get("sha"),
            summary_json_path=str(cycle_dir / "hunt_summary.json"),
        )

    # Live-dashboard event emission: every subprocess (recon, debate,
    # poc-llm, ...) inherits JELLEO_CYCLE_LOG_PATH so its emit_event
    # calls write into THIS cycle's hunt.log.jsonl, which the SSE
    # service tails and broadcasts to subscribed customer dashboards.
    # The python process running hunt.py reads the same env var.
    #
    # POST-AUDIT FIX: hoisted out of the new-cycle `else:` branch so
    # resumed cycles (--resume-cycle) ALSO set these vars. Previously
    # resume bypassed the assignments, so subprocesses spawned during
    # a resumed cycle wrote events into whatever cycle the
    # event_log fallback resolved (latest dir by mtime) — could leak
    # events into a different cycle's log.
    import os as _os
    _os.environ["JELLEO_CYCLE_LOG_PATH"] = str(cycle_dir / "hunt.log.jsonl")
    _os.environ["JELLEO_WORKSPACE"] = str(workspace)

    source_mode_line = (
        f"Source mode:  snapshot ({source_repo}@{resolved_sha[:10]})"
        if source_repo
        else f"Source mode:  local clone ({engine_dir_for_cargo})"
    )

    resume_marker = (
        "[yellow]RESUMING from prior partial state[/yellow]\n"
        if resume_cycle else ""
    )
    console.print(
        Panel.fit(
            f"[bold]Hunt cycle [cyan]{cycle_id}[/cyan][/bold]  ({target})\n"
            f"{resume_marker}\n"
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
    summary_path = recon_out / "recon_summary.json"

    # RESUME: if recon_summary.json already exists and is non-empty, skip
    # Layer 1 entirely. The recon command writes the summary at the END of
    # its work, so presence == completion. FIX A-H1: also try-parse the JSON
    # to catch the SIGKILL-mid-write case where the file exists but is
    # truncated. Corrupt summary => fall through to re-run recon.
    summary_is_valid_for_resume = False
    if resume_cycle and summary_path.exists() and summary_path.stat().st_size > 50:
        try:
            _probe = json.loads(summary_path.read_text())
            if isinstance(_probe, dict) and "verdicts" in _probe:
                summary_is_valid_for_resume = True
        except (json.JSONDecodeError, OSError) as e:
            log("layer1_resume_summary_corrupt", error=str(e))
            console.print(
                f"[yellow]Layer 1 resume blocked — recon_summary.json is "
                f"corrupt ({e}). Re-running Layer 1.[/yellow]"
            )
    if summary_is_valid_for_resume:
        log("layer1_resumed_from_existing", path=str(summary_path))
        console.print(
            f"[green]Layer 1 SKIPPED — resuming from existing recon_summary.json "
            f"({summary_path.stat().st_size} bytes)[/green]"
        )
    else:
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
            # PHASE 1h: pass the target language so recon picks the right
            # system prompt + language-specific framing. Without this the
            # recon subprocess defaults to Solana semantics regardless of
            # what workspace.json said.
            "--language", language,
        ]
        # L1 recon Defect 01 (the cycle-20260511 retraction root cause):
        # thread --use-tools and --tool-max-turns through the autonomous
        # hunt loop's recon subprocess so source-grounded tool use is the
        # DEFAULT behavior, not a manual override the operator has to
        # remember on each invocation.
        recon_argv += ["--use-tools" if use_tools else "--no-use-tools"]
        if use_tools:
            recon_argv += ["--tool-max-turns", str(tool_max_turns)]
        if source_repo:
            recon_argv += ["--source-repo", source_repo, "--source-sha", resolved_sha]
        # Layer 1 budget: 4 hours. The default _run() timeout is 30 min
        # which is too short for a 471-hyp library at 4-concurrent (each
        # API call is 30-90s). A timeout here aborts the entire recon
        # subprocess, returns rc=124, leaves the cycle without a
        # recon_summary.json so all downstream layers get skipped, AND
        # wastes whatever Layer 1 budget already spent. Set a wide upper
        # bound; real cost is gated by --budget-cap-usd anyway.
        rc = _run(recon_argv, timeout=14400)
        log("layer1_done", returncode=rc)

    if not summary_path.exists():
        log("layer1_no_summary", path=str(summary_path))
        console.print(f"[red]Layer 1 did not produce a summary at {summary_path}[/red]")
        db.finish_cycle(cycle_id, n_dispatched=0, n_confirmed=0, total_cost_usd=0)
        return

    summary = json.loads(summary_path.read_text())
    layer1_cost = float(summary.get("total_cost_usd", 0.0))
    total_cost += layer1_cost
    # POST-AUDIT FIX: on resume, the daily_cap already recorded this
    # cost during the original cycle. Re-recording it here over multiple
    # resumes would artificially exhaust the cap. Only record when we
    # actually ran Layer 1 in this invocation.
    if not summary_is_valid_for_resume:
        daily_cap.record_spend(layer1_cost)
    verdicts: list[dict[str, Any]] = summary.get("verdicts", [])

    # Build a hyp_id -> hypothesis-meta map (need class for severity derivation)
    import yaml
    with open(hypotheses) as f:
        hyp_meta_list = yaml.safe_load(f).get("hypotheses", [])
    # FIX #7: detect duplicate hyp ids — when --protocol-class merges multiple
    # YAML files, two files could share an id (e.g. `H1` in percolator.yaml
    # and percolator_deep.yaml). Silent overwrite was masking real duplicates;
    # now we fail loudly with the conflicting ids so the user can fix them.
    seen_ids: dict[str, int] = {}
    duplicates: list[str] = []
    for h in hyp_meta_list:
        hid = h.get("id")
        if hid in seen_ids:
            duplicates.append(hid)
        seen_ids[hid] = seen_ids.get(hid, 0) + 1
    if duplicates:
        log("hyp_id_collisions", duplicates=duplicates)
        console.print(
            f"[yellow]Warning: {len(duplicates)} hyp id collision(s): "
            f"{duplicates[:5]}{'...' if len(duplicates) > 5 else ''}. "
            f"Later entries overwrite earlier ones in hyp_meta.[/yellow]"
        )
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
            challenger_resp = debate_out / f"{hyp_id}_challenger_response.md"
            # RESUME: skip per-hyp if challenger response already exists from
            # a prior run. The debate output file is written at the END of the
            # debate command, so presence == completion. Don't bill the
            # flat-rate cost when we didn't actually call the LLM.
            debate_was_skipped = False
            if resume_cycle and challenger_resp.exists() and challenger_resp.stat().st_size > 50:
                log("debate_resumed_from_existing", hyp_id=hyp_id)
                rc = 0
                debate_was_skipped = True
            else:
                debate_argv = [
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "debate",
                    "--hypothesis-id", hyp_id,
                    "--proposer-verdict", str(proposer_path),
                    "--hypotheses-file", hypotheses,
                    "--output", str(debate_out),
                    "--auto",
                    # PHASE 1h: pass language so the challenger uses the
                    # right adversarial frame (Solana/Anchor vs C/UB vs
                    # Solidity/EVM vs Aptos/Move).
                    "--language", language,
                ]
                # L1.5 debate independence — thread through into the hunt
                # loop's debate subprocess so cycles get the independence
                # guarantees by default, not just when run manually.
                debate_argv += [
                    "--redact-proposer-evidence" if redact_proposer_evidence
                    else "--full-proposer-evidence"
                ]
                if challenger_model:
                    debate_argv += ["--challenger-model", challenger_model]
                if source_repo:
                    debate_argv += ["--source-repo", source_repo, "--source-sha", resolved_sha]
                rc = _run(debate_argv)
            challenger_resp = debate_out / f"{hyp_id}_challenger_response.md"
            verdict_changed = False
            demoted = False
            if challenger_resp.exists():
                txt = challenger_resp.read_text(encoding="utf-8", errors="replace").upper()
                proposer_verdict = v.get("verdict")
                # Distinguish challenger outcomes — "DISAGREE" means the
                # challenger refutes the proposer; "NEEDS_LAYER_2" means the
                # challenger could not decide and explicitly asks for empirical
                # proof at Layer 2. The legacy code conflated them and demoted
                # both, which silently dropped hyps the challenger wanted
                # tested. Net effect on the 2026-05-11 Step 3 cycle: 108
                # TRUE/HIGH-at-L1 hyps including the BR-F7-helper-conservation
                # regression hyp got dropped without ever reaching Layer 2 PoC.
                # Word-boundary matching — "DISAGREE" as a substring of
                # "DISAGREEMENT" used to silently DEMOTE every TRUE/HIGH
                # whose challenger mentioned "resolve the disagreement"
                # in its narrative. aptos-small dry-run hit this: all 14
                # L1-confirmed TRUE/HIGH bugs (APT1-10, etc.) got dropped
                # from L2 candidates, so the orchestrator only PoC'd the
                # 26 challenger-promoted FALSE/HIGH ones. $27 wasted on
                # the wrong set. Use \b boundaries so DISAGREEMENT no
                # longer matches DISAGREE.
                import re as _re_debate
                challenger_disagrees = bool(
                    _re_debate.search(r"\bDISAGREE\b", txt)
                )
                challenger_needs_l2 = "NEEDS_LAYER_2" in txt
                if proposer_verdict == "FALSE" and (challenger_disagrees or challenger_needs_l2):
                    # Challenger says FALSE may be wrong — promote to L2.
                    if v not in candidates:
                        candidates.append(v)
                        promoted_ids.add(hyp_id)
                        verdict_changed = True
                elif proposer_verdict == "TRUE" and challenger_needs_l2 and not challenger_disagrees:
                    # Challenger asks for empirical proof. KEEP in candidates
                    # so Layer 2 PoC fires. This is the whole point of Layer 2.
                    if v not in candidates:
                        candidates.append(v)
                        promoted_ids.add(hyp_id)
                    verdict_changed = True
                elif proposer_verdict == "TRUE" and challenger_disagrees:
                    # Challenger refutes the TRUE verdict — demote from
                    # candidates to avoid wasting Layer 2 cost on a likely
                    # false positive. Verdict recorded as TRUE but marked
                    # debate-demoted in the DB details.
                    candidates[:] = [c for c in candidates if c is not v]
                    demoted = True
            debate_results[hyp_id] = {
                "returncode": rc,
                "challenger_response_path": str(challenger_resp),
                "promoted_to_layer2": verdict_changed,
                "demoted_by_debate": demoted,
            }
            log("debate_one", hypothesis_id=hyp_id, returncode=rc, promoted=verdict_changed)
            # Don't bill flat-rate cost on resumed (skipped) debates.
            # CALIBRATED 2026-05-11: real debate cost ~$0.03 (3k in + 1.2k
            # out @ Sonnet); flat-rate $0.05 includes small outlier cushion.
            if not debate_was_skipped:
                daily_cap.record_spend(0.05)
                total_cost += 0.05

    # ---------- Layer 2: PoC scaffold + run ----------
    #
    # PHASE 1h: dispatch by `language`. Solana keeps the existing
    # cargo-test path (unchanged) because we have a large body of Solana
    # cycles that depend on it. Other languages route through the
    # `poc_adapters/` package — same interface, language-specific tooling.
    poc_results: dict[str, dict[str, Any]] = {}
    # Resolve the language adapter once. Used by the non-Solana branches.
    _l2_adapter = None
    if language != "solana":
        from audit_pipeline.poc_adapters import get_adapter as _get_poc_adapter
        try:
            _l2_adapter = _get_poc_adapter(language)
        except Exception as e:  # noqa: BLE001
            log("l2_adapter_unavailable", language=language, error=str(e))
            console.print(
                f"[red]No L2 PoC adapter for language={language!r}; "
                f"skipping Layer 2 entirely[/red]"
            )
    if not skip_poc and candidates:
        console.print()
        console.print(
            f"[bold]Layer 2 - empirical PoC scaffolding on "
            f"{len(candidates)} candidates ({language})[/bold]"
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

            # ----- PHASE 1h: non-Solana languages use adapter dispatch -----
            if language != "solana":
                if _l2_adapter is None:
                    # Adapter unavailable; record skip and move on.
                    poc_results[hyp_id] = {
                        "scaffold_path": None,
                        "scaffold_rc": -1,
                        "compile_test_rc": None,
                        "fired": False,
                        "outcome": "adapter_unavailable",
                        "authoring_mode": f"adapter:{language}",
                    }
                    continue

                # ---- Per-hyp adapter resume check ----
                # Without this, --resume-cycle re-spent ~$0.10/hyp re-
                # authoring + re-running L2 PoCs for hyps that already
                # had outcomes from a prior run. Adapter path now mirrors
                # the Solana legacy path: if runlog exists from a prior
                # run in this cycle dir, reload the outcome from
                # hunt.log.jsonl instead of re-running.
                _adapter_log_path = poc_out / f"runlog_{finding_name}.log"
                if (
                    resume_cycle
                    and _adapter_log_path.is_file()
                    and _adapter_log_path.stat().st_size > 50
                ):
                    # Find last poc_adapter_done event for this hyp
                    prior_event = None
                    try:
                        with (cycle_dir / "hunt.log.jsonl").open("r", encoding="utf-8") as _f:
                            for _line in _f:
                                if '"poc_adapter_done"' not in _line:
                                    continue
                                if f'"hypothesis_id": "{hyp_id}"' not in _line:
                                    continue
                                try:
                                    prior_event = json.loads(_line)
                                except json.JSONDecodeError:
                                    continue
                    except OSError:
                        prior_event = None
                    if prior_event:
                        poc_results[hyp_id] = {
                            "scaffold_path": prior_event.get("scaffold_path"),
                            "scaffold_rc": 0,
                            "compile_test_rc": None,
                            "fired": bool(prior_event.get("fired", False)),
                            "outcome": (
                                "test_failed_bug_reproduced"
                                if prior_event.get("fired")
                                else "test_passed_no_bug"
                            ),
                            "cargo_log_path": str(_adapter_log_path),
                            "authoring_mode": f"adapter:{language}",
                            "framework": prior_event.get("framework"),
                            "reason": prior_event.get("reason"),
                            "duration_s": 0,
                            "resumed": True,
                        }
                        log("poc_adapter_resumed_from_existing",
                            hypothesis_id=hyp_id,
                            fired=bool(prior_event.get("fired")))
                        continue
                    # Runlog exists but no event in log → re-run defensively

                meta = hyp_meta.get(hyp_id, {})
                # Build the LLM authoring prompt + call the model
                from audit_pipeline.utils import complete as _complete_l2
                source_context = _grounded_source_for_hyp(
                    engine_dir, meta, ground_code=ground_code,
                )
                prompt = _l2_adapter.build_author_prompt(
                    hyp=meta, source_context=source_context,
                    target_repo_root=engine_dir,
                )
                try:
                    resp = _complete_l2(prompt)
                    raw_text = getattr(resp, "text", str(resp))
                    body = _l2_adapter.parse_test_body(raw_text)
                except Exception as e:  # noqa: BLE001
                    log("l2_author_failed", hypothesis_id=hyp_id, error=str(e))
                    poc_results[hyp_id] = {
                        "scaffold_path": None,
                        "scaffold_rc": -2,
                        "compile_test_rc": None,
                        "fired": False,
                        "outcome": "author_failed",
                        "authoring_mode": f"adapter:{language}",
                    }
                    continue
                # CALIBRATED: per-hyp PoC author cost ~$0.05 (same as Solana).
                daily_cap.record_spend(0.05)
                total_cost += 0.05
                # Write test file then run it via the adapter
                try:
                    test_path = _l2_adapter.write_test_file(
                        workspace, finding_name, body,
                    )
                    outcome_obj = _l2_adapter.run_test(
                        workspace, finding_name, engine_dir,
                    )
                except Exception as e:  # noqa: BLE001
                    log("l2_run_error", hypothesis_id=hyp_id, error=str(e))
                    poc_results[hyp_id] = {
                        "scaffold_path": None,
                        "scaffold_rc": 0,
                        "compile_test_rc": -3,
                        "fired": False,
                        "outcome": f"adapter_run_error:{type(e).__name__}",
                        "authoring_mode": f"adapter:{language}",
                    }
                    continue
                # Write the cargo-equivalent log so L2.5 triage can read it.
                # Each adapter exposes stdout/stderr — combine into one log
                # file in the same format as the Solana cargo log.
                log_path = poc_out / f"runlog_{finding_name}.log"
                log_path.write_text(
                    (outcome_obj.stdout or "") + "\n--- STDERR ---\n"
                    + (outcome_obj.stderr or ""),
                    encoding="utf-8",
                )
                poc_results[hyp_id] = {
                    "scaffold_path": str(test_path),
                    "scaffold_rc": 0,
                    "compile_test_rc": outcome_obj.returncode,
                    "fired": outcome_obj.fired,
                    "outcome": (
                        "test_failed_bug_reproduced"
                        if outcome_obj.fired
                        else (
                            "compile_error"
                            if outcome_obj.metadata.get("phase") == "compile"
                            else "test_passed_no_bug"
                        )
                    ),
                    "cargo_log_path": str(log_path),
                    "authoring_mode": f"adapter:{language}",
                    "framework": outcome_obj.framework,
                    "reason": outcome_obj.reason,
                    "duration_s": outcome_obj.duration_s,
                }
                log("poc_adapter_done", hypothesis_id=hyp_id,
                    language=language, fired=outcome_obj.fired,
                    reason=outcome_obj.reason[:160])
                continue
            # ----- Solana legacy cargo path (unchanged below) -----
            scaffold_path = poc_out / f"test_{finding_name}.rs"
            cargo_log_path = poc_out / f"cargo_{finding_name}.log"
            # RESUME: if both scaffold AND cargo log exist from a prior run,
            # reload the outcome and skip both LLM authoring AND cargo test.
            # FIX A-H2: only accept the cargo log if it contains a TERMINAL
            # marker — without one, the prior cargo run was killed mid-stream
            # and re-parsing it could silently demote a real bug to "no bug".
            cargo_log_complete = False
            combined = ""
            if (
                resume_cycle
                and scaffold_path.exists()
                and cargo_log_path.exists()
                and cargo_log_path.stat().st_size > 100
            ):
                combined = cargo_log_path.read_text(encoding="utf-8", errors="replace")
                cargo_log_complete = (
                    "test result:" in combined
                    or "could not compile" in combined
                    or "error: could not" in combined
                )
            if cargo_log_complete:
                log("poc_resumed_from_existing", hypothesis_id=hyp_id,
                    finding=finding_name)
                # Phase B 12-audit fix: replace substring classifier with
                # structured per-test parse. An `assertion failed` substring
                # in an unrelated test's panic message used to spoof "fired"
                # here; `test result: ok` from a sibling binary used to
                # spoof "passed". The structured parser attributes only to
                # the specific test_<finding_name> target.
                from audit_pipeline.utils.cargo_text import parse_test_outcome
                _outcome = parse_test_outcome(combined, f"test_{finding_name}")
                if _outcome.status == "compile_failed":
                    fired, outcome = False, "compile_error"
                elif _outcome.status == "fired":
                    fired, outcome = True, "test_failed_bug_reproduced"
                elif _outcome.status == "passed":
                    fired, outcome = False, "test_passed_no_bug"
                elif _outcome.status == "ignored":
                    fired, outcome = False, "test_ignored_no_witness"
                elif _outcome.status == "not_run":
                    fired, outcome = False, "test_not_in_binary"
                else:
                    fired, outcome = False, "unknown_resumed"
                poc_results[hyp_id] = {
                    "scaffold_path": str(scaffold_path),
                    "scaffold_rc": 0,
                    # FIX: was referencing undefined `looks_compile_failed` (legacy
                    # refactor leftover). Derive from structured outcome instead —
                    # compile_failed status maps to rc=101, every other status to 0.
                    "compile_test_rc": 101 if _outcome.status == "compile_failed" else 0,
                    "fired": fired,
                    "outcome": outcome,
                    "cargo_log_path": str(cargo_log_path),
                    "authoring_mode": "resumed",
                }
                # Don't bill flat-rate cost on resumed PoC — no LLM call made.
                continue
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
                # CALIBRATED 2026-05-11: real poc-llm cost measured at
                # ~$0.03 per hyp (4k input + 0.8k output @ Sonnet pricing).
                # Previous $0.30 estimate was a 10x overestimate. Set to
                # $0.05 for small cushion. Real outliers (large grounded
                # source) might hit $0.10 — still well under the buffer.
                daily_cap.record_spend(0.05)
                total_cost += 0.05
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
                    # FIX C3: Capture output and distinguish compile failure
                    # from test assertion failure. cargo's exit code is non-zero
                    # for BOTH cases. Without this distinction, every malformed
                    # LLM-authored PoC reads as a "confirmed bug" and triggers
                    # P2/P3 chain. Parse the output to find the actual outcome.
                    test_log = (cycle_dir / "poc" / f"cargo_{finding_name}.log")
                    cargo_proc = subprocess.run(
                        ["cargo", "test", "--features", "test", "--test",
                         f"test_{finding_name}"],
                        cwd=str(engine_dir),
                        capture_output=True,
                        text=True,
                        timeout=600,  # raised from 300 to handle cold builds
                    )
                    cargo_rc = cargo_proc.returncode
                    combined = (cargo_proc.stdout or "") + "\n" + (cargo_proc.stderr or "")
                    test_log.write_text(combined, encoding="utf-8")

                    # Phase B 12-audit fix: structured per-test parse.
                    # Substring classifier could attribute a sibling test's
                    # `panicked at` to the target test (false-fire) or
                    # accept an unrelated `test result: ok` as proof the
                    # target passed (false-confirm). The structured parse
                    # looks for ONE specific `test test_<finding_name> ...
                    # <result>` line and classifies on that alone.
                    from audit_pipeline.utils.cargo_text import parse_test_outcome
                    _outcome = parse_test_outcome(combined, f"test_{finding_name}")
                    if _outcome.status == "compile_failed":
                        outcome = "compile_error"
                        fired = False
                    elif _outcome.status == "fired":
                        outcome = "test_failed_bug_reproduced"
                        fired = True
                    elif _outcome.status == "passed":
                        outcome = "test_passed_no_bug"
                        fired = False
                    elif _outcome.status == "ignored":
                        # L1.5+L2 audit Defect 02: `#[ignore]`d tests
                        # used to silently fold into pass/fail counts.
                        outcome = "test_ignored_no_witness"
                        fired = False
                    elif _outcome.status == "not_run":
                        # Test name not in this binary — typically a
                        # PoC author who wrote a `fn` with the wrong name
                        # (slugify mismatch — L1.5+L2 audit Defect 03).
                        outcome = "test_not_in_binary"
                        fired = False
                    else:
                        outcome = f"unknown_rc_{cargo_rc}"
                        fired = False  # Don't escalate unknowns to P2/P3

                    poc_results[hyp_id]["compile_test_rc"] = cargo_rc
                    poc_results[hyp_id]["fired"] = fired
                    poc_results[hyp_id]["outcome"] = outcome
                    poc_results[hyp_id]["cargo_log_path"] = str(test_log)
                    log("poc_test_run", hypothesis_id=hyp_id, cargo_rc=cargo_rc,
                        outcome=outcome, fired=fired)
                except subprocess.TimeoutExpired:
                    poc_results[hyp_id]["outcome"] = "timeout"
                    log("poc_test_timeout", hypothesis_id=hyp_id)
                except Exception as e:  # noqa: BLE001
                    log("poc_test_error", hypothesis_id=hyp_id, error=str(e))

    # ---------- Layer 2.5: fire triage (STRONG / SOFT / FALSE / LOST) ----------
    # Productized from the manual STRONG/SOFT/FALSE bucket-sort that
    # collapsed cycle 20260511's 64 PoC fires down to 7 STRONG (4 root
    # causes). Without this stage, Layer 3 + Layer 4 would burn $300+
    # on Kani/LiteSVM against false-fire PoC infra panics.
    #
    # POST-AUDIT FIX: this block was previously BELOW Layer 4, so the
    # triage filter narrowed Kani but NOT LiteSVM — false fires still
    # incurred the LiteSVM author + dispatch spend. Now correctly runs
    # before both downstream layers so `layer3_dispatch_filter` gates
    # ALL costly downstream layers.
    #
    # Default: ON when --use-tools is on. Skipped when triage_fires is
    # off OR there are zero PoC fires.
    kani_results: dict[str, dict[str, Any]] = {}
    litesvm_results: dict[str, dict[str, Any]] = {}
    narrative_results: dict[str, str] = {}
    layer3_dispatch_filter: set[str] | None = None
    triage_summary: dict[str, Any] | None = None
    if triage_fires and any(pr.get("fired") for pr in poc_results.values()):
        from audit_pipeline.layer25_triage import triage_cycle as _triage
        console.print()
        console.print("[bold]Layer 2.5 - fire triage (STRONG/SOFT/FALSE/LOST)[/bold]")

        def _engine_loader(fn_name: str) -> str:
            if not fn_name:
                return ""
            try:
                from audit_pipeline.utils.code_extract import collect_grounded_code
                engine_src = engine_dir_for_cargo / "src"
                rs_files = sorted(engine_src.glob("*.rs")) if engine_src.is_dir() else []
                grounded = collect_grounded_code([fn_name], rs_files, max_lines=80)
                return next((v for v in grounded.values() if v), "")
            except Exception:  # noqa: BLE001
                return ""

        # Derive framework from the first poc_result that recorded one.
        # The adapter writes ``framework`` per fire (``aptos-cli`` for
        # Aptos, ``forge`` for Solidity, ``cargo`` for Solana, ``clang``
        # for C). The judge prompt header gets this so the LLM knows
        # what kind of fire signal it's reading (Move abort code vs
        # cargo panic vs ASan report). Without it, the Solana-tuned
        # prompt misread Aptos move-test output as "fire signal is
        # empty" and classified all 7 fires FALSE / SOFT — Operator
        # caught this on cycle 20260513-191318.
        _framework_for_triage: str | None = None
        for _pr in poc_results.values():
            _fw = _pr.get("framework")
            if _fw:
                _framework_for_triage = _fw
                break
        try:
            triage_out = _triage(
                cycle_dir,
                poc_results=poc_results,
                hyp_meta=hyp_meta,
                engine_src_loader=_engine_loader,
                judge_model=challenger_model,  # if operator passed --challenger-model, reuse it
                # PHASE 1h: pass language so the FALSE-pattern fast-path
                # matches the language's toolchain output (cargo for
                # Solana, ASan for C, forge for Solidity, aptos move
                # test abort codes for Aptos), and so the LLM judge's
                # fence tag matches the test body's syntax.
                language=language,
                framework=_framework_for_triage,
            )
            log("triage_done",
                strong=triage_out["counts"]["STRONG"],
                soft=triage_out["counts"]["SOFT"],
                false=triage_out["counts"]["FALSE"],
                lost=triage_out["counts"]["LOST"],
                n_clusters=len(triage_out["clusters"]),
                n_llm_calls=triage_out["n_llm_calls"],
                dispatch_set_size=len(triage_out["layer3_dispatch_set"]))
            console.print(
                f"  STRONG={triage_out['counts']['STRONG']} "
                f"SOFT={triage_out['counts']['SOFT']} "
                f"FALSE={triage_out['counts']['FALSE']} "
                f"LOST={triage_out['counts']['LOST']} | "
                f"{len(triage_out['clusters'])} root-cause cluster(s) | "
                f"Layer 3 dispatch set: {len(triage_out['layer3_dispatch_set'])} reps"
            )
            for r in triage_out["results"]:
                log("triage_one",
                    hypothesis_id=r["hyp_id"],
                    classification=r["classification"],
                    cluster_id=r.get("cluster_id"),
                    is_representative=r.get("is_representative", False),
                    used_llm=r.get("used_llm", False),
                    reason=r.get("reason", "")[:200])
            layer3_dispatch_filter = set(triage_out["layer3_dispatch_set"])
            # Capture for cycle_summary — minus the per-fire results list
            # (already in triage.jsonl) so summary stays compact.
            triage_summary = {
                "counts": triage_out["counts"],
                "n_clusters": len(triage_out["clusters"]),
                "n_llm_calls": triage_out["n_llm_calls"],
                "layer3_dispatch_set": triage_out["layer3_dispatch_set"],
                "clusters": triage_out["clusters"],
            }
        except Exception as e:  # noqa: BLE001 — never crash hunt over triage
            log("triage_failed", error=str(e)[:300])
            console.print(f"[yellow]triage failed ({e}); falling back to all-fires dispatch[/yellow]")
            layer3_dispatch_filter = None
            triage_summary = {"error": str(e)[:300]}

    # ---------- Layer 4: LiteSVM / runtime fuzz (PoC-fired + triage-filtered) ----------
    # PHASE 1h: language-aware. Solana keeps LiteSVM (existing path).
    # C → AFL++ fuzz; Solidity → forge-fuzz; Aptos → aptos-move-test
    # property runs. All routed through `runtime_adapters/`.
    fired_for_litesvm = [
        v for v in candidates
        if poc_results.get(v["hypothesis_id"], {}).get("fired")
        and (layer3_dispatch_filter is None
             or v["hypothesis_id"] in layer3_dispatch_filter)
    ]
    _l4_adapter = None
    if language != "solana" and not skip_litesvm:
        from audit_pipeline.runtime_adapters import (
            get_adapter as _get_runtime_adapter,
        )
        try:
            _l4_adapter = _get_runtime_adapter(language)
        except Exception as e:  # noqa: BLE001
            log("l4_adapter_unavailable", language=language, error=str(e))
            console.print(
                f"[yellow]No L4 runtime adapter for language={language!r}; "
                f"skipping Layer 4[/yellow]"
            )
    if not skip_litesvm and fired_for_litesvm and language != "solana":
        if _l4_adapter is not None:
            console.print()
            console.print(
                f"[bold]Layer 4 - {_l4_adapter.fuzzer} runtime fuzz on "
                f"{len(fired_for_litesvm)} PoC-fired findings ({language})[/bold]"
            )
            fuzz_out = cycle_dir / "fuzz"
            fuzz_out.mkdir(parents=True, exist_ok=True)
            for v in fired_for_litesvm:
                hyp_id = v["hypothesis_id"]
                meta = hyp_meta.get(hyp_id, {})
                harness_name = _slugify(hyp_id)
                from audit_pipeline.utils import complete as _complete_l4
                source_context = _grounded_source_for_hyp(
                    engine_dir_for_cargo, meta, ground_code=True,
                )
                prompt = _l4_adapter.build_harness_prompt(
                    hyp=meta, source_context=source_context,
                    target_repo_root=engine_dir_for_cargo,
                )
                try:
                    resp = _complete_l4(prompt)
                    body = _l4_adapter.parse_harness_body(getattr(resp, "text", str(resp)))
                    _l4_adapter.write_harness_file(workspace, harness_name, body)
                    runtime_outcome = _l4_adapter.run_fuzzer(
                        workspace, harness_name, engine_dir_for_cargo,
                    )
                except Exception as e:  # noqa: BLE001
                    log("l4_adapter_error", hypothesis_id=hyp_id, error=str(e))
                    litesvm_results[hyp_id] = {
                        "returncode": -1,
                        "scaffold_dir": str(fuzz_out),
                        "error": f"{type(e).__name__}: {e}",
                    }
                    continue
                daily_cap.record_spend(0.10)
                total_cost += 0.10
                litesvm_results[hyp_id] = {
                    "returncode": runtime_outcome.returncode,
                    "scaffold_dir": str(fuzz_out),
                    "crash_found": runtime_outcome.crash_found,
                    "ran_clean": runtime_outcome.ran_clean,
                    "fuzzer": runtime_outcome.fuzzer,
                    "reason": runtime_outcome.reason,
                    "duration_s": runtime_outcome.duration_s,
                    "n_witnesses": len(runtime_outcome.witness_inputs or []),
                }
                log("l4_adapter_done", hypothesis_id=hyp_id,
                    language=language,
                    crash_found=runtime_outcome.crash_found,
                    ran_clean=runtime_outcome.ran_clean)
        # Non-Solana L4 done — skip the Solana LiteSVM section below.
    elif not skip_litesvm and fired_for_litesvm:
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
        wrapper_dir = workspace / config.get("wrapper", {}).get("local", "target/wrapper")
        for v in fired_for_litesvm:
            hyp_id = v["hypothesis_id"]
            finding_name = _slugify(hyp_id)
            litesvm_file = litesvm_out / f"test_{finding_name}_bound_analysis.rs"
            if resume_cycle and litesvm_file.exists():
                log("litesvm_resumed_from_existing", hypothesis_id=hyp_id)
                rc_author = 0
            else:
                rc_author = _run([
                    _audit_pipeline_bin(), "--workspace", str(workspace),
                    "litesvm", "author",
                    "--finding", finding_name,
                    "--template", "litesvm_bound_analysis",
                    "--output", str(litesvm_out),
                ])
            dispatch_rc: int | None = None
            if rc_author == 0 and dispatch_script.exists():
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

    # ---------- Layer 3: formal verification (Kani / CBMC / SMTChecker / Move Prover) ----------
    #
    # PHASE 1h: dispatch L3 by language. Solana keeps the existing Kani
    # path (unchanged). Other languages route through `formal_adapters/`.
    fired_for_kani = [
        v for v in candidates
        if poc_results.get(v["hypothesis_id"], {}).get("fired")
        and (layer3_dispatch_filter is None
             or v["hypothesis_id"] in layer3_dispatch_filter)
    ]
    _l3_adapter = None
    if language != "solana" and not skip_kani:
        from audit_pipeline.formal_adapters import get_adapter as _get_formal_adapter
        try:
            _l3_adapter = _get_formal_adapter(language)
        except Exception as e:  # noqa: BLE001
            log("l3_adapter_unavailable", language=language, error=str(e))
            console.print(
                f"[yellow]No L3 formal adapter for language={language!r}; "
                f"skipping Layer 3[/yellow]"
            )
    if not skip_kani and fired_for_kani and language != "solana":
        if _l3_adapter is not None:
            console.print()
            console.print(
                f"[bold]Layer 3 - {_l3_adapter.verifier} formal verification on "
                f"{len(fired_for_kani)} PoC-fired findings ({language})[/bold]"
            )
            formal_out = cycle_dir / "formal"
            formal_out.mkdir(parents=True, exist_ok=True)
            for v in fired_for_kani:
                if daily_cap.remaining_today() < 0.50:
                    log("l3_halted_daily_cap", spent=daily_cap.today_spend())
                    break
                hyp_id = v["hypothesis_id"]
                meta = hyp_meta.get(hyp_id, {})
                harness_name = f"{_slugify(hyp_id)}_invariant"
                # Build LLM prompt + author harness
                from audit_pipeline.utils import complete as _complete_l3
                source_context = _grounded_source_for_hyp(
                    engine_dir_for_cargo, meta, ground_code=True,
                )
                prompt = _l3_adapter.build_harness_prompt(
                    hyp=meta, source_context=source_context,
                    target_repo_root=engine_dir_for_cargo,
                )
                try:
                    resp = _complete_l3(prompt)
                    body = _l3_adapter.parse_harness_body(getattr(resp, "text", str(resp)))
                    _l3_adapter.write_harness_file(workspace, harness_name, body)
                    formal_outcome = _l3_adapter.run_verifier(
                        workspace, harness_name, engine_dir_for_cargo,
                    )
                except Exception as e:  # noqa: BLE001
                    log("l3_adapter_error", hypothesis_id=hyp_id, error=str(e))
                    kani_results[hyp_id] = {
                        "returncode": -1,
                        "harness_dir": str(formal_out),
                        "error": f"{type(e).__name__}: {e}",
                    }
                    continue
                # Charge ~$0.50 like synth-kani
                daily_cap.record_spend(0.50)
                total_cost += 0.50
                kani_results[hyp_id] = {
                    "returncode": formal_outcome.returncode,
                    "harness_dir": str(formal_out),
                    "proved": formal_outcome.proved,
                    "counterexample": formal_outcome.counterexample,
                    "reason": formal_outcome.reason,
                    "duration_s": formal_outcome.duration_s,
                    "verifier": formal_outcome.verifier,
                }
                log("l3_adapter_done", hypothesis_id=hyp_id,
                    language=language, proved=formal_outcome.proved,
                    counterexample=formal_outcome.counterexample)
        # Non-Solana L3 done — skip the Solana Kani section below.
    elif not skip_kani and fired_for_kani and daily_cap.remaining_today() > 0.50:
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
            harness_name = f"{_slugify(hyp_id)}_invariant"
            kani_file = kani_out / f"proofs_{harness_name}.rs"
            # RESUME: if the Kani harness file already exists from a prior
            # run, skip the synth-kani LLM call + cargo kani re-run. cargo
            # kani is expensive (5-30 min per harness) so this is the most
            # important skip.
            if resume_cycle and kani_file.exists():
                log("kani_resumed_from_existing", hypothesis_id=hyp_id,
                    harness_name=harness_name)
                kani_results[hyp_id] = {
                    "returncode": 0,
                    "harness_dir": str(kani_out),
                    "resumed": True,
                }
                continue
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

        finding_id = db.upsert_finding(
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
        # POST-AUDIT FIX: emit a finding_persisted event so the Bridge
        # dashboard's findings waterfall populates in real time, not
        # only on the next 60s snapshot poll.
        log("finding_persisted",
            id=finding_id,
            hypothesis_id=hyp_id,
            severity=sev.value if hasattr(sev, "value") else str(sev),
            status=status.value if hasattr(status, "value") else str(status),
            title=title,
            cycle_id=cycle_id,
            poc_fired=poc_fired)

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
        "triage": triage_summary,            # Layer 2.5 STRONG/SOFT/FALSE counts + clusters
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
    # FIX C8: Define a per-cycle budget guard for pillar work. The legacy code
    # let P2 ($1.50/finding) + P3 ($5/finding) compound past the user-set
    # --budget-cap-usd, which would have blown a $250 cap to $400+ with 10
    # Med+ findings. We now check the running total before each pillar call
    # and gracefully skip when the cap is approached.
    def _cycle_budget_remaining() -> float:
        return max(0.0, budget_cap_usd - total_cost)

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
                # FIX C8: budget gate
                if _cycle_budget_remaining() < 1.50:
                    log("propagate_halted_cycle_budget",
                        total_cost=total_cost, budget=budget_cap_usd)
                    pillar_results["propagate"]["_halted"] = "cycle budget exhausted"
                    break
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
                ], timeout=1800)  # raised from 600s — corpus traversal can be slow
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
            # FIX C8: budget gate (bundle is more expensive than propagate)
            if _cycle_budget_remaining() < 5.00:
                log("bundle_halted_cycle_budget",
                    total_cost=total_cost, budget=budget_cap_usd)
                pillar_results["bundle"]["_halted"] = "cycle budget exhausted"
                break
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
    # Bridge dashboard listens for `cycle_complete` and `hunt_complete`
    # in addition to `hunt_done`. Emit all three so any handler shape
    # the frontend uses sees the cycle finishing.
    log("cycle_complete", cycle_id=cycle_id,
        elapsed=elapsed, n_confirmed=len(confirmed),
        cost_usd=round(total_cost, 4))
    log("hunt_complete", cycle_id=cycle_id,
        elapsed=elapsed, n_confirmed=len(confirmed))

    if webhook_url and confirmed:
        try:
            _post_webhook(webhook_url, cycle_summary, hyp_meta, promoted_ids)
            log("webhook_sent", url=webhook_url[:50])
        except Exception as e:  # noqa: BLE001
            log("webhook_failed", error=str(e))

    # ---------- Auto-publish cycle artifacts + per-finding emails ----------
    # Two steps:
    #   1. `audit-pipeline report cycle` generates hunt_report.html in the
    #      cycle dir. publish_cycle.sh's chromium-render step needs that file
    #      as input; the hunt only writes hunt_report.md by default.
    #   2. deploy/publish_cycle.sh then:
    #      - Copies cycle to public examples/recent-hunts/ + git push
    #      - Copies cycle to /var/www/jelleo.com/cycles/<id>/ for customer download
    #      - Renders + signs PDF from hunt_report.html
    #      - Emails Critical/High confirmed findings via notifier.json
    #      - Telegram alert if HUNT_TELEGRAM_* env vars set
    # Best-effort: failure does not fail the hunt. Skipped silently if the
    # script is missing on this host (dev machines without /root/audit-pipeline-cli).
    if auto_publish:
        # 12-audit post-cycle QA gate (HIGH-priority remediation): re-run
        # cheap pre-disclosure checks over every confirmed finding BEFORE
        # auto-pushing the cycle to GitHub + /var/www/jelleo.com/cycles/.
        # Yesterday's 20260511-183154 retraction would have been caught
        # here if this gate existed — symbol_grep on the PoCs catches the
        # F11/F13 hallucinations and the residual-conservation cluster's
        # PoCs that contain pseudo-pass markers.
        from audit_pipeline.gates.post_cycle import (
            check_post_cycle,
            write_block_sentinel,
        )
        try:
            cycle_dir = workspace / "hunts" / cycle_id
            engine_src = (workspace / config["engine"]["local"] / "src"
                          if config.get("engine") else None)
            wrapper_src = (workspace / config["wrapper"]["local"] / "src"
                           if config.get("wrapper") else None)
            # PHASE 1h B1: same DB-relocation logic as the main DB open
            # above — when customer_id is set, the post-cycle QA reads
            # from the SHARED customer-eval DB, not a per-workspace one.
            _db = open_findings_db(db_workspace)
            confirmed = [
                f for f in _db.list_findings(limit=1000)
                if f.get("cycle_id") == cycle_id and f.get("poc_fired")
            ]
            post_report = check_post_cycle(
                cycle_dir=cycle_dir,
                confirmed_findings=confirmed,
                engine_src_dir=engine_src,
                wrapper_src_dir=wrapper_src,
            )
            log("post_cycle_qa", passed=post_report.passed,
                n_findings=post_report.n_findings,
                n_failed=post_report.n_failed)
            if not post_report.passed:
                sentinel = write_block_sentinel(cycle_dir, post_report)
                console.print(
                    f"[red]Post-cycle QA FAILED[/red] — "
                    f"{post_report.n_failed} of {post_report.n_findings} "
                    f"confirmed findings did not clear re-checks. Wrote "
                    f"sentinel to {sentinel}. Auto-publish ABORTED."
                )
                log("auto_publish_blocked",
                    reason="post_cycle_qa_failed",
                    sentinel=str(sentinel))
                return        # short-circuit: do not run publish_cycle.sh
        except Exception as e:  # noqa: BLE001
            # Don't let a bug in the gate itself block legitimate cycles.
            # Log + fail-open here (single layer; the gate itself is the
            # belt; publish_cycle.sh sentinel check is the suspenders).
            log("post_cycle_qa_error", error=str(e))

        rc_html = _run([
            _audit_pipeline_bin(), "--workspace", str(workspace),
            "report", "cycle", "--cycle-id", cycle_id, "--public",
        ], timeout=120)
        log("auto_publish_html_rendered", rc=rc_html, cycle_id=cycle_id)
        script_path = _resolve_publish_script()
        if script_path is not None:
            console.print()
            console.print(f"[bold]Auto-publish:[/bold] [cyan]{script_path}[/cyan]")
            rc_publish = _run(["bash", str(script_path)], timeout=600)
            log("auto_publish", rc=rc_publish, script=str(script_path))
        else:
            log("auto_publish_skipped", reason="publish_cycle.sh not found")


def _resolve_publish_script() -> Path | None:
    """Locate publish_cycle.sh. Returns None if not found.

    Search order:
      1. $JELLEO_PUBLISH_SCRIPT (override)
      2. /root/audit-pipeline-cli/deploy/publish_cycle.sh (VPS default)
      3. <repo>/deploy/publish_cycle.sh (editable install / dev)
    """
    override = os.environ.get("JELLEO_PUBLISH_SCRIPT")
    if override:
        p = Path(override)
        return p if p.is_file() else None
    vps_default = Path("/root/audit-pipeline-cli/deploy/publish_cycle.sh")
    if vps_default.is_file():
        return vps_default
    # Walk up from this file: src/audit_pipeline/commands/hunt.py -> repo root
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "deploy" / "publish_cycle.sh"
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grounded_source_for_hyp(
    engine_dir: Path,
    meta: dict[str, Any],
    *,
    ground_code: bool = True,
    max_bytes: int = 60_000,
) -> str:
    """Resolve a hyp's target_file (single path or glob) to inline source.

    Why this exists: aptos-small dry-run failed L2 PoC on EVERY hyp
    because `target_file: sources/*.move` is a glob, not a file. The
    earlier `tf.is_file()` check returned False → source_context stayed
    empty → LLM wrote tests calling `mutatis::engine::withdraw` (a
    hallucinated module) instead of the actual `mutatis::token_vault::
    withdraw`. Every test then failed to compile.

    Now we:
      1. Use glob() if the path contains * or ?
      2. Concatenate matching files (with header lines so the LLM
         knows which module is which) up to max_bytes
      3. Fall back to reading single file path if no glob match
    """
    if not ground_code:
        return ""
    target_file = meta.get("target_file")
    if not target_file:
        return ""

    paths: list[Path] = []
    if "*" in target_file or "?" in target_file:
        try:
            paths = sorted(engine_dir.glob(target_file))
        except OSError:
            paths = []
    else:
        candidate = engine_dir / target_file
        if candidate.is_file():
            paths = [candidate]

    if not paths:
        return ""

    chunks: list[str] = []
    used = 0
    for p in paths:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.name
        try:
            rel = str(p.relative_to(engine_dir))
        except (ValueError, AttributeError):
            pass
        header = f"// ===== {rel} =====\n"
        budget = max_bytes - used - len(header)
        if budget <= 0:
            break
        chunks.append(header + body[:budget])
        used += len(chunks[-1])
    return "\n".join(chunks)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_from(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


RC_TIMEOUT = 124  # POSIX timeout convention
RC_NOT_FOUND = 127  # POSIX command-not-found convention


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800) -> int:
    """Run a subprocess, stream output, return exit code.

    FIX #1: Distinguishes timeout (rc=124) and not-found (rc=127) from
    a real non-zero exit via the standard POSIX sentinel codes. Callers
    can check `rc == RC_TIMEOUT` / `rc == RC_NOT_FOUND` instead of
    conflating them with real failures.
    """
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
    # Phase B 12-audit L1.5+L2 Defect 03: was independently truncating
    # to [:60] while poc_llm.py's didn't, causing long hyp_ids to route
    # to F7-fallback scaffold. Both now share `utils.slug.slug_for_hypothesis`.
    from audit_pipeline.utils.slug import slug_for_hypothesis
    return slug_for_hypothesis(text)


# Orchestration audit Defect 08 (MED): SSRF guard for webhook_url.
# An attacker-controlled webhook URL would otherwise leak engine_sha,
# wrapper_sha, finding counts, severities to whatever endpoint they
# specified — including AWS / GCP instance-metadata endpoints.
_WEBHOOK_ALLOW_HOSTS_RE = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)*(?:slack\.com|discord(?:app)?\.com|hooks\.slack\.com|"
    r"webhook\.site|api\.telegram\.org|teams\.microsoft\.com|"
    r"events\.pagerduty\.com)$",
    re.IGNORECASE,
)
_WEBHOOK_BLOCKED_HOSTS_RE = re.compile(
    r"^(?:127\.|169\.254\.|10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"localhost|metadata\.google\.internal|metadata\.aws|0\.0\.0\.0|::1|"
    r"fe80:|fc00:|fd00:)",
    re.IGNORECASE,
)


def _webhook_url_safe(url: str) -> tuple[bool, str]:
    """Return (allowed, reason) for ``url``."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return (False, "could not parse URL")
    if parsed.scheme != "https":
        return (False, f"refusing non-https scheme {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        return (False, "URL has no host")
    if _WEBHOOK_BLOCKED_HOSTS_RE.match(host):
        return (False, f"refusing internal/metadata host {host!r}")
    if not _WEBHOOK_ALLOW_HOSTS_RE.match(host):
        return (False, f"host {host!r} not in webhook allow-list "
                       f"(slack/discord/teams/telegram/pagerduty/webhook.site)")
    return (True, "ok")


def _post_webhook(
    url: str,
    summary: dict[str, Any],
    hyp_meta: dict[str, dict],
    promoted_ids: set[str],
) -> None:
    allowed, reason = _webhook_url_safe(url)
    if not allowed:
        # FIX: `log()` is a closure local to _hunt_run, not visible here.
        # Use a plain console.print + stderr fallback so the SSRF block
        # surfaces without crashing the hunt with NameError.
        try:
            console.print(
                f"[yellow]webhook_blocked: {reason} (url={url[:80]})[/yellow]"
            )
        except Exception:
            import sys
            print(f"webhook_blocked: {reason}", file=sys.stderr)
        return
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
