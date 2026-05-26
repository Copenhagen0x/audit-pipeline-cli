"""Regression tests for audit-021 (Bucket L L6 — bundle path traversal).

L6 — `hypothesis_id` flowed directly into file path components in
`debate.py` with no sanitization. A malicious / typo'd value like
`../../etc/passwd` (or its Windows variant) would let the write escape
the intended `output` directory. Fix: `safe_hyp_slug()` strips
separators + reserved chars + dots-only names.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# --- helper unit tests ------------------------------------------------------


def test_safe_hyp_slug_passes_normal_id() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    assert safe_hyp_slug("HYP1") == "HYP1"
    assert safe_hyp_slug("hyp_42") == "hyp_42"
    assert safe_hyp_slug("HYP-NEW6.2") == "HYP-NEW6.2"


def test_safe_hyp_slug_strips_posix_traversal() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    out = safe_hyp_slug("../../etc/passwd")
    # No path separators survive; no leading dots that Path would
    # interpret as cwd / parent.
    assert "/" not in out
    assert "\\" not in out
    # Leading dots in the SANITIZED slug are safe — without a separator
    # following them, ``Path("..-foo")`` is a literal filename, not a
    # traversal. The "no separators" assertions above are what matter.
    # The actual segment names ARE preserved as filename-safe chars.
    assert "etc" in out and "passwd" in out


def test_safe_hyp_slug_strips_windows_traversal() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    out = safe_hyp_slug("..\\..\\Windows\\System32\\foo")
    assert "\\" not in out
    assert "/" not in out
    # Leading dots in the SANITIZED slug are safe — without a separator
    # following them, ``Path("..-foo")`` is a literal filename, not a
    # traversal. The "no separators" assertions above are what matter.


def test_safe_hyp_slug_rejects_dots_only() -> None:
    """``..`` / ``.`` would direct ``Path`` to cwd / parent — refuse."""
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    assert safe_hyp_slug("..") == "finding"
    assert safe_hyp_slug(".") == "finding"
    assert safe_hyp_slug("...") == "finding"
    assert safe_hyp_slug("./../.") == "finding"


def test_safe_hyp_slug_rejects_empty_and_none() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    assert safe_hyp_slug("") == "finding"
    assert safe_hyp_slug(None) == "finding"
    assert safe_hyp_slug("   ") == "finding"


def test_safe_hyp_slug_strips_null_bytes() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    out = safe_hyp_slug("HYP\x001\x00malicious")
    assert "\x00" not in out


def test_safe_hyp_slug_strips_windows_reserved_chars() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    out = safe_hyp_slug('HYP<>:"|?*1')
    for bad in '<>:"|?*':
        assert bad not in out
    assert "HYP" in out and "1" in out


def test_safe_hyp_slug_caps_max_length() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    out = safe_hyp_slug("A" * 1000)
    assert len(out) <= 128


def test_safe_hyp_slug_custom_fallback() -> None:
    from audit_pipeline.utils.safe_slug import safe_hyp_slug
    assert safe_hyp_slug("..", fallback="X") == "X"
    assert safe_hyp_slug(None, fallback="X") == "X"


# --- debate.py integration --------------------------------------------------


def test_debate_render_does_not_escape_output(tmp_path: Path) -> None:
    """Source-level pin: ``debate.py`` must call ``safe_hyp_slug`` on
    ``hypothesis_id`` before composing the output path. Verifies the
    wiring rather than re-deriving it from the same regex.
    """
    import audit_pipeline.commands.debate as debate_mod
    src = Path(debate_mod.__file__).read_text(encoding="utf-8")
    # Sanitization helper imported (Audit-021 marker).
    assert "from audit_pipeline.utils.safe_slug import safe_hyp_slug" in src, (
        "debate.py must import safe_hyp_slug (audit-021 L6 fix)"
    )
    # Old unsanitized pattern is gone — no raw f"{hypothesis_id}_..." in
    # an output-path expression. We accept f"{_hyp_slug}_..." as the new
    # form.
    assert 'output / f"{hypothesis_id}_challenger.md"' not in src, (
        "debate.py still has unsanitized output-path with raw "
        "hypothesis_id (audit-021 L6 regression)"
    )
    assert 'output / f"{hypothesis_id}_challenger_response.md"' not in src, (
        "debate.py still has unsanitized response-path with raw "
        "hypothesis_id (audit-021 L6 regression)"
    )
