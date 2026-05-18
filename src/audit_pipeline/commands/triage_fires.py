"""`audit-pipeline triage-fires` — Layer 2.5 fire triage on a finished cycle.

Productized form of the manual STRONG/SOFT/FALSE classification that
collapsed cycle 20260511's 64 PoC fires down to 7 STRONG (4 distinct
root causes). Saves ~$280 of wasted Kani+LiteSVM spend per cycle.

Modes:
  - Read-only: scan a finished cycle, write triage.jsonl, print summary.
    Does NOT modify the cycle's recon_summary or trigger downstream layers.
  - Auto-called from `audit-pipeline hunt` between Layer 2 and Layer 3.
    Filters the Layer 3 dispatch set to STRONG representatives only.

The CLI form is for retrospective triage of finished cycles (e.g. cycle
20260511) and for re-running the judge against a new prompt or model.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from audit_pipeline.layer25_triage import triage_cycle

console = Console()


@click.command(name="triage-fires")
@click.argument("cycle_id", type=str)
@click.option(
    "--hypotheses", type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Hypotheses YAML used by the cycle (default: auto-detect from cycle dir)",
)
@click.option(
    "--engine-src-dir", type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Engine source dir for grounding the LLM judge (default: <workspace>/target/engine/src)",
)
@click.option(
    "--judge-model", default=None,
    help="Model to route the judge through (default: cycle-default).",
)
@click.option(
    "--no-llm", is_flag=True, default=False,
    help="Skip the LLM judge entirely; classify only via fast-path FALSE "
         "patterns. Unmatched fires default to SOFT. Useful for $0-cost "
         "retroactive triage.",
)
@click.option(
    "--language",
    type=click.Choice(
        ["solana", "c", "solidity", "aptos"], case_sensitive=False,
    ),
    default="solana",
    show_default=True,
    help="Target language under audit. Selects the language-specific "
         "fast-path FALSE-pattern set + judge syntax fence. Defaults to "
         "'solana' to preserve existing Percolator workflows.",
)
@click.option(
    "--framework", default=None,
    help="Test framework name (cargo / clang+sanitizers / forge / "
         "aptos-cli). Appears in the judge prompt header for context. "
         "Optional; defaults to the language's canonical framework.",
)
@click.option(
    "--apply-to-db", is_flag=True, default=False,
    help="After triage, recompute each finding's status from the new "
         "triage classification + clustering and UPDATE the findings DB "
         "row. FALSE/LOST fires are demoted to NEW (or REJECTED if the "
         "verdict was also FALSE), SOFT and non-representative STRONG "
         "fires are demoted to TRIAGED, STRONG cluster representatives "
         "land on CONFIRMED. Without this flag, triage is read-only.",
)
@click.pass_context
def triage_fires_cmd(
    ctx: click.Context,
    cycle_id: str,
    hypotheses: str | None,
    engine_src_dir: Path | None,
    judge_model: str | None,
    no_llm: bool,
    language: str,
    framework: str | None,
    apply_to_db: bool,
) -> None:
    """Triage a cycle's PoC fires into STRONG / SOFT / FALSE / LOST.

    Writes ``<workspace>/hunts/<cycle-id>/triage.jsonl`` and prints a
    summary. With ``--apply-to-db``, also recomputes each finding's
    status based on the new triage verdict + cluster representative
    role and updates the findings DB.
    """
    workspace = Path(ctx.obj["workspace"])
    cycle_dir = workspace / "hunts" / cycle_id
    if not cycle_dir.is_dir():
        raise click.ClickException(f"no cycle dir at {cycle_dir}")

    # Load hunt_summary.json → poc_results
    summary_path = cycle_dir / "hunt_summary.json"
    if not summary_path.is_file():
        raise click.ClickException(
            f"no hunt_summary.json at {summary_path} — cycle not finished?"
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    poc_results = summary.get("poc", {}) or {}

    # Load hypothesis metadata
    if hypotheses is None:
        # Try to auto-detect from cycle dir
        for candidate in cycle_dir.glob("hypotheses*.yaml"):
            hypotheses = str(candidate)
            break
    hyp_meta: dict[str, dict] = {}
    if hypotheses and Path(hypotheses).is_file():
        data = yaml.safe_load(Path(hypotheses).read_text(encoding="utf-8")) or {}
        for h in (data.get("hypotheses") or []):
            if isinstance(h, dict) and h.get("id"):
                hyp_meta[h["id"]] = h

    # If summary has hypothesis metadata snapshotted, fall back to it
    if not hyp_meta:
        for v in summary.get("verdicts", []) or []:
            hid = v.get("hypothesis_id")
            if hid:
                hyp_meta.setdefault(hid, {}).update({
                    k: v.get(k) for k in ("claim", "bug_class", "engine_function", "target_file")
                    if v.get(k)
                })

    # Engine src loader
    if engine_src_dir is None:
        engine_src_dir = workspace / "target" / "engine" / "src"

    def _engine_loader(fn_name: str) -> str:
        if not engine_src_dir.is_dir() or not fn_name:
            return ""
        from audit_pipeline.utils.code_extract import collect_grounded_code
        from audit_pipeline.utils.language_profile import profile_for as _pf
        try:
            # Phase 2 (language-aware grounding): walk the profile's source
            # extensions, recursive for languages whose source lives in
            # subdirs (C: auth/, common/, vendor/...).
            prof = _pf(language)
            src_files: list[Path] = []
            for ext in prof.source_exts:
                if language == "c":
                    src_files.extend(sorted(engine_src_dir.rglob(f"*{ext}")))
                else:
                    src_files.extend(sorted(engine_src_dir.glob(f"*{ext}")))
            grounded = collect_grounded_code([fn_name], src_files, max_lines=80)
            return next((v for v in grounded.values() if v), "")
        except Exception:  # noqa: BLE001
            return ""

    complete_fn = None
    if no_llm:
        # Stub that always raises so judge falls back to default-SOFT
        def _no_llm_stub(*a, **kw):
            raise RuntimeError("--no-llm: LLM judge disabled by operator")
        complete_fn = _no_llm_stub

    console.print(f"[bold]Triaging cycle {cycle_id}[/bold]")
    n_fired = sum(1 for pr in poc_results.values() if pr.get("fired"))
    console.print(f"  {n_fired} PoC fires to classify")

    out = triage_cycle(
        cycle_dir,
        poc_results=poc_results,
        hyp_meta=hyp_meta,
        engine_src_loader=_engine_loader,
        complete_fn=complete_fn,
        judge_model=judge_model,
        language=language,
        framework=framework,
    )

    # Print summary table
    table = Table(title=f"Triage results for {cycle_id}")
    table.add_column("Class")
    table.add_column("Count", justify="right")
    color = {"STRONG": "red", "SOFT": "yellow", "FALSE": "dim", "LOST": "magenta"}
    for cls in ("STRONG", "SOFT", "FALSE", "LOST"):
        n = out["counts"][cls]
        table.add_row(f"[{color[cls]}]{cls}[/{color[cls]}]", str(n))
    console.print(table)

    n_clusters = len(out["clusters"])
    console.print(
        f"[bold]Layer 3 dispatch set: {len(out['layer3_dispatch_set'])} "
        f"representatives across {n_clusters} root-cause cluster(s)[/bold]"
    )
    if out["layer3_dispatch_set"]:
        console.print("  representatives:")
        for cid in out["layer3_dispatch_set"]:
            members = out["clusters"].get(cid, [])
            if len(members) > 1:
                covered = ", ".join(m for m in members if m != cid)
                console.print(f"    [cyan]{cid}[/cyan] covers: {covered}")
            else:
                console.print(f"    [cyan]{cid}[/cyan]")
    console.print(f"[dim]LLM calls: {out['n_llm_calls']} (fast-path saved "
                  f"{n_fired - out['n_llm_calls']})[/dim]")
    console.print(f"[dim]triage.jsonl: {out['triage_jsonl_path']}[/dim]")

    if apply_to_db:
        _apply_triage_to_findings(workspace, cycle_id, out)


def _apply_triage_to_findings(
    workspace: Path, cycle_id: str, triage_out: dict,
) -> None:
    """Recompute each finding's status using the new triage results.

    Used to retroactively correct cycles persisted BEFORE the
    persistence step learned to honor triage classification — e.g. cycle
    20260513-191318 where 2 FALSE fires and 1 SOFT fire were incorrectly
    promoted to status=CONFIRMED, and 4 duplicates of one root cause
    each got their own CONFIRMED row.

    Idempotent: re-runs converge to the same DB state.
    """
    from audit_pipeline.db import open_findings_db
    from audit_pipeline.lifecycle import from_hunt_outcome

    db = open_findings_db(workspace)
    # Build hyp_id → triage info from the freshly-written results
    triage_by_hyp: dict[str, dict] = {}
    for r in triage_out.get("results", []):
        triage_by_hyp[r["hyp_id"]] = {
            "classification": r.get("classification"),
            "cluster_id": r.get("cluster_id"),
            "is_representative": bool(r.get("is_representative", True)),
            "reason": r.get("reason", ""),
        }

    all_findings = [
        f for f in db.list_findings(limit=10_000)
        if f.get("cycle_id") == cycle_id
    ]
    n_changed = 0
    transitions_log: list[tuple[str, str, str, str]] = []
    for f in all_findings:
        hyp_id = f.get("hypothesis_id") or ""
        verdict = f.get("verdict") or "UNKNOWN"
        debate_promoted = bool(f.get("debate_promoted"))
        poc_fired = bool(f.get("poc_fired"))
        current_status_str = f.get("status") or "new"
        info = triage_by_hyp.get(hyp_id, {})
        new_status = from_hunt_outcome(
            verdict, debate_promoted, poc_fired,
            triage_classification=info.get("classification"),
            is_cluster_representative=info.get("is_representative", True),
        )
        if new_status.value == current_status_str:
            continue
        # Some lifecycle transitions are forbidden (e.g. FIXED → NEW).
        # `_force_update_status` bypasses the state-machine guard since
        # we're doing a corrective re-classification, not a forward
        # workflow step. The transitions table records the reason.
        try:
            _force_update_status(
                db, finding_id=int(f["id"]),
                from_status=current_status_str,
                to_status=new_status.value,
                reason=(
                    f"triage reclassify: cls="
                    f"{info.get('classification') or '?'} "
                    f"rep={info.get('is_representative', True)}"
                ),
            )
            n_changed += 1
            transitions_log.append((
                hyp_id, current_status_str, new_status.value,
                info.get("classification") or "—",
            ))
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]skip finding {f['id']} ({hyp_id}): {e}[/yellow]")

    console.print()
    console.print(f"[bold]Applied triage to DB:[/bold] {n_changed} status change(s)")
    if transitions_log:
        tbl = Table(show_header=True, header_style="bold")
        tbl.add_column("Hypothesis")
        tbl.add_column("From")
        tbl.add_column("To")
        tbl.add_column("Triage")
        for hyp, frm, to, cls in transitions_log:
            tbl.add_row(hyp, frm, to, cls)
        console.print(tbl)


def _force_update_status(
    db, *, finding_id: int, from_status: str, to_status: str, reason: str,
) -> None:
    """Bypass the state-machine guard to write a corrective status.

    The lifecycle state machine rejects backward transitions (CONFIRMED
    → NEW) by design — that's correct for forward workflow. But a
    reclassify pass is a CORRECTION, not a workflow step: the previous
    CONFIRMED was wrong (triage said FALSE all along, the persistence
    step ignored it). We update directly and log the transition.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db._conn() as c:  # noqa: SLF001 — purposeful low-level write
        c.execute(
            "UPDATE findings SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, now, finding_id),
        )
        c.execute(
            """INSERT INTO transitions
               (finding_id, from_status, to_status, reason, actor, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (finding_id, from_status, to_status, reason, "triage-reclassify", now),
        )


__all__ = ["triage_fires_cmd"]
