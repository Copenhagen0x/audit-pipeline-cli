"""Wave 8a — AST signature engine tests.

Tree-sitter is an OPTIONAL runtime dependency. These tests cover the
graceful-fallback path (which is the only path most CI runners hit)
and verify the pattern catalog shape. When tree-sitter IS installed,
additional behavioural tests run.

Patch #15 (audit-015 / HIGH ffd663b2) regression tests live at the
bottom of this file — they cover the symlink + NTFS-junction guards
in `scan_corpus_for_ast_patterns`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _can_symlink(tmp_path: Path) -> bool:
    """Probe whether the current runtime can create symlinks.

    On Windows, symlink creation requires admin privilege OR developer
    mode — many CI runners (and devs) don't have it. We skip the
    integration tests in that case but still run the unit tests that
    don't require live symlinks.
    """
    probe = tmp_path / "_probe_target"
    probe.write_text("x", encoding="utf-8")
    link = tmp_path / "_probe_link"
    try:
        os.symlink(probe, link)
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            link.unlink()
        except OSError:
            pass
        try:
            probe.unlink()
        except OSError:
            pass
    return True


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


# ──────────────────────────────────────────────────────────────────────
# Patch #15 (audit-015) — symlink / junction guard regressions
# ──────────────────────────────────────────────────────────────────────


def test_is_link_or_junction_false_for_regular_file(tmp_path: Path) -> None:
    """Regular files must NOT trigger the link/junction guard."""
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    f = tmp_path / "real.rs"
    f.write_text("fn main() {}", encoding="utf-8")
    assert _is_link_or_junction(f) is False


def test_is_link_or_junction_false_for_regular_directory(tmp_path: Path) -> None:
    """Regular dirs must NOT trigger the guard."""
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    d = tmp_path / "real_dir"
    d.mkdir()
    assert _is_link_or_junction(d) is False


def test_is_link_or_junction_true_for_symlink(tmp_path: Path) -> None:
    """File symlinks must trigger the guard."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    target = tmp_path / "target.rs"
    target.write_text("fn target() {}", encoding="utf-8")
    link = tmp_path / "link.rs"
    os.symlink(target, link)
    assert _is_link_or_junction(link) is True


def test_is_link_or_junction_true_for_directory_symlink(tmp_path: Path) -> None:
    """Directory symlinks must trigger the guard."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    target_dir = tmp_path / "target_dir"
    target_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    os.symlink(target_dir, link_dir, target_is_directory=True)
    assert _is_link_or_junction(link_dir) is True


def test_is_link_or_junction_true_for_broken_symlink(tmp_path: Path) -> None:
    """Dangling symlinks must trigger the guard (fail-closed)."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    link = tmp_path / "broken.rs"
    os.symlink(tmp_path / "does_not_exist.rs", link)
    assert _is_link_or_junction(link) is True


def test_is_link_or_junction_true_for_mocked_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the Windows reparse-point branch via mocked os.lstat.

    Patch #15 R2: the live `mklink /J` test below only runs on Windows.
    On Linux CI the reparse-point branch (`flags & 0x400`) is otherwise
    unreachable. This mock confirms the branch fires correctly when the
    OS reports a reparse-point file attribute, regardless of platform.

    Defends against a future refactor that accidentally zeros the flag
    check — Linux CI would still catch the regression.
    """
    import audit_pipeline.commands.propagate_ast as mod

    real_file = tmp_path / "decoy.rs"
    real_file.write_text("fn x() {}", encoding="utf-8")

    # Build a fake lstat result whose st_file_attributes has the
    # reparse-point bit set. The real st_file_attributes is only
    # populated by Python on Windows; we synthesize it for the test.
    real_stat = os.lstat(real_file)

    class _FakeStat:
        st_file_attributes = mod._FILE_ATTRIBUTE_REPARSE_POINT  # 0x400
        def __getattr__(self, name: str):
            return getattr(real_stat, name)

    def fake_lstat(_p):
        return _FakeStat()

    monkeypatch.setattr(mod.os, "lstat", fake_lstat)
    # The file is not actually a symlink, so the first branch
    # (`p.is_symlink()`) returns False, and we fall through to
    # the lstat branch. NOTE: `p.is_symlink()` may also call
    # os.lstat internally on some platforms, but it returns the
    # cached boolean rather than re-checking st_file_attributes.
    # In practice on POSIX `is_symlink()` returns False for our
    # regular file and the lstat branch fires. On Windows likewise.
    # Sanity: confirm the real file is not a symlink.
    assert real_file.is_symlink() is False
    assert mod._is_link_or_junction(real_file) is True


_MKLINK_AVAILABLE = sys.platform == "win32"


def _try_create_junction(link: Path, target: Path) -> bool:
    """Attempt to create an NTFS junction via `mklink /J`. Returns True
    on success. `mklink /J` does NOT require admin or developer mode,
    but `cmd.exe` may not be on PATH in unusual CI environments."""
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and link.exists()


def test_is_link_or_junction_true_for_ntfs_junction(tmp_path: Path) -> None:
    """Exercise the live Windows-junction branch.

    `mklink /J` creates an NTFS junction (reparse point of type
    IO_REPARSE_TAG_MOUNT_POINT) for which `Path.is_symlink()` returns
    False on all Python versions. The `os.lstat` reparse-point branch
    is the PRIMARY guard against the original CVE attack on Windows.
    """
    if not _MKLINK_AVAILABLE:
        pytest.skip("mklink only available on Windows")
    from audit_pipeline.commands.propagate_ast import _is_link_or_junction
    target_dir = tmp_path / "junction_target"
    target_dir.mkdir()
    junction = tmp_path / "junction_link"
    if not _try_create_junction(junction, target_dir):
        pytest.skip("mklink /J unavailable in this environment")
    # Sanity: junctions do NOT show up as symlinks via is_symlink().
    assert junction.is_symlink() is False, (
        "Test premise broken: Path.is_symlink() now catches junctions; "
        "if this fires, the reparse-point branch may be removable."
    )
    assert _is_link_or_junction(junction) is True


def test_scan_skips_ntfs_junction_at_repo_dir_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test: `mklink /J corpus\\evil C:\\some_outside_dir`
    must not let the AST scanner read files in the junction target.

    Mirrors `test_scan_skips_symlinked_repo_dir_at_corpus_level` but
    uses an NTFS junction (no admin/dev-mode required) instead of a
    POSIX symlink. On Windows CI this exercises the outer-loop
    `_is_link_or_junction` guard via the live reparse-point path."""
    if not _MKLINK_AVAILABLE:
        pytest.skip("mklink only available on Windows")
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns

    outside_repo = tmp_path / "outside_repo"
    outside_repo.mkdir()
    (outside_repo / "secret.rs").write_text(_SECRET_MARKER, encoding="utf-8")

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    junction_path = corpus / "evil_repo"
    if not _try_create_junction(junction_path, outside_repo):
        pytest.skip("mklink /J unavailable in this environment")

    scanned: list[str] = []
    _install_fake_ast_scanner(monkeypatch, scanned)
    scan_corpus_for_ast_patterns(corpus, "insurance-counter-vault-divergence")
    assert all(_SECRET_MARKER not in s for s in scanned), (
        "Secret content scanned through NTFS junction at corpus level!"
    )


def _install_fake_ast_scanner(monkeypatch: pytest.MonkeyPatch, scanned: list[str]) -> None:
    """Force the AST gates open and record every content blob passed to
    the per-file scanner. Lets us assert that an attacker-controlled file
    is NEVER opened, even on machines without tree-sitter installed."""
    from audit_pipeline.commands import propagate_ast

    monkeypatch.setattr(propagate_ast, "_try_load_tree_sitter", lambda: True)
    monkeypatch.setattr(
        propagate_ast,
        "patterns_for_bug_class",
        lambda _bug_class: [("p1", "(any) @x")],
    )

    def _tracking_scan(content: str, _patterns: list) -> list:
        scanned.append(content)
        return []

    monkeypatch.setattr(
        propagate_ast, "scan_file_for_ast_patterns", _tracking_scan,
    )


_SECRET_MARKER = "JELLEO_TEST_SECRET_DO_NOT_LEAK"


def test_scan_skips_planted_file_symlink_to_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original CVE: corpus/repo/programs/x.rs → /outside/secret.rs
    must NOT be opened or scanned."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns

    secret = tmp_path / "outside" / "secret.rs"
    secret.parent.mkdir()
    secret.write_text(_SECRET_MARKER, encoding="utf-8")

    corpus = tmp_path / "corpus"
    repo = corpus / "repo_a"
    programs = repo / "programs"
    programs.mkdir(parents=True)
    (programs / "real.rs").write_text("fn real() {}", encoding="utf-8")
    os.symlink(secret, programs / "x.rs")

    scanned: list[str] = []
    _install_fake_ast_scanner(monkeypatch, scanned)
    scan_corpus_for_ast_patterns(corpus, "insurance-counter-vault-divergence")
    assert all(_SECRET_MARKER not in s for s in scanned), (
        "Secret content was passed to the scanner — symlink guard failed!"
    )


def test_scan_skips_symlinked_subdirectory_inside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`corpus/repo/programs` is itself a symlink to /outside_secrets.
    The .rs files INSIDE that symlinked directory aren't symlinks themselves,
    so the file-level is_symlink() check doesn't fire. The defense-in-depth
    relative_to() check must catch the escape."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns

    secrets_dir = tmp_path / "outside_secrets"
    secrets_dir.mkdir()
    (secrets_dir / "secret.rs").write_text(_SECRET_MARKER, encoding="utf-8")

    corpus = tmp_path / "corpus"
    repo = corpus / "repo_b"
    repo.mkdir(parents=True)
    os.symlink(secrets_dir, repo / "programs", target_is_directory=True)

    scanned: list[str] = []
    _install_fake_ast_scanner(monkeypatch, scanned)
    scan_corpus_for_ast_patterns(corpus, "insurance-counter-vault-divergence")
    assert all(_SECRET_MARKER not in s for s in scanned), (
        "Secret content scanned through symlinked subdirectory!"
    )


def test_scan_skips_symlinked_repo_dir_at_corpus_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`corpus/evil_repo` itself is a symlink to a directory outside the
    corpus. Without the R1 outer-loop guard the resolved repo_dir would
    point at the attacker target and the containment check would become
    tautological. The outer-loop _is_link_or_junction() guard must catch
    this BEFORE repo_dir_resolved is computed."""
    if not _can_symlink(tmp_path):
        pytest.skip("symlink creation unavailable on this runtime")
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns

    outside_repo = tmp_path / "outside_repo"
    outside_repo.mkdir()
    (outside_repo / "secret.rs").write_text(_SECRET_MARKER, encoding="utf-8")

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    os.symlink(outside_repo, corpus / "evil_repo", target_is_directory=True)

    scanned: list[str] = []
    _install_fake_ast_scanner(monkeypatch, scanned)
    scan_corpus_for_ast_patterns(corpus, "insurance-counter-vault-divergence")
    assert all(_SECRET_MARKER not in s for s in scanned), (
        "Secret content scanned through symlinked repo_dir at corpus level!"
    )


def test_scan_still_reads_real_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the security guards must not block legitimate file scans.
    A normal corpus/repo/programs/real.rs should still be passed to the
    AST scanner."""
    from audit_pipeline.commands.propagate_ast import scan_corpus_for_ast_patterns

    corpus = tmp_path / "corpus"
    programs = corpus / "repo_c" / "programs"
    programs.mkdir(parents=True)
    marker = "JELLEO_TEST_REAL_CODE_MARKER"
    (programs / "real.rs").write_text(f"fn x() {{ {marker} }}", encoding="utf-8")

    scanned: list[str] = []
    _install_fake_ast_scanner(monkeypatch, scanned)
    scan_corpus_for_ast_patterns(corpus, "insurance-counter-vault-divergence")
    assert any(marker in s for s in scanned), (
        "Legitimate file was not scanned — security guards regressed!"
    )


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

    # Corpus with one empty repo subdir (post-audit-018-R3, an entirely
    # empty corpus returns ok=False with reason=corpus_all_repos_filtered).
    # A single empty repo subdir gives safe_repos=[1] so the scan
    # proceeds normally and we exercise the ast-summary code path.
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "empty_repo").mkdir()
    out_dir = tmp_path / "report"
    result = run_for_finding(db, fid, corpus_dir, out_dir)

    assert result.get("ok") is True, result
    assert "ast" in result, "run_for_finding must include ast summary"
    ast = result["ast"]
    assert "available" in ast
    assert "n_patterns" in ast
    assert "n_matches" in ast
