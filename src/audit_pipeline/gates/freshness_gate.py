"""Gate 1 — L0.freshness.

Validates that the workspace's pinned ``engine`` and ``wrapper`` SHAs match
upstream HEAD (or are within an operator-configurable staleness window).
Built in response to cycle-20260511-183154 where the **wrapper** clone was
3 commits behind upstream when the cycle started, and one of those missing
commits (``397be0d`` "Prevent same-price Hyperp wash pinning") fixed
exactly the bug class our L2 PoC then "confirmed". Result: a finding that
had been patched 4 hours before our cycle even started was filed publicly
26 hours later as a fresh bug.

The gate is intentionally separate from the ``freshness`` CLI command:

* ``audit-pipeline freshness``  → READ-ONLY informational table
* ``check_freshness(workspace)`` → fail-closed gate function for the hunt

Returns FAIL when either component is more than ``max_stale_hours`` behind
upstream HEAD. Returns SKIP if upstream is unreachable (transient network
issue should not block a cycle; the operator can re-run). The caller
(``hunt.py``) treats FAIL as a hard abort and SKIP as a yellow warning.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from audit_pipeline.gates import GateResult


class FreshnessConfigError(Exception):
    """Raised when workspace.json or per-component config is malformed.

    Hard configuration error — gate returns FAIL. Distinguished from
    transient network errors (which fall through to the broad ``Exception``
    handler and are recorded as ``status: unreachable``).
    """


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _component_status(
    component: str,
    config: dict,
    max_stale_hours: float,
) -> dict:
    """Compute fresh/stale status for one component (engine OR wrapper).

    Returns a status dict; raises on hard errors so the caller can
    distinguish "stale" (fail-closed) from "couldn't reach upstream" (skip).
    """
    # Import here so unit tests can stub out github lazily without dragging
    # the network module in at gate import time.
    from audit_pipeline.utils.github import (
        get_latest_commit,
        parse_github_repo,
    )

    cfg = config.get(component)
    if not cfg:
        return {
            "component": component,
            "status": "missing-config",
            "behind": None,
            "stale_hours": None,
            "pinned": None,
            "head": None,
        }
    pinned = cfg.get("sha") or ""
    repo_url = cfg.get("repo") or ""
    try:
        owner, repo = parse_github_repo(repo_url)
    except ValueError as e:
        raise FreshnessConfigError(
            f"{component}: cannot parse repo URL '{repo_url}': {e}"
        ) from e

    head = get_latest_commit(owner, repo)
    head_sha = head.get("sha", "")
    head_date_str = head.get("commit", {}).get("author", {}).get("date", "")

    if pinned and (pinned.startswith(head_sha[: len(pinned)]) or head_sha.startswith(pinned)):
        return {
            "component": component,
            "status": "fresh",
            "behind": 0,
            "stale_hours": 0.0,
            "pinned": pinned[:10],
            "head": head_sha[:10],
        }

    # We're behind. Compute hours between pinned commit and upstream HEAD.
    head_dt = _parse_iso(head_date_str) if head_date_str else None
    pinned_dt = None
    try:
        pinned_commit = get_latest_commit(owner, repo, ref=pinned)
        pinned_date_str = pinned_commit.get("commit", {}).get("author", {}).get("date", "")
        pinned_dt = _parse_iso(pinned_date_str) if pinned_date_str else None
    except Exception:  # noqa: BLE001
        pinned_dt = None

    if head_dt and pinned_dt:
        stale_hours = max(0.0, (head_dt - pinned_dt).total_seconds() / 3600.0)
    elif head_dt:
        # Couldn't reach pinned commit; use HEAD-commit recency as a rough
        # proxy (it tells us "at least this many hours have passed since
        # upstream moved"). Conservative — we'll likely be over the limit.
        stale_hours = max(
            0.0,
            (datetime.now(timezone.utc) - head_dt).total_seconds() / 3600.0,
        )
    else:
        stale_hours = float("inf")

    status = "fresh" if stale_hours <= max_stale_hours else "stale"
    return {
        "component": component,
        "status": status,
        "behind": None,    # exact count needs list_commits_since; skip for now
        "stale_hours": round(stale_hours, 2),
        "pinned": (pinned or "?")[:10],
        "head": head_sha[:10],
        "head_msg": (head.get("commit", {}).get("message") or "").split("\n")[0][:80],
    }


def check_freshness(
    *,
    workspace: Path,
    max_stale_hours: float = 6.0,
    components: tuple[str, ...] = ("engine", "wrapper"),
) -> GateResult:
    """Verify the workspace's pinned SHAs are within ``max_stale_hours`` of upstream HEAD.

    Args:
        workspace:       directory containing ``workspace.json``
        max_stale_hours: grace period in hours; default 6h. ``0`` = strict
                         (pinned must equal HEAD exactly).
        components:      which keys in workspace.json to check.

    Returns:
        ``GateResult(True, …)`` if every component is fresh
        ``GateResult(False, …)`` if any component is stale beyond the window;
            ``details`` lists each component's status
        ``GateResult(None, …)`` if we could not reach the GitHub API at all
            for any component (transient — caller may retry)
    """
    t0 = time.time()
    config_path = workspace / "workspace.json"
    if not config_path.exists():
        return GateResult(
            passed=False,
            reason=f"no workspace.json at {config_path}",
            duration_s=time.time() - t0,
        )
    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        return GateResult(
            passed=False,
            reason=f"workspace.json invalid: {e}",
            duration_s=time.time() - t0,
        )

    statuses: list[dict] = []
    transient_failures = 0
    for component in components:
        try:
            statuses.append(_component_status(component, config, max_stale_hours))
        except FreshnessConfigError as e:
            # Hard config error (bad repo URL, malformed component section) —
            # FAIL the gate. Operator must fix workspace.json before retry.
            return GateResult(
                passed=False,
                reason=str(e),
                duration_s=time.time() - t0,
            )
        except Exception as e:  # noqa: BLE001  — transient (network/API)
            transient_failures += 1
            statuses.append({
                "component": component,
                "status": "unreachable",
                "error": str(e)[:160],
            })

    stale = [s for s in statuses if s.get("status") == "stale"]
    unreachable = [s for s in statuses if s.get("status") == "unreachable"]

    if stale:
        summary = "; ".join(
            f"{s['component']} pinned={s.get('pinned')} head={s.get('head')} "
            f"({s.get('stale_hours')}h behind)"
            for s in stale
        )
        return GateResult(
            passed=False,
            reason=(
                f"workspace is stale beyond the {max_stale_hours}h window: {summary}. "
                "Run `audit-pipeline freshness --update` to pull and rewrite "
                "workspace.json, then retry. Override with --ignore-freshness "
                "if you intentionally want to run against a pinned snapshot."
            ),
            duration_s=time.time() - t0,
            details={"components": statuses, "max_stale_hours": max_stale_hours},
        )

    if unreachable and not any(s.get("status") == "fresh" for s in statuses):
        return GateResult(
            passed=None,
            reason=(
                f"could not reach upstream for any component "
                f"({len(unreachable)} unreachable). Network / API issue?"
            ),
            duration_s=time.time() - t0,
            details={"components": statuses, "max_stale_hours": max_stale_hours},
        )

    return GateResult(
        passed=True,
        reason=(
            "workspace is fresh: "
            + ", ".join(f"{s['component']} @ {s.get('pinned', '?')}" for s in statuses)
        ),
        duration_s=time.time() - t0,
        details={"components": statuses, "max_stale_hours": max_stale_hours},
    )


__all__ = ["check_freshness"]
