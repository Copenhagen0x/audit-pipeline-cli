"""Hypothesis scoping: load, validate, filter.

The loader filter is what makes the hypothesis library safe to grow at scale.
At 27 protocols and 1500+ hypotheses, dispatching every hypothesis against
every target is incorrect — perp-DEX-only hypotheses should not be tested
against AMMs, and AMM-only hypotheses should not be tested against perps.

This module enforces three filters before dispatch:
    1. applies_to        — hypothesis loads only if the target is in its
                           applies_to list (or applies_to includes '*')
    2. scope_conditions  — hypothesis loads only if every predicate is
                           satisfied by the target's workspace.json config
    3. severity floor    — hypothesis loads only if its declared severity
                           is at or above the cycle's --min-severity

See docs/HYPOTHESIS_SCHEMA.md for the canonical schema reference. See
website/deploy/methodology.html#scoping for the public reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from audit_pipeline.severity import Severity


# Vocabulary of known scope predicates. Hypotheses may declare predicates
# outside this set — the loader records a warning but still allows them
# to run, so the schema stays additive as new protocol shapes onboard.
KNOWN_PREDICATES: set[str] = {
    "has_insurance_pool",
    "has_haircut_accounting",
    "perpetual_funding",
    "uses_pyth_oracle",
    "uses_switchboard_oracle",
    "liquidation_engine",
    "multi_market",
    "clob_orderbook",
    "amm_constant_product",
    "flash_loan",
    "multi_collateral",
    "cross_program_invocation_heavy",
}


# Recognized hypothesis classes (must match severity.derive_severity input).
KNOWN_CLASSES: set[str] = {
    "invariant_property",
    "state_transition",
    "authorization",
    "arithmetic_overflow",
    "implicit_invariant",
}


# id pattern: H<number>-<lowercase-slug-with-dashes>
_ID_RE = re.compile(r"^H\d+-[a-z][a-z0-9-]*$")
_BUG_CLASS_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class SkipReason(str, Enum):
    SCOPED_OUT_APPLIES_TO = "scope_applies_to"
    SCOPED_OUT_CONDITIONS = "scope_conditions"
    SCOPED_OUT_SEVERITY = "min_severity"


class HypothesisValidationError(Exception):
    """Raised when a hypothesis YAML entry fails schema validation."""


@dataclass
class SkippedHypothesis:
    hypothesis_id: str
    reason: SkipReason
    detail: str = ""


@dataclass
class ScopingResult:
    applicable: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[SkippedHypothesis] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def applicable_ids(self) -> list[str]:
        return [h["id"] for h in self.applicable]

    def skip_counts_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            out[s.reason.value] = out.get(s.reason.value, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_hypotheses(yaml_path: Path) -> list[dict[str, Any]]:
    """Load + validate every hypothesis in a YAML file.

    Returns the raw list of hypothesis dicts (with applies_to / scope_conditions
    / bug_class defaulted to permissive values if absent). Raises
    HypothesisValidationError if any required field is missing or malformed.
    """
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    hyps = raw.get("hypotheses") or []
    if not isinstance(hyps, list):
        raise HypothesisValidationError(
            f"{yaml_path}: top-level 'hypotheses' must be a list, got {type(hyps).__name__}"
        )

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, h in enumerate(hyps):
        if not isinstance(h, dict):
            raise HypothesisValidationError(
                f"{yaml_path}: hypothesis #{i} is not a mapping"
            )
        normalized = _normalize_and_validate(h, yaml_path, i)
        if normalized["id"] in seen_ids:
            raise HypothesisValidationError(
                f"{yaml_path}: duplicate hypothesis id {normalized['id']!r}"
            )
        seen_ids.add(normalized["id"])
        out.append(normalized)
    return out


def filter_hypotheses(
    hyps: list[dict[str, Any]],
    target_name: str,
    target_conditions: dict[str, bool] | None = None,
    min_severity: Severity | None = None,
) -> ScopingResult:
    """Apply the 3-step scoping filter to a list of hypotheses.

    Args:
        hyps:              Output of load_hypotheses().
        target_name:       The target's name (e.g. 'percolator', 'drift').
                           Matched case-insensitively against applies_to.
        target_conditions: Mapping of predicate-name -> bool. Predicates
                           absent from the mapping default to False.
                           Pass None for "no scope filtering".
        min_severity:      If provided, hypotheses with severity below this
                           value are skipped. None means no severity filter.

    Returns:
        ScopingResult with `applicable` (list of dicts ready to dispatch),
        `skipped` (list of SkippedHypothesis with reason + detail), and
        `warnings` (free-form strings for unknown predicates etc.).
    """
    result = ScopingResult()
    target_lc = target_name.lower()
    cond = target_conditions or {}

    # Surface warnings on unknown predicates referenced in the library
    for h in hyps:
        for p in h.get("scope_conditions") or []:
            if p not in KNOWN_PREDICATES and p not in cond:
                msg = (
                    f"hypothesis {h['id']} uses unknown predicate {p!r} "
                    f"(treating as False — define it in workspace.json conditions or extend KNOWN_PREDICATES)"
                )
                if msg not in result.warnings:
                    result.warnings.append(msg)

    for h in hyps:
        # 1. applies_to filter
        applies_to_raw = h.get("applies_to") or ["*"]
        applies_to_lc = {a.lower() for a in applies_to_raw}
        if "*" not in applies_to_lc and target_lc not in applies_to_lc:
            result.skipped.append(SkippedHypothesis(
                hypothesis_id=h["id"],
                reason=SkipReason.SCOPED_OUT_APPLIES_TO,
                detail=f"applies_to={sorted(applies_to_lc)}, target={target_lc}",
            ))
            continue

        # 2. scope_conditions filter
        unmet = []
        for p in h.get("scope_conditions") or []:
            if not cond.get(p, False):
                unmet.append(p)
        if unmet:
            result.skipped.append(SkippedHypothesis(
                hypothesis_id=h["id"],
                reason=SkipReason.SCOPED_OUT_CONDITIONS,
                detail=f"unmet={unmet}",
            ))
            continue

        # 3. severity floor
        if min_severity is not None:
            hyp_sev = Severity.parse(h.get("severity"), default=Severity.MEDIUM)
            if _SEVERITY_ORDER[hyp_sev] < _SEVERITY_ORDER[min_severity]:
                result.skipped.append(SkippedHypothesis(
                    hypothesis_id=h["id"],
                    reason=SkipReason.SCOPED_OUT_SEVERITY,
                    detail=f"hyp_severity={hyp_sev.value}, floor={min_severity.value}",
                ))
                continue

        result.applicable.append(h)

    return result


def conditions_from_workspace_config(config: dict[str, Any]) -> dict[str, bool]:
    """Read scope-condition predicates from a target's workspace.json config.

    Recognized keys (each maps to one predicate; absent = False):
        has_insurance_pool, has_haircut_accounting, perpetual_funding,
        uses_pyth_oracle, uses_switchboard_oracle, liquidation_engine,
        multi_market, clob_orderbook, amm_constant_product, flash_loan,
        multi_collateral, cross_program_invocation_heavy.

    The config can declare these flags directly at the top level OR nested
    under a 'conditions' key. The latter is recommended for new workspaces.
    """
    out: dict[str, bool] = {}
    nested = config.get("conditions") or {}
    for k in KNOWN_PREDICATES:
        if k in nested:
            out[k] = bool(nested[k])
        elif k in config:
            out[k] = bool(config[k])
        else:
            out[k] = False
    return out


# ---------------------------------------------------------------------------
# Internal validation
# ---------------------------------------------------------------------------


def _normalize_and_validate(
    h: dict[str, Any],
    yaml_path: Path,
    index: int,
) -> dict[str, Any]:
    """Validate required fields, default optional ones, return normalized dict."""
    where = f"{yaml_path}: hypothesis #{index}"

    # Required: id
    hid = h.get("id")
    if not isinstance(hid, str) or not _ID_RE.match(hid):
        raise HypothesisValidationError(
            f"{where}: 'id' must match {_ID_RE.pattern} (got {hid!r})"
        )

    # Required: class
    klass = h.get("class")
    if klass not in KNOWN_CLASSES:
        raise HypothesisValidationError(
            f"{where} ({hid}): 'class' must be one of {sorted(KNOWN_CLASSES)} "
            f"(got {klass!r})"
        )

    # Required: claim
    claim = h.get("claim")
    if not isinstance(claim, str) or len(claim.strip()) < 20:
        raise HypothesisValidationError(
            f"{where} ({hid}): 'claim' must be a string of at least 20 characters"
        )

    # Optional: severity
    sev_raw = h.get("severity")
    if sev_raw is not None:
        if not isinstance(sev_raw, str) or sev_raw.strip().capitalize() not in {s.value for s in Severity}:
            raise HypothesisValidationError(
                f"{where} ({hid}): 'severity' must be Critical/High/Medium/Low/Info "
                f"(got {sev_raw!r})"
            )

    # Optional: applies_to (default ['*'])
    applies_to = h.get("applies_to")
    if applies_to is None:
        applies_to = ["*"]
    elif not isinstance(applies_to, list) or not all(isinstance(a, str) for a in applies_to):
        raise HypothesisValidationError(
            f"{where} ({hid}): 'applies_to' must be a list of strings"
        )

    # Optional: scope_conditions (default [])
    scope_conditions = h.get("scope_conditions")
    if scope_conditions is None:
        scope_conditions = []
    elif not isinstance(scope_conditions, list) or not all(isinstance(p, str) for p in scope_conditions):
        raise HypothesisValidationError(
            f"{where} ({hid}): 'scope_conditions' must be a list of strings"
        )

    # Optional: bug_class
    bug_class = h.get("bug_class")
    if bug_class is not None:
        if not isinstance(bug_class, str) or not _BUG_CLASS_RE.match(bug_class):
            raise HypothesisValidationError(
                f"{where} ({hid}): 'bug_class' must match {_BUG_CLASS_RE.pattern} "
                f"(got {bug_class!r})"
            )

    # Build normalized dict (preserves anchor fields target_file etc.)
    out = dict(h)
    out["applies_to"] = applies_to
    out["scope_conditions"] = scope_conditions
    if bug_class is not None:
        out["bug_class"] = bug_class
    return out
