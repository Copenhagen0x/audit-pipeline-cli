"""Tests for the L1 coverage count (step 7).

Runnable: `python -m pytest tests/test_l1_coverage.py` OR `python tests/test_l1_coverage.py`.

Cardinal property: never OVER-claim coverage. A percentage is reported ONLY when both the
denominator (instruction authority) and the numerator (surface scan) are complete; covered
requires a precise (handler_fn, handler_file) match; unknown handler file => conservative miss.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.coverage import (  # noqa: E402
    CoverageReport, coverage, coverage_repo,
)
from audit_pipeline.l1.entrypoints import Entrypoint, EntrypointAuthority  # noqa: E402
from audit_pipeline.l1.surfaces import Surface, SurfaceReport  # noqa: E402


def _auth(instrs, status="OK", kind="native") -> EntrypointAuthority:
    a = EntrypointAuthority(program_kind=kind, status=status)
    for name, handler, hf in instrs:
        a.instructions.append(Entrypoint(instruction=name, handler=handler, handler_file=hf,
                                         handler_line=1, routed=True))
    return a


def _surfs(pairs, status="OK") -> SurfaceReport:
    r = SurfaceReport(status=status)
    for fn, f in pairs:
        r.surfaces.append(Surface(surface_type="arithmetic", file=f, line=1, enclosing_fn=fn,
                                  detail="+", snippet="s"))
    return r


def test_partial_coverage_reports_pct_and_uncovered():
    auth = _auth([("ix_a", "ix_a", "a.rs"), ("ix_b", "ix_b", "a.rs")])
    surf = _surfs([("ix_a", "a.rs")])  # only ix_a has a surface
    rep = coverage(auth, surf)
    assert rep.status == "OK"
    assert {i.instruction for i in rep.covered()} == {"ix_a"}
    assert {i.instruction for i in rep.uncovered()} == {"ix_b"}
    assert rep.summary()["coverage_pct"] == 50.0


def test_authority_unknown_masks_pct_but_lists_uncovered():
    auth = _auth([("ix_a", "ix_a", "a.rs")], status="COVERAGE_UNKNOWN")
    rep = coverage(auth, _surfs([]))
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.summary()["coverage_pct"] is None  # never a % off an unknown denominator
    assert rep.summary()["uncovered"] == ["ix_a"]


def test_surface_incomplete_masks_pct():
    auth = _auth([("ix_a", "ix_a", "a.rs")])
    rep = coverage(auth, _surfs([("ix_a", "a.rs")], status="COVERAGE_INCOMPLETE"))
    assert rep.status == "COVERAGE_INCOMPLETE"
    assert rep.summary()["coverage_pct"] is None


def test_unknown_handler_file_is_conservative_miss():
    auth = _auth([("ix_a", "ix_a", None)])  # handler file unresolved
    rep = coverage(auth, _surfs([("ix_a", "a.rs")]))
    assert rep.uncovered() and rep.uncovered()[0].instruction == "ix_a"


def test_name_collision_in_other_file_does_not_cover():
    auth = _auth([("ix_a", "ix_a", "a.rs")])
    surf = _surfs([("ix_a", "b.rs")])  # same fn NAME, different file
    rep = coverage(auth, surf)
    assert rep.uncovered()[0].instruction == "ix_a", "name-only match would be a false over-claim"


def test_no_instructions_is_unknown():
    rep = coverage(_auth([]), _surfs([("x", "a.rs")]))
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.summary()["coverage_pct"] is None
    assert rep.items == []


def test_surface_count_recorded():
    auth = _auth([("ix_a", "ix_a", "a.rs")])
    surf = _surfs([("ix_a", "a.rs"), ("ix_a", "a.rs"), ("ix_a", "a.rs")])
    rep = coverage(auth, surf)
    assert rep.covered()[0].surface_count == 3


def test_status_validation_rejects_garbage():
    try:
        CoverageReport(status="NOPE")
    except ValueError:
        return
    raise AssertionError("CoverageReport must reject an invalid status")


def test_full_coverage_pct_100():
    auth = _auth([("a", "a", "x.rs"), ("b", "b", "x.rs")])
    rep = coverage(auth, _surfs([("a", "x.rs"), ("b", "x.rs")]))
    assert rep.status == "OK"
    assert rep.summary()["coverage_pct"] == 100.0


def test_coverage_repo_anchor_end_to_end():
    src = """
#[program]
pub mod prog {
    pub fn with_math(ctx: Context<X>, a: u64, b: u64) -> Result<()> {
        let c = a + b;
        let d = c * a;
        Ok(())
    }
    pub fn trivial(ctx: Context<Y>) -> Result<()> { Ok(()) }
}
"""
    d = Path(tempfile.mkdtemp())
    (d / "lib.rs").write_text(src, encoding="utf-8")
    rep = coverage_repo(d)
    assert rep.program_kind == "anchor"
    names = {i.instruction for i in rep.items}
    assert {"with_math", "trivial"} <= names
    # with_math has arithmetic surfaces inside it -> covered
    cov = {i.instruction for i in rep.covered()}
    assert "with_math" in cov, rep.summary()


# ----- review round 1: both-enumerated union (decoy denominator-shrink) + neither branch -----
def test_coverage_repo_both_enumerated_unions_and_is_unknown():
    # decoy attack: a tiny #[program] (1 fn) beside a real native dispatch (3 variants). The
    # denominator must be the UNION (4), not the smaller Anchor set, and status must be UNKNOWN
    # (no % off an ambiguous program kind) — never a silent denominator-shrink.
    src = """
#[program]
pub mod decoy {
    pub fn decoy_ix(ctx: Context<X>) -> Result<()> { Ok(()) }
}

pub enum Instruction { Init, Update, Close }
pub fn process_instruction(ix: Instruction) -> Result<()> {
    match ix {
        Instruction::Init => do_init(),
        Instruction::Update => do_update(),
        Instruction::Close => do_close(),
    }
}
fn do_init() -> Result<()> { Ok(()) }
fn do_update() -> Result<()> { Ok(()) }
fn do_close() -> Result<()> { Ok(()) }
"""
    d = Path(tempfile.mkdtemp())
    (d / "lib.rs").write_text(src, encoding="utf-8")
    rep = coverage_repo(d)
    names = {i.instruction for i in rep.items}
    # if extract_native did not enumerate the dispatch, this fixture can't exercise the branch;
    # guard so the test fails loudly with context rather than silently passing.
    assert {"decoy_ix", "Init", "Update", "Close"} <= names, ("both readers must contribute to the "
                                                              f"union denominator; got {names}")
    assert rep.program_kind == "ambiguous"
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.summary()["coverage_pct"] is None  # no % off an ambiguous denominator
    assert any("ambiguous program kind" in n for n in rep.notes)


def test_coverage_repo_neither_enumerated_is_unknown():
    d = Path(tempfile.mkdtemp())  # empty repo: no .rs files
    rep = coverage_repo(d)
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.items == []
    assert rep.summary()["coverage_pct"] is None


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
