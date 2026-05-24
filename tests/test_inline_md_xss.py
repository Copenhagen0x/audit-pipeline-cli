"""Patch #6 — XSS via _inline_md (audit CRITICAL 4eebb073).

The previous _inline_md substituted captured URLs into href attributes
without scheme validation. A LLM-authored narrative with
`[click](javascript:alert(1))` produced live XSS in the signed HTML
report. Patch #6 adds a scheme allowlist (https / http / mailto /
anchor / relative) and renders rejected URLs as plain text.
"""

from __future__ import annotations

from audit_pipeline.commands.report import _inline_md


def test_javascript_uri_is_rendered_as_plain_text() -> None:
    """javascript: scheme must NOT produce an href — must render verbatim."""
    out = _inline_md("[click](javascript:alert(1))")
    assert "<a href=" not in out
    assert "javascript:" not in out.lower() or "href=\"javascript:" not in out


def test_data_uri_rejected() -> None:
    out = _inline_md("[click](data:text/html,<script>alert(1)</script>)")
    assert "<a href=" not in out


def test_vbscript_uri_rejected() -> None:
    out = _inline_md("[click](vbscript:msgbox)")
    assert "<a href=" not in out


def test_file_uri_rejected() -> None:
    out = _inline_md("[click](file:///etc/passwd)")
    assert "<a href=" not in out


def test_https_url_renders_as_link() -> None:
    out = _inline_md("[GitHub](https://github.com/foo/bar)")
    assert '<a href="https://github.com/foo/bar">GitHub</a>' in out


def test_http_url_renders_as_link() -> None:
    out = _inline_md("[link](http://example.com)")
    assert '<a href="http://example.com">link</a>' in out


def test_mailto_url_renders_as_link() -> None:
    out = _inline_md("[email](mailto:foo@bar.com)")
    assert '<a href="mailto:foo@bar.com">email</a>' in out


def test_anchor_renders_as_link() -> None:
    out = _inline_md("[top](#section)")
    assert '<a href="#section">top</a>' in out


def test_relative_path_renders_as_link() -> None:
    out = _inline_md("[doc](/docs/index.html)")
    assert '<a href="/docs/index.html">doc</a>' in out


def test_url_with_embedded_newline_rejected() -> None:
    """Defense against header-injection-style URL smuggling."""
    out = _inline_md("[x](https://example.com\nX-Injected: yes)")
    assert "<a href=" not in out


def test_inline_code_still_works() -> None:
    out = _inline_md("call `foo()` to fix")
    assert "<code>foo()</code>" in out


def test_bold_still_works() -> None:
    out = _inline_md("this is **bold** text")
    assert "<strong>bold</strong>" in out
