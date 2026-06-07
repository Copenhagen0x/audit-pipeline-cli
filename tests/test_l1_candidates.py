"""Tests for the L1 surface labeler (step 2).

Runnable two ways: `python -m pytest tests/test_l1_candidates.py` OR
`python tests/test_l1_candidates.py` (standalone, prints PASS/FAIL).
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.candidates import (  # noqa: E402
    _FALLBACK,
    SURFACE_TYPE_BUGCLASSES,
    label_repo,
    label_surfaces,
)
from audit_pipeline.l1.surfaces import (  # noqa: E402
    ALL_SURFACE_TYPES,
    S_ARITH,
    S_CLOSE,
    S_CPI,
    S_DESERIALIZE,
    S_INIT,
    S_ORACLE,
    Surface,
    SurfaceReport,
)

# Use the LIVE loader regex (not a local copy) so the test can't drift from what actually gates.
from audit_pipeline.scoping import (  # noqa: E402
    _BUG_CLASS_RE,
    KNOWN_CLASSES,
    _normalize_and_validate,
)

_RICH = """
pub fn handle_withdraw(ctx: Context<W>, amount: u64) -> Result<()> {
    require!(ctx.accounts.authority.is_signer, E::No);
    let n = ctx.accounts.vault.balance - amount;
    let p = ctx.accounts.oracle.get_price_no_older_than(60)?;
    let (pda, b) = Pubkey::find_program_address(&[b"v"], ctx.program_id);
    let s = VaultState::try_from_slice(&data)?;
    invoke_signed(&ix, accts, seeds)?;
    for a in ctx.remaining_accounts { use_it(a); }
    ctx.accounts.vault.realloc(64, false)?;
    **ctx.accounts.vault.to_account_info().lamports.borrow_mut() = 0;
    Ok(())
}
#[derive(Accounts)]
pub struct W<'info> { #[account(init, payer = a, space = 8)] pub fresh: Account<'info, X> }
"""


def _repo(body: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "lib.rs").write_text(body, encoding="utf-8")
    return d


def test_every_surface_type_has_a_mapping():
    # completeness: no known surface type may fall through to the fallback.
    for t in ALL_SURFACE_TYPES:
        assert t in SURFACE_TYPE_BUGCLASSES and SURFACE_TYPE_BUGCLASSES[t], t


def test_all_classes_are_loader_valid():
    # every `class` we emit (incl. the fallback) must be in the live KNOWN_CLASSES.
    for specs in SURFACE_TYPE_BUGCLASSES.values():
        for spec in specs:
            assert spec.cls in KNOWN_CLASSES, spec.cls
    assert _FALLBACK.cls in KNOWN_CLASSES


def test_bug_classes_match_schema_regex():
    for specs in list(SURFACE_TYPE_BUGCLASSES.values()) + [[_FALLBACK]]:
        for spec in specs:
            assert _BUG_CLASS_RE.match(spec.bug_class), spec.bug_class


def test_candidates_pass_the_real_loader_validator():
    # THE killer test: every candidate, given an id, must pass scoping._normalize_and_validate
    # (the real load-time gate) — proving L1 output will actually load, not just look right.
    rep = label_repo(_repo(_RICH))
    assert rep.candidates, "expected candidates from the rich snippet"
    for i, c in enumerate(rep.candidates):
        h = c.to_dict() | {"id": f"L1-{i}-{c.bug_class}"}
        _normalize_and_validate(h, Path("test.yaml"), i)  # raises on any invalid field


def test_claim_min_length_20():
    rep = label_repo(_repo(_RICH))
    for c in rep.candidates:
        assert len(c.claim.strip()) >= 20, c.claim


def test_cpi_and_oracle_yield_multiple_bug_classes():
    rep = label_repo(_repo(_RICH))
    by_type_bug = {}
    for c in rep.candidates:
        by_type_bug.setdefault(c.surface_type, set()).add(c.bug_class)
    assert len(by_type_bug.get(S_CPI, set())) >= 2, by_type_bug.get(S_CPI)
    assert len(by_type_bug.get(S_ORACLE, set())) >= 2, by_type_bug.get(S_ORACLE)


def test_status_carries_from_surface_report():
    # an UNKNOWN surface scan must produce an UNKNOWN labeled set (coverage never silently clean).
    sr = SurfaceReport(status="COVERAGE_UNKNOWN", surfaces=[], notes=["x"])
    cr = label_surfaces(sr)
    assert cr.status == "COVERAGE_UNKNOWN"
    assert cr.summary()["total_candidates"] is None


def test_unmapped_surface_type_uses_fallback_and_flags_incomplete():
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type="some_future_type", file="a.rs", line=1,
                enclosing_fn="f", detail="x", snippet="y"),
    ])
    cr = label_surfaces(sr)
    assert cr.status == "COVERAGE_INCOMPLETE"
    assert any(c.bug_class == _FALLBACK.bug_class for c in cr.candidates)


def test_deterministic():
    r = _repo(_RICH)
    a = label_repo(r).summary()
    b = label_repo(r).summary()
    assert a["by_bug_class"] == b["by_bug_class"]
    assert a["raw_candidate_count"] == b["raw_candidate_count"]


# ----- round-1 review: added bug classes + detail-aware arithmetic + hardening -----
def _bugs(sr: SurfaceReport) -> set[str]:
    return {c.bug_class for c in label_surfaces(sr).candidates}


def _surf(stype: str, detail: str = "x") -> SurfaceReport:
    return SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=stype, file="a.rs", line=1, enclosing_fn="f", detail=detail, snippet="s")])


def test_deserialize_includes_missing_owner_check():
    assert {"type-confusion", "missing-owner-check"} <= _bugs(_surf(S_DESERIALIZE))


def test_init_includes_rent_and_init_if_needed():
    assert {"reinitialization", "missing-rent-exemption", "init-if-needed-repeated-write"} <= _bugs(_surf(S_INIT))


def test_close_includes_destination_check():
    assert "close-destination-unvalidated" in _bugs(_surf(S_CLOSE))


def test_cpi_includes_authority_not_signer():
    assert "cpi-authority-not-signer" in _bugs(_surf(S_CPI))


def test_oracle_includes_manipulation():
    assert "oracle-price-manipulation" in _bugs(_surf(S_ORACLE))


def test_arithmetic_division_only_for_division():
    # division op -> division-by-zero; plain `+` must NOT get division-by-zero (no flooding).
    assert "division-by-zero" in _bugs(_surf(S_ARITH, "/"))
    assert "division-by-zero" not in _bugs(_surf(S_ARITH, "+"))


def test_arithmetic_cast_and_index_extras():
    assert "cast-truncation" in _bugs(_surf(S_ARITH, "as u32"))
    assert "index-out-of-bounds" in _bugs(_surf(S_ARITH, "index[]"))


def test_every_surface_type_yields_loader_valid_candidates():
    # independent of the _RICH fixture: one synthetic surface per known type, every produced
    # candidate (incl. arithmetic detail extras) must pass the real loader validator.
    extra_arith_details = ["+", "/", "as u64", "index[]"]
    surfaces = [Surface(surface_type=t, file="a.rs", line=1, enclosing_fn="f", detail="x", snippet="s")
                for t in ALL_SURFACE_TYPES]
    surfaces += [Surface(surface_type=S_ARITH, file="a.rs", line=1, enclosing_fn="f", detail=d, snippet="s")
                 for d in extra_arith_details]
    cr = label_surfaces(SurfaceReport(status="OK", surfaces=surfaces))
    assert cr.candidates
    for i, c in enumerate(cr.candidates):
        _normalize_and_validate(c.to_dict() | {"id": f"L1-{i}-{c.bug_class}"}, Path("t.yaml"), i)


def test_all_severities_high_so_high_floor_keeps_them():
    # money-math/realloc surfaces must not be pre-dropped at --min-severity High.
    for specs in SURFACE_TYPE_BUGCLASSES.values():
        for spec in specs:
            assert spec.severity == "High", (spec.bug_class, spec.severity)


def test_claim_sanitizes_injected_newlines():
    # a malicious fn name with newlines / YAML structure must not leak into the claim.
    evil = "f\n---\nhypotheses:\n- id: EVIL1-x\n  claim: injected"
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_CPI, file="a.rs", line=1, enclosing_fn=evil, detail="d", snippet="s")])
    for c in label_surfaces(sr).candidates:
        # the real injection vector is the NEWLINE (YAML structure needs line breaks); the claim
        # must collapse to a single line so it can't break out of a scalar. The surviving
        # "hypotheses:" token on one line is harmless (and yaml.safe_dump quotes it at synthesis).
        assert "\n" not in c.claim and "\r" not in c.claim
        assert c.claim.count("\n") == 0


def test_status_validation_rejects_garbage():
    from audit_pipeline.l1.candidates import CandidateReport  # noqa: PLC0415
    try:
        CandidateReport(status="DEFINITELY_NOT_VALID")
    except ValueError:
        return
    raise AssertionError("CandidateReport must reject an invalid status (no forged-OK coverage)")


def test_candidate_cap_marks_unknown():
    import audit_pipeline.l1.candidates as cand  # noqa: PLC0415
    saved = cand._MAX_TOTAL_CANDIDATES
    cand._MAX_TOTAL_CANDIDATES = 5
    try:
        surfaces = [Surface(surface_type=S_ARITH, file="a.rs", line=i, enclosing_fn="f", detail="+", snippet="s")
                    for i in range(50)]
        cr = label_surfaces(SurfaceReport(status="OK", surfaces=surfaces))
        assert cr.status == "COVERAGE_UNKNOWN", cr.status
        assert cr.summary()["total_candidates"] is None
    finally:
        cand._MAX_TOTAL_CANDIDATES = saved


# ----- round-2 review: rem variants, remaining-accounts length, file/fn sanitization -----
def test_arithmetic_rem_variants_get_division_by_zero():
    # every divisor/remainder form surfaces.py can emit -> divisor-zero risk; ALL must get
    # division-by-zero. (rem method variants + raw / % operators + compound /= %= assigns.)
    for op in ("saturating_rem", "overflowing_rem", "checked_rem", "wrapping_rem",
               "unchecked_rem", "%", "/", "/=", "%="):
        assert "division-by-zero" in _bugs(_surf(S_ARITH, op)), op
    # a non-divisor op must NOT (no flooding).
    assert "division-by-zero" not in _bugs(_surf(S_ARITH, "saturating_add"))


def test_remaining_includes_length_unchecked():
    from audit_pipeline.l1.surfaces import S_REMAINING  # noqa: PLC0415
    assert {"remaining-account-substitution", "remaining-accounts-length-unchecked"} <= _bugs(_surf(S_REMAINING))


def test_to_dict_file_and_fn_sanitized():
    # a malicious file path / fn name with newlines must not leak raw into the emitted dict
    # (claim was already sanitized; this covers the file + enclosing_fn fields too).
    evil_file = "a.rs\n---\nhypotheses:\n- id: X"
    evil_fn = "f\n  bad: yaml"
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_CPI, file=evil_file, line=1, enclosing_fn=evil_fn, detail="d\n", snippet="s")])
    for c in label_surfaces(sr).candidates:
        d = c.to_dict()
        assert "\n" not in d["file"] and "\r" not in d["file"], d["file"]
        assert d["enclosing_fn"] is None or ("\n" not in d["enclosing_fn"] and "\r" not in d["enclosing_fn"]), d["enclosing_fn"]
        assert "\n" not in d["detail"] and "\r" not in d["detail"], d["detail"]
        # and the fully-sanitized record still passes the real loader.
        _normalize_and_validate(d | {"id": "L1-evil-0"}, Path("t.yaml"), 0)


def test_empty_enclosing_fn_stays_none():
    # an empty/whitespace-only fn name must serialize as None, not "".
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_CPI, file="a.rs", line=1, enclosing_fn="   ", detail="d", snippet="s")])
    for c in label_surfaces(sr).candidates:
        assert c.to_dict()["enclosing_fn"] is None


def test_long_file_path_round_trips_intact():
    # a real deep path (> the 160-char claim cap) must NOT be silently truncated in file /
    # target_file — a truncated path mis-resolves AND collides in the dedup key.
    deep = "programs/" + "/".join(f"very_long_module_segment_{i}" for i in range(8)) + "/handler.rs"
    assert len(deep) > 160, len(deep)
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_ARITH, file=deep, line=3, enclosing_fn="f", detail="/", snippet="s")])
    for c in label_surfaces(sr).candidates:
        d = c.to_dict()
        assert d["file"] == deep, d["file"]
        assert d["target_file"] == deep, d["target_file"]


def test_target_file_present_and_distinguishes_files():
    # to_dict must emit target_file == file so scoping's (bug_class, target_file, claim) dedup
    # key separates same-bug-class surfaces in DIFFERENT files (else one is silently dropped).
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_ARITH, file="a/x.rs", line=1, enclosing_fn="f", detail="+", snippet="s"),
        Surface(surface_type=S_ARITH, file="b/y.rs", line=1, enclosing_fn="f", detail="+", snippet="s")])
    cands = label_surfaces(sr).candidates
    keys = {(c.bug_class, c.to_dict()["target_file"]) for c in cands}
    assert ("arithmetic-overflow", "a/x.rs") in keys
    assert ("arithmetic-overflow", "b/y.rs") in keys
    for c in cands:
        assert c.to_dict()["target_file"] == c.to_dict()["file"]


def test_path_sanitizer_strips_linebreaks_keeps_spaces():
    # path sanitizer must drop newline/control chars (YAML-injection safe) but PRESERVE
    # legitimate spaces in a directory name (collapsing them would mis-resolve the path).
    from audit_pipeline.l1.candidates import _san_path  # noqa: PLC0415
    assert _san_path("dir name/lib.rs") == "dir name/lib.rs"      # single space kept
    assert _san_path("two  spaces/lib.rs") == "two  spaces/lib.rs"  # double space kept
    assert "\n" not in _san_path("a.rs\n---\nhypotheses:")
    assert "\t" not in _san_path("a\t.rs")
    assert _san_path(None) == "" and _san_path("") == ""


# ----- round-4 review: path containment, dedup-window line, empty-cast label -----
def test_san_path_contains_traversal():
    # target_file is consumed downstream as `engine_repo / target_file`; the labeler must NEVER
    # emit a path that escapes the repo root (traversal / absolute / drive).
    from audit_pipeline.l1.candidates import _san_path  # noqa: PLC0415
    for evil in ("../../etc/passwd", "/etc/passwd", "..\\..\\secrets\\.env",
                 "C:\\Windows\\system32\\x.rs", "a/../../b.rs", "./../x.rs"):
        out = _san_path(evil)
        assert not out.startswith("/"), out
        assert ".." not in out.split("/"), out
        assert not re.match(r"^[A-Za-z]:", out), out
        assert "\\" not in out, out
    # a clean relative path with spaces still round-trips
    assert _san_path("programs/my mod/lib.rs") == "programs/my mod/lib.rs"


def test_claim_line_in_dedup_window():
    # two surfaces, SAME deep-path file, SAME bug_class, DIFFERENT lines must NOT collide in
    # scoping's dedup key (canonicalized claim, first 120 chars). The line must be in-window.
    deep = "programs/" + "/".join(f"long_module_segment_name_{i}" for i in range(6)) + "/h.rs"
    assert len(deep) > 120, len(deep)
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_ARITH, file=deep, line=103, enclosing_fn="f", detail="+", snippet="s"),
        Surface(surface_type=S_ARITH, file=deep, line=104, enclosing_fn="f", detail="+", snippet="s")])
    cands = [c for c in label_surfaces(sr).candidates if c.bug_class == "arithmetic-overflow"]
    assert len(cands) == 2

    def _canon(claim):  # mirror scoping.load_class_library's claim canonicalization
        return " ".join(claim.lower().split())[:120]
    keys = {(c.bug_class, c.to_dict()["target_file"], _canon(c.claim)) for c in cands}
    assert len(keys) == 2, f"different-line surfaces collapsed in the dedup window: {keys}"


def test_double_drive_prefix_contained():
    # defense-in-depth: even a contrived "C:D:evil.rs" must not leave a drive segment that could
    # resolve drive-relative on Windows (NTFS bans ':' in real names, so this is adversarial-only).
    from audit_pipeline.l1.candidates import _san_path  # noqa: PLC0415
    for evil in ("C:D:evil.rs", "C:\\D:\\x.rs", "foo/C:bar.rs"):
        out = _san_path(evil)
        assert not any(re.match(r"^[A-Za-z]:", p) for p in out.split("/")), out


def test_same_line_same_class_arith_merge_is_acceptable():
    # `a/b + c/d` on ONE line yields two arithmetic-overflow surfaces with different operator
    # detail. They share (bug_class, target_file, claim) so scoping dedups them to one — this is
    # ACCEPTABLE: the base overflow check is detail-agnostic (no downstream code dispatches on
    # `detail`), and the `/` operators each independently get a DISTINCT division-by-zero candidate.
    sr = SurfaceReport(status="OK", surfaces=[
        Surface(surface_type=S_ARITH, file="a.rs", line=10, enclosing_fn="f", detail="+", snippet="s"),
        Surface(surface_type=S_ARITH, file="a.rs", line=10, enclosing_fn="f", detail="/", snippet="s")])
    cands = label_surfaces(sr).candidates

    def _canon(claim):
        return " ".join(claim.lower().split())[:120]
    overflow_keys = {(c.bug_class, c.to_dict()["target_file"], _canon(c.claim))
                     for c in cands if c.bug_class == "arithmetic-overflow"}
    assert len(overflow_keys) == 1, "same-line overflow surfaces should share one dedup key"
    # but division-by-zero is still produced for the `/` operator (not lost to the merge).
    assert any(c.bug_class == "division-by-zero" for c in cands)


def test_empty_cast_still_gets_truncation():
    # surfaces.py can emit detail "as " (empty/unparsed cast type); after _san it collapses to
    # "as" — it must STILL get cast-truncation (the label must survive the sanitization).
    assert "cast-truncation" in _bugs(_surf(S_ARITH, "as "))
    assert "cast-truncation" in _bugs(_surf(S_ARITH, "as u64"))
    # a real method name with an underscore must NOT be mistaken for a cast.
    assert "cast-truncation" not in _bugs(_surf(S_ARITH, "as_ref"))


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
