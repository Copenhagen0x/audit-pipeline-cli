"""Tests for the severity explicit-override cap (cross-cutting Defect 04).

A YAML's explicit ``severity: Critical`` previously short-circuited the
empirical derivation. Now it can only LOWER, not RAISE, beyond what
poc_fired / verdict / class signals support.
"""

from audit_pipeline.severity import Severity, derive_severity


class TestSeverityCap:
    def test_explicit_critical_when_poc_fired_critical_class_passes(self):
        sev = derive_severity(
            hypothesis_class="invariant_property",
            verdict="TRUE", poc_fired=True, debate_promoted=False,
            explicit="Critical",
        )
        assert sev == Severity.CRITICAL

    def test_explicit_low_lowers_intrinsic_high(self):
        """Author can lower severity (e.g. they think the impact is muted)."""
        sev = derive_severity(
            hypothesis_class="state_transition",
            verdict="TRUE", poc_fired=True, debate_promoted=False,
            explicit="Low",
        )
        assert sev == Severity.LOW

    def test_explicit_critical_on_rejected_no_poc_caps_at_info(self):
        """The yesterday-style attack: YAML claims Critical but verdict is
        REJECTED and poc_fired=False. Intrinsic is INFO; explicit Critical
        is clamped DOWN to INFO."""
        sev = derive_severity(
            hypothesis_class="invariant_property",
            verdict="FALSE", poc_fired=False, debate_promoted=False,
            explicit="Critical",
        )
        assert sev == Severity.INFO

    def test_explicit_high_on_passed_poc_caps_at_medium(self):
        """Author tried to claim High but no PoC fired AND not debate-
        promoted → intrinsic is MEDIUM, explicit High is clamped."""
        sev = derive_severity(
            hypothesis_class="implicit_invariant",
            verdict="TRUE", poc_fired=False, debate_promoted=False,
            explicit="High",
        )
        assert sev == Severity.MEDIUM

    def test_no_explicit_falls_through(self):
        assert derive_severity(
            hypothesis_class="invariant_property",
            verdict="TRUE", poc_fired=True, debate_promoted=False,
        ) == Severity.CRITICAL
