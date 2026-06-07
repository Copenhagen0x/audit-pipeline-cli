"""Tests for the L1 native entrypoint-authority extractor.

Covers the behaviors the 3-agent code red-team (2026-06-06) required before ship:
no silent false-clean, encoder/self-scrutinee exclusion, naming-agnostic handler
resolution, panic/stub arms flagged, multi-enum ambiguity, determinism.

Runnable two ways: `python -m pytest tests/test_l1_entrypoints.py` OR
`python tests/test_l1_entrypoints.py` (standalone, prints PASS/FAIL).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.entrypoints import extract_native  # noqa: E402


def _prog(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


_DISPATCH = """
pub enum Instruction { Alpha, Beta { x: u64 } }
pub fn process_instruction(data: &[u8]) -> Result<(), E> {
    let instruction = decode(data);
    match instruction {
        Instruction::Alpha => handle_alpha(),
        Instruction::Beta { x } => handle_beta(x),
        _ => Err(E::Bad),
    }
}
fn handle_alpha() -> Result<(), E> { Ok(()) }
fn handle_beta(x: u64) -> Result<(), E> { Ok(()) }
"""


def test_happy_path():
    a = extract_native(_prog({"lib.rs": _DISPATCH}))
    assert a.status == "OK" and a.authority_complete
    assert len(a.instructions) == 2
    assert all(e.routed and e.handler_file for e in a.instructions)
    assert a.summary()["coverage_failures"] == 0
    assert a.catch_all_present


def test_no_enum_is_unknown_not_clean():
    a = extract_native(_prog({"lib.rs": "pub fn add(a:u64)->u64{a}\n"}))
    assert a.status == "COVERAGE_UNKNOWN" and not a.authority_complete
    assert a.summary()["coverage_failures"] is None  # never read "0 failures" off UNKNOWN


def test_encoder_self_match_excluded():
    # An encode() impl matches `self` — must NOT be mistaken for the dispatch.
    src = _DISPATCH + """
impl Instruction {
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match self {
            Instruction::Alpha => out.push(0u8),
            Instruction::Beta { x } => out.push(1u8),
        }
        out
    }
}
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    # handlers must resolve to the real handle_* fns, not the encoder's out.push
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha"
    assert by["Beta"].handler == "handle_beta"


def test_naming_agnostic_handlers():
    # Handlers named do_/run_ (not handle_/process_) must still resolve.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha => do_alpha(),
        Instruction::Beta => run_beta(),
        _ => Err(()),
    }
}
fn do_alpha() -> R { Ok(()) }
fn run_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "do_alpha" and by["Alpha"].handler_file
    assert by["Beta"].handler == "run_beta" and by["Beta"].handler_file


def test_panic_or_stub_arm_is_failure():
    src = """
pub enum Instruction { Real, Stub }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Real => handle_real(),
        Instruction::Stub => panic!("not implemented"),
        _ => Err(()),
    }
}
fn handle_real() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    by = {e.instruction: e for e in a.instructions}
    assert not by["Real"].coverage_failure
    # routed (has an arm) but no handler call -> MUST be a failure, not silently OK
    assert by["Stub"].routed and by["Stub"].handler is None
    assert by["Stub"].coverage_failure


def test_multiple_instruction_enums_unknown():
    a = extract_native(_prog({
        "lib.rs": _DISPATCH,
        "other.rs": "pub enum Instruction { Gamma, Delta }\n",
    }))
    assert a.status == "COVERAGE_UNKNOWN"  # ambiguous; must not silently pick one
    assert a.instructions == []  # M3: UNKNOWN authority carries NO populated instruction list


def test_free_function_encoder_not_selected():
    # A FREE-function encoder (not a `self`-match) appears BEFORE the real dispatch and
    # matches the same variants -> equal enum overlap. The encoder's arms return literals
    # (handler_calls=0) while the real dispatch calls handle_* (handler_calls=2), so the real
    # dispatch wins on the OBJECTIVE rank's handler_calls term — no fn-name nudge (removed R10).
    src = """
pub enum Instruction { Alpha, Beta }
pub fn encode_ix(ix: Instruction) -> u8 {
    match ix {
        Instruction::Alpha => 0u8,
        Instruction::Beta => 1u8,
    }
}
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha => handle_alpha(),
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha" and by["Alpha"].handler_file
    assert by["Beta"].handler == "handle_beta" and by["Beta"].handler_file


def test_enum_name_honored():
    # The instruction enum is named `Ix`, not `Instruction`. Default name -> UNKNOWN
    # (no denominator); honoring the explicit name -> clean authority.
    src = """
pub enum Ix { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Ix::Alpha => handle_alpha(),
        Ix::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    root = _prog({"lib.rs": src})
    assert extract_native(root).status == "COVERAGE_UNKNOWN"  # no `Instruction` enum
    a = extract_native(root, instruction_enum_name="Ix")
    assert a.status == "OK", a.notes
    assert {e.instruction for e in a.instructions} == {"Alpha", "Beta"}
    assert all(e.handler_file for e in a.instructions)


def test_deterministic():
    files = {"a.rs": _DISPATCH, "b.rs": "pub fn helper(){}\n"}
    s1 = extract_native(_prog(files)).summary()
    s2 = extract_native(_prog(files)).summary()
    assert s1["declared_instructions"] == s2["declared_instructions"]
    assert s1["dispatch_at"] and s1["status"] == s2["status"]


def test_impl_dispatch_selected_over_free_encoder():
    # R3 regression: the real dispatch lives in `impl Processor { fn process_instruction }`
    # (the canonical SPL-Token pattern). A FREE-function encoder whose name even contains
    # "dispatch" must NOT steal it. The discriminator is handler-calls (encoder arms return
    # literals); the old in-impl penalty wrongly demoted the real impl dispatch.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn dispatch_encode(ix: Instruction) -> u8 {
    match ix {
        Instruction::Alpha => 0u8,
        Instruction::Beta => 1u8,
    }
}
impl Processor {
    pub fn process_instruction(d: &[u8]) -> R {
        match decode(d) {
            Instruction::Alpha => handle_alpha(),
            Instruction::Beta => handle_beta(),
            _ => Err(()),
        }
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha" and by["Alpha"].handler_file
    assert by["Beta"].handler == "handle_beta" and by["Beta"].handler_file


def test_or_pattern_routes_every_branch():
    # R3 regression: `A | B => h()` must route BOTH variants, not just the first (the
    # dropped branch previously showed up as a false coverage failure).
    src = """
pub enum Instruction { Alpha, Beta, Gamma }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha | Instruction::Beta => handle_ab(),
        Instruction::Gamma => handle_gamma(),
        _ => Err(()),
    }
}
fn handle_ab() -> R { Ok(()) }
fn handle_gamma() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].routed and by["Alpha"].handler == "handle_ab"
    assert by["Beta"].routed and by["Beta"].handler == "handle_ab"
    assert by["Gamma"].handler == "handle_gamma"
    assert a.summary()["coverage_failures"] == 0  # no false failure on the 2nd or-branch


def test_decoy_error_classifier_not_selected():
    # A same-shape match with COINCIDENTAL variant names (an error classifier returning
    # literals) must lose to the real dispatch via the handler-call discriminator.
    src = """
pub enum Ix { Alpha, Beta }
pub fn a_classify(e: MyErr) -> u8 {
    match e {
        Ix::Alpha => 7u8,
        Ix::Beta => 8u8,
    }
}
pub fn route(d: &[u8]) -> R {
    match decode(d) {
        Ix::Alpha => handle_alpha(),
        Ix::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}), instruction_enum_name="Ix")
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha"
    assert by["Beta"].handler == "handle_beta"


def test_skip_dirs_no_longer_hides_tests_dir():
    # R3 coverage-evasion fix: a second `Instruction` enum planted under a tests/-named dir
    # must NOT be silently dropped — it must surface as ambiguity (COVERAGE_UNKNOWN), never
    # a clean OK over the visible subset.
    a = extract_native(_prog({
        "src/lib.rs": _DISPATCH,
        "tests/decoy.rs": "pub enum Instruction { Gamma, Delta }\n",
    }))
    assert a.status == "COVERAGE_UNKNOWN"


def test_deep_nesting_does_not_crash():
    # R3: a pathologically deep AST must not raise RecursionError past the status guards.
    # _walk is iterative, so this returns a status instead of crashing extract_native.
    deep = "fn f() { " + "if true { " * 3000 + "let _x = 1; " + "} " * 3000 + "}\n"
    a = extract_native(_prog({"lib.rs": deep}))
    assert a.status in {"COVERAGE_UNKNOWN", "COVERAGE_INCOMPLETE", "OK"}  # no crash


def test_mark_rejects_unknown_status():
    from audit_pipeline.l1.entrypoints import EntrypointAuthority  # noqa: PLC0415
    auth = EntrypointAuthority()
    try:
        auth._mark("DEFINITELY_NOT_A_STATUS", "typo")
    except ValueError:
        return
    raise AssertionError("_mark must raise on an unknown status, not silently no-op")


def test_foreign_enum_variant_collision_not_routed():
    # R4: a foreign enum that SHARES a variant name (ProgramError::Custom where Custom is
    # also an Instruction variant) must NOT be attributed to the Instruction dispatch.
    # Here the only match is the foreign error-classifier -> no real dispatch -> UNKNOWN,
    # NOT a clean OK over phantom routing.
    src = """
pub enum Instruction { Custom, Transfer }
pub fn classify(e: ProgramError) -> u8 {
    match e {
        ProgramError::Custom => 1u8,
        ProgramError::Transfer => 2u8,
    }
}
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "COVERAGE_UNKNOWN", a.notes  # foreign match must not pose as dispatch


def test_suffix_enum_name_not_matched():
    # R4: substring collision — enum `Ix`, decoy enum `SomeIx`. `"Ix::" in "SomeIx::Alpha"`
    # must NOT route SomeIx arms into the Ix dispatch. The real dispatch (Ix::) wins cleanly.
    src = """
pub enum Ix { Alpha, Beta }
pub fn a_encode(s: SomeIx) -> u8 {
    match s {
        SomeIx::Alpha => 0u8,
        SomeIx::Beta => 1u8,
    }
}
pub fn route(d: &[u8]) -> R {
    match decode(d) {
        Ix::Alpha => handle_alpha(),
        Ix::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}), instruction_enum_name="Ix")
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha"
    assert by["Beta"].handler == "handle_beta"


def test_nested_match_arm_handler_not_misattributed():
    # R4: an arm that delegates to a nested match (`Alpha => match get_sub() {..}`) must NOT
    # resolve its handler to the nested scrutinee `get_sub`; nested arms must not leak into
    # the outer dispatch. Alpha delegates -> handler unresolved -> coverage_failure (flagged,
    # not silently wrong). Beta resolves normally.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha => match get_sub() {
            Sub::X => handle_x(),
            Sub::Y => handle_y(),
        },
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn get_sub() -> Sub { Sub::X }
fn handle_x() -> R { Ok(()) }
fn handle_y() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler != "get_sub"  # not the nested scrutinee
    assert "X" not in by and "Y" not in by    # nested-match variants didn't leak in
    assert by["Beta"].handler == "handle_beta"


def test_guarded_duplicate_variant_flags_incomplete():
    # R4: two arms for the SAME variant routing to DIFFERENT handlers (guard split). Only the
    # first is recorded -> the rest must be flagged COVERAGE_INCOMPLETE, never silently dropped.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha if cond() => handle_alpha_a(),
        Instruction::Alpha => handle_alpha_b(),
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn cond() -> bool { true }
fn handle_alpha_a() -> R { Ok(()) }
fn handle_alpha_b() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "COVERAGE_INCOMPLETE", a.notes
    assert any("guarded/duplicate" in n for n in a.notes)


def test_bare_use_imported_variant_arms_routed():
    # R5: `use Instruction::*;` then bare `Alpha => h()` arms (identifier, not scoped). These
    # MUST still be enumerated (a real Solana idiom) — not silently dropped to UNKNOWN.
    src = """
use crate::instruction::Instruction::*;
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Alpha => handle_alpha(),
        Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].handler == "handle_alpha" and by["Alpha"].handler_file
    assert by["Beta"].handler == "handle_beta" and by["Beta"].handler_file


def test_guard_expression_variants_not_routed():
    # R5: a guard `X::Beta if cond(X::Alpha)` must route ONLY Beta — the `X::Alpha` inside the
    # guard expression must NOT be picked up as a routed variant (the match_pattern wrapper
    # holds the guard; we descend only into the pattern proper).
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Beta if precheck(Instruction::Alpha) => handle_beta(),
        Instruction::Alpha => handle_alpha(),
        _ => Err(()),
    }
}
fn precheck(_x: Instruction) -> bool { true }
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    # Beta routes to handle_beta (NOT misattributed via the guard); Alpha to handle_alpha.
    assert by["Beta"].handler == "handle_beta"
    assert by["Alpha"].handler == "handle_alpha"
    assert a.summary()["coverage_failures"] == 0


def test_reference_pattern_arm_routed():
    # R5: matching on `&ix` produces `&Instruction::Alpha` reference patterns — must route.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(ix: &Instruction) -> R {
    match ix {
        &Instruction::Alpha => handle_alpha(),
        &Instruction::Beta => handle_beta(),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    assert {e.instruction for e in a.instructions} == {"Alpha", "Beta"}
    assert all(e.handler_file for e in a.instructions)


def test_zero_variant_enum_is_unknown():
    # R5: an empty `Instruction {}` enum must NOT report a clean "0 instructions" — the real
    # surface may be discriminant/macro dispatched. Flag UNKNOWN.
    a = extract_native(_prog({"lib.rs": "pub enum Instruction {}\npub fn f(){}\n"}))
    assert a.status == "COVERAGE_UNKNOWN", a.notes
    assert a.instructions == []


def test_direct_nested_match_arm_is_coverage_failure():
    # SCOPE: a DIRECT `=> match {..}` arm has no single handler (best-effort resolution does not
    # descend into the nested match), so it surfaces as a coverage_failure — flagged, never a
    # silently-clean handler. The TOP-LEVEL entrypoint count stays correct (Admin + Beta).
    src = """
pub enum Instruction { Admin, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Admin => match sub(d) {
            Sub::A => handle_a(),
            Sub::B => handle_b(),
        },
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn sub(d: &[u8]) -> Sub { Sub::A }
fn handle_a() -> R { Ok(()) }
fn handle_b() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes  # top-level authority is complete
    by = {e.instruction: e for e in a.instructions}
    assert {e.instruction for e in a.instructions} == {"Admin", "Beta"}  # top-level count correct
    assert by["Admin"].routed and by["Admin"].handler is None and by["Admin"].coverage_failure
    assert by["Beta"].handler == "handle_beta" and not by["Beta"].coverage_failure


def test_wrapped_subdispatch_keeps_top_level_enumeration():
    # SCOPE (red-team R6-R9): intra-instruction sub-dispatch is NOT classified here (statically
    # undecidable vs. an internal `match cfg {..}`). A block/if-wrapped sub-dispatch must NOT
    # false-flag the WHOLE authority; the top-level entrypoints stay correctly enumerated and the
    # sibling instruction resolves cleanly. (Full sub-instruction enumeration = secondary generator.)
    block_src = """
pub enum Instruction { Admin, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Admin => {
            pre_check();
            match sub(d) { Sub::A => handle_a(), Sub::B => handle_b() }
        },
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn pre_check() {}
fn sub(d: &[u8]) -> Sub { Sub::A }
fn handle_a() -> R { Ok(()) }
fn handle_b() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": block_src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert {e.instruction for e in a.instructions} == {"Admin", "Beta"}
    assert by["Beta"].handler == "handle_beta" and not by["Beta"].coverage_failure
    # ACCEPTED trade-off (characterized so a regression is visible): a block-wrapped sub-dispatch
    # resolves Admin to the best-effort pre-call `pre_check`. The top-level count is correct; the
    # SCOPE note discloses that intra-instruction sub-dispatch is the secondary generator's job.
    assert by["Admin"].handler == "pre_check"
    assert any("SCOPE:" in n for n in a.notes)  # scope gap disclosed on the authority


def test_named_encoder_does_not_beat_unnamed_real_dispatch():
    # R10: dropping the `dispatch_named` name nudge — a helper-CALLING encoder named
    # `dispatch_encode` must NOT outrank a real dispatch named `route` (both tie on
    # score+handler_calls). With no name tiebreak, the tie is flagged ambiguous (INCOMPLETE),
    # never silently resolved to the encoder's wrong handlers.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn dispatch_encode(ix: Instruction) -> u8 {
    match ix { Instruction::Alpha => to_byte_a(), Instruction::Beta => to_byte_b() }
}
pub fn route(d: &[u8]) -> R {
    match decode(d) { Instruction::Alpha => handle_alpha(), Instruction::Beta => handle_beta(), _ => Err(()) }
}
fn to_byte_a() -> u8 { 0 }
fn to_byte_b() -> u8 { 1 }
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "COVERAGE_INCOMPLETE", a.notes  # flagged, never silently the encoder
    assert any("equally-ranked" in n for n in a.notes)


def test_helper_calling_encoder_does_not_outrank_real_dispatch_with_subdispatch_arm():
    # R11: a same-coverage helper-CALLING match (e.g. a per-variant logger/metrics fn) has MORE
    # handler_calls than a real dispatch where one arm is an inline sub-dispatch (handler None).
    # handler_calls alone would hand the win to the logger (wrong handlers, false-clean Admin).
    # Two full-coverage handler-calling matches that route differently must flag ambiguous.
    src = """
pub enum Instruction { Alpha, Beta, Admin }
pub fn log_all(ix: Instruction) {
    match ix {
        Instruction::Alpha => log_a(),
        Instruction::Beta  => log_b(),
        Instruction::Admin => log_c(),
    }
}
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha => handle_alpha(),
        Instruction::Beta  => handle_beta(),
        Instruction::Admin => match sub(d) { Sub::X => admin_x(), Sub::Y => admin_y() },
        _ => Err(()),
    }
}
fn log_a() {} fn log_b() {} fn log_c() {}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
fn sub(d: &[u8]) -> Sub { Sub::X }
fn admin_x() -> R { Ok(()) }
fn admin_y() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "COVERAGE_INCOMPLETE", a.notes  # flagged, never the logger's wrong handlers
    assert any("equally-ranked" in n for n in a.notes)


def test_captured_at_binding_pattern_routed():
    # R6: `ix @ Instruction::Alpha => h(ix)` — the captured (@) binding wraps the variant path;
    # the variant must still route, not be dropped to a false coverage failure.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        ix @ Instruction::Alpha => handle_alpha(ix),
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn handle_alpha(_x: Instruction) -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Alpha"].routed and by["Alpha"].handler == "handle_alpha"
    assert by["Beta"].handler == "handle_beta"


def test_inline_value_match_not_flagged_as_subdispatch():
    # R7 regression: an incidental `match` on an Option/local inside a normal arm (arms return
    # literals/bindings, NOT function calls) must NOT be mistaken for a sub-dispatch. The arm
    # routes cleanly to its real handler; status stays OK.
    src = """
pub enum Instruction { Transfer, Mint }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Transfer => {
            let mode = match opt() { Some(v) => v, None => 0 };
            handle_transfer(mode)
        },
        Instruction::Mint => handle_mint(),
        _ => Err(()),
    }
}
fn opt() -> Option<u8> { None }
fn handle_transfer(_m: u8) -> R { Ok(()) }
fn handle_mint() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Transfer"].handler == "handle_transfer" and not by["Transfer"].coverage_failure
    assert by["Mint"].handler == "handle_mint"
    assert a.summary()["coverage_failures"] == 0


def test_captured_binding_name_collision_routes_only_pattern():
    # R7 regression: `Alpha @ Instruction::Beta => h()` — the binding `Alpha` shares a name with
    # a variant. Only Beta (the pattern after @) must route; the binding must NOT be counted.
    src = """
pub enum Instruction { Alpha, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Alpha @ Instruction::Beta => handle_beta(Alpha),
        Instruction::Alpha => handle_alpha(),
        _ => Err(()),
    }
}
fn handle_alpha() -> R { Ok(()) }
fn handle_beta(_x: Instruction) -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    # Beta routes via the captured pattern; Alpha routes via its own arm (NOT via the binding).
    assert by["Beta"].handler == "handle_beta"
    assert by["Alpha"].handler == "handle_alpha"


def test_method_call_subdispatch_top_level_still_enumerated():
    # SCOPE: a method-call sub-dispatch inside an arm is intra-instruction surface (handled by
    # the secondary-dispatch generator, not here). The TOP-LEVEL authority must stay correct:
    # both entrypoints enumerated, the sibling resolves cleanly, and the nested-match scrutinee
    # is never silently claimed as Admin's handler with a clean bill.
    src = """
pub enum Instruction { Admin, Beta }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Admin => match sub(d) {
            Sub::Create => p.handle_create(),
            Sub::Delete => p.handle_delete(),
        },
        Instruction::Beta => handle_beta(),
        _ => Err(()),
    }
}
fn sub(d: &[u8]) -> Sub { Sub::Create }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert {e.instruction for e in a.instructions} == {"Admin", "Beta"}
    # Direct `=> match {..}` arm -> no single handler -> coverage_failure (not silently clean).
    assert by["Admin"].handler != "sub" and by["Admin"].coverage_failure
    assert by["Beta"].handler == "handle_beta" and not by["Beta"].coverage_failure


def test_result_method_value_match_not_flagged():
    # R8: a Result value-match whose arms call METHODS (`v.process()`, `e.log()`) is NOT a
    # sub-dispatch (Ok/Err are unqualified — not scoped enum routing). Must stay status OK.
    src = """
pub enum Instruction { Transfer, Mint }
pub fn process_instruction(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Transfer => {
            let x = match parse(d) { Ok(v) => v.normalize(), Err(e) => e.recover() };
            handle_transfer(x)
        },
        Instruction::Mint => handle_mint(),
        _ => Err(()),
    }
}
fn parse(d: &[u8]) -> Result<u8, E> { Ok(0) }
fn handle_transfer(_x: u8) -> R { Ok(()) }
fn handle_mint() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "OK", a.notes
    by = {e.instruction: e for e in a.instructions}
    assert by["Transfer"].handler == "handle_transfer" and not by["Transfer"].coverage_failure
    assert a.summary()["coverage_failures"] == 0


def test_two_function_calling_matches_over_same_enum_flagged():
    # R4: two equally-ranked matches over the SAME enum, both calling handlers but routing
    # DIFFERENTLY -> ambiguous dispatch -> COVERAGE_INCOMPLETE (never silently pick one).
    src = """
pub enum Instruction { Alpha, Beta }
pub fn validate(ix: &Instruction) -> R {
    match ix {
        Instruction::Alpha => check_alpha(),
        Instruction::Beta => check_beta(),
    }
}
pub fn route(d: &[u8]) -> R {
    match decode(d) {
        Instruction::Alpha => handle_alpha(),
        Instruction::Beta => handle_beta(),
    }
}
fn check_alpha() -> R { Ok(()) }
fn check_beta() -> R { Ok(()) }
fn handle_alpha() -> R { Ok(()) }
fn handle_beta() -> R { Ok(()) }
"""
    a = extract_native(_prog({"lib.rs": src}))
    assert a.status == "COVERAGE_INCOMPLETE", a.notes
    assert any("equally-ranked" in n for n in a.notes)


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
