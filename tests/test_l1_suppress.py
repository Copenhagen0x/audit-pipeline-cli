"""Tests for the L1 provably-safe suppressor (step 3).

Runnable two ways: `python -m pytest tests/test_l1_suppress.py` OR
`python tests/test_l1_suppress.py` (standalone, prints PASS/FAIL).

The cardinal property under test: this step must NEVER over-suppress. Only `checked_*`
arithmetic-overflow is provably safe; wrapping/saturating/overflowing/unchecked and every
non-arithmetic class stay (in doubt). And nothing is ever deleted — every input candidate
appears in the output exactly once.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.candidates import (  # noqa: E402
    Candidate, CandidateReport, label_repo,
)
from audit_pipeline.l1.suppress import (  # noqa: E402
    SuppressionReport, suppress_candidates, suppress_repo,
)


def _cand(bug_class: str, detail: str = "x", file: str = "a.rs", line: int = 1,
          cls: str = "arithmetic_overflow") -> Candidate:
    return Candidate(surface_type="arithmetic", file=file, line=line, enclosing_fn="f",
                     cls=cls, bug_class=bug_class, claim="c" * 25, severity="High", detail=detail)


def _rep(cands: list[Candidate], status: str = "OK") -> CandidateReport:
    return CandidateReport(status=status, candidates=cands)


def test_checked_arith_is_suppressed():
    for m in ("checked_add", "checked_sub", "checked_mul", "checked_div", "checked_rem",
              "checked_pow", "checked_neg", "checked_shl", "checked_shr", "checked_add_signed"):
        r = suppress_candidates(_rep([_cand("arithmetic-overflow", m)]))
        v = r.verdicts[0]
        assert v.suppressed, m
        assert v.reason and v.rule == "checked-arith", m
        assert r.active() == [], m


def test_unsafe_arith_forms_are_kept():
    # the whole point: NON-checked arithmetic must NOT be dropped (they are real bug patterns).
    for m in ("+", "-", "*", "/", "%", "<<", ">>",
              "wrapping_add", "saturating_add", "saturating_sub", "overflowing_add",
              "unchecked_add", "unchecked_div",
              "pow", "sum", "product"):  # bare pow/sum/product are surfaced too — must be kept
        r = suppress_candidates(_rep([_cand("arithmetic-overflow", m)]))
        assert not r.verdicts[0].suppressed, m
        assert len(r.active()) == 1, m


def test_checked_set_matches_surfaces_arith_methods():
    # guard against drift: the suppressor's checked_* set MUST equal the checked_* subset of
    # surfaces.py's _ARITH_METHODS, or surfaces could emit a checked_* detail we never suppress
    # (harmless) or we could list one surfaces never emits (dead rule). Keep them in lock-step.
    from audit_pipeline.l1.surfaces import _ARITH_METHODS  # noqa: PLC0415
    from audit_pipeline.l1.suppress import _CHECKED_OVERFLOW_SAFE  # noqa: PLC0415
    checked_subset = {m for m in _ARITH_METHODS if m.startswith("checked_")}
    assert checked_subset == _CHECKED_OVERFLOW_SAFE, (checked_subset ^ _CHECKED_OVERFLOW_SAFE)


def test_division_by_zero_never_suppressed_even_on_checked():
    # checked_div returns None on zero, but .unwrap() still panics — keep the div-by-zero
    # candidate (its panic concern is real and distinct from overflow).
    for m in ("checked_div", "checked_rem", "/", "%"):
        r = suppress_candidates(_rep([_cand("division-by-zero", m)]))
        assert not r.verdicts[0].suppressed, m


def test_non_arithmetic_classes_are_kept():
    for bc in ("missing-authorization-check", "unchecked-cpi-account", "oracle-staleness",
               "type-confusion", "cast-truncation", "index-out-of-bounds", "reinitialization"):
        r = suppress_candidates(_rep([_cand(bc, "x", cls="account-validation")]))
        assert not r.verdicts[0].suppressed, bc


def test_nothing_is_deleted():
    # completeness: every input candidate must appear in the output exactly once.
    cands = [_cand("arithmetic-overflow", "checked_add"),  # suppressed
             _cand("arithmetic-overflow", "+"),            # kept
             _cand("division-by-zero", "checked_div"),     # kept
             _cand("missing-owner-check", "x")]            # kept
    r = suppress_candidates(_rep(cands))
    assert len(r.verdicts) == len(cands)
    assert len(r.active()) == 3
    assert len(r.suppressed_verdicts()) == 1


def test_active_excludes_only_suppressed():
    cands = [_cand("arithmetic-overflow", "checked_mul"), _cand("arithmetic-overflow", "*")]
    r = suppress_candidates(_rep(cands))
    active_details = {c.detail for c in r.active()}
    assert active_details == {"*"}


def test_status_carries_and_gates_count():
    r = suppress_candidates(_rep([_cand("arithmetic-overflow", "checked_add")], status="COVERAGE_UNKNOWN"))
    assert r.status == "COVERAGE_UNKNOWN"
    assert r.summary()["total_active"] is None  # untrustworthy count is masked
    assert r.summary()["raw_candidate_count"] == 1


def test_status_validation_rejects_garbage():
    try:
        SuppressionReport(status="NOPE")
    except ValueError:
        return
    raise AssertionError("SuppressionReport must reject an invalid status")


def test_by_rule_counts():
    cands = [_cand("arithmetic-overflow", "checked_add"),
             _cand("arithmetic-overflow", "checked_sub"),
             _cand("arithmetic-overflow", "+")]
    r = suppress_candidates(_rep(cands))
    assert r.by_rule() == {"checked-arith": 2}
    assert "suppressed 2/3" in " ".join(r.notes)


def test_verdict_to_dict_shape():
    r = suppress_candidates(_rep([_cand("arithmetic-overflow", "checked_add"),
                                  _cand("arithmetic-overflow", "+")]))
    supp = next(v for v in r.verdicts if v.suppressed).to_dict()
    kept = next(v for v in r.verdicts if not v.suppressed).to_dict()
    assert supp["suppressed"] is True and supp["suppress_reason"] and supp["suppress_rule"] == "checked-arith"
    assert kept["suppressed"] is False and "suppress_reason" not in kept


def test_empty_reason_rule_is_rejected():
    # a buggy future rule returning "" (instead of None) must raise, not silently over-suppress.
    import audit_pipeline.l1.suppress as supp  # noqa: PLC0415
    saved = supp._SAFE_RULES
    supp._SAFE_RULES = [("bad-rule", lambda c: "")]
    try:
        supp.suppress_candidates(_rep([_cand("arithmetic-overflow", "+")]))
    except ValueError:
        return
    finally:
        supp._SAFE_RULES = saved
    raise AssertionError("an empty-reason rule must raise ValueError, not silently suppress")


def test_deterministic():
    cands = [_cand("arithmetic-overflow", "checked_add"), _cand("arithmetic-overflow", "/")]
    a = suppress_candidates(_rep(cands)).summary()
    b = suppress_candidates(_rep(cands)).summary()
    assert a == b


def test_on_real_repo_reduces_but_keeps_most():
    # end-to-end on a synthetic repo: checked math suppressed, raw math kept.
    src = """
pub fn f(a: u64, b: u64) -> u64 {
    let ok = a.checked_add(b).unwrap();
    let raw = a + b;
    let w = a.wrapping_mul(b);
    ok + raw + w
}
"""
    d = Path(tempfile.mkdtemp())
    (d / "lib.rs").write_text(src, encoding="utf-8")
    r = suppress_repo(d)
    assert r.status == "OK"
    assert r.suppressed_verdicts(), "expected at least the checked_add overflow candidate suppressed"
    # the raw `+`/`wrapping_mul` overflow candidates must survive.
    active_overflow = [c for c in r.active() if c.bug_class == "arithmetic-overflow"]
    assert active_overflow, "raw/wrapping arithmetic overflow candidates must be kept"
    # and every suppression is a checked_* form.
    for v in r.suppressed_verdicts():
        assert v.candidate.detail.startswith("checked_"), v.candidate.detail


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def _run() -> int:
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
