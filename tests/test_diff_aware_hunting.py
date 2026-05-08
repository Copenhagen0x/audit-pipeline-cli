"""Tier 2 #11 — diff-aware hunting filter tests.

Covers:
  - filter_hypotheses_by_diff() keep/skip semantics
  - hyps without target_file always run
  - empty diff falls back to running everything (conservative)
  - case + path-separator normalization
  - changed_files_between() returns empty on non-git directory
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from audit_pipeline.scoping import (
    changed_files_between,
    filter_hypotheses_by_diff,
)


def _hyp(hid: str, target_file: str | None = None) -> dict[str, Any]:
    """Minimal valid hypothesis for filter testing (loader is bypassed)."""
    return {
        "id": hid,
        "class": "invariant_property",
        "severity": "High",
        "claim": "x" * 30,
        "applies_to": ["*"],
        "scope_conditions": [],
        "bug_class": "test",
        "target_file": target_file,
    }


# ─────────────────────────── filter_hypotheses_by_diff ─────────────────────


def test_keeps_hyp_when_target_file_in_diff() -> None:
    hyps = [_hyp("X1-a", "src/a.rs")]
    kept, skipped = filter_hypotheses_by_diff(hyps, {"src/a.rs"})
    assert [h["id"] for h in kept] == ["X1-a"]
    assert skipped == []


def test_skips_hyp_when_target_file_not_in_diff() -> None:
    hyps = [_hyp("X1-a", "src/a.rs")]
    kept, skipped = filter_hypotheses_by_diff(hyps, {"src/b.rs"})
    assert kept == []
    assert [h["id"] for h in skipped] == ["X1-a"]


def test_no_target_file_always_kept() -> None:
    """Whole-protocol invariants without target_file always run."""
    hyps = [_hyp("X1-no-tf")]
    hyps[0]["target_file"] = None
    kept, _ = filter_hypotheses_by_diff(hyps, {"src/unrelated.rs"})
    assert [h["id"] for h in kept] == ["X1-no-tf"]


def test_missing_target_file_key_always_kept() -> None:
    """Hyps with the target_file key absent (not just None) also always run."""
    hyps = [{"id": "X1-no-tf", "class": "invariant_property", "severity": "High",
             "claim": "x" * 30}]
    kept, _ = filter_hypotheses_by_diff(hyps, {"src/unrelated.rs"})
    assert [h["id"] for h in kept] == ["X1-no-tf"]


def test_empty_diff_runs_full_library() -> None:
    """No diff info -> conservative fallback (run everything)."""
    hyps = [_hyp("X1-a", "src/a.rs"), _hyp("X2-b", "src/b.rs")]
    kept, skipped = filter_hypotheses_by_diff(hyps, set())
    assert len(kept) == 2
    assert skipped == []


def test_directory_prefix_match() -> None:
    """A hyp targeting a directory matches any file inside that directory."""
    hyps = [_hyp("X1-dir", "src/insurance")]
    kept, _ = filter_hypotheses_by_diff(hyps, {"src/insurance/buffer.rs"})
    assert [h["id"] for h in kept] == ["X1-dir"]


def test_diff_path_prefix_match() -> None:
    """A diff containing a directory matches a hyp's file inside that directory."""
    hyps = [_hyp("X1-file", "src/insurance/buffer.rs")]
    kept, _ = filter_hypotheses_by_diff(hyps, {"src/insurance"})
    assert [h["id"] for h in kept] == ["X1-file"]


def test_windows_separators_normalized() -> None:
    """Backslash paths in either side normalize to forward-slash before matching."""
    hyps = [_hyp("X1-win", "src\\a.rs")]
    kept, _ = filter_hypotheses_by_diff(hyps, {"src/a.rs"})
    assert [h["id"] for h in kept] == ["X1-win"]


def test_mixed_kept_and_skipped() -> None:
    """Realistic: many hyps, only some intersect the diff."""
    hyps = [
        _hyp("X1-a", "src/a.rs"),
        _hyp("X2-b", "src/b.rs"),
        _hyp("X3-c", "src/c.rs"),
        _hyp("X4-no-tf"),  # always run
    ]
    hyps[3]["target_file"] = None
    kept, skipped = filter_hypotheses_by_diff(hyps, {"src/a.rs", "README.md"})
    kept_ids = sorted(h["id"] for h in kept)
    skipped_ids = sorted(h["id"] for h in skipped)
    assert kept_ids == ["X1-a", "X4-no-tf"]
    assert skipped_ids == ["X2-b", "X3-c"]


# ─────────────────────────── changed_files_between ─────────────────────────


def test_changed_files_returns_empty_on_non_git_dir(tmp_path: Path) -> None:
    """A directory without .git must return an empty set, never raise."""
    assert changed_files_between(tmp_path, "HEAD~1", "HEAD") == set()


def test_changed_files_returns_empty_on_missing_dir(tmp_path: Path) -> None:
    """A non-existent path must return an empty set."""
    bogus = tmp_path / "does-not-exist"
    assert changed_files_between(bogus, "HEAD~1", "HEAD") == set()
