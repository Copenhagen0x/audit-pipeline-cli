"""`audit-pipeline bundle` — closed-loop fix bundle pipeline (P3).

Subcommands:

    draft <finding-id>    LLM-author the patch + writeup, package the
                          bundle directory under <workspace>/recon/bundles/<id>/

    verify <finding-id>   Run all 4 (5 with Kani) machine gates, persist
                          verification.json. Refuses to fire if patch.diff
                          is missing.

    review <finding-id>   Interactive operator review. Shows diff +
                          verification table + Claude's written assessment.
                          Operator types the long-form authorization phrase
                          to authorize. Writes authorization.json.

    open-pr <finding-id>  Open the upstream PR. REFUSES to fire unless a
                          valid authorization marker exists for the exact
                          (finding, engine_sha, patch_sha) tuple. This is
                          the hard-rule enforcement point.

    status <finding-id>   Print the bundle's current state (no mutations).

    list                  Show every bundle in the workspace + status.

    override <finding-id> --patch <file>
                          Replace the auto-drafted patch with an
                          operator-authored one. Invalidates any
                          existing authorization marker.

THE HARD RULE
─────────────

Engine NEVER auto-opens upstream PRs. The five-gate chain enforced here:

  1. Machine verification (verify) must show all gates passed
  2. Claude assessment must accompany the diff at review time
  3. Operator must read the diff
  4. Operator must type `yes-authorize-finding-<id>-<patch-sha[:12]>` literally
  5. (finding_id, engine_sha, patch_sha) tuple must still match at open-pr time

Any change to patch.diff, verification.json, or the engine_sha after
authorization invalidates the marker. Re-review is required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.bundle import paths as bpaths
from audit_pipeline.bundle.assembly import (
    bundle_digest,
    sign_bundle,
    transition_status,
    write_meta,
    write_patch,
    write_writeup,
)
from audit_pipeline.bundle.auth import (
    AuthorizationInvalid,
    expected_phrase,
    file_sha256,
    load_authorization,
    validate_authorization,
    write_authorization,
)
from audit_pipeline.bundle.patcher import author_patch
from audit_pipeline.bundle.templates import template_for
from audit_pipeline.bundle.verifier import all_passed, run_all_gates
from audit_pipeline.db import open_findings_db

console = Console()


def _ws(ctx: click.Context) -> Path:
    return Path(ctx.obj["workspace"])


# ───────────────────────── draft ──────────────────────────


@click.group(name="bundle")
def bundle_cmd() -> None:
    """P3 — closed-loop fix bundle pipeline. See module docstring for the hard rule."""


@bundle_cmd.command(name="draft")
@click.argument("finding_id", type=int)
@click.option("--engine-repo", type=click.Path(path_type=Path), default=None,
              help="Local checkout of the engine repo (used to read the target file)")
@click.option("--target-file", type=str, default=None,
              help="File the patch should modify (relative to engine_repo). "
                   "If omitted, drafts a writeup-only bundle.")
@click.option("--poc-source-file", type=click.Path(path_type=Path), default=None,
              help="Path to the PoC test source — included in the prompt")
@click.option("--poc-test-name", type=str, default=None,
              help="Stable name of the PoC test (used by `verify` later)")
@click.pass_context
def draft_cmd(
    ctx: click.Context,
    finding_id: int,
    engine_repo: Path | None,
    target_file: str | None,
    poc_source_file: Path | None,
    poc_test_name: str | None,
) -> None:
    """Author a patch + writeup for a confirmed finding (LLM-driven)."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    finding = db.get_finding(finding_id)
    if not finding:
        raise click.ClickException(f"finding {finding_id} not found")
    if (finding.get("status") or "") != "confirmed":
        raise click.ClickException(
            f"finding {finding_id} status is {finding.get('status')!r}; "
            f"only 'confirmed' findings can be bundled"
        )

    bug_class = finding.get("bug_class") or "unknown"
    template = template_for(bug_class)

    # Read the target source file if given (for LLM context)
    target_source = ""
    if target_file and engine_repo:
        tpath = engine_repo / target_file
        if tpath.is_file():
            target_source = tpath.read_text(encoding="utf-8", errors="replace")
        else:
            console.print(f"[yellow]warn:[/yellow] target file {tpath} not found")

    # Read the PoC source (for LLM context)
    poc_source = ""
    if poc_source_file and poc_source_file.is_file():
        poc_source = poc_source_file.read_text(encoding="utf-8", errors="replace")

    # Initialize bundle meta
    cycle = next((c for c in db.list_cycles()
                   if c.get("cycle_id") == finding.get("cycle_id")), {})
    engine_sha = cycle.get("engine_sha") or ""

    write_meta(
        workspace,
        finding_id=finding_id,
        engine_sha=engine_sha,
        bug_class=bug_class,
        hypothesis_id=finding.get("hypothesis_id") or "",
        severity=finding.get("severity") or "Medium",
        title=finding.get("title") or "",
        template_used=bug_class if bug_class in template.headline else "generic",
        status="drafted",
        poc_test_name=poc_test_name,
        target_file=target_file,
    )

    # Draft the patch via LLM (or skip if no target source)
    if target_source:
        draft = author_patch(
            hypothesis_id=finding.get("hypothesis_id") or "",
            bug_class=bug_class,
            severity=finding.get("severity") or "Medium",
            title=finding.get("title") or "",
            poc_source=poc_source or "(PoC source not provided)",
            target_file_path=target_file or "unknown",
            target_source=target_source,
        )
        if draft.diff:
            write_patch(workspace, finding_id, draft.diff)
            console.print(f"[green]drafted patch[/green] ({len(draft.diff)} bytes)")
        else:
            console.print(f"[yellow]no patch drafted[/yellow] — {draft.rationale}")
    else:
        console.print("[dim]skipping patch draft — no --engine-repo + --target-file[/dim]")
        draft = None

    # Render writeup skeleton
    writeup = template.writeup_skeleton.format(
        finding_id=finding_id,
        title=finding.get("title") or "",
        hypothesis_id=finding.get("hypothesis_id") or "",
        bug_class=bug_class,
        poc_path=str(poc_source_file or "(unknown)"),
        handler_function="(fill in)",
        trigger="(fill in)",
        absorbed_amount="(fill in)",
        value_type="(fill in)",
        trigger_conditions="(fill in)",
    )
    if draft and draft.rationale:
        writeup = (
            f"<!-- LLM rationale: {draft.rationale} -->\n\n" + writeup
        )
    write_writeup(workspace, finding_id, writeup)
    console.print("[green]wrote writeup skeleton[/green]")

    # Sign the bundle digest
    sig = sign_bundle(workspace, finding_id,
                       signing_key=workspace / "keys" / "jelleo.ed25519.priv")
    if sig:
        console.print(f"[green]signed[/green] {sig.relative_to(workspace)}")

    bdir = bpaths.bundle_dir(workspace, finding_id)
    console.print(f"\n[bold]bundle ready[/bold] at {bdir.relative_to(workspace)}")
    console.print(f"  next: [cyan]audit-pipeline bundle verify {finding_id}[/cyan] "
                   f"(after providing --engine-repo + --poc-test-name)")


# ───────────────────────── verify ──────────────────────────


@bundle_cmd.command(name="verify")
@click.argument("finding_id", type=int)
@click.option("--engine-repo", type=click.Path(path_type=Path), default=None,
              help="Engine repo where cargo / kani run")
@click.option("--poc-test-name", type=str, default=None,
              help="Cargo test name (e.g. test_f7_residual)")
@click.option("--kani-harness", type=str, default=None,
              help="Optional Kani harness name to re-prove post-patch")
@click.pass_context
def verify_cmd(
    ctx: click.Context, finding_id: int,
    engine_repo: Path | None, poc_test_name: str | None,
    kani_harness: str | None,
) -> None:
    """Run the 4-5 machine verification gates and persist verification.json."""
    workspace = _ws(ctx)
    if not bpaths.patch_path(workspace, finding_id).is_file():
        raise click.ClickException(
            f"no patch.diff for finding {finding_id} — run `bundle draft` first"
        )

    # Load engine_sha + persisted draft-time params from meta. Fall back so
    # the operator doesn't have to re-pass --poc-test-name / --kani-harness
    # if they were already given at draft time.
    mp = bpaths.meta_path(workspace, finding_id)
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {}
    engine_sha = meta.get("engine_sha", "")
    effective_poc_test = poc_test_name or meta.get("poc_test_name")
    effective_kani = kani_harness or meta.get("kani_harness")

    result = run_all_gates(
        workspace, finding_id,
        engine_sha=engine_sha,
        engine_repo=engine_repo,
        poc_test_name=effective_poc_test,
        kani_harness=effective_kani,
    )

    # Render gate result table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Gate")
    table.add_column("Result")
    table.add_column("Reason", overflow="fold")
    table.add_column("Time", justify="right")
    for name, g in result["gates"].items():
        passed = g.get("passed")
        if passed is True:
            r = "[green]PASS[/green]"
        elif passed is False:
            r = "[red]FAIL[/red]"
        else:
            r = "[yellow]SKIP[/yellow]"
        table.add_row(name, r, g.get("reason", "")[:120], f"{g.get('duration_s', 0):.2f}s")
    console.print(table)

    if all_passed(result):
        transition_status(workspace, finding_id, "verified",
                           note="all gates passed")
        console.print(f"\n[bold green]ALL GATES PASSED.[/bold green] "
                       f"Next: [cyan]audit-pipeline bundle review {finding_id}[/cyan]")
    else:
        n_fail = sum(1 for g in result["gates"].values() if g.get("passed") is False)
        n_skip = sum(1 for g in result["gates"].values() if g.get("passed") is None)
        console.print(f"\n[bold red]{n_fail} fail, {n_skip} skip[/bold red] — "
                       f"bundle is NOT eligible for authorization")


# ───────────────────────── review (interactive auth) ──────────────────────────


@bundle_cmd.command(name="review")
@click.argument("finding_id", type=int)
@click.option("--authorizer", default=None,
              help="Operator name recorded in authorization.json (defaults to env JELLEO_OPERATOR)")
@click.option("--ttl-hours", type=int, default=24, show_default=True,
              help="How long the authorization stays valid")
@click.pass_context
def review_cmd(
    ctx: click.Context, finding_id: int,
    authorizer: str | None, ttl_hours: int,
) -> None:
    """Interactive review + authorization — the human-in-the-loop step."""
    import os as _os
    workspace = _ws(ctx)

    p_path = bpaths.patch_path(workspace, finding_id)
    v_path = bpaths.verification_path(workspace, finding_id)
    if not p_path.is_file():
        raise click.ClickException(f"no patch.diff at {p_path} — run `bundle draft` first")
    if not v_path.is_file():
        raise click.ClickException(f"no verification.json at {v_path} — run `bundle verify` first")

    # Show the diff
    console.print("\n[bold]──── PATCH ────[/bold]")
    diff_text = p_path.read_text(encoding="utf-8", errors="replace")
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            console.print(f"[green]{line}[/green]")
        elif line.startswith("-") and not line.startswith("---"):
            console.print(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            console.print(f"[cyan]{line}[/cyan]")
        else:
            console.print(line)

    # Show the verification table
    console.print("\n[bold]──── VERIFICATION ────[/bold]")
    verification = json.loads(v_path.read_text(encoding="utf-8"))
    for name, g in verification.get("gates", {}).items():
        passed = g.get("passed")
        marker = "[green]✓ PASS[/green]" if passed is True else \
                 "[red]✗ FAIL[/red]" if passed is False else "[yellow]· SKIP[/yellow]"
        console.print(f"  {marker}  {name}: {g.get('reason', '')[:120]}")

    if not all_passed(verification):
        raise click.ClickException(
            "verification has failing or skipped gates — refusing to authorize. "
            "Re-run `bundle verify` after fixing the underlying issue."
        )

    # Show the writeup head (operator can `cat` for full)
    w_path = bpaths.writeup_path(workspace, finding_id)
    if w_path.is_file():
        console.print("\n[bold]──── WRITEUP (head) ────[/bold]")
        head = "\n".join(w_path.read_text(encoding="utf-8").splitlines()[:20])
        console.print(head)

    # The hard rule prompt
    p_sha = file_sha256(p_path)
    expected = expected_phrase(finding_id, p_sha)
    console.print("\n[bold yellow]──── AUTHORIZATION ────[/bold yellow]")
    console.print(
        f"To authorize this bundle for PR opening, type the following phrase EXACTLY:\n\n"
        f"  [bold cyan]{expected}[/bold cyan]\n\n"
        f"Then press Enter. Anything else aborts."
    )

    typed = click.prompt("phrase", default="", show_default=False)
    op = authorizer or _os.environ.get("JELLEO_OPERATOR") or "kirill"

    try:
        marker = write_authorization(
            workspace,
            finding_id=finding_id,
            engine_sha=verification.get("engine_sha", ""),
            authorizer=op,
            typed_phrase=typed,
            ttl_hours=ttl_hours,
        )
    except AuthorizationInvalid as e:
        raise click.ClickException(str(e)) from e

    transition_status(workspace, finding_id, "authorized",
                       note=f"authorized by {op} (ttl {ttl_hours}h)")
    console.print(
        f"\n[bold green]AUTHORIZED.[/bold green] expires {marker.expires_at}\n"
        f"  next: [cyan]audit-pipeline bundle open-pr {finding_id} --repo owner/name[/cyan]"
    )


# ───────────────────────── open-pr (gated) ──────────────────────────


@bundle_cmd.command(name="open-pr")
@click.argument("finding_id", type=int)
@click.option("--repo", required=True, help="Upstream repo (owner/name)")
@click.option("--base-branch", default="main", show_default=True,
              help="Branch to PR against")
@click.option("--branch-name", default=None,
              help="Branch name to push (default: jelleo-fix-<id>)")
@click.option("--dry-run", is_flag=True,
              help="Print the gh command but don't fire it")
@click.pass_context
def open_pr_cmd(
    ctx: click.Context, finding_id: int,
    repo: str, base_branch: str, branch_name: str | None, dry_run: bool,
) -> None:
    """Open the upstream PR — REFUSES without valid authorization marker."""
    workspace = _ws(ctx)

    mp = bpaths.meta_path(workspace, finding_id)
    if not mp.is_file():
        raise click.ClickException(f"no bundle for finding {finding_id}")
    meta = json.loads(mp.read_text(encoding="utf-8"))
    engine_sha = meta.get("engine_sha", "")

    # THE HARD RULE — must pass before anything fires
    try:
        marker = validate_authorization(workspace, finding_id, engine_sha)
    except AuthorizationInvalid as e:
        raise click.ClickException(
            f"REFUSED: authorization invalid.\n"
            f"  reason: {e}\n"
            f"  fix: re-run `audit-pipeline bundle review {finding_id}` "
            f"(operator must explicitly re-authorize)"
        ) from e

    console.print(f"[green]authorization valid[/green] (authorized by {marker.authorizer} "
                   f"at {marker.authorized_at}, expires {marker.expires_at})")

    branch = branch_name or f"jelleo-fix-{finding_id}"
    p_path = bpaths.patch_path(workspace, finding_id)
    w_path = bpaths.writeup_path(workspace, finding_id)
    bug_class = meta.get("bug_class", "unknown")
    title_short = meta.get("title", "")[:80]
    pr_title = f"[Jelleo] Fix {bug_class}: {title_short}"

    body_lines = [
        f"Disclosed by Jelleo continuous audit (finding #{finding_id}).",
        "",
        f"**Bug class**: `{bug_class}`",
        f"**Severity**: {meta.get('severity', 'Medium')}",
        f"**Hypothesis**: `{meta.get('hypothesis_id', '')}`",
        f"**Engine SHA**: `{engine_sha[:12]}`",
        "",
        "## Writeup",
        "",
    ]
    if w_path.is_file():
        body_lines.append(w_path.read_text(encoding="utf-8"))
    body_lines.append("")
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(f"Bundle digest: `sha256:{bundle_digest(workspace, finding_id)}`")
    body_lines.append(f"Authorization: signed by `{marker.authorizer}` at {marker.authorized_at}")
    body = "\n".join(body_lines)

    # The actual gh CLI command
    if dry_run:
        console.print("\n[bold]DRY-RUN — would execute:[/bold]")
        console.print(f"  gh pr create --repo {repo} --base {base_branch} --head {branch} \\")
        console.print(f"               --title {pr_title!r} --body-file <body>")
        console.print(f"  patch:  {p_path}")
        console.print(f"  body:   {len(body)} bytes")
        return

    if not shutil.which("gh"):
        raise click.ClickException("gh CLI not in PATH. Install: https://cli.github.com/")

    # Stash the body to a temp file the gh command can read
    body_path = bpaths.bundle_dir(workspace, finding_id) / "pr-body.md"
    body_path.write_text(body, encoding="utf-8")

    # NOTE: this prints the command; actual execution requires the operator's
    # gh auth + a fork-and-push step that varies by repo. For Y0, we stop
    # short of auto-firing the push and leave the operator to:
    #   1. cd <fork>; git checkout -b <branch>; git apply <patch>
    #   2. git commit / git push
    #   3. gh pr create ... --body-file <body>
    console.print("\n[bold cyan]READY TO OPEN PR.[/bold cyan]")
    console.print(f"  repo:    {repo}")
    console.print(f"  branch:  {branch}")
    console.print(f"  title:   {pr_title}")
    console.print(f"  body:    {body_path.relative_to(workspace)} ({len(body)} bytes)")
    console.print(f"  patch:   {p_path.relative_to(workspace)}")
    console.print("\n[dim]Final step (operator runs in the fork):[/dim]")
    console.print(f"  git checkout -b {branch}")
    console.print(f"  git apply {p_path}")
    console.print(f"  git commit -am {pr_title!r}")
    console.print(f"  git push -u origin {branch}")
    console.print(f"  gh pr create --repo {repo} --base {base_branch} \\")
    console.print(f"               --head {branch} --title {pr_title!r} \\")
    console.print(f"               --body-file {body_path}")

    transition_status(workspace, finding_id, "pr-opened",
                       note=f"PR command rendered for {repo}@{branch}")


# ───────────────────────── status / list / override ──────────────────────────


@bundle_cmd.command(name="status")
@click.argument("finding_id", type=int)
@click.pass_context
def status_cmd(ctx: click.Context, finding_id: int) -> None:
    """Print current bundle state for a finding."""
    workspace = _ws(ctx)
    mp = bpaths.meta_path(workspace, finding_id)
    if not mp.is_file():
        console.print(f"[dim]no bundle for finding {finding_id}[/dim]")
        return
    meta = json.loads(mp.read_text(encoding="utf-8"))
    console.print(f"[bold]bundle {finding_id}[/bold]")
    console.print(f"  status:    {meta.get('status', '?')}")
    console.print(f"  bug_class: {meta.get('bug_class', '?')}")
    console.print(f"  template:  {meta.get('template_used', '?')}")
    console.print(f"  updated:   {meta.get('updated_at', '?')}")

    # Authorization marker?
    try:
        m = load_authorization(workspace, finding_id)
        console.print(f"  authz:     [green]valid[/green] (by {m.authorizer}, expires {m.expires_at})")
    except AuthorizationInvalid:
        console.print("  authz:     [yellow]none[/yellow]")

    # Verification?
    v_path = bpaths.verification_path(workspace, finding_id)
    if v_path.is_file():
        v = json.loads(v_path.read_text(encoding="utf-8"))
        n_pass = sum(1 for g in v.get("gates", {}).values() if g.get("passed") is True)
        n_fail = sum(1 for g in v.get("gates", {}).values() if g.get("passed") is False)
        n_skip = sum(1 for g in v.get("gates", {}).values() if g.get("passed") is None)
        console.print(f"  verify:    {n_pass} pass, {n_fail} fail, {n_skip} skip")


@bundle_cmd.command(name="list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List every bundle in the workspace."""
    workspace = _ws(ctx)
    root = bpaths.bundle_root(workspace)
    if not root.is_dir():
        console.print(f"[dim]no bundles dir at {root}[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Status")
    table.add_column("Bug class")
    table.add_column("Template")
    table.add_column("Updated")
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        table.add_row(str(m.get("finding_id", d.name)),
                      m.get("status", "?"),
                      m.get("bug_class", "?")[:36],
                      m.get("template_used", "?")[:30],
                      m.get("updated_at", "?")[:19])
    console.print(table)


@bundle_cmd.command(name="override")
@click.argument("finding_id", type=int)
@click.option("--patch", "patch_file", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Operator-authored patch file to install in place of the LLM draft")
@click.pass_context
def override_cmd(ctx: click.Context, finding_id: int, patch_file: Path) -> None:
    """Replace the auto-drafted patch with an operator-authored one.

    Invalidates any existing authorization marker (since patch_sha changes).
    """
    workspace = _ws(ctx)
    if not bpaths.meta_path(workspace, finding_id).is_file():
        raise click.ClickException(f"no bundle for finding {finding_id} — draft first")

    src = patch_file.read_text(encoding="utf-8")
    write_patch(workspace, finding_id, src)
    # Invalidate authz by removing the marker file (if any)
    auth_path = bpaths.authorization_path(workspace, finding_id)
    if auth_path.is_file():
        auth_path.unlink()
        console.print("[yellow]existing authorization marker removed (patch_sha changed)[/yellow]")

    transition_status(workspace, finding_id, "drafted",
                       note=f"operator patch override from {patch_file}")
    console.print("[green]installed operator patch[/green]")
    console.print(f"  next: [cyan]audit-pipeline bundle verify {finding_id}[/cyan] (then review again)")


# ───────────────────────── per-finding disclosure repo (item 9) ──────────────────────


@bundle_cmd.command(name="init-repo")
@click.argument("finding_id", type=int)
@click.option("--out-dir", type=click.Path(path_type=Path), default=None,
              help="Where to materialize the repo (default: <workspace>/disclosure-repos/<id>/)")
@click.option("--git-init/--no-git-init", default=True, show_default=True,
              help="Run `git init` in the new repo")
@click.pass_context
def init_repo_cmd(
    ctx: click.Context, finding_id: int,
    out_dir: Path | None, git_init: bool,
) -> None:
    """Materialize a standalone per-finding disclosure repo from the bundle.

    Layout matches the F7-style disclosure repo:

      <out-dir>/
        README.md           — entry point + how to read this repo
        DISCLOSURE.md       — full writeup (copied from bundle's writeup.md)
        RECOMMENDED_PATCH.md — patch.diff wrapped in a fenced block + commentary
        VERIFICATION.md     — verification.json rendered as a table
        bundle/             — exact bundle dir snapshot (signed)
        LICENSE             — Apache-2.0 (matches platform license)

    Operator can then `gh repo create` and push.
    """
    workspace = _ws(ctx)
    mp = bpaths.meta_path(workspace, finding_id)
    if not mp.is_file():
        raise click.ClickException(f"no bundle for finding {finding_id} — draft first")
    meta = json.loads(mp.read_text(encoding="utf-8"))

    out = out_dir or (workspace / "disclosure-repos" / str(finding_id))
    out.mkdir(parents=True, exist_ok=True)

    # Snapshot bundle dir
    bdir = bpaths.bundle_dir(workspace, finding_id)
    bundle_snapshot = out / "bundle"
    if bundle_snapshot.is_dir():
        shutil.rmtree(bundle_snapshot)
    shutil.copytree(str(bdir), str(bundle_snapshot))

    # README
    readme = (
        f"# Jelleo disclosure · finding #{finding_id}\n\n"
        f"**Bug class**: `{meta.get('bug_class', '')}`\n"
        f"**Severity**: {meta.get('severity', '')}\n"
        f"**Hypothesis**: `{meta.get('hypothesis_id', '')}`\n"
        f"**Engine SHA**: `{meta.get('engine_sha', '')[:12]}`\n\n"
        f"## How to read this repo\n\n"
        f"| File | What it is |\n"
        f"|---|---|\n"
        f"| `DISCLOSURE.md` | Root-cause writeup (engine-authored, operator-reviewed) |\n"
        f"| `RECOMMENDED_PATCH.md` | The recommended fix (unified diff + commentary) |\n"
        f"| `VERIFICATION.md` | Machine-verification gate results |\n"
        f"| `bundle/` | Exact signed bundle artifacts |\n"
        f"| `LICENSE` | Apache-2.0 |\n\n"
        f"---\n\n"
        f"Disclosed via [jelleo.com](https://jelleo.com) "
        f"under the platform's [coordinated disclosure policy]"
        f"(https://jelleo.com/security.html).\n"
    )
    (out / "README.md").write_text(readme, encoding="utf-8")

    # DISCLOSURE.md — copy writeup
    w_path = bpaths.writeup_path(workspace, finding_id)
    if w_path.is_file():
        (out / "DISCLOSURE.md").write_text(w_path.read_text(encoding="utf-8"),
                                             encoding="utf-8")

    # RECOMMENDED_PATCH.md
    p_path = bpaths.patch_path(workspace, finding_id)
    if p_path.is_file():
        diff = p_path.read_text(encoding="utf-8")
        body = (
            f"# Recommended patch\n\n"
            f"Apply the diff below with `git apply` against engine SHA "
            f"`{meta.get('engine_sha', '')[:12]}`.\n\n"
            f"```diff\n{diff}\n```\n"
        )
        (out / "RECOMMENDED_PATCH.md").write_text(body, encoding="utf-8")

    # VERIFICATION.md
    v_path = bpaths.verification_path(workspace, finding_id)
    if v_path.is_file():
        v = json.loads(v_path.read_text(encoding="utf-8"))
        rows = ["| Gate | Result | Reason | Time |", "|---|---|---|---|"]
        for name, g in v.get("gates", {}).items():
            passed = g.get("passed")
            mark = "PASS" if passed is True else ("FAIL" if passed is False else "SKIP")
            rows.append(f"| `{name}` | {mark} | {(g.get('reason') or '')[:120]} | "
                         f"{g.get('duration_s', 0):.2f}s |")
        body = (
            f"# Verification\n\n"
            f"All gates run on bundle digest `{bundle_digest(workspace, finding_id)[:12]}`.\n\n"
            + "\n".join(rows) + "\n"
        )
        (out / "VERIFICATION.md").write_text(body, encoding="utf-8")

    # LICENSE — write a minimal Apache-2.0 placeholder
    (out / "LICENSE").write_text(
        "Apache License, Version 2.0\n"
        "https://www.apache.org/licenses/LICENSE-2.0\n",
        encoding="utf-8",
    )

    if git_init and shutil.which("git"):
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(out), check=True)
            subprocess.run(["git", "add", "-A"], cwd=str(out), check=True)
            subprocess.run(["git", "-c", "commit.gpgsign=false",
                             "commit", "-q", "-m", f"Initial: Jelleo disclosure {finding_id}"],
                            cwd=str(out), check=False)
        except Exception as e:
            console.print(f"[yellow]git init step failed:[/yellow] {e}")

    console.print(f"[green]wrote disclosure repo[/green] {out}")
    console.print("\nNext (operator):")
    console.print(f"  cd {out}")
    console.print(f"  gh repo create jelleo-disclosure-{finding_id} --private --source . --push")


# ───────────────────────── PR webhook (lifecycle bridge) ──────────────────────


@bundle_cmd.command(name="publish-archive")
@click.argument("finding_id", type=int)
@click.option("--archive-root", type=click.Path(path_type=Path),
              default=None, show_default=False,
              help="Public archive root (rsync target on the api host). "
                   "Defaults to JELLEO_PUBLIC_ROOT/bundles (env-var aware).")
@click.option("--public-only/--full", default=True, show_default=True,
              help="--public excludes verification.json + authorization.json + hooks/ "
                   "(those are operator-private). --full publishes everything; only "
                   "use for fully-disclosed bundles.")
@click.pass_context
def publish_archive_cmd(
    ctx: click.Context, finding_id: int,
    archive_root: Path | None, public_only: bool,
) -> None:
    """P3 Item 14: copy a bundle into the public archive at api.jelleo.com/bundles/<id>/.

    Pre-disclosure rule: only `merged` or `fixed` bundles may be published with --full.
    --public is allowed at any status >= verified but redacts operator-private files.
    """
    if archive_root is None:
        from audit_pipeline.utils.vps_paths import public_bundles_dir
        archive_root = public_bundles_dir()
    workspace = _ws(ctx)
    mp = bpaths.meta_path(workspace, finding_id)
    if not mp.is_file():
        raise click.ClickException(f"no bundle for finding {finding_id}")
    meta = json.loads(mp.read_text(encoding="utf-8"))
    status = meta.get("status") or "drafted"

    if public_only and status not in ("verified", "authorized", "pr-opened", "merged", "fixed"):
        raise click.ClickException(
            f"refusing to publish: status {status!r} is below 'verified'. "
            f"Run `bundle verify` first."
        )
    if not public_only and status not in ("merged", "fixed"):
        raise click.ClickException(
            f"refusing --full publish: status {status!r} is not 'merged' or 'fixed'. "
            f"Pre-disclosure rule blocks --full archive of in-flight bundles."
        )

    src = bpaths.bundle_dir(workspace, finding_id)
    dst = archive_root / str(finding_id)
    dst.mkdir(parents=True, exist_ok=True)

    EXCLUDE_PUBLIC = {"verification.json", "authorization.json", "hooks", "pr-body.md"}
    n_copied = 0
    for item in src.iterdir():
        if public_only and item.name in EXCLUDE_PUBLIC:
            continue
        target = dst / item.name
        if item.is_dir():
            if target.is_dir():
                shutil.rmtree(target)
            shutil.copytree(str(item), str(target))
        else:
            shutil.copy2(str(item), str(target))
        n_copied += 1

    console.print(f"[green]published[/green] {n_copied} item(s) to {dst}")
    console.print(f"  visible at: https://api.jelleo.com/bundles/{finding_id}/")


@bundle_cmd.command(name="record-pr-event")
@click.argument("finding_id", type=int)
@click.option("--event", required=True,
              type=click.Choice(["opened", "reviewed", "merged", "closed", "rejected"]))
@click.option("--pr-url", default=None, help="GitHub PR URL")
@click.option("--note", default=None, help="Free-text note (e.g. maintainer feedback)")
@click.pass_context
def record_pr_event_cmd(
    ctx: click.Context, finding_id: int, event: str,
    pr_url: str | None, note: str | None,
) -> None:
    """Record a PR lifecycle event and transition bundle status accordingly.

    Designed to be invoked by a `gh webhook` listener (out of scope for Y0)
    or manually by the operator after observing the PR upstream.
    """
    workspace = _ws(ctx)
    mp = bpaths.meta_path(workspace, finding_id)
    if not mp.is_file():
        raise click.ClickException(f"no bundle for finding {finding_id}")

    status_map = {
        "opened":   "pr-opened",
        "reviewed": "pr-opened",
        "merged":   "merged",
        "closed":   "rejected",
        "rejected": "rejected",
    }
    new_status = status_map[event]

    note_full = f"event={event}"
    if pr_url:
        note_full += f" url={pr_url}"
    if note:
        note_full += f" note={note}"
    transition_status(workspace, finding_id, new_status, note=note_full)

    # On merge, walk the underlying finding's lifecycle to FIXED. The state
    # machine is confirmed -> disclosed -> fixed (per lifecycle.VALID_TRANSITIONS),
    # so we can't go straight from confirmed to fixed. Walk through whichever
    # intermediate state we need, surfacing a hard error if any leg fails.
    if event == "merged":
        from audit_pipeline.lifecycle import Status
        db = open_findings_db(workspace)
        f = db.get_finding(finding_id)
        if not f:
            raise click.ClickException(
                f"bundle marked merged but finding {finding_id} disappeared from DB"
            )
        current = f.get("status") or "new"
        chain: list[Status] = []
        if current == "confirmed":
            chain = [Status.DISCLOSED, Status.FIXED]
        elif current == "disclosed":
            chain = [Status.FIXED]
        elif current == "fixed":
            chain = []  # already there
            console.print(f"[dim]finding {finding_id} already fixed[/dim]")
        else:
            raise click.ClickException(
                f"finding {finding_id} is in state {current!r}; cannot walk to fixed. "
                f"Valid origin states: confirmed, disclosed, fixed."
            )
        for to_status in chain:
            db.transition_finding(
                finding_id=finding_id,
                to_status=to_status,
                reason=(f"PR merged upstream: {pr_url or '(url unknown)'} "
                        f"(walked via bundle.record-pr-event)"),
                actor="bundle.record-pr-event",
            )
            console.print(f"[green]finding {finding_id} -> {to_status.value}[/green]")

    console.print(f"[green]bundle {finding_id} -> {new_status}[/green]")
