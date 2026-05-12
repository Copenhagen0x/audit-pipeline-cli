"""Tests for `audit-pipeline lint-hypotheses` (pre-cycle YAML linter).

Pre-cycle gate that catches schema breakage, near-duplicates, and
unknown severities before a paid hunt cycle dispatches against
broken hypothesis YAMLs.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner


def _yaml_file(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _invoke(workspace: Path, *args: str):
    from audit_pipeline.commands.lint_hypotheses import lint_hypotheses_cmd
    runner = CliRunner()
    return runner.invoke(
        lint_hypotheses_cmd, list(args), obj={"workspace": str(workspace)},
    )


def test_lint_clean_yaml_exits_zero(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "good.yaml", """
hypotheses:
  - id: H1
    class: implicit_invariant
    claim: A reasonable claim about a property
    target_file: src/lib.rs
    bug_class: implicit_invariant
    severity: Medium
""")
    r = _invoke(tmp_path)
    assert r.exit_code == 0, r.output


def test_lint_missing_required_field_exits_nonzero(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "bad.yaml", """
hypotheses:
  - id: H1
    class: implicit_invariant
    # claim missing
""")
    r = _invoke(tmp_path)
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "claim" in flat


def test_lint_duplicate_id_within_file(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "dup.yaml", """
hypotheses:
  - id: H1
    class: implicit_invariant
    claim: First claim
  - id: H1
    class: implicit_invariant
    claim: Second claim with same id
""")
    r = _invoke(tmp_path)
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "duplicate" in flat.lower()


def test_lint_unknown_severity_is_warning(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "warn.yaml", """
hypotheses:
  - id: H1
    class: implicit_invariant
    claim: x
    severity: WAY_TOO_CRITICAL
""")
    r = _invoke(tmp_path)
    # Warning only — exit 0, but the unknown severity must appear
    assert r.exit_code == 0, r.output
    assert "WAY_TOO_CRITICAL" in r.output


def test_lint_yaml_syntax_error_reported(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "syntax.yaml", "hypotheses:\n  - id: H1\n  - x\n   bad-indent: y\n")
    r = _invoke(tmp_path)
    # parse error should produce an "error" row (exit nonzero)
    assert r.exit_code != 0 or "error" in r.output.lower()


def test_lint_near_duplicate_detection(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "near.yaml", """
hypotheses:
  - id: H1
    class: implicit_invariant
    bug_class: implicit_invariant
    target_file: src/lib.rs
    claim: Vault Balance Equation Has Rounding Error
  - id: H2
    class: implicit_invariant
    bug_class: implicit_invariant
    target_file: src/lib.rs
    claim: vault balance equation has rounding error
""")
    r = _invoke(tmp_path)
    # Same (bug_class, target_file, normalized claim) → near-duplicate warning
    flat = " ".join(r.output.split())
    assert "near-duplicate" in flat.lower() or "duplicate" in flat.lower()


def test_lint_top_level_must_be_list(tmp_path: Path) -> None:
    _yaml_file(tmp_path, "shape.yaml", """
hypotheses: not_a_list
""")
    r = _invoke(tmp_path)
    assert r.exit_code != 0
