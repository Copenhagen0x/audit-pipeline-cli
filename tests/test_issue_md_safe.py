"""Tests for the ``_md_safe`` helper in issue.py (Disclosure Defect 03)."""

from audit_pipeline.commands.issue import _md_safe


class TestMdSafe:
    def test_none_returns_empty(self):
        assert _md_safe(None) == ""

    def test_empty_returns_empty(self):
        assert _md_safe("") == ""

    def test_backticks_replaced(self):
        # Backticks would close inline code spans
        out = _md_safe("title with `evil` backticks")
        assert "`" not in out
        assert "'" in out

    def test_html_angle_brackets_escaped(self):
        out = _md_safe("title <script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;" in out and "&gt;" in out

    def test_pipe_escaped_for_table_safety(self):
        out = _md_safe("a|b|c")
        # Pipe gets backslash-escaped (so `\|` remains as a literal `|`
        # in rendered output, but markdown tables don't split on it).
        assert "\\|" in out
        # Any raw `|` left in the output must be preceded by a backslash.
        for i, ch in enumerate(out):
            if ch == "|":
                assert i > 0 and out[i - 1] == "\\"

    def test_newlines_collapsed(self):
        out = _md_safe("line1\nline2\r\nline3")
        assert "\n" not in out
        assert "\r" not in out

    def test_max_len_truncates(self):
        out = _md_safe("x" * 1000, max_len=50)
        assert len(out) <= 50

    def test_yesterday_style_no_attack(self):
        """A clean title from yesterday's library passes through unchanged
        modulo escape transforms — no false positives."""
        out = _md_safe("Post-haircut residual cash conservation")
        assert "Post-haircut residual cash conservation" == out
