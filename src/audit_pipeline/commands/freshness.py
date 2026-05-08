"""`audit-pipeline freshness` — one-shot "how stale is my workspace?" check.

Compares the engine + wrapper SHAs pinned in workspace.json to the current
upstream HEAD via the GitHub API. Reports the gap (commit count, list of
new commits, time-since-last-commit). Optional --update flag pulls the
local clones to current and rewrites workspace.json.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.utils.github import (
    get_latest_commit,
    list_commits_since,
    parse_github_repo,
)

console = Console()


@click.command(name="freshness")
@click.option(
    "--update",
    is_flag=True,
    help=(
        "Pull each clone to its upstream HEAD AND rewrite workspace.json "
        "with the new SHAs. Without this flag, freshness is read-only."
    ),
)
@click.option(
    "--ref-engine",
    default="HEAD",
    show_default=True,
    help="Branch / ref to compare engine against",
)
@click.option(
    "--ref-wrapper",
    default="HEAD",
    show_default=True,
    help="Branch / ref to compare wrapper against",
)
@click.pass_context
def freshness_cmd(
    ctx: click.Context,
    update: bool,
    ref_engine: str,
    ref_wrapper: str,
) -> None:
    """Show how stale your workspace's pinned engine + wrapper SHAs are.

    Reads workspace.json, hits the GitHub compare API for each repo, prints
    a table showing the gap. With --update, also pulls the local clones
    and rewrites workspace.json so subsequent commands run against fresh
    code.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )
    config = json.loads(config_path.read_text())

    table = Table(title="Workspace freshness check")
    table.add_column("Component", style="bold")
    table.add_column("Pinned SHA", style="dim")
    table.add_column("Latest SHA", style="dim")
    table.add_column("Behind", justify="right", style="bold")
    table.add_column("Latest commit", style="cyan")

    rows: list[tuple[str, dict, str, str]] = []

    for component, ref in (("engine", ref_engine), ("wrapper", ref_wrapper)):
        cfg = config[component]
        pinned = cfg["sha"]
        repo_url = cfg["repo"]
        try:
            owner, repo = parse_github_repo(repo_url)
        except ValueError as e:
            table.add_row(component, pinned[:10], "?", "?", f"[red]{e}[/red]")
            continue

        try:
            latest = get_latest_commit(owner, repo, ref=ref)
            latest_sha = latest["sha"]
            latest_msg = latest["commit"]["message"].split("\n")[0]
            latest_date = latest["commit"]["author"]["date"]

            if pinned.startswith(latest_sha[: len(pinned)]) or latest_sha.startswith(pinned):
                behind = 0
                commits: list[dict] = []
            else:
                commits = list_commits_since(owner, repo, pinned, ref=ref)
                behind = len(commits)

            behind_str = "[green]up-to-date[/green]" if behind == 0 else f"[yellow]+{behind}[/yellow]"
            table.add_row(
                component,
                pinned[:10],
                latest_sha[:10],
                behind_str,
                f"{latest_msg[:60]} ({latest_date[:10]})",
            )
            rows.append((component, latest, repo_url, pinned))
        except Exception as e:  # noqa: BLE001
            table.add_row(component, pinned[:10], "?", "?", f"[red]{e}[/red]")

    console.print(table)

    # Detail: list new commits per component
    for component, latest, repo_url, pinned in rows:
        owner, repo = parse_github_repo(repo_url)
        try:
            commits = list_commits_since(owner, repo, pinned)
        except Exception:  # noqa: BLE001
            commits = []
        if not commits:
            continue
        console.print(
            f"\n[bold]{component}[/bold] — {len(commits)} new commit(s) since pinned:"
        )
        for c in commits[:20]:
            sha = c["sha"][:10]
            msg = c["commit"]["message"].split("\n")[0]
            date = c["commit"]["author"]["date"][:10]
            console.print(f"  [dim]{date}[/dim] [cyan]{sha}[/cyan] {msg}")

    if not update:
        console.print(
            "\n[dim]Read-only check. Run with [cyan]--update[/cyan] to pull "
            "clones and rewrite workspace.json.[/dim]"
        )
        return

    # ---- UPDATE MODE ----
    new_config = json.loads(config_path.read_text())
    for component, latest, repo_url, _pinned in rows:
        local_dir = workspace / config[component]["local"]
        if not (local_dir / ".git").exists():
            console.print(
                f"[yellow]{component}: no .git in {local_dir}, skipping pull[/yellow]"
            )
            continue
        console.print(f"[cyan]{component}: pulling {local_dir}...[/cyan]")
        try:
            subprocess.run(
                ["git", "fetch", "origin"], cwd=str(local_dir),
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "checkout", latest["sha"]],
                cwd=str(local_dir),
                check=True, capture_output=True, text=True,
            )
            new_config[component]["sha"] = latest["sha"]
            console.print(
                f"  [green]{component} now at {latest['sha'][:10]}[/green]"
            )
        except subprocess.CalledProcessError as e:
            console.print(
                f"  [red]{component} pull failed: {e.stderr or e.stdout}[/red]"
            )

    new_config["last_freshness_update"] = datetime.now(timezone.utc).isoformat()
    config_path.write_text(json.dumps(new_config, indent=2))
    console.print(
        "\n[bold green]workspace.json updated.[/bold green] "
        "Subsequent commands will run against the new SHAs."
    )
