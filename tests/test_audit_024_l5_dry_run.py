"""Regression test for audit-024 (Bucket L L5 — --dry-run state-write gate).

L5 finding: ``CLI --dry-run mode still writes state to disk``. Verified
at audit-024 time across the 3 CLI files that expose ``--dry-run``
flags (``bundle.py``, ``issue.py``, ``notify.py``):

* ``bundle.py:804`` — early ``return`` after dry-run print.
* ``issue.py:130`` — early ``return`` after dry-run print.
* ``issue.py:258`` — ``continue`` (loop iteration skip) — correct
  because the dry-run path is inside a per-issue loop and must keep
  reporting subsequent issues that WOULD have been transitioned.
* ``notify.py`` — ``dry_run`` flag flows to ``NotifierSettings.load``
  which uses it to short-circuit the SMTP-send step.

The L5 risk is a future ``if dry_run:`` block that PRINTS but does
NOT then ``return``/``continue`` — letting code below it run and
mutate state. This test scans the source for every ``if dry_run:``
block and asserts the block contains a control-flow exit
(``return``, ``continue``, ``break``, ``raise``).
"""

from __future__ import annotations

import re
from pathlib import Path

_CLI_FILES_WITH_DRY_RUN = (
    "src/audit_pipeline/commands/bundle.py",
    "src/audit_pipeline/commands/issue.py",
    "src/audit_pipeline/commands/notify.py",
)


def _enclosing_block(src: str, header_match: re.Match[str]) -> str:
    """Return the indented block following ``if dry_run:`` (excluding the
    header). Block ends when a line has indent <= the header's indent
    and is non-empty.
    """
    header_indent = len(header_match.group(1))
    rest_start = header_match.end()
    lines = src[rest_start:].split("\n")
    # Skip the leading newline so the FIRST line we see is the first
    # body statement.
    block: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            block.append(line)
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= header_indent:
            break
        block.append(line)
    return "\n".join(block)


def test_every_if_dry_run_block_has_control_flow_exit() -> None:
    """For every ``if dry_run:`` block in the CLI files, the body must
    contain one of: ``return``, ``continue``, ``break``, ``raise``.
    Otherwise execution falls through to state-mutating code below.

    This catches the L5 regression class: a developer adds ``if dry_run:
    console.print(...)`` and forgets the early-exit, letting the
    state-mutating code beneath it run on dry-run invocations.
    """
    bad: list[tuple[str, int, str]] = []
    header_re = re.compile(r"(?m)^(\s*)if dry_run:\s*$")
    exit_re = re.compile(r"\b(return|continue|break|raise)\b")

    for rel in _CLI_FILES_WITH_DRY_RUN:
        path = Path(rel)
        assert path.is_file(), f"{rel} not found"
        src = path.read_text(encoding="utf-8")
        for m in header_re.finditer(src):
            block = _enclosing_block(src, m)
            if not exit_re.search(block):
                line_no = src[: m.start()].count("\n") + 1
                bad.append((rel, line_no, block[:200]))

    assert not bad, (
        "--dry-run block(s) missing a control-flow exit (audit-024 L5 "
        "regression): execution falls through to state-mutating code.\n"
        + "\n".join(
            f"  {rel}:{lineno} block-preview:\n    {preview!r}"
            for rel, lineno, preview in bad
        )
    )


def test_bundle_dry_run_returns_before_file_write() -> None:
    """Specific lock for ``bundle.py:dry_run`` — must return BEFORE
    the ``body_path.write_text(body, ...)`` write at the post-dry-run
    section. Order-sensitive: the dry-run block must appear above the
    write call.
    """
    src = Path("src/audit_pipeline/commands/bundle.py").read_text(encoding="utf-8")
    # Find the index of the dry_run check and the body_path.write_text call.
    dry_pos = src.find("if dry_run:")
    write_pos = src.find("body_path.write_text")
    assert dry_pos != -1, "bundle.py missing 'if dry_run:' branch"
    assert write_pos != -1, "bundle.py missing 'body_path.write_text' call"
    assert dry_pos < write_pos, (
        f"bundle.py dry_run block at byte {dry_pos} must come BEFORE "
        f"the body_path.write_text call at byte {write_pos}, with a "
        f"return in between. Current ordering would let dry-run still "
        f"write the PR body file."
    )


def test_issue_dry_run_blocks_db_transition() -> None:
    """``issue.py:258`` uses ``continue`` to skip per-iteration; the
    ``db.transition_finding`` call must appear AFTER ``continue`` in
    the same block so dry-run can't trigger the DB write.
    """
    src = Path("src/audit_pipeline/commands/issue.py").read_text(encoding="utf-8")
    # Slice from the closed-not-planned BRANCH (the state == "CLOSED" and
    # reason in {...} guard) — that's where the audit-024 dry_run +
    # transition_finding pair lives. Skips earlier mentions of
    # ``Status.CLOSED_NOT_PLANNED`` (e.g. imports / enum definitions).
    branch_marker = 'state == "CLOSED"'
    branch_pos = src.find(branch_marker)
    assert branch_pos != -1, "issue.py sync branch marker not found"
    sync_block = src[branch_pos:]
    dry_run_pos = sync_block.find("if dry_run:")
    transition_pos = sync_block.find("db.transition_finding")
    assert dry_run_pos != -1, "sync block missing dry_run check"
    assert transition_pos != -1, "sync block missing transition_finding call"
    assert dry_run_pos < transition_pos, (
        "issue.py sync block dry_run check must come BEFORE the DB "
        "transition (with a `continue` in between). Otherwise dry-run "
        "would commit state changes."
    )
