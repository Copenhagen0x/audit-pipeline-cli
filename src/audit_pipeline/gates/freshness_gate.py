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
import re
import time
from datetime import datetime, timezone
from pathlib import Path

# P11 R4 (goober LOW + threat-modeler LOW): hoisted from inside the
# except-block. `requests` is a hard dependency (pulled in
# unconditionally by `audit_pipeline.utils.github`); the lazy import
# was guarding a never-occurring `ImportError`. Importing at module
# scope removes a silent-downgrade attack surface where a 404 could
# be misclassified as transient if the inner `import` somehow failed.
import requests  # noqa: E402  # used in the foreign-SHA classifier

from audit_pipeline.gates import GateResult

# P11 R2: SHA-1 hex pattern — module-scope so callers can validate
# without re-importing `re` inside hot paths. 40 lowercase hex chars,
# anchored via fullmatch().
_RE_SHA1_40 = re.compile(r"[0-9a-f]{40}")


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
        # P11 R1 (threat-modeler HIGH): a missing component (no key, or
        # `engine: {}`) previously returned `status: missing-config`
        # which `check_freshness` then ignored — `stale=[]`,
        # `unreachable=[]`, fell through to `passed=True`. That meant
        # silently deleting a workspace.json entry bypassed the gate.
        # Treat missing-config as a hard config error so it routes
        # through the `FreshnessConfigError` path → gate returns
        # passed=False with an actionable message.
        raise FreshnessConfigError(
            f"{component}: missing or empty section in workspace.json — "
            f"add `{component}: {{repo: ..., sha: <40-char SHA>}}` or "
            f"remove the gate from the cycle config."
        )
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

    # Patch #11 (audit CRITICAL 0efd25c3): the previous prefix-match
    # check `pinned.startswith(head_sha[:len(pinned)])` was trivially
    # spoofable. An attacker who could submit a pin of length 7 (e.g.
    # `abcdef0`) needed only to brute-force a commit whose 40-char SHA
    # had ANY 7-char prefix matching their target (~10^9 combinations
    # for a real upstream — easily reachable via auto-generated commits
    # in a fork). The 7-prefix would compare equal and the freshness
    # gate would PASS for an attacker-controlled commit. Round-1:
    # require an EXACT 40-character SHA match. Pin must be the full
    # SHA — short pins are explicitly rejected so the operator can't
    # accidentally bypass the gate by typing a short hash.
    # P11 R1+R2 (code-reviewer + goober + threat-modeler): validate the
    # pin is a 40-char hex SHA-1 BEFORE any comparison. Short pins,
    # non-hex pins (tag names, branch names), AND empty pins are all
    # hard config errors — not "fall through to time-based fallback"
    # which would silently degrade the gate to date-proximity.
    #
    # R2 (goober CRITICAL): the R1 guard `if _pin_norm and not
    # fullmatch(...)` had a bypass — when `_pin_norm` was `""` the
    # short-circuit skipped the guard entirely. An operator with
    # `engine: {sha: "", repo: ...}` (key present, value blank) hit
    # the time-based fallback, and if HEAD was within the staleness
    # window the gate returned PASSED. R2 fix: unconditional guard.
    #
    # SHA-256 (64 chars) is not yet wired here; revisit when git
    # transitions in the wider ecosystem.
    _pin_norm = (pinned or "").lower().strip()
    _head_norm = (head_sha or "").lower().strip()
    if not _RE_SHA1_40.fullmatch(_pin_norm):
        raise FreshnessConfigError(
            f"{component}: pinned SHA must be exactly 40 lowercase hex "
            f"chars (SHA-1); got {pinned!r} (length={len(_pin_norm)}). "
            f"Run `audit-pipeline freshness --update` to refresh the "
            f"pin to a full SHA, or correct workspace.json manually."
        )
    if _pin_norm == _head_norm:
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
    except Exception as _e_pin:
        # P11 R2 (threat-modeler MEDIUM #4): the pinned SHA passed the
        # 40-char-hex format guard but doesn't resolve in the upstream
        # repo. A foreign SHA from a different repo, or a force-pushed
        # commit that no longer exists, falls here. The previous code
        # silently degraded to `(now - head_dt)` which returned `fresh`
        # for any recent HEAD — bypassing the pin verification.
        #
        # P11 R3 (all 3 reviewers): R2 caught EVERY exception and
        # raised FreshnessConfigError, which incorrectly turned
        # transient network failures (timeout, 5xx, 429 rate-limit)
        # into hard cycle aborts with a misleading "force-pushed
        # branch" error. Distinguish:
        #   - HTTPError 4xx PERMANENT (404 not-found, 401 bad-token,
        #     403 forbidden / private repo) → hard FAIL (ConfigError);
        #     these can NEVER self-heal on retry against the same
        #     workspace config + GitHub state.
        #   - any other exception (timeout, 5xx, connection-reset,
        #     429 rate-limit) → transient, re-raise so the outer
        #     handler records `status: unreachable` and the gate
        #     returns `passed=None` (caller may retry).
        # P11 R4 (threat-modeler LOW + goober LOW): `requests` is now
        # imported at module scope (it's already a hard dep via
        # github.py), eliminating the silent-downgrade attack window
        # the lazy import created.
        _is_permanent_4xx = False
        _code: int | None = None
        if isinstance(_e_pin, requests.HTTPError):
            _resp = getattr(_e_pin, "response", None)
            if _resp is not None:
                _code = getattr(_resp, "status_code", None)
                # 404 = SHA not in repo (foreign / force-pushed).
                # 401 = bad/expired/missing token — operator must
                #       rotate, not retry.
                # 403 = ambiguous: could be access-denied (permanent)
                #       OR GitHub secondary rate-limit (transient,
                #       Retry-After header present). Inspect headers
                #       to distinguish. R4 originally classified all
                #       403 as permanent; R5 narrows to the headerless
                #       case after goober + threat-modeler flagged the
                #       secondary-rate-limit false-positive.
                if _code in (401, 404):
                    _is_permanent_4xx = True
                elif _code == 403:
                    _headers = getattr(_resp, "headers", None) or {}
                    # If Retry-After or x-ratelimit-remaining: 0 is
                    # present, treat as transient (rate-limit).
                    # Otherwise treat as permanent (access-denied).
                    _retry_after = _headers.get("Retry-After")
                    _ratelimit_remaining = _headers.get("x-ratelimit-remaining")
                    if _retry_after is not None or _ratelimit_remaining == "0":
                        _is_permanent_4xx = False  # transient — re-raise
                    else:
                        _is_permanent_4xx = True
        if _is_permanent_4xx:
            raise FreshnessConfigError(
                f"{component}: pinned SHA {pinned[:10]}… does not resolve "
                f"in upstream repo (HTTP {_code}). The pin may be from a "
                f"force-pushed branch, a fork, or a private/deleted repo. "
                f"If you see HTTP 401, the GITHUB_TOKEN env var is missing, "
                f"expired, or lacks scope — rotate the token. Otherwise "
                f"run `audit-pipeline freshness --update` to refresh the "
                f"pinned SHA."
            ) from _e_pin
        # Transient — re-raise to the outer handler in check_freshness,
        # which records `status: unreachable` → passed=None (retry).
        raise

    if head_dt and pinned_dt:
        stale_hours = max(0.0, (head_dt - pinned_dt).total_seconds() / 3600.0)
    else:
        # Either head_dt or pinned_dt missing despite pinned fetch
        # succeeding (partial API response). Treat as stale.
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
    # P11 R2 (threat-modeler LOW): empty `components=()` made the gate
    # return passed=True with no checks. Caller-surface bypass — guard
    # at the API boundary.
    if not components:
        return GateResult(
            passed=False,
            reason="check_freshness called with empty components list",
            duration_s=time.time() - t0,
        )
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
