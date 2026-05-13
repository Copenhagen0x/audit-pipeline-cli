"""`audit-pipeline export-scabench` — emit findings in ScaBench / AuditAgent JSON.

OtterSec's vendor-evaluation pipeline (and the broader ScaBench community
benchmark) uses Nethermind's open-source ``auditagent-scoring-algo``. The
scorer expects each tool's findings as a JSON array of objects with a
specific shape::

    {
      "project": "<repo or project id>",
      "files_analyzed": <int>,
      "total_findings": <int>,
      "findings": [
        {
          "title": "Reentrancy in withdraw",
          "description": "...detailed root cause + impact...",
          "severity": "high",   // high | medium | low | info | bestpractices
          "location": "withdraw() function",
          "file": "Vault.sol"
        },
        ...
      ]
    }

This command:
  * reads findings.db rows for a given cycle (or target),
  * filters to confirmed/disclosed/fixed/verified/closed_not_planned (the
    "real" findings — anything still in NEW or REJECTED is excluded so
    pre-triage noise doesn't pollute the submission),
  * drops Info severity by default (it doesn't count as TP in scoring and
    only inflates FP counts if mis-graded),
  * maps internal severity (Critical/High/Medium/Low/Info) to ScaBench
    lowercase (high/medium/low/info/bestpractices),
  * builds title/description/location/file from existing finding fields
    + the narrative writeup (if available),
  * writes the JSON to ``<output>`` (default:
    ``<workspace>/submissions/<project>_results.json``).

Used by the OtterSec eval submission pipeline + any future client whose
deliverable is "ScaBench-compliant JSON" rather than a PDF.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from audit_pipeline.db import open_findings_db
from audit_pipeline.severity import Severity

console = Console()


# ScaBench severity vocabulary is lowercase + has `bestpractices`.
# Our internal Critical collapses into ScaBench `high` (the scorer's
# top tier — there's no `critical` in their rubric).
_SEVERITY_MAP: dict[Severity, str] = {
    Severity.CRITICAL: "high",
    Severity.HIGH: "high",
    Severity.MEDIUM: "medium",
    Severity.LOW: "low",
    Severity.INFO: "info",
}


def _scabench_severity(internal: str | None) -> str:
    """Map our internal Severity string to ScaBench lowercase."""
    try:
        sev = Severity(internal) if internal else Severity.MEDIUM
    except ValueError:
        sev = Severity.MEDIUM
    return _SEVERITY_MAP[sev]


def _location_for(finding: dict, details: dict) -> str:
    """Best-effort location string for ScaBench.

    The scorer uses location for one-to-one matching (same location +
    same root cause + same attack vector + same impact = exact match).
    Specificity helps; vagueness hurts the F1.

    Order of preference:
      1. details_json["target_lines"] + details_json["target_file"]
      2. details_json["target_function"] / "function"
      3. hypothesis_id (last resort — at least it's consistent)
    """
    lines = details.get("target_lines") or details.get("lines")
    file_ = details.get("target_file") or details.get("file")
    func = (
        details.get("target_function")
        or details.get("function")
        or details.get("entrypoint")
    )
    parts: list[str] = []
    if func:
        parts.append(f"{func}()")
    if lines:
        parts.append(f"L{lines}")
    if file_ and not func:
        parts.append(f"in {file_}")
    if parts:
        return " ".join(parts)
    return finding.get("hypothesis_id") or "(location unknown)"


def _file_for(finding: dict, details: dict) -> str:
    """Best-effort file path for ScaBench.

    Trying details_json["target_file"] first, falling back to a guess
    derived from the hypothesis id (e.g. ``vault_drain_h12`` → vault).
    The scorer is LLM-based and forgiving; even an approximate file
    increases match probability.
    """
    if details.get("target_file"):
        return str(details["target_file"])
    if details.get("file"):
        return str(details["file"])
    return "(file unknown)"


def _description_for(finding: dict, details: dict, narrative_path: Path | None) -> str:
    """Build the longest-possible accurate description.

    Concat (in order):
      1. The finding's title (so the matcher always sees the headline)
      2. The hypothesis claim (root-cause hypothesis, from details_json)
      3. The first ~500 chars of the narrative writeup if available
         (narrative.py generates one of these per confirmed finding)
      4. The PoC excerpt (truncated) if present

    Cap at ~2000 chars — the scoring LLM has finite context and a giant
    blob hurts matching precision (it can't pick out the root cause).
    """
    parts: list[str] = []
    title = finding.get("title") or ""
    if title:
        parts.append(title)
    claim = details.get("claim") or details.get("hypothesis_claim")
    if claim:
        parts.append(f"Claim: {claim}")
    impact = details.get("impact")
    if impact:
        parts.append(f"Impact: {impact}")
    if narrative_path and narrative_path.is_file():
        try:
            narrative = narrative_path.read_text(encoding="utf-8")
            # Skip the markdown header lines, keep the body
            body = "\n".join(
                ln for ln in narrative.splitlines()
                if not ln.startswith("#") and ln.strip()
            )
            parts.append(body[:1200])
        except OSError:
            pass
    blob = "\n\n".join(p for p in parts if p)
    return blob[:2000] if len(blob) > 2000 else blob


# Real-finding statuses (mirrors report.py REAL_STATUSES). Anything not
# in here is dropped from the submission.
_REAL_STATUSES: frozenset[str] = frozenset({
    "confirmed", "disclosed", "fixed", "verified", "closed_not_planned",
})


@click.command(name="export-scabench")
@click.option("--cycle-id", default=None,
              help="Cycle id to export (defaults to ALL real findings).")
@click.option("--target-name", default=None,
              help="Target name to scope by (matches findings.target_id via name).")
@click.option("--project-id", default=None,
              help="ScaBench `project` field in the output JSON. "
                   "Defaults to --target-name or 'jelleo-export'.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Output path. Default: "
                   "<workspace>/submissions/<project>_results.json")
@click.option("--include-info/--no-include-info", default=False,
              show_default=True,
              help="Include Info-severity findings. ScaBench scorer "
                   "drops Info+BestPractices from FP counting, so they "
                   "are safe to include — but they also can't be TPs, "
                   "so they generally just inflate the count.")
@click.option("--narrative-dir", type=click.Path(path_type=Path), default=None,
              help="Directory of per-finding narrative markdown "
                   "(default: <workspace>/findings/finding_<id>.md). "
                   "If found, the narrative body is appended to each "
                   "finding's description.")
@click.pass_context
def export_scabench_cmd(
    ctx: click.Context,
    cycle_id: str | None,
    target_name: str | None,
    project_id: str | None,
    output: Path | None,
    include_info: bool,
    narrative_dir: Path | None,
) -> None:
    """Export confirmed findings as ScaBench / AuditAgent JSON.

    Drop-in for any external evaluator that uses Nethermind's scoring
    algorithm. Used as the submission step for the OtterSec pilot.

    Examples:

      \b
      # Single-cycle export, default location, default project name
      audit-pipeline -w ./ws export-scabench --cycle-id 20260513-0930

      # Scope to one target, custom output path
      audit-pipeline -w ./ws export-scabench \\
          --target-name osec-solana-small \\
          --project-id solana-small \\
          --output submissions/solana-small_results.json
    """
    workspace = Path(ctx.obj["workspace"])
    db = open_findings_db(workspace)

    findings = db.list_findings(limit=2000)

    # Resolve target id from name if requested
    target_id: int | None = None
    if target_name:
        for t in db.list_targets():
            if t.get("name") == target_name:
                target_id = int(t["id"])
                break
        if target_id is None:
            raise click.ClickException(
                f"No target found with name '{target_name}' in this workspace's DB"
            )

    selected: list[dict] = []
    for f in findings:
        if cycle_id and f.get("cycle_id") != cycle_id:
            continue
        if target_id is not None and int(f.get("target_id") or -1) != target_id:
            continue
        status = (f.get("status") or "").lower()
        if status not in _REAL_STATUSES:
            continue
        sev = (f.get("severity") or "Medium")
        if not include_info and sev == Severity.INFO.value:
            continue
        selected.append(f)

    # Default project id derivation
    if not project_id:
        project_id = target_name or "jelleo-export"

    # Build the ScaBench findings array
    out_findings: list[dict[str, Any]] = []
    narrative_root = narrative_dir or (workspace / "findings")
    for f in selected:
        try:
            details = json.loads(f.get("details_json") or "{}")
        except json.JSONDecodeError:
            details = {}
        nid = f.get("id")
        narrative_path = narrative_root / f"finding_{nid}.md"
        out_findings.append({
            "title": f.get("title") or f.get("hypothesis_id") or "(untitled)",
            "description": _description_for(f, details, narrative_path),
            "severity": _scabench_severity(f.get("severity")),
            "location": _location_for(f, details),
            "file": _file_for(f, details),
        })

    payload = {
        "project": project_id,
        "files_analyzed": len({(d.get("target_file") or "") for d in [
            json.loads(f.get("details_json") or "{}") for f in selected
        ] if d.get("target_file")}),
        "total_findings": len(out_findings),
        "findings": out_findings,
    }

    # Default output location
    if output is None:
        out_dir = workspace / "submissions"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{project_id}_results.json"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)

    output.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    console.print(
        f"[green]wrote[/green] {output} "
        f"({len(out_findings)} findings · project={project_id})"
    )

    # Print a quick severity breakdown so the operator can sanity-check
    sev_counts: dict[str, int] = {}
    for f in out_findings:
        s = f["severity"]
        sev_counts[s] = sev_counts.get(s, 0) + 1
    if sev_counts:
        parts = [f"{n} {s}" for s, n in sorted(sev_counts.items())]
        console.print(f"[dim]  by severity: {', '.join(parts)}[/dim]")
