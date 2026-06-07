"""Tests for the L1 bug-spot finder (surface enumeration).

Runnable two ways: `python -m pytest tests/test_l1_surfaces.py` OR
`python tests/test_l1_surfaces.py` (standalone, prints PASS/FAIL).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.surfaces import (  # noqa: E402
    ALL_SURFACE_TYPES, S_ARITH, S_AUTH, S_CLOSE, S_CPI, S_DESERIALIZE, S_HANDLER,
    S_INIT, S_ORACLE, S_PDA, S_REALLOC, S_REMAINING, extract_surfaces,
)


def _prog(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _types(rep) -> set[str]:
    return {s.surface_type for s in rep.surfaces}


_RICH = """
use anchor_lang::prelude::*;

pub fn handle_withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
    require!(ctx.accounts.authority.is_signer, ErrorCode::Unauthorized);
    let bal = ctx.accounts.vault.balance;
    let new_bal = bal - amount;                 // arithmetic (binary)
    let checked = bal.checked_sub(amount)?;     // arithmetic (method)
    let casted = amount as u32;                 // arithmetic (cast)
    let price = ctx.accounts.oracle.get_price_no_older_than(60)?;  // oracle
    let (pda, bump) = Pubkey::find_program_address(&[b"vault"], ctx.program_id);  // pda
    let state = VaultState::try_from_slice(&ctx.accounts.vault.data.borrow())?;   // deserialize
    invoke_signed(&transfer_ix, accts, signer_seeds)?;            // cpi
    for acc in ctx.remaining_accounts {                          // remaining_accounts
        msg!("acc {}", acc.key);
    }
    ctx.accounts.vault.realloc(128, false)?;                    // realloc
    **ctx.accounts.vault.to_account_info().lamports.borrow_mut() = 0;  // close
    Ok(())
}

#[derive(Accounts)]
pub struct Withdraw<'info> {
    #[account(mut, has_one = authority)]
    pub vault: Account<'info, VaultState>,
    #[account(init, payer = authority, space = 64)]
    pub fresh: Account<'info, VaultState>,
    pub authority: Signer<'info>,
}
"""


def test_finds_every_surface_type():
    rep = extract_surfaces(_prog({"lib.rs": _RICH}))
    assert rep.status == "OK", rep.notes
    found = _types(rep)
    for t in (S_HANDLER, S_AUTH, S_ARITH, S_ORACLE, S_PDA, S_DESERIALIZE,
              S_CPI, S_REMAINING, S_REALLOC, S_CLOSE, S_INIT):
        assert t in found, f"missing surface type {t}; found={sorted(found)}"


def test_arithmetic_detects_binary_method_and_cast():
    rep = extract_surfaces(_prog({"lib.rs": _RICH}))
    arith = [s for s in rep.surfaces if s.surface_type == S_ARITH]
    details = {s.detail for s in arith}
    assert "-" in details          # binary op
    assert "checked_sub" in details  # method
    assert any(d.startswith("as ") for d in details)  # cast


def test_enclosing_fn_recorded():
    rep = extract_surfaces(_prog({"lib.rs": _RICH}))
    cpis = [s for s in rep.surfaces if s.surface_type == S_CPI]
    assert cpis and all(s.enclosing_fn == "handle_withdraw" for s in cpis)


def test_handler_detected_by_params_and_name():
    src = """
pub fn process_instruction(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    Ok(())
}
fn pure_helper(a: u64) -> u64 { a }
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    handlers = {s.detail for s in rep.surfaces if s.surface_type == S_HANDLER}
    assert "process_instruction" in handlers
    assert "pure_helper" not in handlers  # no accounts/ctx params, non-handler name


def test_empty_repo_is_unknown_not_clean():
    rep = extract_surfaces(_prog({"README.md": "no rust here\n"}))
    assert rep.status == "COVERAGE_UNKNOWN"
    assert rep.summary()["total_surfaces"] is None  # never "0 surfaces, done" off an unknown scan


def test_clean_rust_no_surfaces_is_ok():
    rep = extract_surfaces(_prog({"lib.rs": "pub fn add_one(a: u64) -> u64 { a }\n"}))
    # a pure getter with no bug surfaces still parses fine -> OK, possibly zero surfaces
    assert rep.status == "OK", rep.notes
    assert rep.summary()["total_surfaces"] == len(rep.surfaces)


def test_skipped_file_marks_incomplete():
    # a >8MB file is skipped -> INCOMPLETE, but the small file still scans.
    big = "// pad\n" + ("// filler line to exceed the cap\n" * 300000)
    rep = extract_surfaces(_prog({"lib.rs": _RICH, "huge.rs": big}))
    assert rep.status == "COVERAGE_INCOMPLETE", rep.notes
    assert any("8MB" in n for n in rep.notes)
    assert rep.surfaces  # the small file's surfaces are still there


def test_deterministic():
    files = {"a.rs": _RICH}
    s1 = extract_surfaces(_prog(files)).summary()
    s2 = extract_surfaces(_prog(files)).summary()
    assert s1["by_type"] == s2["by_type"]
    assert s1["raw_surface_count"] == s2["raw_surface_count"]


def test_by_type_covers_exactly_known_types():
    # `==` (not `>=`) so a Surface emitted with a type missing from ALL_SURFACE_TYPES is caught.
    rep = extract_surfaces(_prog({"lib.rs": _RICH}))
    assert set(rep.by_type().keys()) == set(ALL_SURFACE_TYPES)


# ----- 3-agent-review completeness gaps (2026-06-06): real-world forms that were missed -----
def test_anchor_cpicontext_form_detected():
    # The dominant Anchor CPI: token::transfer(CpiContext::new(...), amt) — the outer callee is
    # `token::transfer` (not invoke); the CPI must be caught via the nested CpiContext::new call.
    src = """
pub fn t(ctx: Context<T>, amt: u64) -> Result<()> {
    token::transfer(CpiContext::new(ctx.accounts.tp.to_account_info(), Transfer{}), amt)?;
    anchor_spl::token::burn(CpiContext::new_with_signer(p, a, seeds), amt)?;
    Ok(())
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    cpis = [s for s in rep.surfaces if s.surface_type == S_CPI]
    assert len(cpis) >= 2, [s.detail for s in cpis]


def test_pinocchio_invoke_unchecked_detected():
    src = "pub fn f() { invoke_signed_unchecked(&ix); invoke_unchecked(&ix2); }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    cpis = {s.detail for s in rep.surfaces if s.surface_type == S_CPI}
    assert any("invoke_signed_unchecked" in d for d in cpis)
    assert any("invoke_unchecked" in d for d in cpis)


def test_arithmetic_index_sum_pow_detected():
    src = "pub fn f(v: Vec<u64>, data: &[u8], i: usize) -> u64 { let _ = data[i]; v.iter().sum::<u64>() + 2u64.pow(3) }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    details = {s.detail for s in rep.surfaces if s.surface_type == S_ARITH}
    assert "index[]" in details   # array indexing (bounds/panic surface)
    assert "sum" in details        # iterator reducer (overflow)
    assert "pow" in details        # exponent (overflow)


def test_deserialize_transmute_and_pod_detected():
    src = """
pub fn f(p: *const u8, data: &[u8]) {
    let _a = std::mem::transmute::<_, u64>(p);
    let _b = bytemuck::pod_read_unaligned::<State>(data);
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    deser = {s.detail for s in rep.surfaces if s.surface_type == S_DESERIALIZE}
    assert "transmute" in deser
    assert "pod_read_unaligned" in deser


def test_shift_operators_are_arithmetic():
    # R2: `<<` / `>>` panic (debug) or wrap (release) on out-of-range shift — overflow surfaces.
    src = "pub fn f(a: u64, b: u32) -> u64 { let x = a << b; let mut y = a >> 2; y <<= 1; x + y }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    details = {s.detail for s in rep.surfaces if s.surface_type == S_ARITH}
    assert "<<" in details and ">>" in details and "<<=" in details, details


def test_parenthesized_callee_detected():
    # R2: `(invoke_signed)(...)` — a parenthesized callee must still resolve to the CPI surface.
    src = "pub fn f() { (invoke_signed)(&ix, accts, seeds); }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert any(s.surface_type == S_CPI for s in rep.surfaces), [s.detail for s in rep.surfaces]


def test_macro_note_present_on_total_cap_path():
    # R2: the standing macro-blindness note must be on the total-surface-cap early-return path too.
    import audit_pipeline.l1.surfaces as surf  # noqa: PLC0415
    saved = surf._MAX_TOTAL_SURFACES
    surf._MAX_TOTAL_SURFACES = 3
    try:
        body = "pub fn f() { " + "; ".join(f"let x{i} = a + b" for i in range(40)) + "; }\n"
        rep = extract_surfaces(_prog({"lib.rs": body}))
        assert rep.status in ("COVERAGE_UNKNOWN", "COVERAGE_INCOMPLETE"), rep.status
        assert any("macro-generated" in n for n in rep.notes)
    finally:
        surf._MAX_TOTAL_SURFACES = saved


def test_custom_guard_macros_detected_as_authority():
    src = "pub fn f(ctx: Ctx) { ensure!(ctx.ok, E); access_control!(only(ctx)); invariant!(x == y); }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    auth = {s.detail for s in rep.surfaces if s.surface_type == S_AUTH}
    assert {"ensure!", "access_control!", "invariant!"} <= auth, auth


def test_lamport_drain_compound_assign_is_close():
    # native close pattern: drain lamports to dest (`+=`) then zero source — both are close surfaces.
    src = """
pub fn f(dst: AccountInfo, src: AccountInfo) {
    **dst.lamports.borrow_mut() += **src.lamports.borrow_mut();
    let zero: u64 = 0;
    **src.lamports.borrow_mut() = zero;
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    closes = [s for s in rep.surfaces if s.surface_type == S_CLOSE]
    assert len(closes) >= 2, [s.detail for s in closes]  # the += drain AND the = zero set


def test_anchor_seeds_attr_is_pda_surface():
    # Pure-Anchor PDA: `#[account(seeds = [...], bump)]` derives a PDA at macro-expansion time,
    # producing NO find_program_address call. Must still be flagged as a PDA surface.
    src = """
#[derive(Accounts)]
pub struct Open<'info> {
    #[account(seeds = [b"vault", user.key().as_ref()], bump)]
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    pdas = [s for s in rep.surfaces if s.surface_type == S_PDA]
    assert pdas and any("seeds" in s.detail for s in pdas)


def test_macro_blindness_note_on_every_report():
    # Disclosed, never silent: the macro-coverage caveat must be present on OK and non-OK reports.
    ok = extract_surfaces(_prog({"lib.rs": _RICH}))
    unknown = extract_surfaces(_prog({"README.md": "x\n"}))
    assert any("macro-generated" in n for n in ok.notes)
    assert any("macro-generated" in n for n in unknown.notes)


def test_surface_cap_marks_incomplete():
    # A flood of surfaces past the per-file cap must downgrade to COVERAGE_INCOMPLETE (DoS guard),
    # never a silent clean OK. (Lower the cap so the test stays cheap.)
    import audit_pipeline.l1.surfaces as surf  # noqa: PLC0415
    saved = surf._MAX_SURFACES_PER_FILE
    surf._MAX_SURFACES_PER_FILE = 5
    try:
        body = "pub fn f() { " + "; ".join(f"let x{i} = a + b" for i in range(50)) + "; }\n"
        rep = extract_surfaces(_prog({"lib.rs": body}))
        assert rep.status == "COVERAGE_INCOMPLETE", rep.notes
        assert any("cap" in n for n in rep.notes)
        assert len(rep.surfaces) <= surf._MAX_SURFACES_PER_FILE
    finally:
        surf._MAX_SURFACES_PER_FILE = saved


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
