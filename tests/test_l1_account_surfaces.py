"""Tests for the L1 Anchor account-field detector.

Covers the surfaces the attribute detectors are blind to: raw UncheckedAccount/AccountInfo
fields (Anchor does NO validation on them) and writable accounts with no identity/authority
tie. These are the #1 Anchor vulnerability class — a MISSING constraint.

Runnable two ways: `python -m pytest tests/test_l1_account_surfaces.py` OR
`python tests/test_l1_account_surfaces.py` (standalone, prints PASS/FAIL).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.candidates import label_repo  # noqa: E402
from audit_pipeline.l1.surfaces import (  # noqa: E402
    _BINDING_KEYS,
    S_ACCOUNT,
    S_UNCHECKED,
    _strip_invisible,
    extract_surfaces,
)


def _prog(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _surf(rep, stype):
    return [s for s in rep.surfaces if s.surface_type == stype]


# An Accounts struct exercising every branch of the field detector.
_STRUCT = """
use anchor_lang::prelude::*;

#[derive(Accounts)]
#[instruction(x: u64)]
pub struct DoThing<'info> {
    #[account(mut, has_one = authority)]
    pub bound_vault: Account<'info, Vault>,          // mut + has_one -> NOT flagged
    #[account(mut, seeds = [b"v"], bump)]
    pub pda_vault: Account<'info, Vault>,            // mut + seeds -> NOT flagged
    #[account(mut)]
    pub loose_vault: Account<'info, Vault>,          // mut, no tie -> S_ACCOUNT
    #[account(mut)]
    pub boxed: Box<Account<'info, Vault>>,           // mut, boxed, no tie -> S_ACCOUNT
    pub readonly: Account<'info, Vault>,             // not mut -> NOT flagged
    /// CHECK: bound by proof
    pub checked_raw: UncheckedAccount<'info>,        // raw -> S_UNCHECKED (check_doc=True)
    pub naked_raw: UncheckedAccount<'info>,          // raw, no CHECK -> S_UNCHECKED
    #[account(mut)]
    pub raw_info: AccountInfo<'info>,                 // raw AccountInfo -> S_UNCHECKED (mut=True)
    pub authority: Signer<'info>,                     // signer primitive -> NOT flagged
    pub system_program: Program<'info, System>,       // program primitive -> NOT flagged
    pub rent: Sysvar<'info, Rent>,                    // sysvar primitive -> NOT flagged
}
"""


def test_unchecked_accounts_flagged():
    rep = extract_surfaces(_prog({"lib.rs": _STRUCT}))
    assert rep.status == "OK", rep.notes
    names = {s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)}
    assert names == {"checked_raw", "naked_raw", "raw_info"}, names


def test_unchecked_records_check_doc_and_mut():
    rep = extract_surfaces(_prog({"lib.rs": _STRUCT}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["checked_raw"]
    assert "check_doc=False" in by_name["naked_raw"]
    assert "mut=True" in by_name["raw_info"]
    assert "mut=False" in by_name["naked_raw"]


def test_writable_account_without_binding_flagged():
    rep = extract_surfaces(_prog({"lib.rs": _STRUCT}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert names == {"loose_vault", "boxed"}, names


def test_bound_and_readonly_and_primitive_fields_not_flagged():
    rep = extract_surfaces(_prog({"lib.rs": _STRUCT}))
    flagged = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)} | {
        s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)
    }
    for safe in ("bound_vault", "pda_vault", "readonly", "authority",
                 "system_program", "rent"):
        assert safe not in flagged, f"{safe} should not be flagged; flagged={flagged}"


def test_non_accounts_struct_ignored():
    src = """
#[derive(Clone, Debug)]
pub struct NotAccounts<'info> {
    pub raw: UncheckedAccount<'info>,
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_UNCHECKED)
    assert not _surf(rep, S_ACCOUNT)


def test_only_accounts_struct_fields_flagged_when_mixed():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> { pub raw: UncheckedAccount<'info> }

#[derive(Debug)]
pub struct Data<'info> { pub also_raw: UncheckedAccount<'info> }
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)}
    assert names == {"raw"}, names


def test_accounts_struct_as_only_item():
    src = "#[derive(Accounts)]\npub struct Solo<'info> { pub raw: UncheckedAccount<'info> }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_new_surface_types_are_mapped_to_bug_classes():
    # label_repo must not mark COVERAGE_INCOMPLETE for an unmapped surface type, and must emit
    # the expected bug classes for the new surfaces.
    rep = label_repo(_prog({"lib.rs": _STRUCT}))
    bug_classes = {c.bug_class for c in rep.candidates}
    assert "unchecked-account" in bug_classes
    assert "missing-owner-check" in bug_classes
    assert "authorization-bypass" in bug_classes
    assert "account-substitution" in bug_classes
    # every candidate is loader-shaped (class + >=20-char claim)
    for c in rep.candidates:
        assert c.cls and len(c.claim) >= 20


def test_field_caps_do_not_crash_on_empty_struct():
    src = "#[derive(Accounts)]\npub struct Empty<'info> { pub _p: std::marker::PhantomData<&'info ()> }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert rep.status == "OK", rep.notes


def test_constraint_only_is_not_identity_binding():
    # a bare `constraint = <expr>` enforces logic, not identity -> must still surface (no suppress).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, constraint = state.version == 1)]
    pub state: Account<'info, Foo>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "state" in names, names


def test_has_one_plus_constraint_is_bound():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, has_one = authority, constraint = state.version == 1)]
    pub state: Account<'info, Foo>,
    pub authority: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "state" not in names, names


def test_system_account_mut_unbound_flagged():
    # SystemAccount only checks owner==SystemProgram; a mut unbound one is substitutable.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub fee_dest: SystemAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert names == {"fee_dest"}, names


def test_system_account_with_address_not_flagged():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, address = KNOWN)]
    pub fee_dest: SystemAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_token_account_mint_only_flagged():
    # token::mint binds the MINT, not the owner -> recipient is still substitutable -> surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, token::mint = mint)]
    pub dest_ata: Account<'info, TokenAccount>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "dest_ata" in names, names


def test_token_account_authority_not_flagged():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, token::mint = mint, token::authority = owner)]
    pub dest_ata: Account<'info, TokenAccount>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_doc_commented_struct_still_detected():
    # a /// doc line between the struct and its #[derive(Accounts)] must not zero out detection.
    src = """
/// This accounts struct handles X.
#[derive(Accounts)]
pub struct Documented<'info> {
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_triple_slash_is_check_doc_double_slash_is_not():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: validated in handler
    pub doc_checked: UncheckedAccount<'info>,
    // check: just a normal comment
    pub fake_checked: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["doc_checked"]
    assert "check_doc=False" in by_name["fake_checked"]


def test_integration_multifile_anchor_program():
    files = {
        "instructions/claim.rs": """
#[derive(Accounts)]
pub struct Claim<'info> {
    #[account(mut, seeds = [b"vault"], bump)]
    pub vault: Account<'info, Vault>,
    /// CHECK: bound by proof
    pub recipient: UncheckedAccount<'info>,
    #[account(mut)]
    pub loose: Account<'info, Vault>,
    pub authority: Signer<'info>,
}
""",
        "instructions/sweep.rs": """
#[derive(Accounts)]
pub struct Sweep<'info> {
    #[account(mut)]
    pub fee_dest: SystemAccount<'info>,
    #[account(mut, token::mint = mint)]
    pub dest_ata: Account<'info, TokenAccount>,
}
""",
    }
    rep = extract_surfaces(_prog(files))
    assert rep.status == "OK", rep.notes
    unchecked = {s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)}
    account = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert unchecked == {"recipient"}, unchecked
    assert account == {"loose", "fee_dest", "dest_ata"}, account


def test_binding_word_inside_constraint_value_does_not_suppress():
    # the word "signer" appears inside a constraint EXPRESSION value, not as a constraint key —
    # it must NOT mark the field bound (that was the cardinal false-negative / silence vector).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, constraint = ctx.accounts.signer.key() == vault.authority)]
    pub state: Account<'info, Foo>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "state" in names, names


def test_signer_named_generic_param_not_misclassified():
    # generic PARAMETER contains "Signer" — must not classify the field as a Signer primitive.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub cfg: Account<'info, FeeSignerConfig>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "cfg" in names, names


def test_program_named_generic_param_not_misclassified():
    # generic PARAMETER contains "Program" — must not classify the field as infra.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub st: Account<'info, ProgramState>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "st" in names, names


def test_real_program_and_interface_wrappers_skipped():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    pub token_program: Program<'info, Token>,
    pub token_if: Interface<'info, TokenInterface>,
    #[account(mut)]
    pub ata: InterfaceAccount<'info, TokenAccount>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert names == {"ata"}, names  # only the writable InterfaceAccount, not the program/interface


def test_lazy_account_mut_unbound_flagged():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub state: LazyAccount<'info, Foo>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert names == {"state"}, names


def test_unknown_aliased_type_mut_unbound_still_surfaced():
    # a type alias (e.g. `type MyVault<'info> = Account<'info, Vault>`) is unrecognized -> kind None
    # -> must still surface when writable + unbound (never silently skipped).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub vault: MyVault<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    flagged = [s for s in _surf(rep, S_ACCOUNT) if s.detail.split("`")[1] == "vault"]
    assert flagged, "unknown-typed writable unbound account must still surface"
    assert "kind=unknown" in flagged[0].detail


def test_associated_token_is_binding():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, associated_token::mint = mint, associated_token::authority = owner)]
    pub ata: Account<'info, TokenAccount>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_non_anchor_account_attr_does_not_provide_binding():
    # a `#[my_account(seeds = x)]` proc-macro must NOT count as an Anchor #[account(...)] binding;
    # only the real #[account(mut)] applies -> field is mut + unbound -> surfaced.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[my_account(seeds = whatever)]
    #[account(mut)]
    pub v: Account<'info, Foo>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "v" in names, names


def test_check_doc_requires_anchor_colon_token():
    # only `/// CHECK:` (colon) counts — natural-language 'check' / 'checkbox' / 'double-check'
    # must NOT set check_doc=True (kills the spoof vector + false positives).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: validated in the handler
    pub real: UncheckedAccount<'info>,
    /// please double-check this account before use
    pub fake1: UncheckedAccount<'info>,
    /// checkbox state tracker
    pub fake2: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["real"]
    assert "check_doc=False" in by_name["fake1"]
    assert "check_doc=False" in by_name["fake2"]


def test_associated_token_mint_only_flagged():
    # ATA bound by mint but NOT authority -> owner still substitutable -> must surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, associated_token::mint = mint)]
    pub ata: Account<'info, TokenAccount>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "ata" in names, names


def test_seeds_without_bump_flagged():
    # seeds alone does not pin the PDA address (Anchor verifies only with bump) -> must surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, seeds = [b"x"])]
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "vault" in names, names


def test_seeds_with_bump_not_flagged():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, seeds = [b"x"], bump)]
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_stacked_account_attrs_binding_found():
    # binding constraint split across TWO #[account(...)] attrs on the same field must suppress.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    #[account(has_one = authority)]
    pub vault: Account<'info, Vault>,
    pub authority: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_long_attribute_binding_key_not_truncated():
    # has_one appears AFTER a >512-byte constraint expression — must still be seen as bound.
    pad = "x" * 700
    src = f"""
#[derive(Accounts)]
pub struct Ix<'info> {{
    #[account(mut, constraint = some_check == {pad}, has_one = authority)]
    pub vault: Account<'info, Vault>,
    pub authority: Signer<'info>,
}}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT), "binding key past 512 bytes must not be truncated away"


def test_check_doc_between_attr_and_field_detected():
    # darkdrop's initialize*.rs pattern: #[account(init...)] then /// CHECK: then the field.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(init, payer = payer, space = 8)]
    /// CHECK: created and validated here
    pub raw: UncheckedAccount<'info>,
    pub payer: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["raw"]


def test_check_doc_is_case_sensitive():
    # Anchor's convention is uppercase CHECK: — lowercase 'check:' must NOT spoof check_doc=True.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: real anchor check
    pub real: UncheckedAccount<'info>,
    /// check: lowercase should not count
    pub spoof: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["real"]
    assert "check_doc=False" in by_name["spoof"]


def test_block_doc_check_not_honored():
    # Anchor honors only `/// CHECK:` line docs; a `/** ... CHECK: ... */` block comment must not.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /** normal state tracking. CHECK: buried to appear compliant. */
    pub buried: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=False" in by_name["buried"]


def test_signer_only_unchecked_reports_signer_not_bound():
    # #[account(signer)] authenticates but does not pin identity -> bound=False, signer=True.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(signer)]
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    detail = _surf(rep, S_UNCHECKED)[0].detail
    assert "bound=False" in detail
    assert "signer=True" in detail


def test_check_doc_above_attribute_detected():
    # darkdrop migrate_schema_v2 pattern: /// CHECK: ABOVE #[account(...)] ABOVE the field.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: reconstructed and validated below
    #[account(mut, seeds = [b"x"], bump)]
    pub merkle_tree: AccountInfo<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    detail = _surf(rep, S_UNCHECKED)[0].detail
    assert "check_doc=True" in detail
    assert "bound=True" in detail  # seeds + bump


def test_unchecked_with_no_attribute_at_all():
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: doc only, no #[account]
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    detail = _surf(rep, S_UNCHECKED)[0].detail
    assert "mut=False" in detail and "bound=False" in detail


def test_accountloader_init_seeds_bump_not_flagged():
    # darkdrop initialize_mint_trees pattern: AccountLoader with init/seeds/bump + CHECK doc.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(init, payer = payer, seeds = [b"t"], bump, space = 8)]
    /// CHECK: zero-copy account
    pub tree: AccountLoader<'info, Foo>,
    pub payer: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)} | {
        s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)
    }
    assert "tree" not in names, names


def test_turbofish_in_constraint_does_not_hide_has_one():
    # a turbofish comma inside a constraint expr must not over-split away the has_one binding.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, constraint = pick::<u64>(a, b) > 0, has_one = authority)]
    pub vault: Account<'info, Vault>,
    pub authority: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_strip_invisible_removes_zero_width_and_format_chars():
    # zero-width space, RLM, BOM must all be stripped so they can't defeat the prefix check.
    assert _strip_invisible("#[ac​count(‏mut)]﻿") == "#[account(mut)]"
    assert _strip_invisible("  has_one\t=\nauth ") == "has_one=auth"


def test_excluded_keys_are_not_binding():
    # design-lock: keys that do NOT pin identity must never be in _BINDING_KEYS (a future accidental
    # addition would silently suppress real surfaces).
    for k in ("constraint", "owner", "token::mint", "mint", "seeds", "signer"):
        assert k not in _BINDING_KEYS, f"{k} must not be a binding key"


def test_derive_accounts_past_512_bytes_still_detected():
    # a long derive list pushing `Accounts` past byte 512 must not silently drop the struct.
    long_derive = ", ".join(f"Trait{i}" for i in range(120))  # ~900 bytes before Accounts
    src = f"""
#[derive({long_derive}, Accounts)]
pub struct Big<'info> {{
    pub raw: UncheckedAccount<'info>,
}}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1, "struct with long derive list must still be detected"


def test_aliased_unknown_account_gets_owner_check_bug_class():
    # a writable unbound field of an unrecognized/aliased type surfaces as S_ACCOUNT kind=unknown,
    # and must ALSO carry the raw-account bug classes (missing-owner-check / unchecked-account).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut)]
    pub vault: MyRawAlias<'info>,
}
"""
    rep = label_repo(_prog({"lib.rs": src}))
    bug_classes = {c.bug_class for c in rep.candidates}
    assert "missing-owner-check" in bug_classes
    assert "unchecked-account" in bug_classes


def test_check_doc_never_suppresses_unchecked_bug_classes():
    # check_doc is metadata only — an UncheckedAccount with a valid /// CHECK: must STILL get the
    # unchecked-account + missing-owner-check bug classes (check_doc must not gate them).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: explained
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = label_repo(_prog({"lib.rs": src}))
    bug_classes = {c.bug_class for c in rep.candidates}
    assert "unchecked-account" in bug_classes
    assert "missing-owner-check" in bug_classes


def test_bump_without_seeds_is_flagged():
    # `bump = state.bump` WITHOUT `seeds` does not verify the PDA -> writable field must surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, bump = state.bump)]
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "vault" in names, names


def test_instruction_attr_interstitial_detected():
    # the universal Anchor pattern: #[derive(Accounts)] #[instruction(...)] pub struct.
    src = """
#[derive(Accounts)]
#[instruction(nullifier_hash: [u8; 32])]
pub struct Ix<'info> {
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_zero_width_char_in_accounts_derive_still_detected():
    # a zero-width space inside `Accounts` must not defeat the struct gate (cardinal failure).
    src = "#[derive(Acco​unts)]\npub struct Ix<'info> { pub raw: UncheckedAccount<'info> }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_double_slash_uppercase_check_not_honored():
    # `// CHECK:` (uppercase but only TWO slashes) is NOT an Anchor doc -> must not set check_doc.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    // CHECK: this is a plain comment, not a /// doc
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=False" in _surf(rep, S_UNCHECKED)[0].detail


def test_readonly_aliased_unknown_account_surfaced():
    # a READ-ONLY field of an unrecognized/aliased type may be a raw AccountInfo behind an alias —
    # it must still surface (never silently dropped), even without `mut`.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    pub vault: MyRawAlias<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    flagged = [s for s in _surf(rep, S_ACCOUNT) if s.detail.split("`")[1] == "vault"]
    assert flagged, "read-only unrecognized account must still surface"
    assert "kind=unknown" in flagged[0].detail


def test_bare_unchecked_no_attr_no_doc():
    src = "#[derive(Accounts)]\npub struct Ix<'info> { pub raw: UncheckedAccount<'info> }\n"
    rep = extract_surfaces(_prog({"lib.rs": src}))
    detail = _surf(rep, S_UNCHECKED)[0].detail
    assert "mut=False" in detail and "check_doc=False" in detail
    assert "bound=False" in detail and "signer=False" in detail


def test_s_account_detail_reports_constraint_presence():
    # L2 needs to know a constraint expr is present even though it isn't a recognized binding key.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, constraint = a.owner == b.key())]
    pub state: Account<'info, Foo>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    detail = [s for s in _surf(rep, S_ACCOUNT) if s.detail.split("`")[1] == "state"][0].detail
    assert "constraint=True" in detail


def test_stored_bump_with_seeds_not_flagged():
    # the real darkdrop PDA form: seeds + `bump = state.bump` (stored canonical bump) -> bound.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, seeds = [b"vault"], bump = state.bump)]
    pub vault: Account<'info, Vault>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT)


def test_init_only_account_surfaced():
    # `init` implies writable; init WITHOUT a PDA derivation (no seeds) must surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(init, payer = payer, space = 8)]
    pub fresh: Account<'info, Foo>,
    pub payer: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "fresh" in names, names


def test_init_is_not_a_binding_key():
    assert "init" not in _BINDING_KEYS and "init_if_needed" not in _BINDING_KEYS


def test_block_comment_between_check_and_field_keeps_check_doc():
    # a benign /* */ comment between a /// CHECK: doc and the field must NOT clear check_doc.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: validated in handler
    /* impl note */
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=True" in _surf(rep, S_UNCHECKED)[0].detail


def test_four_slash_check_not_honored():
    # `////CHECK:` (four slashes) is a plain comment in Rust, NOT a /// doc — must not set check_doc.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    ////CHECK: not a real doc comment
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=False" in _surf(rep, S_UNCHECKED)[0].detail


def test_two_slash_space_slash_check_not_honored():
    # `// /CHECK:` must not fuse into `///CHECK:` after whitespace stripping (forge guard).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    // /CHECK: forged via whitespace fusion
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=False" in _surf(rep, S_UNCHECKED)[0].detail


def test_use_between_derive_and_struct_still_detected():
    # a light item (use/const/type) between the derive and the struct must not hide it.
    src = """
#[derive(Accounts)]
use crate::state::Vault;
pub struct Ix<'info> {
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_subaccounts_lookalike_derive_not_detected():
    # `SubAccounts` / `NotAccounts` must NOT match the `\\bAccounts\\b` derive gate.
    for trait_name in ("SubAccounts", "NotAccounts"):
        src = f"#[derive({trait_name})]\npub struct Ix<'info> {{ pub raw: UncheckedAccount<'info> }}\n"
        rep = extract_surfaces(_prog({"lib.rs": src}))
        assert not _surf(rep, S_UNCHECKED), trait_name


def test_close_only_account_surfaced():
    # `close` implies writable; a close-without-binding account must surface.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(close = payer)]
    pub target: Account<'info, Foo>,
    pub payer: Signer<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_ACCOUNT)}
    assert "target" in names, names


def test_struct_between_derive_and_target_not_misattributed():
    # the derive belongs to the FIRST struct after it; a later struct must NOT inherit it.
    src = """
#[derive(Accounts)]
pub struct A<'info> { pub raw_a: UncheckedAccount<'info> }
pub struct B<'info> { pub raw_b: UncheckedAccount<'info> }
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    names = {s.detail.split("`")[1] for s in _surf(rep, S_UNCHECKED)}
    assert names == {"raw_a"}, names  # A is Accounts; B has no derive -> raw_b NOT flagged


def test_static_between_derive_and_struct_still_detected():
    src = """
#[derive(Accounts)]
pub static DISCRIMINATOR: u8 = 1;
pub struct Ix<'info> {
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert len(_surf(rep, S_UNCHECKED)) == 1


def test_stacked_attr_after_large_first_keeps_binding():
    # a large first #[account(...)] block must not cause a second stacked binding attr to be dropped.
    pad = "x" * 7000
    src = f"""
#[derive(Accounts)]
pub struct Ix<'info> {{
    #[account(mut, constraint = c == {pad})]
    #[account(has_one = authority)]
    pub vault: Account<'info, Vault>,
    pub authority: Signer<'info>,
}}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert not _surf(rep, S_ACCOUNT), "has_one in the second stacked attr must still bind"


def test_bound_aliased_unknown_still_surfaced():
    # an aliased raw account behind seeds+bump is NOT owner/type-checked by Anchor — must surface
    # even though bound=True (the "never dropped" guarantee for unknown types).
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    #[account(mut, seeds = [b"x"], bump)]
    pub vault: MyAliasedRaw<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    flagged = [s for s in _surf(rep, S_ACCOUNT) if s.detail.split("`")[1] == "vault"]
    assert flagged, "bound unknown/aliased type must still surface (never dropped)"
    assert "kind=unknown" in flagged[0].detail


def test_check_doc_survives_non_account_attribute():
    # a /// CHECK: above a non-Anchor attribute (e.g. #[cfg]) still applies to the field.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: validated in handler
    #[allow(unused)]
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=True" in _surf(rep, S_UNCHECKED)[0].detail


def test_check_doc_does_not_bleed_past_signer_field():
    # a /// CHECK: before a Signer (skipped) must NOT bleed onto the next UncheckedAccount.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: this is about the authority
    pub authority: Signer<'info>,
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=False" in by_name["raw"]


def test_check_with_space_before_colon_not_honored():
    # Anchor requires the exact token `CHECK:` — `CHECK :` (space before colon) is NOT honored.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK : space before colon is not the Anchor token
    pub raw: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    assert "check_doc=False" in _surf(rep, S_UNCHECKED)[0].detail


def test_check_doc_applies_through_non_account_attr_but_not_past_a_field():
    # Rust: a /// CHECK: doc and an interleaved #[cfg] BOTH bind to the SAME following field, so the
    # CHECK documents `raw` (check_doc=True). But it must NOT bleed PAST a field: `other`'s
    # field_declaration resets has_check, so the later `naked` field gets check_doc=False.
    src = """
#[derive(Accounts)]
pub struct Ix<'info> {
    /// CHECK: documents raw
    #[cfg(feature = "x")]
    pub raw: UncheckedAccount<'info>,
    pub naked: UncheckedAccount<'info>,
}
"""
    rep = extract_surfaces(_prog({"lib.rs": src}))
    by_name = {s.detail.split("`")[1]: s.detail for s in _surf(rep, S_UNCHECKED)}
    assert "check_doc=True" in by_name["raw"]
    assert "check_doc=False" in by_name["naked"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
