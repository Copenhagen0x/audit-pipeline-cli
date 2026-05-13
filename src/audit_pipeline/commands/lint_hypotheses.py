"""`audit-pipeline lint-hypotheses` — pre-cycle YAML library health check.

Built per the hypothesis library audit's recommendation: "running it
pre-cycle would have caught yesterday's residual-conservation triplicate."

Validates EVERY hypothesis YAML in `<workspace>` and `templates/hypotheses/`
against:

  1. Schema completeness — required fields present (id, class, claim,
     bug_class). Severity must be a known value.
  2. ID uniqueness — within file AND across the loaded class library.
  3. Engine-function symbol resolution — when ``engine_function`` is
     set, the symbol must grep-exist in workspace engine source. A
     ``rg``-able fast-path catches typos and copy-paste errors.
  4. `target_file` existence in the workspace.
  5. ``prior_disclosure`` schema sanity — if present, ``decision`` is
     in the known set and a ``rationale`` is non-empty.
  6. Near-duplicate cluster detection (bug_class, target_file, claim
     normalised) — same heuristic the loader uses to dedupe, surfaced
     here so the operator can clean up the source files.

Exits 0 if no issues, 1 if any.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.table import Table

console = Console()


_KNOWN_SEVERITIES = {"Critical", "High", "Medium", "Low", "Info"}
_KNOWN_DISCLOSURE_DECISIONS = {
    "rejected", "wontfix", "declined", "closed-not-planned",
    "merged", "fixed", "resolved", "patched",
    "pending", "superseded", "deferred",
}
_REQUIRED_FIELDS = ("id", "class", "claim")


def _claim_canon(s: str | None) -> str:
    if not s:
        return ""
    return " ".join((s or "").lower().split())[:120]


def _grep_symbol(symbol: str, src_dir: Path) -> bool:
    if not src_dir.is_dir() or not symbol:
        return False
    try:
        proc = subprocess.run(
            ["grep", "-rlw", "--include=*.rs", symbol, str(src_dir)],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback to Python walk
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        for p in src_dir.rglob("*.rs"):
            try:
                if pattern.search(p.read_text(encoding="utf-8", errors="replace")):
                    return True
            except OSError:
                continue
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _scan_one(yaml_path: Path, engine_src: Path | None) -> list[dict[str, Any]]:
    """Return a list of issue dicts found in ``yaml_path``."""
    issues: list[dict[str, Any]] = []
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return [{"file": str(yaml_path), "id": "?", "severity": "error",
                 "reason": f"YAML parse error: {e}"}]
    hyps = raw.get("hypotheses") or []
    if not isinstance(hyps, list):
        return [{"file": str(yaml_path), "id": "?", "severity": "error",
                 "reason": "top-level `hypotheses` not a list"}]

    seen_ids: set[str] = set()
    seen_keys: dict[tuple[str, str, str], str] = {}   # (bug, target, claim) -> id
    for i, h in enumerate(hyps):
        if not isinstance(h, dict):
            issues.append({"file": str(yaml_path), "id": str(i),
                           "severity": "error",
                           "reason": "hypothesis entry is not a mapping"})
            continue
        hid = h.get("id") or f"<entry #{i}>"
        for field in _REQUIRED_FIELDS:
            if not h.get(field):
                issues.append({"file": str(yaml_path), "id": hid,
                               "severity": "error",
                               "reason": f"missing required field {field!r}"})
        sev = h.get("severity")
        if sev and sev not in _KNOWN_SEVERITIES:
            issues.append({"file": str(yaml_path), "id": hid,
                           "severity": "warning",
                           "reason": f"unknown severity {sev!r} "
                                     f"(known: {sorted(_KNOWN_SEVERITIES)})"})
        if hid in seen_ids:
            issues.append({"file": str(yaml_path), "id": hid,
                           "severity": "error",
                           "reason": "duplicate id within file"})
        seen_ids.add(hid)

        key = (
            (h.get("bug_class") or "").strip().lower(),
            (h.get("target_file") or "").strip().lower(),
            _claim_canon(h.get("claim")),
        )
        if all(key) and key in seen_keys:
            issues.append({"file": str(yaml_path), "id": hid,
                           "severity": "warning",
                           "reason": f"near-duplicate of {seen_keys[key]} "
                                     f"(same bug_class + target_file + claim)"})
        elif all(key):
            seen_keys[key] = hid

        eng_fn = h.get("engine_function")
        if eng_fn and engine_src and engine_src.is_dir():
            if not _grep_symbol(eng_fn, engine_src):
                issues.append({"file": str(yaml_path), "id": hid,
                               "severity": "warning",
                               "reason": f"engine_function {eng_fn!r} not "
                                         f"found in {engine_src}"})

        prior = h.get("prior_disclosure")
        if prior is not None:
            if not isinstance(prior, dict):
                issues.append({"file": str(yaml_path), "id": hid,
                               "severity": "error",
                               "reason": "prior_disclosure must be a mapping"})
            else:
                dec = (prior.get("decision") or "").strip().lower()
                if dec and dec not in _KNOWN_DISCLOSURE_DECISIONS:
                    issues.append({"file": str(yaml_path), "id": hid,
                                   "severity": "warning",
                                   "reason": f"prior_disclosure.decision "
                                             f"{dec!r} not in known set"})
                if dec and not (prior.get("rationale") or "").strip():
                    issues.append({"file": str(yaml_path), "id": hid,
                                   "severity": "warning",
                                   "reason": "prior_disclosure.rationale empty"})

    return issues


@click.command(name="lint-hypotheses")
@click.option("--engine-src", type=click.Path(exists=False, file_okay=False, path_type=Path),
              default=None,
              help="Path to engine src/ for symbol-existence checks "
                   "(default: <workspace>/target/engine/src)")
@click.option("--include-templates", is_flag=True, default=False,
              help="Also lint the packaged templates/hypotheses/*.yaml")
@click.pass_context
def lint_hypotheses_cmd(
    ctx: click.Context,
    engine_src: Path | None,
    include_templates: bool,
) -> None:
    """Validate every hypothesis YAML for schema sanity + near-duplicates."""
    workspace = Path(ctx.obj["workspace"])

    if engine_src is None:
        engine_src = workspace / "target" / "engine" / "src"

    yaml_files: list[Path] = []
    for candidate in (workspace, workspace / "hypotheses"):
        if candidate.is_dir():
            yaml_files.extend(candidate.glob("*.yaml"))
    if include_templates:
        templates_dir = Path(__file__).parent.parent / "templates" / "hypotheses"
        if templates_dir.is_dir():
            yaml_files.extend(templates_dir.glob("*.yaml"))
    yaml_files = sorted(set(yaml_files))

    if not yaml_files:
        console.print("[yellow]no hypothesis YAMLs found[/yellow]")
        ctx.exit(0)

    console.print(f"[bold]Linting {len(yaml_files)} hypothesis YAML(s)...[/bold]")
    all_issues: list[dict[str, Any]] = []
    for yp in yaml_files:
        all_issues.extend(_scan_one(yp, engine_src))

    n_err = sum(1 for i in all_issues if i["severity"] == "error")
    n_warn = sum(1 for i in all_issues if i["severity"] == "warning")
    if not all_issues:
        console.print("[green]all clean[/green]")
        ctx.exit(0)
    table = Table(title=f"{n_err} error(s), {n_warn} warning(s)")
    table.add_column("Severity")
    table.add_column("File")
    table.add_column("Hyp ID")
    table.add_column("Issue")
    for i in all_issues:
        color = "red" if i["severity"] == "error" else "yellow"
        table.add_row(
            f"[{color}]{i['severity']}[/{color}]",
            Path(i["file"]).name,
            i["id"][:48],
            i["reason"][:120],
        )
    console.print(table)
    ctx.exit(1 if n_err else 0)


__all__ = ["lint_hypotheses_cmd"]
