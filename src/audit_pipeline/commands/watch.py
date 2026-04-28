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

    console.print(
        Panel.fit(
            f"[bold]Watch starting[/bold]\n\n"
            f"Workspace:  {workspace}\n"
            f"Polling:    {len(plan)} repo(s) every {interval}s "
            f"({'one-shot' if once else 'continuous'})\n"
            f"Auto-pull:  {auto_pull}\n"
            f"Update-pin: {update_pin}\n"
            f"On-update:  {on_update or '(none)'}\n"
            f"Log:        {log_path}\n",
            title="Layer 0.5 - Source code watch",
        )
    )

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
                    try:
                        subprocess.run(
                            ["git", "fetch", "origin"],
                            cwd=str(local_dir), check=True,
                            capture_output=True, text=True, timeout=120,
                        )
                        subprocess.run(
                            ["git", "checkout", latest_sha],
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

            if on_update:
                cmd = on_update.format(sha=latest_sha, component=component)
                console.print(f"  [bold]running on-update:[/bold] {cmd}")
                try:
                    subprocess.run(
                        shlex.split(cmd),
                        cwd=str(workspace), check=False, timeout=3600,
                    )
                except Exception as e:  # noqa: BLE001
                    console.print(f"  [red]on-update failed: {e}[/red]")
                    _log(log_path, f"  on-update FAILED: {e}")

            # Persist state
            state.setdefault(component, {})["last_seen_sha"] = latest_sha
            state[component]["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            state_path.write_text(json.dumps(state, indent=2))

        if once:
            console.print("\n[green]One-shot poll complete.[/green]")
            return
        time.sleep(interval)


def _log(log_path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")
