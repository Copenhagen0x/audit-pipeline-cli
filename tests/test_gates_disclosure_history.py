"""Tests for ``audit_pipeline.gates.disclosure_history`` (Gate 5)."""

from __future__ import annotations

import pytest

from audit_pipeline.gates.disclosure_history import (
    check_disclosure_history,
    filter_hypotheses_by_disclosure_history,
)


class TestCheckDisclosureHistory:
    def test_no_prior_disclosure_passes(self):
        h = {"id": "F1", "claim": "x"}
        r = check_disclosure_history(h)
        assert r.passed is True
        assert "no prior" in r.reason

    def test_prior_rejected_no_revisit_fails(self):
        h = {
            "id": "H1-residual-conservation",
            "prior_disclosure": {
                "pr": "https://github.com/aeyakovenko/percolator-prog/pull/39",
                "decision": "rejected",
                "rationale": "insurance absorption intentionally remains in residual...",
            },
        }
        r = check_disclosure_history(h)
        assert r.passed is False
        assert "rejected" in r.reason
        assert "revisit_justification" in r.reason
        assert r.details["decision"] == "rejected"

    def test_prior_rejected_with_revisit_passes(self):
        h = {
            "id": "H1-r2",
            "prior_disclosure": {
                "pr": "owner/repo#39",
                "decision": "rejected",
                "rationale": "previous rationale",
            },
            "revisit_justification": (
                "new commit abc123 invalidates the prior rationale because it removes"
                " the A1 regression coverage that the team relied on"
            ),
        }
        r = check_disclosure_history(h)
        assert r.passed is True
        assert "revisit_justification is present" in r.reason

    def test_prior_merged_returns_skip(self):
        h = {
            "id": "F5-T1",
            "prior_disclosure": {
                "pr": "owner/repo#commit-397be0d",
                "decision": "merged",
                "rationale": "fixed upstream",
            },
        }
        r = check_disclosure_history(h)
        assert r.passed is None      # SKIP
        assert "merged" in r.reason
        assert "upstream" in r.reason

    def test_prior_fixed_returns_skip(self):
        h = {"id": "X", "prior_disclosure": {"decision": "fixed"}}
        r = check_disclosure_history(h)
        assert r.passed is None

    def test_prior_pending_passes(self):
        h = {"id": "X", "prior_disclosure": {"pr": "p#1", "decision": "pending"}}
        r = check_disclosure_history(h)
        assert r.passed is True
        assert "in flight" in r.reason

    def test_unknown_decision_fails(self):
        h = {"id": "X", "prior_disclosure": {"decision": "not-a-thing"}}
        r = check_disclosure_history(h)
        assert r.passed is False
        assert "not in the known set" in r.reason

    def test_missing_decision_fails(self):
        h = {"id": "X", "prior_disclosure": {"pr": "p"}}
        r = check_disclosure_history(h)
        assert r.passed is False
        assert "missing required" in r.reason

    def test_non_dict_prior_disclosure_fails(self):
        h = {"id": "X", "prior_disclosure": "https://example.com/pr/1"}
        r = check_disclosure_history(h)
        assert r.passed is False
        assert "must be a mapping" in r.reason

    def test_case_insensitive_decision(self):
        h = {"id": "X", "prior_disclosure": {"decision": "REJECTED"}}
        r = check_disclosure_history(h)
        assert r.passed is False    # rejected with no revisit

    def test_aliases_work(self):
        # 'wontfix' and 'declined' are aliases for rejected
        for alias in ("wontfix", "declined", "closed-not-planned"):
            r = check_disclosure_history(
                {"id": "X", "prior_disclosure": {"decision": alias}}
            )
            assert r.passed is False, alias


class TestFilterHypothesesByDisclosureHistory:
    def test_splits_allowed_and_blocked(self):
        hyps = [
            {"id": "A", "claim": "ok"},
            {"id": "B", "prior_disclosure": {"decision": "rejected"}},
            {"id": "C", "prior_disclosure": {"decision": "merged"}},
            {"id": "D", "prior_disclosure": {"decision": "pending"}},
        ]
        allowed, blocked = filter_hypotheses_by_disclosure_history(hyps)
        assert {h["id"] for h in allowed} == {"A", "D"}
        # B (rejected, no revisit) → blocked with passed=False
        # C (merged)              → blocked with passed=None
        blocked_ids = {h["id"]: gr.passed for h, gr in blocked}
        assert blocked_ids == {"B": False, "C": None}

    def test_empty_list(self):
        a, b = filter_hypotheses_by_disclosure_history([])
        assert a == [] and b == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
