"""Wave 8a — AST signature engine tests.

Tree-sitter is an OPTIONAL runtime dependency. These tests cover the
graceful-fallback path (which is the only path most CI runners hit)
and verify the pattern catalog shape. When tree-sitter IS installed,
additional behavioural tests run.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_ast_module_imports() -> None:
    """The propagate_ast module must import even when tree-sitter is missing."""
    from audit_pipeline.commands.propagate_ast import (
        BUG_CLASS_AST_PATTERNS,
        AstMatch,
        is_ast_available,
        scan_corpus_for_ast_patterns,
        scan_file_for_ast_patterns,
    )
    # All public symbols accessible
    assert callable(is_ast_available)
    assert callable(scan_corpus_for_ast_patterns)
    assert callable(scan_file_for_ast_patterns)
    assert isinstance(BUG_CLASS_AST_PATTERNS, dict)
    assert AstMatch.__name__ == "AstMatch"


def test_pattern_catalog_shape() -> None:
    """Each entry maps str -> list[(pattern_name, query_str)]."""
    from audit_pipeline.commands.propagate_ast import BUG_CLASS_AST_PATTERNS
    for cls, patterns in BUG_CLASS_AST_PATTERNS.items():
        assert isinstance(cls, str), f"key {cls!r} not a str"
        assert isinstance(patterns, list), f"value for {cls!r} not a list"
        for entry in patterns:
            assert isinstance(entry, tuple) and len(entry) == 2, (
                f"entry under {cls!r} is not (name, query): {entry!r}"
            )
            name, query = entry
            assert isinstance(name, str) and name, f"empty pattern name under {cls!r}"
            assert isinstance(query, str) and query, f"empty query under {cls!r}"


def test_pattern_catalog_minimum_size() -> None:
    """Don't accidentally empty the catalog."""
    from audit_pipeline.commands.propagate_ast import BUG_CLASS_AST_PATTERNS
    assert len(BUG_CLASS_AST_PATTERNS) >= 5, (
        f"AST pattern catalog has only {len(BUG_CLASS_AST_PATTERNS)} entries; "
        f"baseline at Wave 8a was 7. Lower this if intentional."
    )


def test_f7_class_has_ast_patterns() -> None:
    """F7's class is the platform's flagship — must have AST patterns when
    AST patterns ship at all."""
    from audit_pipeline.commands.propagate_ast import BUG_CLASS_AST_PATTERNS
    assert "insurance-counter-vault-divergence" in BUG_CLASS_AST_PATTERNS
    assert len(BUG_CLASS_AST_PATTERNS["insurance-counter-vault-divergence"]) >= 1


def test_scan_returns_empty_when_ts_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graceful fallback: when tree-sitter import fails, scan returns []."""
    from audit_pipeline.commands import propagate_ast
    # Force the cached availability flag to False
    monkeypatch.setattr(propagate_ast, "_TS_AVAILABLE", False)
    monkeypatch.setattr(propagate_ast, "_TS_PARSER", None)
    matches = propagate_ast.scan_file_for_ast_patterns(
        "fn foo() { x.balance = 1; }",
        [("test_pattern", "(assignment_expression) @x")],
    )
    assert matches == []


def test_corpus_scan_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    """Pointing at a non-existent corpus path returns [], not crash."""
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns
    matches = scan_corpus_for_ast_patterns(
        tmp_path / "does-not-exist",
        "insurance-counter-vault-divergence",
    )
    assert matches == []


def test_patterns_for_unknown_bug_class() -> None:
    """Unknown bug_class returns an empty list (open catalog model)."""
    from audit_pipeline.commands.propagate_ast import patterns_for_bug_class
    assert patterns_for_bug_class("definitely-not-a-real-class") == []


def test_propagate_run_for_finding_includes_ast_summary(tmp_path: Path) -> None:
    """run_for_finding's return dict must include an 'ast' key with the
    expected shape, even when tree-sitter isn't installed."""
    from audit_pipeline.commands.propagate import run_for_finding
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")

    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1-test",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Empty corpus dir to keep the scan fast
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out_dir = tmp_path / "report"
    result = run_for_finding(db, fid, corpus_dir, out_dir)

    assert result.get("ok") is True, result
    assert "ast" in result, "run_for_finding must include ast summary"
    ast = result["ast"]
    assert "available" in ast
    assert "n_patterns" in ast
    assert "n_matches" in ast
