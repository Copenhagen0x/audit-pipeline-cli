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


def derive_severity(
    hypothesis_class: str,
    verdict: str,
    poc_fired: bool,
    debate_promoted: bool,
    explicit: str | None = None,
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
    """
    if explicit:
        return Severity.parse(explicit)

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
