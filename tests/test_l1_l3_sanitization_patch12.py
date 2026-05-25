"""Patch #12 — L1-L3 sanitization / anchor-mode gate-bypass tests.

Closes audit findings:
  * HIGH 7be6aca5 — `_is_anchor_workspace` called with LLM-authored
    PoC text instead of real engine source → gate-bypass via prompt
    injection containing Anchor markers.

Plus R1 expansions caught by reviewer audit:
  * `target_file` from YAML was a YAML-controlled trust signal
    (substring match `programs/*/lib.rs` with no filesystem check)
    → attacker could bypass the gate even with a correct engine_source.
"""

from __future__ import annotations

from audit_pipeline.commands.poc_llm import _is_anchor_workspace

# ─────────────── R0: engine_source signal is authoritative ───────────────


def test_anchor_detection_from_real_engine_source_positive() -> None:
    """Sanity: a real Anchor engine source returns True via the
    primary signal (#[program] / use anchor_lang::prelude::*)."""
    src = (
        "use anchor_lang::prelude::*;\n"
        "\n"
        "#[program]\n"
        "pub mod my_program { /* ... */ }\n"
    )
    assert _is_anchor_workspace(engine_source=src, target_file="any") is True


def test_anchor_detection_from_clean_engine_source_negative() -> None:
    """A non-Anchor engine source returns False even when target_file
    looks anchor-shaped (without filesystem verification)."""
    src = "fn percolator_function() { /* not anchor */ }\n"
    assert _is_anchor_workspace(
        engine_source=src, target_file="programs/x/src/lib.rs"
    ) is False


# ─────────────── R0 regression: LLM output can't spoof gate ───────────────


def test_anchor_detection_rejects_llm_output_as_engine_source() -> None:
    """P12 R0 (HIGH 7be6aca5): the previous bug passed the LLM-authored
    PoC text as engine_source. An attacker who prompted the LLM to
    include Anchor markers could bypass the gate. After R0, only the
    REAL engine source drives detection.

    This test exercises the function directly. The CALL SITE fix
    (passing the real engine_source, not rust_content) is what closes
    the bug in the pipeline; this test asserts the function works
    correctly given a correctly-sourced engine_source."""
    real_engine_clean = "fn percolator() {}"
    llm_authored_anchor_markers = (
        "// LLM-generated test\n"
        "use anchor_lang::prelude::*;\n"
        "#[program]\n"
        "mod fake { fn test() {} }\n"
    )
    # When real engine_source is clean, the function returns False
    # — even if a separate variable (rust_content) contains anchor
    # markers. The pre-R0 bug was that the caller passed rust_content
    # AS engine_source. Verify the function is well-behaved given the
    # post-R0 input shape:
    assert _is_anchor_workspace(
        engine_source=real_engine_clean,
        target_file="src/percolator.rs",
    ) is False
    # ... and would be True if the LLM markers were ACTUALLY in
    # engine_source (the wrong-source scenario that was the bug):
    assert _is_anchor_workspace(
        engine_source=llm_authored_anchor_markers,
        target_file="src/percolator.rs",
    ) is True


# ─────────────── R1: target_file YAML-controlled bypass ───────────────


def test_anchor_detection_target_file_substring_no_longer_bypasses(
    tmp_path,
) -> None:
    """P12 R1 (goober + threat-modeler CRITICAL): the old code returned
    True if `target_file` contained "programs/" and ended with
    "lib.rs", with NO filesystem check. A YAML
    `target_file: "src/programs/x/lib.rs"` would force Anchor mode on
    a non-Anchor target and skip the symbol_grep gate.

    After R1, the path-based signal REQUIRES engine_root to be passed
    AND the file to actually exist under it. Without engine_root, the
    path signal is dropped entirely → fail-safe to non-Anchor."""
    # No engine_root passed → path signal is ignored entirely
    assert _is_anchor_workspace(
        engine_source="// not anchor\n",
        target_file="src/programs/evil/lib.rs",
    ) is False
    # With engine_root passed but the target_file doesn't exist there
    # → path signal correctly returns False
    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    assert _is_anchor_workspace(
        engine_source="// not anchor\n",
        target_file="programs/evil/src/lib.rs",
        engine_root=engine_root,
    ) is False


def test_anchor_detection_target_file_with_real_disk_file_passes(
    tmp_path,
) -> None:
    """The path-based signal must still work for LEGITIMATE Anchor
    workspaces — a `target_file: "programs/X/src/lib.rs"` that
    actually exists on disk under engine_root."""
    engine_root = tmp_path / "engine"
    (engine_root / "programs" / "x" / "src").mkdir(parents=True)
    (engine_root / "programs" / "x" / "src" / "lib.rs").write_text(
        "// real anchor program\n"
    )
    # engine_source is empty → only the path signal can fire
    assert _is_anchor_workspace(
        engine_source="",
        target_file="programs/x/src/lib.rs",
        engine_root=engine_root,
    ) is True


def test_anchor_detection_rejects_path_traversal(tmp_path) -> None:
    """P12 R1: a `target_file` containing `..` must NOT escape
    engine_root even if the resolved file exists outside."""
    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    outside = tmp_path / "outside" / "programs" / "x" / "src"
    outside.mkdir(parents=True)
    (outside / "lib.rs").write_text("// fake anchor outside engine_root\n")
    # The `..` should be rejected at the regex check
    assert _is_anchor_workspace(
        engine_source="",
        target_file="../outside/programs/x/src/lib.rs",
        engine_root=engine_root,
    ) is False


def test_anchor_detection_rejects_wildcard_in_target_file(tmp_path) -> None:
    """P12 R1: `target_file` with glob wildcards (`*` or `?`) must NOT
    pass the path-based signal — the validator requires an exact
    Anchor-shape path. (The wildcard's effect on engine_source
    selection is a separate concern handled at engine_source build
    time; here we lock that the gate signal itself rejects globs.)"""
    engine_root = tmp_path / "engine"
    (engine_root / "programs" / "x" / "src").mkdir(parents=True)
    (engine_root / "programs" / "x" / "src" / "lib.rs").write_text("// x\n")
    assert _is_anchor_workspace(
        engine_source="",
        target_file="programs/*/src/lib.rs",
        engine_root=engine_root,
    ) is False


def test_anchor_detection_rejects_bracket_glob(tmp_path) -> None:
    """P12 R1 (threat-modeler LOW): bracket char-class glob `[x]` is
    rejected — Python's Path treats `[abc]` as a glob class, so
    `target_file: "[s]rc/lib.rs"` could match `src/lib.rs` and skip
    the wildcard guards if `[` weren't checked."""
    engine_root = tmp_path / "engine"
    assert _is_anchor_workspace(
        engine_source="",
        target_file="programs/[x]/src/lib.rs",
        engine_root=engine_root,
    ) is False


# ─────────────── R1: wildcard target_file exfil defense ───────────────


def test_wildcard_glob_restricted_to_source_extensions() -> None:
    """P12 R1 (threat-modeler MEDIUM): the wildcard glob branch must
    only include known source-code extensions (.rs/.move/.sol/.c/.h),
    NOT .env / .toml / .json which could carry secrets.

    Source-grep verify the production code includes the allow-list
    and rejects bracket char-class globs."""
    import inspect

    from audit_pipeline.commands import poc_llm
    src = inspect.getsource(poc_llm)
    # The safe extensions tuple must be present
    assert "_SAFE_GLOB_EXTS" in src
    assert '".rs"' in src and '".move"' in src
    # And the bracket-rejection branch must exist
    assert '"[" in target_file' in src
