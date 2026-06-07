"""Tests for the L1 Anchor entrypoint reader (step 6).

Runnable: `python -m pytest tests/test_l1_anchor_entrypoints.py` OR
`python tests/test_l1_anchor_entrypoints.py`.

Cardinal property: never a clean-looking empty authority. No #[program] -> COVERAGE_UNKNOWN;
found-but-unenumerable / multiple / skipped -> COVERAGE_INCOMPLETE. Detection is by the
#[program] attribute (module name is irrelevant) and instructions by `pub` visibility.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_pipeline.l1.anchor_entrypoints import extract_anchor  # noqa: E402

_VALID = """
use anchor_lang::prelude::*;
declare_id!("Fg6PaFpoGXkYsidMpWxqSWY3r7e7uXjQ9zfQ8sJ2example");

#[program]
pub mod my_program {
    use super::*;

    pub fn initialize(ctx: Context<Initialize>, data: u64) -> Result<()> {
        Ok(())
    }

    pub fn update_value(ctx: Context<Update>, x: u64) -> Result<()> {
        Ok(())
    }

    fn internal_helper(a: u64) -> u64 { a + 1 }  // non-pub: NOT an instruction
}

#[derive(Accounts)]
pub struct Initialize<'info> { pub user: Signer<'info> }
"""

# attribute on its own line above the mod, mod renamed arbitrarily (name must not matter)
_RENAMED = """
#[program]
pub mod totally_different_name {
    pub fn only_one(ctx: Context<X>) -> Result<()> { Ok(()) }
}
"""

_NATIVE = """
pub enum Instruction { A, B }
pub fn process(ix: Instruction) -> u64 {
    match ix { Instruction::A => 1, Instruction::B => 2 }
}
"""

_TWO_PROGRAMS = """
#[program]
pub mod prog_a { pub fn a_one(ctx: Context<X>) -> Result<()> { Ok(()) } }
#[program]
pub mod prog_b { pub fn b_one(ctx: Context<Y>) -> Result<()> { Ok(()) } }
"""

_EMPTY_PROGRAM = """
#[program]
pub mod empty_prog {
    fn private_only() -> u64 { 0 }
}
"""


def _repo(body: str, name: str = "lib.rs") -> Path:
    d = Path(tempfile.mkdtemp())
    (d / name).write_text(body, encoding="utf-8")
    return d


def test_valid_anchor_enumerates_pub_fns_only():
    auth = extract_anchor(_repo(_VALID))
    assert auth.status == "OK", auth.notes
    names = [e.instruction for e in auth.instructions]
    assert names == ["initialize", "update_value"], names  # internal_helper excluded
    assert auth.program_kind == "anchor"
    # every Anchor instruction is routed by the macro -> no coverage failure
    assert auth.failures == []
    assert all(e.routed and e.handler and e.handler_file for e in auth.instructions)
    # the excluded private helper is noted (informational, status stays OK)
    assert any("excluded" in n for n in auth.notes)


def test_module_name_is_irrelevant():
    auth = extract_anchor(_repo(_RENAMED))
    assert auth.status == "OK", auth.notes
    assert [e.instruction for e in auth.instructions] == ["only_one"]


def test_native_program_is_unknown_not_empty():
    auth = extract_anchor(_repo(_NATIVE))
    assert auth.status == "COVERAGE_UNKNOWN"
    assert auth.instructions == []
    assert not auth.authority_complete
    assert auth.summary()["coverage_failures"] is None  # never "0 failures" off an unknown


def test_empty_repo_is_unknown():
    auth = extract_anchor(Path(tempfile.mkdtemp()))
    assert auth.status == "COVERAGE_UNKNOWN"
    assert auth.instructions == []


def test_multiple_programs_incomplete_but_enumerates_all():
    auth = extract_anchor(_repo(_TWO_PROGRAMS))
    assert auth.status == "COVERAGE_INCOMPLETE"
    names = sorted(e.instruction for e in auth.instructions)
    assert names == ["a_one", "b_one"], names
    assert any("modules found" in n for n in auth.notes)


def test_program_with_no_pub_fns_is_incomplete():
    auth = extract_anchor(_repo(_EMPTY_PROGRAM))
    assert auth.status == "COVERAGE_INCOMPLETE"
    assert auth.instructions == []
    assert any("zero pub instruction" in n for n in auth.notes)


def test_summary_shape():
    auth = extract_anchor(_repo(_VALID))
    s = auth.summary()
    assert s["program_kind"] == "anchor"
    assert s["declared_instructions"] == 2
    assert s["routed"] == 2
    assert s["dispatch_at"] is not None  # the #[program] module location


def test_handler_lines_are_recorded():
    auth = extract_anchor(_repo(_VALID))
    for e in auth.instructions:
        assert isinstance(e.handler_line, int) and e.handler_line > 0
        assert e.handler_file == "lib.rs"


def test_deterministic():
    r = _repo(_VALID)
    a = extract_anchor(r).summary()
    b = extract_anchor(r).summary()
    assert a == b


def test_instructions_across_files():
    d = Path(tempfile.mkdtemp())
    (d / "a.rs").write_text(_RENAMED, encoding="utf-8")
    (d / "b.rs").write_text("pub fn not_in_program(ctx: Context<X>) -> Result<()> { Ok(()) }",
                            encoding="utf-8")
    auth = extract_anchor(d)
    # only the fn inside #[program] counts; the free fn in b.rs is not an instruction
    assert [e.instruction for e in auth.instructions] == ["only_one"]


# ----- review round 1: doc-comment detection, pub(crate) exclusion, scope note, multi-prog -----
_DOC_COMMENTED = """
/// This program does important things.
#[program]
pub mod documented_prog {
    pub fn the_instruction(ctx: Context<X>) -> Result<()> { Ok(()) }
}
"""

_PUB_CRATE = """
#[program]
pub mod mixed_vis {
    pub fn real_instruction(ctx: Context<X>) -> Result<()> { Ok(()) }
    pub(crate) fn crate_helper(a: u64) -> u64 { a }
    pub(super) fn super_helper(a: u64) -> u64 { a }
}
"""


def test_doc_commented_program_is_detected():
    # a /// doc comment between #[program] and the mod must NOT hide detection.
    auth = extract_anchor(_repo(_DOC_COMMENTED))
    assert auth.status == "OK", auth.notes
    assert [e.instruction for e in auth.instructions] == ["the_instruction"]


def test_pub_crate_and_pub_super_excluded():
    # only bare `pub fn` is an Anchor instruction; pub(crate)/pub(super) are helpers, excluded.
    auth = extract_anchor(_repo(_PUB_CRATE))
    assert [e.instruction for e in auth.instructions] == ["real_instruction"]
    assert any("excluded" in n for n in auth.notes), auth.notes
    # status stays OK — restricted-visibility helpers are not a coverage gap
    assert auth.status == "OK", auth.notes


def test_scope_note_always_present():
    for body in (_VALID, _NATIVE, _DOC_COMMENTED, _TWO_PROGRAMS):
        auth = extract_anchor(_repo(body))
        assert any(n.startswith("SCOPE:") for n in auth.notes), (body[:30], auth.notes)


def test_multiple_programs_dispatch_is_ambiguous():
    auth = extract_anchor(_repo(_TWO_PROGRAMS))
    assert auth.summary()["dispatch_at"] is None  # not misleadingly pinned to the first program
    assert any("program mods at:" in n for n in auth.notes)


# ----- review round 2: stacked attr+comment interposition, no-note-when-clean -----
_STACKED_INTERPOSITION = """
#[program]
/// Main program logic.
#[derive(Clone)]
pub mod stacked_prog {
    pub fn bar(ctx: Context<X>) -> Result<()> { Ok(()) }
}
"""

_ONLY_PUB = """
#[program]
pub mod clean_prog {
    pub fn a(ctx: Context<X>) -> Result<()> { Ok(()) }
    pub fn b(ctx: Context<Y>) -> Result<()> { Ok(()) }
}
"""


def test_stacked_attr_and_comment_interposition_detected():
    # #[program] furthest from the mod, with a doc comment AND another attribute between them —
    # the multi-step sibling walk must still find it.
    auth = extract_anchor(_repo(_STACKED_INTERPOSITION))
    assert auth.status == "OK", auth.notes
    assert [e.instruction for e in auth.instructions] == ["bar"]


def test_no_exclusion_note_when_only_pub_fns():
    # a #[program] with ONLY bare pub fns must NOT emit a "not bare pub / excluded" note.
    auth = extract_anchor(_repo(_ONLY_PUB))
    assert [e.instruction for e in auth.instructions] == ["a", "b"]
    assert not any("excluded" in n for n in auth.notes), auth.notes


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
