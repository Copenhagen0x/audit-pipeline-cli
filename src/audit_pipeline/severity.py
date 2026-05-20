"""Severity rubric for findings.

Five tiers, formal definitions. The rubric is what customers see in
reports and dashboards — keep it stable and auditable.

Severity is assigned in two ways:
  1. Manual: the hypothesis YAML can include a `severity:` field
  2. Automatic: derived from hypothesis class + verdict + PoC outcome
     (see `derive_severity` below)
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INFO = "Info"

    @classmethod
    def parse(cls, raw: str | None, default: Severity = None) -> Severity:
        if raw is None:
            return default or cls.MEDIUM
        s = str(raw).strip().capitalize()
        for member in cls:
            if member.value == s:
                return member
        return default or cls.MEDIUM


# Formal definitions — these go into the customer-facing report.
DEFINITIONS = {
    Severity.CRITICAL: (
        "Direct loss of user funds or full protocol takeover with no "
        "meaningful preconditions. Reachable from a permissionless "
        "instruction by any signer. Must be patched immediately."
    ),
    Severity.HIGH: (
        "Significant loss of user funds or protocol invariant violation "
        "under realistic preconditions (specific market state, signer "
        "with limited but obtainable role). Patch should ship in next "
        "release."
    ),
    Severity.MEDIUM: (
        "Hardening issue, partial loss possible, or invariant violation "
        "requiring privileged signer or improbable state. Worth fixing "
        "in normal cadence."
    ),
    Severity.LOW: (
        "Minor issue with no plausible path to fund loss. Code-quality "
        "or defense-in-depth concern."
    ),
    Severity.INFO: (
        "Informational. No security impact. Documentation or style "
        "suggestion."
    ),
}


def emoji(s: Severity) -> str:
    return {
        Severity.CRITICAL: "🔴",
        Severity.HIGH: "🟠",
        Severity.MEDIUM: "🟡",
        Severity.LOW: "🔵",
        Severity.INFO: "⚪",
    }[s]


def color_html(s: Severity) -> str:
    return {
        Severity.CRITICAL: "#dc2626",
        Severity.HIGH: "#ea580c",
        Severity.MEDIUM: "#ca8a04",
        Severity.LOW: "#2563eb",
        Severity.INFO: "#6b7280",
    }[s]


# FIX 3 (2026-05-14): bug-class -> minimum-severity floor table.
#
# Operator caught severe under-rating on aptos-medium 2026-05-14:
#   - APTM20 (user loses entire stake): LLM rated Low. Should be Critical.
#   - APTM21 (full protocol admin takeover): LLM rated Low. Should be Critical.
# After the LLM produces a verdict + we derive intrinsic severity, we
# upgrade to MAX(intrinsic, floor[bug_class]) so the empirical signal
# only raises severity, never lowers it. Keys are lowercased substring
# matches against the hyp's `bug_class` field.
#
# borrow-global-no-auth is special-cased: Critical when target_file
# mentions treasury/vault, else High. See severity_floor_for_bug_class.
SEVERITY_FLOOR_BY_BUG_CLASS: dict[str, Severity] = {
    "treasury-drain": Severity.CRITICAL,
    "treasury_drain": Severity.CRITICAL,
    "acl-mint-cap-permissionless": Severity.CRITICAL,
    "acl_mint_cap_permissionless": Severity.CRITICAL,
    "cap-leak": Severity.CRITICAL,
    "cap_leak": Severity.CRITICAL,
    "share-inflation-first-depositor": Severity.HIGH,
    "share_inflation_first_depositor": Severity.HIGH,
    "emergency-unstake-principal-lost": Severity.HIGH,
    "emergency_unstake_principal_lost": Severity.HIGH,
    "oracle-update-no-auth": Severity.HIGH,
    "oracle_update_no_auth": Severity.HIGH,
    "staking-fund-rewards-no-auth": Severity.HIGH,
    "staking_fund_rewards_no_auth": Severity.HIGH,
    "withdraw-delay-bypass": Severity.HIGH,
    "withdraw_delay_bypass": Severity.HIGH,
}


def severity_floor_for_bug_class(
    bug_class: str | None,
    target_file: str | None = None,
) -> Severity | None:
    """Return the minimum severity for a hyp given its bug_class.

    Returns None if no floor applies (LLM rating stands).
    """
    if not bug_class:
        return None
    bc = str(bug_class).lower().strip()
    # Special case: borrow-global-no-auth + treasury/vault target -> Critical.
    if "borrow-global-no-auth" in bc or "borrow_global_no_auth" in bc:
        tf = (target_file or "").lower()
        if "treasury" in tf or "vault" in tf:
            return Severity.CRITICAL
        return Severity.HIGH
    for key, floor in SEVERITY_FLOOR_BY_BUG_CLASS.items():
        if key in bc:
            return floor
    return None


def derive_severity(
    hypothesis_class: str,
    verdict: str,
    poc_fired: bool,
    debate_promoted: bool,
    explicit: str | None = None,
    bug_class: str | None = None,
    target_file: str | None = None,
) -> Severity:
    """Derive severity from hypothesis metadata + cycle outcome.

    Order of precedence:
      1. Explicit `severity:` field on the hypothesis
      2. PoC fired + invariant_property class -> CRITICAL
      3. PoC fired + arithmetic_overflow class -> HIGH
      4. PoC fired (other classes) -> HIGH
      5. Verdict TRUE / HIGH confidence (no PoC yet) -> MEDIUM
      6. Verdict NEEDS_LAYER_2 -> LOW
      7. Otherwise -> INFO

    After the intrinsic derivation, the result is FLOOR-RAISED by the
    `bug_class` table (FIX 3, 2026-05-14). The floor can only raise,
    never lower, the LLM-derived rating.
    """
    # Cross-cutting audit Defect 04 (HIGH): previously an explicit
    # ``severity: Critical`` in the YAML short-circuited everything. A
    # hypothesis with verdict=REJECTED + poc_fired=False could still
    # surface as Critical on the cover page and customer manifests. The
    # cap below stops authors over-claiming when there's no empirical
    # backing.
    #
    # Refinement (2026-05-17, sol-large audit): the cap was too aggressive
    # when poc_fired=True. PoC firing IS empirical evidence of a real
    # exploit chain, so an explicit Critical claim grounded in a PoC
    # should be honored even when the hypothesis_class is not in the
    # priv-classes set. Otherwise SOL51 (state-machine takeover) and
    # SOL61 (logic inversion) get silently downgraded to High despite
    # both having Layer 4 LiteSVM runtime reproductions. The cap still
    # applies when poc_fired=False.

    def _derive_intrinsic() -> Severity:
        if poc_fired:
            if hypothesis_class == "invariant_property":
                return Severity.CRITICAL
            if hypothesis_class == "authorization":
                return Severity.CRITICAL
            return Severity.HIGH
        if verdict == "TRUE":
            return Severity.HIGH if debate_promoted else Severity.MEDIUM
        if verdict == "NEEDS_LAYER_2_TO_DECIDE":
            return Severity.LOW
        return Severity.INFO

    intrinsic = _derive_intrinsic()
    if explicit:
        requested = Severity.parse(explicit)
        order = list(Severity)
        if poc_fired:
            # Empirical PoC backs the claim — honor explicit verbatim.
            # Author can raise to Critical or lower to Medium / Low; the
            # PoC artifact is the audit trail justifying the choice.
            intrinsic = requested
        elif order.index(requested) >= order.index(intrinsic):
            # No PoC: cap explicit to intrinsic (author can only LOWER).
            intrinsic = requested
        # else: no PoC and tried to claim higher than evidence supports — keep intrinsic.

    # FIX 3 (2026-05-14): raise to bug_class floor if applicable.
    floor = severity_floor_for_bug_class(bug_class, target_file)
    if floor is not None:
        order = list(Severity)
        if order.index(floor) < order.index(intrinsic):
            return floor
    return intrinsic
