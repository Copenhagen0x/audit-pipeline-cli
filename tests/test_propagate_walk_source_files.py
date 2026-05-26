"""Regression tests for the audit-018 hardening on propagate.py.

Patch P18 ports the P15 (audit-015-tree-sitter) hardening pattern to
`propagate.py:_walk_source_files` and adds a sibling `_iter_corpus_repos`
helper so the corpus-level guard runs at the callsite. These tests pin:

  1. `_is_link_or_junction` correctly returns True for symlinks, mocked
     reparse points, and OSError-on-lstat broken entries.
  2. `_walk_source_files` skips a junction/symlink at the repo root.
  3. `_walk_source_files` skips files whose resolved path escapes the repo.
  4. `_walk_source_files` does NOT silently drop legitimate `.rs` files when
     a sibling at the corpus root contains `build`/`target` in its absolute
     path components (the absolute-vs-relative-parts bug R1 fixed).
  5. `_iter_corpus_repos` skips a junction/symlink at the corpus level.

The canonical helper for the same guard lives in `propagate_ast.py` and is
pinned by `tests/test_propagate_ast.py`. This file pins the duplicate copy
in `propagate.py` so a drift between the two cannot regress unnoticed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from audit_pipeline.commands import propagate

# --- _is_link_or_junction ---------------------------------------------------


def test_is_link_or_junction_false_for_regular_file(tmp_path: Path) -> None:
    """A plain `.rs` file is not a link or junction."""
    f = tmp_path / "a.rs"
    f.write_text("fn main() {}", encoding="utf-8")
    assert propagate._is_link_or_junction(f) is False


def test_is_link_or_junction_false_for_regular_dir(tmp_path: Path) -> None:
    """A plain directory is not a link or junction."""
    d = tmp_path / "sub"
    d.mkdir()
    assert propagate._is_link_or_junction(d) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_is_link_or_junction_true_for_posix_symlink(tmp_path: Path) -> None:
    """A POSIX symlink to a file is a link."""
    target = tmp_path / "target.rs"
    target.write_text("// real", encoding="utf-8")
    link = tmp_path / "link.rs"
    link.symlink_to(target)
    assert propagate._is_link_or_junction(link) is True


def test_is_link_or_junction_true_for_mocked_reparse_point(tmp_path: Path) -> None:
    """Mock `os.lstat` to return a `st_file_attributes` with the
    `_FILE_ATTRIBUTE_REPARSE_POINT` bit set. This simulates a Windows
    junction / mount point / cloud-stub placeholder on every platform so
    CI catches the regression even on POSIX runners.
    """
    f = tmp_path / "junction_placeholder"
    f.write_text("x", encoding="utf-8")

    class _MockStat:
        st_file_attributes = propagate._FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = 0o100644
        st_size = 1

    # Surgical mock: only patch ``os.lstat``, not the entire ``os`` module.
    # Replacing the whole module masks every other attribute the helper
    # (or any callee it touches) might need, which paranoid-goober R1 #10
    # flagged as a brittle, over-broad mock.
    # Path.is_symlink is the first check; force it to return False so we
    # exercise the lstat branch.
    with (
        patch.object(propagate.os, "lstat", return_value=_MockStat()),
        patch.object(Path, "is_symlink", return_value=False),
    ):
        assert propagate._is_link_or_junction(f) is True


def test_is_link_or_junction_true_when_lstat_raises_oserror(tmp_path: Path) -> None:
    """If `os.lstat` raises OSError (broken / unreadable parent), treat
    the entry as a link to be safe (fail-closed)."""
    f = tmp_path / "unreadable"
    f.write_text("x", encoding="utf-8")
    with (
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(propagate.os, "lstat", side_effect=OSError("EACCES")),
    ):
        assert propagate._is_link_or_junction(f) is True


def test_is_link_or_junction_true_when_is_symlink_raises_oserror(tmp_path: Path) -> None:
    """If `Path.is_symlink` itself raises OSError (rare but possible),
    treat the entry as a link to be safe."""
    f = tmp_path / "weird"
    f.write_text("x", encoding="utf-8")
    with patch.object(Path, "is_symlink", side_effect=OSError("ELOOP")):
        assert propagate._is_link_or_junction(f) is True


# --- _walk_source_files -----------------------------------------------------


def test_walk_yields_regular_rs_file(tmp_path: Path) -> None:
    """Sanity: a normal `.rs` file under the repo is yielded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("fn main() {}", encoding="utf-8")
    out = list(propagate._walk_source_files(repo))
    assert len(out) == 1
    assert out[0].name == "lib.rs"


def test_walk_skips_non_rs_extensions(tmp_path: Path) -> None:
    """`.py`, `.md`, etc. are not yielded; only `.rs`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("fn main() {}", encoding="utf-8")
    (repo / "README.md").write_text("doc", encoding="utf-8")
    (repo / "tool.py").write_text("# py", encoding="utf-8")
    out = list(propagate._walk_source_files(repo))
    assert len(out) == 1
    assert out[0].name == "lib.rs"


def test_walk_skips_target_node_modules_git_build(tmp_path: Path) -> None:
    """Files under `target/`, `node_modules/`, `.git/`, `build/` are skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("// good", encoding="utf-8")
    for skip in ("target", "node_modules", ".git", "build"):
        sub = repo / skip
        sub.mkdir()
        (sub / "noisy.rs").write_text("// generated", encoding="utf-8")
    out = list(propagate._walk_source_files(repo))
    names = {p.name for p in out}
    assert names == {"lib.rs"}


def test_walk_skip_dirs_uses_relative_parts_not_absolute(tmp_path: Path) -> None:
    """R1 fix: the skip-dir check must compare against components RELATIVE
    to the repo root, not the absolute path. Otherwise a corpus mounted
    under a parent named ``build/`` or ``target/`` (e.g. CI runner that
    clones into ``/build/corpus/``) would silently drop every `.rs` file.
    """
    # Build: tmp_path / "build" / "repo" / "lib.rs"
    # The absolute path contains "build" as a component, but the file is
    # NOT inside a build/ subdirectory of the repo.
    parent = tmp_path / "build"
    parent.mkdir()
    repo = parent / "repo"
    repo.mkdir()
    f = repo / "lib.rs"
    f.write_text("fn main() {}", encoding="utf-8")
    out = list(propagate._walk_source_files(repo))
    names = {p.name for p in out}
    assert names == {"lib.rs"}, (
        f"expected lib.rs to be yielded despite parent dir named 'build'; "
        f"got {names}. The skip-dirs check is still using absolute path "
        f"components instead of repo-relative components."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walk_skips_symlinked_file(tmp_path: Path) -> None:
    """A symlink inside the repo pointing at a real `.rs` outside the repo
    must not be yielded. Otherwise a malicious corpus could exfiltrate
    `/root/.audit-env`-style secrets into the propagation report.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.rs"
    target.write_text("// secret", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("// real", encoding="utf-8")
    (repo / "payload.rs").symlink_to(target)

    out = list(propagate._walk_source_files(repo))
    names = {p.name for p in out}
    assert names == {"lib.rs"}, (
        f"expected only lib.rs; payload.rs is a symlink to outside-repo "
        f"and must be skipped. Got {names}."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walk_skips_repo_root_that_is_a_symlink(tmp_path: Path) -> None:
    """If the `repo` argument itself is a symlink (e.g. `corpus/evil_repo`
    → `/root/`), `_walk_source_files` must yield nothing — otherwise the
    containment check `resolve().relative_to(repo_resolved)` is tautological
    because `repo_resolved` already points at the attacker target.
    """
    real_target = tmp_path / "real"
    real_target.mkdir()
    (real_target / "secret.rs").write_text("// secret", encoding="utf-8")
    junction = tmp_path / "evil_repo"
    junction.symlink_to(real_target, target_is_directory=True)

    out = list(propagate._walk_source_files(junction))
    assert out == [], (
        f"expected zero yields for symlinked repo root; got {out}. The "
        f"outer-loop guard `_is_link_or_junction(repo)` must fire first."
    )


def test_walk_skips_repo_root_that_is_mocked_reparse_point(tmp_path: Path) -> None:
    """Cross-platform variant of the previous test: mock `_is_link_or_junction`
    to return True for the repo argument and verify zero yields. Pins the
    NTFS-junction case without requiring a real Windows runner.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("// would-be-leak", encoding="utf-8")

    real_is_link = propagate._is_link_or_junction
    # Use string comparison via ``os.fspath`` so the mock matches whether
    # the helper passes the original or a resolved Path (e.g. on macOS
    # /tmp resolves to /private/tmp; on POSIX symlink corpora the
    # resolved form differs from the input).
    repo_str = os.fspath(repo)

    def _fake(p: Path) -> bool:
        # Lie about the repo root; otherwise behave normally.
        if os.fspath(p) == repo_str:
            return True
        return real_is_link(p)

    with patch.object(propagate, "_is_link_or_junction", side_effect=_fake):
        out = list(propagate._walk_source_files(repo))
    assert out == [], (
        f"expected zero yields when repo is reported as junction; got {out}."
    )


# --- _iter_corpus_repos -----------------------------------------------------


def test_iter_corpus_yields_safe_repos(tmp_path: Path) -> None:
    """Sanity: regular directories under the corpus are yielded."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "repo_a").mkdir()
    (corpus / "repo_b").mkdir()
    yielded = list(propagate._iter_corpus_repos(corpus))
    names = sorted(r.name for r, _ in yielded)
    assert names == ["repo_a", "repo_b"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_iter_corpus_skips_symlinked_repo_dir(tmp_path: Path) -> None:
    """A repo-dir that is a symlink to outside the corpus must be filtered
    out by `_iter_corpus_repos` BEFORE `_walk_source_files` is called. This
    is the corpus-level outer guard.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real_repo").mkdir()
    outside = tmp_path / "outside_target"
    outside.mkdir()
    (corpus / "evil_repo").symlink_to(outside, target_is_directory=True)

    yielded = list(propagate._iter_corpus_repos(corpus))
    names = sorted(r.name for r, _ in yielded)
    assert names == ["real_repo"], (
        f"expected only real_repo; evil_repo is a symlink to outside the "
        f"corpus and must be skipped by the corpus-level guard. Got {names}."
    )


def test_iter_corpus_skips_mocked_junction_repo_dir(tmp_path: Path) -> None:
    """Cross-platform variant: mock `_is_link_or_junction` to flag one repo
    as a junction; verify it is excluded from the yield set.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    real_repo = corpus / "real_repo"
    real_repo.mkdir()
    junction = corpus / "junction_repo"
    junction.mkdir()

    real_is_link = propagate._is_link_or_junction
    # ``os.fspath`` comparison for resolved-vs-unresolved Path robustness
    # (same reason as the walk-test variant above).
    junction_str = os.fspath(junction)

    def _fake(p: Path) -> bool:
        if os.fspath(p) == junction_str:
            return True
        return real_is_link(p)

    with patch.object(propagate, "_is_link_or_junction", side_effect=_fake):
        yielded = list(propagate._iter_corpus_repos(corpus))
    names = sorted(r.name for r, _ in yielded)
    assert names == ["real_repo"], (
        f"expected junction_repo to be filtered by the corpus-level guard; "
        f"got {names}."
    )


# --- R2 contracts -----------------------------------------------------------


def test_walk_uses_passed_repo_resolved_when_provided(tmp_path: Path) -> None:
    """R2 contract: when caller passes ``repo_resolved``, ``_walk_source_files``
    must use THAT value for the containment check, not re-resolve ``repo``
    internally. This closes the TOCTOU window where ``repo`` could be
    swapped for a junction between the corpus-level resolve (in
    ``_iter_corpus_repos``) and the per-repo resolve.

    Pin this by passing a deliberately-wrong ``repo_resolved`` (a sibling
    path with no overlap to the real repo) and asserting zero yields. If
    the function re-resolved ``repo`` it would still yield ``lib.rs``;
    honoring the passed value means containment fails and nothing yields.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("fn main() {}", encoding="utf-8")

    # A sibling directory; ``lib.rs`` is NOT under this path.
    wrong_resolved = tmp_path / "elsewhere"
    wrong_resolved.mkdir()

    out = list(propagate._walk_source_files(repo, repo_resolved=wrong_resolved))
    assert out == [], (
        f"expected zero yields when repo_resolved is a sibling path; got "
        f"{out}. _walk_source_files must use the caller-provided "
        f"repo_resolved for containment, not silently re-resolve repo."
    )


def test_propagate_search_raises_when_all_repos_filtered(tmp_path: Path) -> None:
    """R2 invariant: ``propagate_search`` must raise ``ClickException`` when
    ``_iter_corpus_repos`` returns an empty list. This distinguishes
    'corpus has no subdirs' from 'every repo was filtered as a junction /
    outside-the-corpus / symlink' — both collapse into the same operator-
    visible error, instead of silently producing an empty report.
    """
    from click.testing import CliRunner

    from audit_pipeline.commands.propagate import propagate_cmd

    empty_corpus = tmp_path / "empty_corpus"
    empty_corpus.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        propagate_cmd,
        ["search", "-c", str(empty_corpus), "-s", "anything"],
        obj={"workspace": str(workspace)},
        standalone_mode=False,
    )
    # With standalone_mode=False, ClickException propagates via result.exception
    # rather than being converted to exit code 1.
    assert result.exception is not None, (
        "expected ClickException when corpus has no safe subdirs; got "
        f"None. Result: exit_code={result.exit_code} output={result.output!r}"
    )
    assert "No safe subdirectories" in str(result.exception), (
        f"expected 'No safe subdirectories' in exception message; got "
        f"{result.exception!r}"
    )


# --- R3 contracts -----------------------------------------------------------


def test_normalize_for_compare_strips_windows_unc_prefix() -> None:
    """``_normalize_for_compare`` strips the Windows ``\\\\?\\``
    extended-path prefix so two Path objects (one resolved with the
    prefix, one without) compare equal under ``relative_to``. Pin
    PG-R2 #4 — without this, paths >MAX_PATH on Windows can be
    silently dropped by the containment check.
    """
    raw = Path("\\\\?\\C:\\Users\\foo\\bar")
    out = propagate._normalize_for_compare(raw)
    assert os.fspath(out) == "C:\\Users\\foo\\bar", (
        f"expected '\\\\?\\' prefix stripped; got {os.fspath(out)!r}"
    )


def test_normalize_for_compare_strips_unc_server_share_prefix() -> None:
    """``_normalize_for_compare`` strips the UNC-share-form prefix
    ``\\\\?\\UNC\\server\\share\\...`` back to ``\\\\server\\share\\...``.
    """
    raw = Path("\\\\?\\UNC\\server\\share\\foo")
    out = propagate._normalize_for_compare(raw)
    assert os.fspath(out) == "\\\\server\\share\\foo", (
        f"expected UNC-share normalization; got {os.fspath(out)!r}"
    )


def test_normalize_for_compare_noop_on_regular_path(tmp_path: Path) -> None:
    """No-op on paths without the extended-path UNC prefix (POSIX and
    Windows short paths). Pin defensive behavior.
    """
    raw = tmp_path / "repo" / "lib.rs"
    out = propagate._normalize_for_compare(raw)
    assert os.fspath(out) == os.fspath(raw), (
        f"normalize must be no-op on paths without UNC prefix; "
        f"input={os.fspath(raw)!r} output={os.fspath(out)!r}"
    )


def test_normalize_for_compare_handles_bytes_pathlike() -> None:
    """R4 (CR-R3 #4 + PG-R3 #2): if ``os.fspath(p)`` returns ``bytes``
    (a ``PathLike`` whose ``__fspath__`` returns bytes), do NOT crash
    with ``TypeError`` on ``.startswith`` against a str. Decode via
    ``os.fsdecode`` and continue normally.
    """

    class _BytesPath:
        def __init__(self, b: bytes) -> None:
            self._b = b

        def __fspath__(self) -> bytes:
            return self._b

    # Plain bytes path (no UNC prefix). Use a relative path so the
    # assertion is platform-independent (Path normalizes / to \\ on
    # Windows; comparing via Path equality side-steps that).
    bytes_pathlike = _BytesPath(b"foo/bar")
    out = propagate._normalize_for_compare(bytes_pathlike)
    assert isinstance(out, Path), (
        f"expected Path returned; got {type(out).__name__}"
    )
    # The Path should equal Path of the fsdecoded bytes.
    assert out == Path(os.fsdecode(b"foo/bar")), (
        f"expected Path(fsdecoded bytes); got {os.fspath(out)!r}"
    )

    # Bytes path WITH UNC prefix should still get stripped
    bytes_unc = _BytesPath(b"\\\\?\\C:\\foo")
    out2 = propagate._normalize_for_compare(bytes_unc)
    assert os.fspath(out2) == "C:\\foo", (
        f"expected '\\\\?\\' stripped from bytes-form input; got "
        f"{os.fspath(out2)!r}"
    )


def test_normalize_for_compare_passthrough_on_empty_extended_path() -> None:
    """R4 (PG-R3 #3): the empty extended-path ``\\\\?\\`` (prefix with
    no content) must NOT collapse to ``Path(".")`` (which would
    falsely pass containment against any CWD-relative anchor). Return
    a Path constructed from the original string so downstream
    containment treats it as the opaque path it is.

    R5 (CR-R4 #3): tightened to assert the exact returned value
    matches the input string-form, rather than only excluding "."
    and "" — pins the precise contract instead of two negative
    exclusions that future regressions could pass without honoring
    the intent.
    """
    raw = Path("\\\\?\\")
    out = propagate._normalize_for_compare(raw)
    # Precise contract: the returned path's string-form equals the
    # input's. This excludes "." and "" by construction, and ALSO
    # excludes any future regression that returns e.g. "\\" or "\\?\\"
    # (Windows-pathlib normalization quirks).
    assert os.fspath(out) == os.fspath(raw), (
        f"normalize must preserve the empty extended-path exactly; "
        f"input={os.fspath(raw)!r} output={os.fspath(out)!r}"
    )


def test_run_for_finding_safe_repos_check_runs_before_re_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 (CR-R3 #2 + PG-R3 #6): the empty-safe_repos guard must run
    BEFORE ``re.compile(s)`` so a malformed regex in
    ``BUG_CLASS_SIGNATURES`` doesn't propagate as an unhandled
    ``re.error`` when the corpus is already-known-empty. Without this
    ordering, the async lifecycle hook's outer ``except Exception:``
    swallows the re.error silently and the operator sees zero output.

    R5 (PG-R4 #2): use pytest's ``monkeypatch`` fixture rather than a
    manual try/finally. The R4-claimed fix only switched the iterdir
    test; this one still had the brittle try/finally pattern where a
    SIGKILL'd worker would leave the module attribute mutated.
    """
    from audit_pipeline.commands import propagate as _propagate
    from audit_pipeline.commands.propagate import run_for_finding
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Empty corpus dir → _iter_corpus_repos yields nothing
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out_dir = tmp_path / "report"

    # Monkeypatch signatures_for_bug_class to return a deliberately
    # malformed regex so we can prove the empty-safe_repos check
    # short-circuits BEFORE re.compile runs (which would raise
    # re.error on the bad pattern).
    def _bad_signatures(_bug_class):
        return ["[unterminated_char_class"]

    monkeypatch.setattr(_propagate, "signatures_for_bug_class", _bad_signatures)
    result = run_for_finding(db, fid, corpus_dir, out_dir)

    # If the ordering is correct, re.compile is never called and we
    # get the clean ok=False return. If the ordering is wrong,
    # re.compile raises re.error which propagates as an exception
    # (this test would error out before the assertion).
    assert result.get("ok") is False, (
        f"expected ok=False; got {result!r}. If this errored with "
        f"re.error, the empty-safe_repos guard runs AFTER re.compile "
        f"and that ordering must be fixed."
    )
    assert result.get("reason") == "corpus_all_repos_filtered", result


def test_propagate_from_finding_async_skips_marker_on_transient_failure(
    tmp_path: Path,
) -> None:
    """R4 (PG-R3 #5): on a TRANSIENT failure (corpus_all_repos_filtered),
    the lifecycle hook MUST NOT write the fired-marker, so the next
    invocation can retry once the operator repairs the corpus.

    Without this gate, a single transient failure permanently idles
    the propagation hook for the finding (the marker's existence is
    checked, not its content), requiring manual ``rm`` of the file.
    """
    from audit_pipeline.commands.propagate import propagate_from_finding_async
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Empty corpus → run_for_finding returns
    # {"ok": False, "reason": "corpus_all_repos_filtered"}
    corpus_dir = workspace / "recon" / "propagate" / "corpus"
    corpus_dir.mkdir(parents=True)

    propagate_from_finding_async(workspace, fid)

    marker = workspace / "recon" / "propagate" / "markers" / f"{fid}.fired"
    assert not marker.is_file(), (
        f"fired-marker MUST NOT be written on transient failures; "
        f"got marker at {marker} (contents: "
        f"{marker.read_text(encoding='utf-8') if marker.is_file() else 'N/A'})"
    )


def test_status_cmd_surfaces_marker_reason(tmp_path: Path) -> None:
    """R5 (PG-R5 #4): ``propagate status <finding_id>`` must surface
    the marker's ``ok=`` and ``reason=`` lines so operators can
    distinguish a real success from a permanent-failure marker that
    needs manual ``rm`` to recover. Without this, ``FIRED`` is
    ambiguous between "propagation ran successfully" and "permanent
    metadata error suppressed all retries".
    """
    from click.testing import CliRunner

    from audit_pipeline.commands.propagate import propagate_cmd
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Pre-write a permanent-failure-style marker so status surfaces it.
    marker_dir = workspace / "recon" / "propagate" / "markers"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{fid}.fired").write_text(
        "2026-05-25T12:34:56+00:00\n"
        f"finding_id={fid}\n"
        "ok=False\n"
        "reason=no_signatures_registered\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        propagate_cmd,
        ["status", str(fid)],
        obj={"workspace": str(workspace)},
    )
    assert result.exit_code == 0, (
        f"status command exited {result.exit_code}: {result.output!r}"
    )
    # Both lines must appear so the operator can see why the marker is
    # set and decide whether to rm it.
    assert "ok=False" in result.output, (
        f"expected 'ok=False' in status output; got:\n{result.output}"
    )
    assert "reason=no_signatures_registered" in result.output, (
        f"expected 'reason=no_signatures_registered' in status output; "
        f"got:\n{result.output}"
    )


def test_status_cmd_does_not_crash_on_rich_markup_in_marker(
    tmp_path: Path,
) -> None:
    """R7 (PG-R6 #1): a marker file whose ``reason=`` value contains an
    unmatched Rich closing tag (e.g. ``[/dim]``) must NOT crash
    ``status_cmd`` with ``rich.errors.MarkupError``. The original R5
    code wrapped only ``except OSError`` — MarkupError is not an
    OSError subclass and would propagate as an unhandled exception.
    R7 fix: escape the marker line via ``rich.markup.escape`` before
    interpolation.
    """
    from click.testing import CliRunner

    from audit_pipeline.commands.propagate import propagate_cmd
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    marker_dir = workspace / "recon" / "propagate" / "markers"
    marker_dir.mkdir(parents=True)
    # Hostile marker content: an unmatched Rich closing tag and an ANSI
    # injection attempt. Rich would raise MarkupError on the first if
    # not escaped, and silently inject styling on the second.
    (marker_dir / f"{fid}.fired").write_text(
        f"2026-05-25T12:34:56+00:00\n"
        f"finding_id={fid}\n"
        f"ok=False\n"
        f"reason=bad[/dim]breakout[red]injected\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        propagate_cmd,
        ["status", str(fid)],
        obj={"workspace": str(workspace)},
    )
    # The key assertion: NO crash. Without the rich.markup.escape fix,
    # this raises rich.errors.MarkupError and exit_code != 0.
    assert result.exit_code == 0, (
        f"status command crashed on hostile marker content "
        f"(MarkupError?). exit_code={result.exit_code} "
        f"exception={result.exception!r} output={result.output!r}"
    )
    # The escaped content should be present in the output (the raw
    # bracket sequence appears, not interpreted as markup).
    assert "[/dim]" in result.output or "/dim" in result.output, (
        f"expected escaped bracket sequence in output; got:\n"
        f"{result.output}"
    )


def test_status_cmd_does_not_crash_on_non_utf8_marker(
    tmp_path: Path,
) -> None:
    """R7 (PG-R6 #2): a marker file written with a non-UTF-8 encoding
    (e.g. Windows CP1252) must NOT crash ``status_cmd`` with
    ``UnicodeDecodeError``. UnicodeDecodeError is a ``ValueError``
    subclass, NOT an ``OSError`` — the original ``except OSError``
    catch would not catch it. R7 fix: ``read_text(..., errors="replace")``
    swallows the decode error and produces a best-effort string.
    """
    from click.testing import CliRunner

    from audit_pipeline.commands.propagate import propagate_cmd
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    marker_dir = workspace / "recon" / "propagate" / "markers"
    marker_dir.mkdir(parents=True)
    # Write raw bytes that are valid CP1252 but invalid UTF-8: 0x90
    # (DCS) is a continuation byte in UTF-8 with no leading byte.
    (marker_dir / f"{fid}.fired").write_bytes(
        b"2026-05-25T12:34:56+00:00\n"
        b"finding_id=" + str(fid).encode("ascii") + b"\n"
        b"ok=False\n"
        b"reason=bad_byte_\x90_here\n"
    )

    runner = CliRunner()
    result = runner.invoke(
        propagate_cmd,
        ["status", str(fid)],
        obj={"workspace": str(workspace)},
    )
    # The key assertion: NO crash. Without errors="replace", this
    # raises UnicodeDecodeError and exit_code != 0.
    assert result.exit_code == 0, (
        f"status command crashed on non-UTF-8 marker "
        f"(UnicodeDecodeError?). exit_code={result.exit_code} "
        f"exception={result.exception!r} output={result.output!r}"
    )
    # ok= and reason= should still surface.
    assert "ok=False" in result.output
    assert "reason=" in result.output
    # R8 (PG-R7 #1): pin that the undecodable byte is REPLACED (with
    # the R8 ``"?"`` substitute that's CP1252-safe), not silently
    # DROPPED. A future regression from ``errors="replace"`` to
    # ``errors="ignore"`` would still pass the `ok=`/`reason=` checks
    # above because both still surface — but `errors="ignore"`
    # silently drops the bad byte rather than indicating data loss.
    assert "reason=bad_byte_?_here" in result.output, (
        f"expected the bad byte to be replaced with '?' (the R8 "
        f"CP1252-safe substitute for U+FFFD); got:\n{result.output}"
    )


def test_permanent_failure_reasons_constant_is_module_level() -> None:
    """R5 (PG-R4 invariant + CR-R4 #4): the permanent-failure allow-list
    must be importable from the module so tests + future maintainers
    can enumerate it. R4 originally defined it inside the function
    body; R5 promoted it to a module-level frozenset.

    Pin the expected membership so a future reclassification (e.g.
    moving ``no_bug_class`` to the transient set) is a deliberate,
    test-touching change rather than a silent edit.
    """
    from audit_pipeline.commands.propagate import PERMANENT_FAILURE_REASONS

    assert isinstance(PERMANENT_FAILURE_REASONS, frozenset), (
        f"expected frozenset for immutability; got "
        f"{type(PERMANENT_FAILURE_REASONS).__name__}"
    )
    assert frozenset({
        "finding_not_found",
        "no_bug_class",
        "no_signatures_registered",
    }) == PERMANENT_FAILURE_REASONS, (
        f"unexpected membership: {PERMANENT_FAILURE_REASONS}. Reclassifying "
        f"any of these as transient (or adding a new permanent reason) "
        f"must update this test and the marker-write contract."
    )


def test_propagate_from_finding_async_writes_marker_on_permanent_failure(
    tmp_path: Path,
) -> None:
    """R4 (PG-R3 #5): on a PERMANENT failure (no_bug_class — bad
    finding metadata that won't change on retry), the lifecycle hook
    MUST write the fired-marker so it doesn't waste cycles re-trying
    forever (rate-limited, but still wasteful).
    """
    from audit_pipeline.commands.propagate import propagate_from_finding_async
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    # Insert a finding with bug_class=None — triggers no_bug_class
    # permanent-failure path in run_for_finding.
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class=None,
    )

    propagate_from_finding_async(workspace, fid)

    marker = workspace / "recon" / "propagate" / "markers" / f"{fid}.fired"
    assert marker.is_file(), (
        f"fired-marker MUST be written on permanent failures so the "
        f"hook doesn't re-try forever; expected marker at {marker}"
    )
    contents = marker.read_text(encoding="utf-8")
    assert "ok=False" in contents
    assert "reason=no_bug_class" in contents


def test_propagate_from_finding_async_writes_marker_on_success(
    tmp_path: Path,
) -> None:
    """R4: on a successful run (ok=True), the marker IS written so
    we don't re-fire on status flip-flop. Sanity-check the happy path
    isn't broken by the R4 marker-gating logic.
    """
    from audit_pipeline.commands.propagate import propagate_from_finding_async
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Corpus with one empty repo subdir → safe_repos non-empty → ok=True
    corpus_dir = workspace / "recon" / "propagate" / "corpus"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "empty_repo").mkdir()

    propagate_from_finding_async(workspace, fid)

    marker = workspace / "recon" / "propagate" / "markers" / f"{fid}.fired"
    assert marker.is_file(), (
        f"fired-marker MUST be written on ok=True so the hook doesn't "
        f"re-fire on status flip-flop; expected marker at {marker}"
    )
    # R5 (CR-R4 #1): assert the MECHANISM (marker written by the
    # ok=True branch of the gate), not just the side effect. Future
    # regressions that add a new early-return path could pass the
    # is_file() check via an unrelated marker write; pinning content
    # makes the test brittle in the right direction.
    contents = marker.read_text(encoding="utf-8")
    assert "ok=True" in contents, (
        f"marker must encode ok=True; got contents={contents!r}"
    )


def test_rglob_no_follow_helper_yields_matches(tmp_path: Path) -> None:
    """``_rglob_no_follow`` returns an iterator of matches regardless
    of Python version (probes ``recurse_symlinks=False`` and falls
    back to legacy call on TypeError). Pin PG-R2 #3.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.rs").write_text("fn main() {}", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "x.rs").write_text("// inner", encoding="utf-8")
    out = sorted(p.name for p in propagate._rglob_no_follow(repo, "*.rs"))
    assert out == ["lib.rs", "x.rs"], (
        f"expected lib.rs + src/x.rs to be yielded; got {out}"
    )


def test_iter_corpus_returns_empty_on_iterdir_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: ``_iter_corpus_repos`` must treat ``OSError`` from
    ``corpus.iterdir()`` as an empty corpus so the upstream
    ClickException / ok:False path fires, instead of letting the
    exception escape as a raw traceback. Pin PG-R2 #2.

    R4 (PG-R3 #9): use pytest's ``monkeypatch`` fixture rather than
    ``unittest.mock.patch.object(Path, ...)``. The latter mutates the
    global ``pathlib.Path.iterdir`` for the duration of the ``with``
    block — under pytest-xdist this is not thread-safe and could
    affect parallel workers that internally call iterdir (e.g. through
    a tmp_path teardown). ``monkeypatch`` auto-cleans per-test and
    interacts correctly with parallel test execution.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    def _raise_oserror(self):
        raise OSError("simulated PermissionError on iterdir")

    monkeypatch.setattr(Path, "iterdir", _raise_oserror)
    out = list(propagate._iter_corpus_repos(corpus))
    assert out == [], (
        f"expected empty list when iterdir raises OSError; got {out}"
    )


def test_run_for_finding_returns_not_ok_when_all_repos_filtered(
    tmp_path: Path,
) -> None:
    """R3: ``run_for_finding`` must return ``ok=False`` with
    ``reason=corpus_all_repos_filtered`` when ``_iter_corpus_repos``
    yields nothing. Without this guard the lifecycle hook would write
    the fired-marker on a silent zero-work success, permanently
    suppressing retry (CR-R2 #1 + PG-R2 #1).
    """
    from audit_pipeline.commands.propagate import run_for_finding
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="test")
    db.insert_cycle(target_id=target_id, cycle_id="C1")
    fid = db.upsert_finding(
        target_id=target_id, cycle_id="C1", hypothesis_id="H1",
        title="test", verdict="TRUE", confidence="HIGH",
        status=Status.NEW, severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )

    # Empty corpus dir → _iter_corpus_repos yields nothing
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    out_dir = tmp_path / "report"
    result = run_for_finding(db, fid, corpus_dir, out_dir)

    assert result.get("ok") is False, (
        f"expected ok=False on empty corpus; got {result!r}"
    )
    assert result.get("reason") == "corpus_all_repos_filtered", (
        f"expected reason=corpus_all_repos_filtered; got {result!r}"
    )


def test_helpers_are_identical_across_propagate_and_propagate_ast() -> None:
    """Drift-protection: the duplicate helpers in ``propagate.py`` and
    ``propagate_ast.py`` must have IDENTICAL source (modulo docstrings).
    R3 originally compared ``__code__.co_code`` but PG-R3 #1 + CR-R3 #1
    showed that's:
      a) version-specific (CPython 3.12 adaptive opcodes change co_code
         even for identical source), and
      b) blind to constant-value drift (LOAD_CONST opcodes load by
         index into co_consts; the same opcode loads different values
         in two copies and co_code is identical).
    R4 switches to ``ast.unparse`` after stripping docstrings — version-
    portable and catches every source-level divergence.

    Also asserts module-level constant equality so a maintainer who
    changes ``_FILE_ATTRIBUTE_REPARSE_POINT`` in one file but not the
    other is caught (PG-R3 #8). Pin PG-R2 #8 + PG-R3 #1 + PG-R3 #8 +
    CR-R3 #1.
    """
    import ast
    import inspect
    import textwrap

    from audit_pipeline.commands import propagate_ast as _ast

    def _normalized_source(fn) -> str:
        """Function source with docstring stripped, AST-canonicalized."""
        src = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(src)
        func_def = tree.body[0]
        # Strip docstring (first Expr with a string Constant value)
        if (
            func_def.body
            and isinstance(func_def.body[0], ast.Expr)
            and isinstance(func_def.body[0].value, ast.Constant)
            and isinstance(func_def.body[0].value.value, str)
        ):
            func_def.body = func_def.body[1:]
        return ast.unparse(func_def)

    for name in ("_is_link_or_junction", "_normalize_for_compare", "_rglob_no_follow"):
        src_a = _normalized_source(getattr(propagate, name))
        src_b = _normalized_source(getattr(_ast, name))
        assert src_a == src_b, (
            f"DRIFT DETECTED: {name} source differs between "
            f"propagate.py and propagate_ast.py (docstrings excluded). "
            f"Update both copies in lockstep, or extract to a shared "
            f"module.\n--- propagate.py ---\n{src_a}\n"
            f"--- propagate_ast.py ---\n{src_b}"
        )

    # Module-level constant equality: ``LOAD_GLOBAL _FILE_ATTRIBUTE_REPARSE_POINT``
    # in ``_is_link_or_junction`` would resolve to different values if
    # the constant diverged between the two modules. The bytecode is
    # identical (same opcode loading from each module's globals), so
    # the source-level drift check above does not catch this — assert
    # explicitly.
    assert (
        propagate._FILE_ATTRIBUTE_REPARSE_POINT
        == _ast._FILE_ATTRIBUTE_REPARSE_POINT
    ), (
        f"DRIFT DETECTED: _FILE_ATTRIBUTE_REPARSE_POINT differs "
        f"({propagate._FILE_ATTRIBUTE_REPARSE_POINT:#x} vs "
        f"{_ast._FILE_ATTRIBUTE_REPARSE_POINT:#x}). This is the "
        f"NTFS-junction reparse-point bit mask — a divergence here "
        f"means the security check fires on different attack surfaces "
        f"in each file."
    )
