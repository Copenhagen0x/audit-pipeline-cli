"""Tests for the L1 synthesize step (step 5) + the hunt/cli wiring.

Runnable: `python -m pytest tests/test_l1_synthesize.py` OR `python tests/test_l1_synthesize.py`.

The killer property: every synthesized hypothesis loads through the REAL scoping.load_hypotheses,
so L1's output is guaranteed to feed the existing pipeline unchanged.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.candidates import Candidate  # noqa: E402
from audit_pipeline.l1.synthesize import (  # noqa: E402
    SynthReport,
    synthesize,
    synthesize_repo,
    write_yaml,
)
from audit_pipeline.l1.triage_filter import FilterReport, FilterVerdict  # noqa: E402
from audit_pipeline.scoping import _ID_RE, _normalize_and_validate, load_hypotheses  # noqa: E402


def _cand(bug_class="arithmetic-overflow", file="lib.rs", line=1, claim=None, cls="arithmetic_overflow") -> Candidate:
    return Candidate(surface_type="arithmetic", file=file, line=line, enclosing_fn="f",
                     cls=cls, bug_class=bug_class,
                     claim=claim or f"The arithmetic at line {line} of {file} may overflow here",
                     severity="High", detail="+")


def _report(cands, status="OK", spend=0.0) -> FilterReport:
    r = FilterReport(status=status, spend_usd=spend)
    r.verdicts = [FilterVerdict(c, dropped=False, reason=None) for c in cands]
    return r


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def test_every_hyp_loads_through_real_loader():
    rep = synthesize(_report([_cand(line=1), _cand(line=2), _cand(bug_class="missing-owner-check", line=3, cls="account-validation")]))
    assert rep.hypotheses
    for i, h in enumerate(rep.hypotheses):
        _normalize_and_validate(dict(h), Path("t.yaml"), i)  # raises on any invalid field
    # and the whole written doc round-trips through load_hypotheses
    out = write_yaml(rep, _tmp() / "h.yaml")
    loaded = load_hypotheses(out)
    assert len(loaded) == len(rep.hypotheses)


def test_ids_are_unique_and_valid():
    rep = synthesize(_report([_cand(line=i) for i in range(1, 6)]))
    ids = [h["id"] for h in rep.hypotheses]
    assert len(ids) == len(set(ids)), "ids must be unique"
    for hid in ids:
        assert _ID_RE.match(hid), hid


def test_dedup_collapses_identical_but_keeps_distinct():
    # two identical (bug_class, file, claim) -> 1; a different-line one -> distinct.
    dup_a = _cand(line=1, claim="The arithmetic at line 1 of lib.rs may overflow here")
    dup_b = _cand(line=1, claim="The arithmetic at line 1 of lib.rs may overflow here")
    distinct = _cand(line=2, claim="The arithmetic at line 2 of lib.rs may overflow here")
    rep = synthesize(_report([dup_a, dup_b, distinct]))
    assert len(rep.hypotheses) == 2, [h["claim"] for h in rep.hypotheses]
    assert rep.duplicates_collapsed == 1
    assert rep.raw_kept == 3


def test_status_carries_and_gates_count():
    rep = synthesize(_report([_cand()], status="COVERAGE_UNKNOWN"))
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.summary()["total_hypotheses"] is None
    assert rep.summary()["raw_hypothesis_count"] == 1


def test_status_validation_rejects_garbage():
    try:
        SynthReport(status="NOPE")
    except ValueError:
        return
    raise AssertionError("SynthReport must reject an invalid status")


def test_spend_coerced_never_none():
    rep = SynthReport(status="OK", spend_usd=None)  # type: ignore[arg-type]
    assert rep.spend_usd == 0.0
    assert f"{rep.spend_usd:.4f}" == "0.0000"  # safe for the CLI format string


def test_spend_carries_through():
    rep = synthesize(_report([_cand()], spend=0.0123))
    assert abs(rep.spend_usd - 0.0123) < 1e-9
    assert rep.summary()["spend_usd"] == 0.0123


def test_write_yaml_creates_parent_and_is_loadable():
    rep = synthesize(_report([_cand(line=1), _cand(line=2)]))
    out = write_yaml(rep, _tmp() / "nested" / "deeper" / "h.yaml")  # parent doesn't exist
    assert out.exists()
    assert len(load_hypotheses(out)) == 2
    # l1_meta block present + informational
    import yaml as _y
    doc = _y.safe_load(out.read_text(encoding="utf-8"))
    assert doc["l1_meta"]["status"] == "OK" and doc["l1_meta"]["generated_by"] == "l1-surface-coverage"


def test_write_yaml_refuses_symlink():
    d = _tmp()
    target = d / "real_secret.txt"
    target.write_text("DO NOT CLOBBER", encoding="utf-8")
    link = d / "h.yaml"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return  # no symlink privilege on this host — skip (the guard is still in place)
    rep = synthesize(_report([_cand()]))
    try:
        write_yaml(rep, link)
    except OSError:
        assert target.read_text(encoding="utf-8") == "DO NOT CLOBBER", "symlink target was clobbered!"
        return
    raise AssertionError("write_yaml must refuse to write through a symlink")


def test_synthesize_repo_no_filter_end_to_end():
    src = """
pub fn handle(ctx: Context<W>, amount: u64) -> Result<()> {
    let n = ctx.accounts.vault.balance - amount;
    let d = n / amount;
    Ok(())
}
"""
    root = _tmp()
    (root / "lib.rs").write_text(src, encoding="utf-8")
    rep = synthesize_repo(root, run_filter=False)  # no LLM
    assert rep.status == "OK"
    assert rep.hypotheses, "expected hypotheses from a repo with arithmetic"
    out = write_yaml(rep, root / "hypotheses.l1.yaml")
    assert len(load_hypotheses(out)) == len(rep.hypotheses)


def test_synthesize_repo_with_fake_filter():
    def fake_keep(prompt, *, system=None, model=None, max_tokens=None, temperature=None):
        idxs = [int(x) for x in re.findall(r"i=(\d+)", prompt)]

        class R:
            text = json.dumps([{"i": i, "verdict": "keep", "reason": "k"} for i in idxs])
            cost_usd = 0.001
        return R()

    root = _tmp()
    (root / "lib.rs").write_text("pub fn f(a: u64, b: u64) -> u64 { a + b }", encoding="utf-8")
    rep = synthesize_repo(root, complete_fn=fake_keep, run_filter=True)
    assert rep.status == "OK"
    assert rep.hypotheses
    for i, h in enumerate(rep.hypotheses):
        _normalize_and_validate(dict(h), Path("t.yaml"), i)


def test_hunt_has_surface_scan_option():
    from audit_pipeline.commands.hunt import hunt_cmd  # noqa: PLC0415
    names = {p.name for p in hunt_cmd.params}
    assert "surface_scan" in names, "hunt must expose --surface-scan"


def test_surface_scan_registered_in_cli():
    from audit_pipeline.cli import main  # noqa: PLC0415
    assert "surface-scan" in main.commands, "surface-scan must be registered on the CLI"


# ----- review round 1: id cap/salt, is_false_clean, write_yaml directory reject -----
def test_id_bug_class_is_capped_and_valid():
    long_bc = "a" + "b" * 60  # 61 chars, valid per _BUG_CLASS_RE (<=64)
    rep = synthesize(_report([_cand(bug_class=long_bc, cls="logic")]))
    hid = rep.hypotheses[0]["id"]
    assert _ID_RE.match(hid), hid
    assert len(hid) < 80, f"id too long ({len(hid)}): {hid}"
    # the embedded bug_class slug is capped at 32 chars
    assert long_bc[:32] in hid and long_bc not in hid


def test_id_salt_makes_ids_distinct():
    a = synthesize(_report([_cand()]), id_salt="aaaaaa").hypotheses[0]["id"]
    b = synthesize(_report([_cand()]), id_salt="bbbbbb").hypotheses[0]["id"]
    assert a != b
    assert _ID_RE.match(a) and _ID_RE.match(b)


def test_two_repos_get_disjoint_ids():
    # the per-repo salt in synthesize_repo must namespace ids so two repos can't collide.
    def _mk(name):
        d = _tmp()
        (d / "lib.rs").write_text(f"pub fn f_{name}(a: u64, b: u64) -> u64 {{ a + b }}", encoding="utf-8")
        return d
    ids_a = {h["id"] for h in synthesize_repo(_mk("aaa"), run_filter=False).hypotheses}
    ids_b = {h["id"] for h in synthesize_repo(_mk("bbb"), run_filter=False).hypotheses}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b), "ids from different repos must not collide"


def test_is_false_clean():
    assert SynthReport(status="COVERAGE_UNKNOWN", hypotheses=[]).is_false_clean() is True
    assert SynthReport(status="COVERAGE_INCOMPLETE", hypotheses=[]).is_false_clean() is True
    assert SynthReport(status="OK", hypotheses=[]).is_false_clean() is False  # clean empty is OK
    assert SynthReport(status="COVERAGE_UNKNOWN", hypotheses=[{"id": "x"}]).is_false_clean() is False


def test_write_yaml_refuses_directory():
    rep = synthesize(_report([_cand()]))
    d = _tmp()
    (d / "hypotheses.l1.yaml").mkdir()  # destination squatted as a directory
    try:
        write_yaml(rep, d / "hypotheses.l1.yaml")
    except OSError:
        return
    raise AssertionError("write_yaml must refuse to write when the destination is a directory")


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
