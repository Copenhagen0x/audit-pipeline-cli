"""Tests for the canonical hypothesis slug utility."""

from audit_pipeline.utils.slug import MAX_SLUG_LEN, slug_for_hypothesis


class TestSlugForHypothesis:
    def test_simple(self):
        assert slug_for_hypothesis("H1-residual-conservation") == "h1_residual_conservation"

    def test_short_id(self):
        assert slug_for_hypothesis("F7") == "f7"

    def test_unicode_drops(self):
        assert slug_for_hypothesis("café-test") == "caf_test"

    def test_empty_returns_default(self):
        assert slug_for_hypothesis("") == "hunt_finding"
        assert slug_for_hypothesis(None or "") == "hunt_finding"

    def test_truncates_at_60(self):
        long = "a" * 200
        out = slug_for_hypothesis(long)
        assert len(out) <= MAX_SLUG_LEN
        assert out == "a" * 60

    def test_strips_leading_trailing_underscore(self):
        assert slug_for_hypothesis("--abc--") == "abc"

    def test_hunt_and_poc_llm_use_same_slug(self):
        """L1.5+L2 audit Defect 03: hunt._slugify and poc_llm._slugify
        previously diverged on long IDs, breaking the test-name binding
        between author and runner. Confirm they now produce identical
        output for any hyp_id."""
        from audit_pipeline.commands import hunt as hunt_mod
        from audit_pipeline.commands import poc_llm as poc_mod
        for hyp in [
            "H1",
            "H1-residual-conservation",
            "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
            "A1-anchor-v2-default-feature-shipped-against-the-full-engine-and-wrapper",
        ]:
            assert hunt_mod._slugify(hyp) == poc_mod._slugify(hyp), hyp
            assert hunt_mod._slugify(hyp) == slug_for_hypothesis(hyp)
