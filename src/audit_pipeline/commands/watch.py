"""`audit-pipeline watch` — continuous source-code monitor with auto-pull.

Long-running daemon. Polls the GitHub API for the workspace's pinned
engine + wrapper repos every --interval seconds. When a new commit on the
watched ref lands:

  1. Logs the commit (sha, message, author, timestamp) to a watch log
  2. Optionally git-pulls the local clone (--auto-pull)
  3. Optionally rewrites workspace.json with the new SHA (--update-pin)
  4. Optionally fires a downstream command (--on-update "<command>")

Designed to run on the VPS under tmux as the source-code analog to
`audit-pipeline shadow start` (which watches mainnet runtime, not source).

Together they answer the user's question: "is there a way to always read
it live and not worry about whether we're auditing the new info or the old
one." Run both, and your audit is always against current state.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from audit_pipeline.utils.github import get_latest_commit, parse_github_repo
from audit_pipeline.utils.github_snapshot import GitHubSnapshot, SnapshotDownloadError

console = Console()


@click.command(name="watch")
@click.option(
    "--ref-engine",
    default="HEAD",
    show_default=True,
    help="Branch / ref to track for the engine repo",
)
@click.option(
    "--ref-wrapper",
    default="HEAD",
    show_default=True,
    help="Branch / ref to track for the wrapper repo",
)
@click.option(
    "--interval",
    type=int,
    default=300,
    show_default=True,
    help="Seconds between polls (default 5 min — GitHub rate limit-friendly)",
)
@click.option(
    "--auto-pull",
    is_flag=True,
    help=(
        "When a new commit is detected, git-fetch + git-checkout the new "
        "sha in the local clone. Without this flag, watch only logs."
    ),
)
@click.option(
    "--update-pin",
    is_flag=True,
    help=(
        "After pulling, rewrite workspace.json with the new SHA so "
        "subsequent CLI commands operate on the fresh code. "
        "Implies --auto-pull."
    ),
)
@click.option(
    "--on-update",
    default=None,
    help=(
        "Shell command to run after a successful update. The command is "
        "interpolated with {sha} and {component} (e.g. 'engine')."
    ),
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single poll cycle and exit (smoke test / cron mode)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir for watch log (defaults to <workspace>/watch/)",
)
@click.option(
    "--source-mode",
    is_flag=True,
    help=(
        "Snapshot mode: when a new commit is detected, do NOT git-pull a "
        "local clone. Instead, append --source-repo <repo> --source-sha "
        "<new_sha> to the --on-update command so the downstream hunt cycle "
        "reads source from an ephemeral GitHub snapshot. This eliminates "
        "the local-clone read path entirely (no git fetch / no checkout). "
        "Implies --auto-pull is OFF (mutually exclusive)."
    ),
)
@click.option(
    "--diff-scope/--no-diff-scope",
    default=False,
    show_default=True,
    help=(
        "OPT-IN (OFF by default). Scope the downstream --on-update hunt to only "
        "the files changed since the last SUCCESSFULLY-audited commit "
        "(auto-appends --diff-since-sha <prev_sha> unless the command already "
        "sets it). Off by default because narrowing what gets scanned is a "
        "security-relevant choice, not a silent behavior flip: enable it only "
        "once --on-update runs a REAL hunt (not a no-op shim) and you accept a "
        "first full-library baseline pass. The FIRST scoped run per component "
        "full-scans to establish that baseline; later commits are diff-scoped. "
        "In local (non-source) mode only engine commits are scoped (hunt diffs "
        "the engine clone); wrapper commits full-scan. Use THIS flag to scope — "
        "a hand-written --diff-since-sha inside --on-update is respected but "
        "bypasses the first-run-full-library and repo-correctness guards (logged "
        "when detected)."
    ),
)
@click.pass_context
def watch_cmd(
    ctx: click.Context,
    ref_engine: str,
    ref_wrapper: str,
    interval: int,
    auto_pull: bool,
    update_pin: bool,
    on_update: str | None,
    once: bool,
    output: Path | None,
    source_mode: bool,
    diff_scope: bool,
) -> None:
    """Continuously watch the workspace's repos for new commits.

    The source-code analog to `shadow start`. Run this on a VPS under
    tmux to keep your audit workspace continuously synced with upstream.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )
    # Source-mode is mutually exclusive with auto-pull / update-pin: in
    # snapshot mode the local clone is irrelevant and we must not maintain
    # state in workspace.json or .git in the watch loop.
    if source_mode and (auto_pull or update_pin):
        raise click.ClickException(
            "--source-mode is mutually exclusive with --auto-pull and "
            "--update-pin: snapshot mode reads source ephemerally from "
            "GitHub, so there is no local clone to pull and no SHA pin "
            "to rewrite. Drop --auto-pull / --update-pin and re-run."
        )

    if update_pin and not auto_pull:
        # Force --auto-pull on; rewriting the pin without pulling is incoherent
        auto_pull = True

    if output is None:
        output = workspace / "watch"
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "watch.log"
    state_path = output / "state.json"

    # Load persistent state (last-seen sha per component)
    state: dict[str, dict] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text())

    # Build poll plan from workspace config
    config = json.loads(config_path.read_text())
    plan: list[tuple[str, str, str, str]] = []  # (component, owner, repo, ref)
    for component, ref in (("engine", ref_engine), ("wrapper", ref_wrapper)):
        try:
            owner, repo = parse_github_repo(config[component]["repo"])
            plan.append((component, owner, repo, ref))
        except ValueError as e:
            _log(log_path, f"WARN: cannot parse {component} repo url: {e}")

    if not plan:
        raise click.ClickException("Nothing to watch — no parseable github repos in workspace.json")

    # In source-mode, smoke-test snapshot reachability for the engine
    # repo at startup so the daemon fails fast on misconfiguration rather
    # than silently swallowing every on-update fire later.
    if source_mode:
        try:
            engine_owner, engine_repo = parse_github_repo(config["engine"]["repo"])
            with GitHubSnapshot(f"{engine_owner}/{engine_repo}") as _smoke:
                _log(
                    log_path,
                    f"source-mode smoke test OK: "
                    f"{engine_owner}/{engine_repo}@{_smoke.resolved_sha[:10]}",
                )
        except (SnapshotDownloadError, ValueError) as e:
            raise click.ClickException(
                f"--source-mode startup snapshot probe failed: {e}. "
                f"Check GITHUB_TOKEN env var and repo URL in workspace.json."
            )

    console.print(
        Panel.fit(
            f"[bold]Watch starting[/bold]\n\n"
            f"Workspace:    {workspace}\n"
            f"Polling:      {len(plan)} repo(s) every {interval}s "
            f"({'one-shot' if once else 'continuous'})\n"
            f"Source mode:  {'snapshot (no local clone)' if source_mode else 'local clone'}\n"
            f"Auto-pull:    {auto_pull}\n"
            f"Update-pin:   {update_pin}\n"
            f"On-update:    {on_update or '(none)'}\n"
            f"Log:          {log_path}\n",
            title="Layer 0.5 - Source code watch",
        )
    )

    # WS6: make the per-commit scoping behavior visible on boot — default-on
    # means an existing `watch --on-update` deploy now re-audits only the diff
    # since the last successful audit instead of the full library each commit.
    if on_update:
        _scope_msg = (
            "diff-scope ON — on-update hunts re-audit only files changed since "
            "the last successful audit (--no-diff-scope for full-library)"
            if diff_scope
            else "diff-scope OFF — on-update hunts re-audit the full library each commit"
        )
        console.print(f"[cyan]{_scope_msg}[/cyan]")
        _log(log_path, _scope_msg)

    poll_count = 0
    while True:
        poll_count += 1
        for component, owner, repo, ref in plan:
            try:
                latest = get_latest_commit(owner, repo, ref=ref)
            except Exception as e:  # noqa: BLE001
                _log(log_path, f"poll #{poll_count} {component} ERROR: {e}")
                console.print(f"[red]poll #{poll_count} {component}: {e}[/red]")
                continue

            latest_sha = latest["sha"]
            last_seen = state.get(component, {}).get("last_seen_sha")
            # First-time bootstrap: align state with workspace pin
            if last_seen is None:
                last_seen = config[component]["sha"]

            if latest_sha == last_seen or latest_sha.startswith(last_seen) or last_seen.startswith(latest_sha):
                _log(log_path, f"poll #{poll_count} {component} unchanged ({latest_sha[:10]})")
                continue

            # New commit detected
            msg = latest["commit"]["message"].split("\n")[0]
            console.print(
                f"[bold magenta]NEW COMMIT[/bold magenta] [cyan]{component}[/cyan] "
                f"{last_seen[:10]} -> {latest_sha[:10]}: {msg[:60]}"
            )
            _log(
                log_path,
                f"poll #{poll_count} {component} NEW {last_seen[:10]} -> {latest_sha[:10]}: {msg}",
            )

            if auto_pull:
                local_dir = workspace / config[component]["local"]
                if (local_dir / ".git").exists():
                    # Audit-001 follow-up (R5b-2026-05-24): GIT_SAFE flags
                    # neutralize malicious upstream `.git/hooks/post-checkout`
                    # or `post-merge` scripts. Same class as freshness.py's
                    # round-5 fix — watch.py was a missed Python sibling.
                    # jelleo-watch.service runs continuously as root and polls
                    # every 300s, so an unhardened auto-pull is the highest-
                    # frequency upstream-touching code path on the VPS.
                    # R5b-2 (2026-05-24): goober found protocol.file.allow blocks
                    # `file://` but not `ext::` which executes shell commands.
                    # Add protocol.ext.allow=never to close the ext-protocol
                    # remote-code-execution surface in submodule URLs etc.
                    GIT_SAFE = ["-c", "core.hooksPath=/dev/null",
                                "-c", "protocol.file.allow=never",
                                "-c", "protocol.ext.allow=never"]
                    try:
                        subprocess.run(
                            ["git", *GIT_SAFE, "fetch", "origin"],
                            cwd=str(local_dir), check=True,
                            capture_output=True, text=True, timeout=120,
                        )
                        subprocess.run(
                            ["git", *GIT_SAFE, "checkout", latest_sha],
                            cwd=str(local_dir), check=True,
                            capture_output=True, text=True, timeout=60,
                        )
                        console.print(
                            f"  [green]pulled {component} -> {latest_sha[:10]}[/green]"
                        )
                        _log(log_path, f"  pulled {component} OK")
                    except subprocess.CalledProcessError as e:
                        console.print(
                            f"  [red]pull failed: {e.stderr or e.stdout}[/red]"
                        )
                        _log(log_path, f"  pull FAILED: {e.stderr or e.stdout}")
                        continue
                else:
                    console.print(
                        f"  [yellow]no .git in {local_dir}, skipping pull[/yellow]"
                    )

            if update_pin:
                config[component]["sha"] = latest_sha
                config["last_watch_update"] = datetime.now(timezone.utc).isoformat()
                config_path.write_text(json.dumps(config, indent=2))
                console.print(
                    f"  [green]workspace.json {component}.sha -> {latest_sha[:10]}[/green]"
                )

            # WS6 (opt-in via --diff-scope): the diff baseline is the last
            # SUCCESSFULLY-diff-scoped SHA. `None` => no baseline yet => the
            # FIRST scoped run for this component full-scans (never scope to the
            # first observed commit — that would skip everything the pin already
            # contained). Only where hunt can diff the RIGHT repo is a component
            # scopeable: source-mode compares the component's own repo (engine OR
            # wrapper); local mode only diffs the ENGINE clone, so a wrapper
            # commit in local mode is left full-scan (safe), never diffed wrong.
            last_scoped = state.get(component, {}).get("last_scoped_sha")
            can_scope = source_mode or component == "engine"
            audit_ok = False
            operator_scoped = False
            if on_update:
                try:
                    cmd = on_update.format(sha=latest_sha, component=component)
                except Exception as e:  # noqa: BLE001 — a bad template must not crash the daemon
                    console.print(f"  [red]bad --on-update template: {e}[/red]")
                    _log(log_path, f"  on-update TEMPLATE ERROR: {e}")
                    cmd = None
                if cmd is not None:
                    argv = shlex.split(cmd)
                    # In source-mode, append the snapshot flags so the
                    # downstream hunt cycle reads source from a fresh GitHub
                    # snapshot pinned to this exact commit. No local clone
                    # touched.
                    if source_mode:
                        argv += [
                            "--source-repo", f"{owner}/{repo}",
                            "--source-sha", latest_sha,
                        ]
                    operator_scoped = any(
                        a == "--diff-since-sha" or a.startswith("--diff-since-sha=")
                        for a in argv
                    )
                    if operator_scoped:
                        # The operator hand-supplied --diff-since-sha. Respect it,
                        # but our auto diff-scope guards (first-run-full-library,
                        # repo-correctness, the managed baseline) do NOT apply to an
                        # operator-chosen value — log it honestly and don't touch
                        # our baseline this cycle (see the persist gate below).
                        _log(
                            log_path,
                            f"  operator-supplied --diff-since-sha in effect for "
                            f"{component}"
                            + (" — auto diff-scope guards bypassed" if diff_scope else ""),
                        )
                    elif diff_scope and can_scope:
                        if last_scoped:
                            argv += ["--diff-since-sha", last_scoped]
                            _log(log_path, f"  diff-scope baseline {last_scoped[:10]}")
                        else:
                            _log(
                                log_path,
                                f"  diff-scope: first run for {component} — full "
                                f"library baseline (scope from next commit)",
                            )
                    elif diff_scope and not can_scope:
                        _log(
                            log_path,
                            f"  diff-scope skipped for {component} (local mode "
                            f"only diffs the engine repo) — full library",
                        )
                    console.print(
                        f"  [bold]running on-update:[/bold] "
                        f"{shlex.join(argv) if hasattr(shlex, 'join') else ' '.join(argv)}"
                    )
                    try:
                        cp = subprocess.run(
                            argv,
                            cwd=str(workspace), check=False, timeout=3600,
                        )
                        audit_ok = cp.returncode == 0
                        if not audit_ok:
                            console.print(
                                f"  [red]on-update exit {cp.returncode} — scoping "
                                f"baseline NOT advanced (re-scoped next cycle)[/red]"
                            )
                            _log(log_path, f"  on-update exit {cp.returncode}")
                    except Exception as e:  # noqa: BLE001
                        console.print(f"  [red]on-update failed: {e}[/red]")
                        _log(log_path, f"  on-update FAILED: {e}")

            # Persist state. `last_seen_sha` always advances (we observed the
            # commit, don't re-fire it). `last_scoped_sha` advances ONLY when WE
            # managed the scoping for this component — diff-scope enabled AND
            # scopeable AND the cycle succeeded AND the operator did not hand-supply
            # their own --diff-since-sha. So scoping-off runs (incl. a no-op
            # on_update shim), never-scoped components (wrapper in local mode),
            # operator-overridden cycles, and failed/crashed cycles never establish
            # or advance a baseline a later auto-scoped run would trust.
            state.setdefault(component, {})["last_seen_sha"] = latest_sha
            state[component]["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            if diff_scope and can_scope and audit_ok and not operator_scoped:
                state[component]["last_scoped_sha"] = latest_sha
            state_path.write_text(json.dumps(state, indent=2))

        if once:
            console.print("\n[green]One-shot poll complete.[/green]")
            return
        time.sleep(interval)


def _log(log_path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")
