"""Tests for sibling derivation diversity check (P1+P2 audit Defect 06)."""

from audit_pipeline.commands.derive_siblings import _enforce_sibling_diversity


def test_unique_bug_classes_kept():
    siblings = [
        {"id": "S1", "bug_class": "haircut-direction", "claim": "x grows in path A"},
        {"id": "S2", "bug_class": "vault-balance", "claim": "y diverges on rollover"},
        {"id": "S3", "bug_class": "oracle-binding", "claim": "z spoofed via different feed"},
    ]
    out = _enforce_sibling_diversity(siblings)
    assert len(out) == 3


def test_duplicate_bug_class_dropped():
    siblings = [
        {"id": "S1", "bug_class": "haircut-direction", "claim": "x grows in path A"},
        {"id": "S2", "bug_class": "haircut-direction", "claim": "x grows in path B totally different words used here"},
    ]
    out = _enforce_sibling_diversity(siblings)
    # Same bug_class — second dropped even though claim text differs
    assert len(out) == 1
    assert out[0]["id"] == "S1"


def test_near_duplicate_claim_dropped():
    siblings = [
        {"id": "S1", "bug_class": "a", "claim": "the residual cash grows without a corresponding credit obligation when insurance shrinks"},
        {"id": "S2", "bug_class": "b", "claim": "the residual cash grows without a corresponding credit obligation when insurance shrinks slightly"},
    ]
    out = _enforce_sibling_diversity(siblings)
    # Different bug_class but near-identical claim — second dropped
    assert len(out) == 1


def test_empty_siblings_handled():
    assert _enforce_sibling_diversity([]) == []


def test_non_dict_skipped():
    out = _enforce_sibling_diversity([{"id": "S1", "bug_class": "a", "claim": "x"}, "not-a-dict", None])
    assert len(out) == 1
