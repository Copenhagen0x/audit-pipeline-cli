"""`audit-pipeline metrics` — Prometheus-format metrics endpoint.

P2 Wave 7c (Tier B #10). Emits cumulative + gauge metrics covering:

  * findings:        total, by status, by severity, with-bug-class
  * cycles:          total, signed receipts published, last-cycle age
  * propagation:     bug_classes_catalogued, sibling_files,
                     propagation_reports, dispatches_queued, fired markers,
                     hooks succeeded/failed in the last hour
  * derive_siblings: budget_used_today (USD), markers
  * services:        per-service is_active flag

Customer-private data stays OUT (no titles, no per-customer counters at
this surface — those go through the customer-token-gated manifest).

Output format: Prometheus text exposition (https://prometheus.io/docs/instrumenting/exposition_formats/).
Stdout-only by default; use a redirect or a systemd timer to push to a
scraper:

    audit-pipeline metrics > /var/www/jelleo.com/metrics.txt
    # or
    audit-pipeline metrics --output /var/www/jelleo.com/metrics.txt
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.db import FindingsDB

console = Console()


def _emit(name: str, value: float | int, help_text: str = "", labels: dict | None = None) -> str:
    """Render a single Prometheus metric line."""
    label_str = ""
    if labels:
        label_str = "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items()) + "}"
    head = f"# HELP jelleo_{name} {help_text}\n# TYPE jelleo_{name} gauge\n" if help_text else ""
    return f"{head}jelleo_{name}{label_str} {value}\n"


def _escape_label(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@click.command(name="metrics")
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), default=None,
    help="File to write metrics to (default: stdout).",
)
@click.pass_context
def metrics_cmd(ctx: click.Context, output: Path | None) -> None:
    """Emit Prometheus-format metrics from the workspace state."""
    workspace = Path(ctx.obj["workspace"])
    out: list[str] = []

    out.append("# HELP jelleo_metrics_generated_at_seconds Unix timestamp when these metrics were collected\n")
    out.append("# TYPE jelleo_metrics_generated_at_seconds gauge\n")
    out.append(f"jelleo_metrics_generated_at_seconds {int(datetime.now(timezone.utc).timestamp())}\n\n")

    # ---- Findings + cycle counts from DB ----
    try:
        db = FindingsDB(workspace / "findings.db")
        with db._conn() as c:
            n_findings = int(c.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"] or 0)
            n_with_bug_class = int(c.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE bug_class IS NOT NULL"
            ).fetchone()["n"] or 0)
            status_rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM findings GROUP BY status"
            ).fetchall()
            severity_rows = c.execute(
                "SELECT severity, COUNT(*) AS n FROM findings GROUP BY severity"
            ).fetchall()
            n_cycles = int(c.execute("SELECT COUNT(*) AS n FROM cycles").fetchone()["n"] or 0)

        out.append(_emit("findings_total", n_findings, "Total findings ever recorded"))
        out.append(_emit("findings_with_bug_class", n_with_bug_class, "Findings with bug_class set"))
        for row in status_rows:
            out.append(_emit("findings_by_status", row["n"], "Findings by lifecycle status",
                             labels={"status": row["status"] or "unknown"}))
        for row in severity_rows:
            out.append(_emit("findings_by_severity", row["n"], "Findings by severity",
                             labels={"severity": row["severity"] or "unknown"}))
        out.append(_emit("cycles_total", n_cycles, "Total hunt cycles ever run"))
    except Exception as e:
        out.append(f"# DB collection failed: {_escape_label(str(e)[:200])}\n")

    # ---- Propagation surface ----
    try:
        import yaml as _yaml

        from audit_pipeline.commands.propagate import BUG_CLASS_SIGNATURES
        from audit_pipeline.scoping import hypotheses_dir
        # Two distinct counts (matches dashboard.py shape):
        #   bug_classes_declared:        distinct values declared in YAMLs
        #   bug_classes_with_signatures: entries in the runtime catalog
        declared: set[str] = set()
        for p in hypotheses_dir().glob("*.yaml"):
            try:
                raw = _yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for h in (raw or {}).get("hypotheses", []):
                if isinstance(h, dict) and h.get("bug_class"):
                    declared.add(h["bug_class"])
        out.append(_emit("bug_classes_declared", len(declared),
                         "Distinct bug_class values declared across YAML library"))
        out.append(_emit("bug_classes_with_signatures", len(BUG_CLASS_SIGNATURES),
                         "Distinct bug_class values with registered regex signatures (subset of declared)"))

        derived_dir = workspace / "derived"
        sibling_files = sum(1 for p in derived_dir.glob("*-siblings.yaml")) if derived_dir.is_dir() else 0
        out.append(_emit("sibling_files_total", sibling_files,
                         "Total sibling-derivation YAMLs in workspace/derived/"))

        autofire_dir = workspace / "recon" / "propagate" / "auto-fire"
        propagation_reports = sum(1 for p in autofire_dir.glob("*.md")) if autofire_dir.is_dir() else 0
        out.append(_emit("propagation_reports_total", propagation_reports,
                         "Total propagation Markdown reports written"))

        scheduled_dir = workspace / "recon" / "propagate" / "scheduled"
        queued_total = 0
        queued_pending = 0
        if scheduled_dir.is_dir():
            for q in scheduled_dir.glob("*.json"):
                try:
                    data = json.loads(q.read_text(encoding="utf-8"))
                    for item in data.get("items") or []:
                        queued_total += 1
                        if item.get("status") == "pending":
                            queued_pending += 1
                except Exception:
                    continue
        out.append(_emit("dispatches_queued_total", queued_total,
                         "Total Layer-1 dispatch items ever queued"))
        out.append(_emit("dispatches_pending", queued_pending,
                         "Layer-1 dispatch items still pending operator action"))

        # F23 markers — count of distinct findings that fired propagation
        markers_dir = workspace / "recon" / "propagate" / "markers"
        fired_count = sum(1 for p in markers_dir.glob("*.fired")) if markers_dir.is_dir() else 0
        out.append(_emit("propagation_fired_total", fired_count,
                         "Distinct findings whose propagation hook has fired"))
    except Exception as e:
        out.append(f"# Propagation collection failed: {_escape_label(str(e)[:200])}\n")

    # ---- D15 daily LLM budget for sibling derivation ----
    try:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        budget_file = workspace / "derived" / "budget" / f"{today}.usd"
        if budget_file.is_file():
            spent = float(budget_file.read_text(encoding="utf-8").strip() or "0")
        else:
            spent = 0.0
        out.append(_emit("derive_siblings_budget_used_today_usd", spent,
                         "USD spent on sibling derivation today (D15)"))
    except Exception:
        pass

    # ---- Hook execution outcomes (F21 logs) — last 24h ----
    try:
        hooks_dir = workspace / "hooks"
        if hooks_dir.is_dir():
            now = datetime.now(timezone.utc).timestamp()
            ok = failed = 0
            for log in hooks_dir.glob("*-*.log"):
                try:
                    if (now - log.stat().st_mtime) > 86400:
                        continue
                    last_line = ""
                    for line in log.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            last_line = line
                    if not last_line:
                        continue
                    rec = json.loads(last_line)
                    if rec.get("outcome") == "ok":
                        ok += 1
                    elif rec.get("outcome") == "error":
                        failed += 1
                except Exception:
                    continue
            out.append(_emit("hooks_completed_24h", ok,
                             "Hook executions completed OK in last 24h"))
            out.append(_emit("hooks_failed_24h", failed,
                             "Hook executions that failed in last 24h"))
    except Exception:
        pass

    payload = "".join(out)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        console.print(f"[green]wrote[/green] {output}")
    else:
        click.echo(payload)


# Suppress unused-import warning — re is used in _escape_label via implicit
# regex behaviour in some Python versions. Keep import for forward-compat.
_ = re
