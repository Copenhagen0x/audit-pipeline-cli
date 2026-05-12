"""Tests for severity.py auxiliary helpers (parse / emoji / color / definitions).

Cross-cutting audit gap: derive_severity is covered by test_severity_cap
but the Severity enum's parse(), emoji(), color_html(), and DEFINITIONS
table were untested. These are surfaces consumed by the customer-facing
HTML reports + cycle PDFs — silent regressions there mean rendering bugs
in the artefacts customers actually read.
"""

from __future__ import annotations

import pytest

from audit_pipeline.severity import (
    DEFINITIONS,
    Severity,
    color_html,
    derive_severity,
    emoji,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Critical", Severity.CRITICAL),
        ("HIGH", Severity.HIGH),
        ("medium", Severity.MEDIUM),
        ("  low  ", Severity.LOW),
        ("Info", Severity.INFO),
    ],
)
def test_parse_canonicalizes_case_and_whitespace(raw: str, expected: Severity) -> None:
    assert Severity.parse(raw) == expected


def test_parse_unknown_returns_default_medium() -> None:
    """Unknown strings fall back to MEDIUM (safer than INFO — surfaces
    a finding for triage instead of silently burying it)."""
    assert Severity.parse("gibberish") == Severity.MEDIUM


def test_parse_unknown_with_explicit_default() -> None:
    assert Severity.parse("gibberish", default=Severity.LOW) == Severity.LOW


def test_parse_none_returns_default() -> None:
    assert Severity.parse(None) == Severity.MEDIUM
    assert Severity.parse(None, default=Severity.INFO) == Severity.INFO


def test_emoji_covers_every_tier() -> None:
    """Every Severity value must have an emoji — otherwise emoji() raises
    KeyError on render and breaks the cycle report."""
    for sev in Severity:
        e = emoji(sev)
        assert e and isinstance(e, str)


def test_color_html_returns_valid_hex_for_every_tier() -> None:
    """Reports / dashboards consume the color_html() palette. Every tier
    must map to a 7-char hex string starting with #."""
    for sev in Severity:
        c = color_html(sev)
        assert c.startswith("#") and len(c) == 7
        # Each char after # must be a hex digit
        int(c[1:], 16)


def test_definitions_table_covers_every_tier() -> None:
    """The customer-facing definitions table must include every severity
    — missing rows would render a blank box in the report."""
    for sev in Severity:
        assert sev in DEFINITIONS
        assert DEFINITIONS[sev]
        assert isinstance(DEFINITIONS[sev], str)
        assert len(DEFINITIONS[sev]) > 20  # not a placeholder


def test_severity_enum_string_values_are_capitalized() -> None:
    """Downstream code (dashboards, PDFs) renders severity.value directly.
    The values must be Capitalized so the cover-page chips don't look
    like SHOUTED yaml literals."""
    for sev in Severity:
        v = sev.value
        assert v[0].isupper(), f"{sev.name} value {v!r} not capitalized"
        assert v[1:].islower() or v[1:].isalnum()


def test_severity_enum_is_str_so_json_serializable() -> None:
    """Severity inherits from str so json.dumps(sev) works without an
    encoder hook. Confirm the contract."""
    import json
    assert json.dumps(Severity.HIGH) == '"High"'


def test_derive_severity_authorization_class_critical_on_poc() -> None:
    """authorization-class hypotheses that fire a PoC should derive
    CRITICAL — a working auth bypass is a top-tier impact."""
    sev = derive_severity(
        hypothesis_class="authorization",
        verdict="TRUE",
        poc_fired=True,
        debate_promoted=False,
    )
    assert sev == Severity.CRITICAL


def test_derive_severity_debate_promoted_lifts_true_no_poc_to_high() -> None:
    """A debate-promoted TRUE verdict (challenger agreed with proposer)
    is stronger signal than a single-pass TRUE — surfaces as HIGH not
    MEDIUM even without a PoC yet."""
    sev = derive_severity(
        hypothesis_class="state_transition",
        verdict="TRUE",
        poc_fired=False,
        debate_promoted=True,
    )
    assert sev == Severity.HIGH


def test_derive_severity_needs_layer_2_is_low() -> None:
    sev = derive_severity(
        hypothesis_class="state_transition",
        verdict="NEEDS_LAYER_2_TO_DECIDE",
        poc_fired=False,
        debate_promoted=False,
    )
    assert sev == Severity.LOW


def test_derive_severity_false_verdict_is_info() -> None:
    sev = derive_severity(
        hypothesis_class="state_transition",
        verdict="FALSE",
        poc_fired=False,
        debate_promoted=False,
    )
    assert sev == Severity.INFO
