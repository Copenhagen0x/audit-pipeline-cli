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
    # R5b — code-reviewer + goober: tautological `or` removed. The
    # invariant we want is: the dangerous string never lands in any
    # rendered href attribute. Since the no-link path renders verbatim,
    # `javascript:` will appear as plain text — but it MUST NOT appear
    # inside an href.
    assert 'href="javascript:' not in out.lower()


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


# ─────────── R5b additions ───────────


def test_protocol_relative_url_rejected() -> None:
    """R5b (2026-05-24) — goober HIGH #1: `//evil.com` previously
    passed via startswith('/'). On HTTPS delivery: open redirect.
    On Windows file:// context: UNC path resolution leaks NTLM
    credentials on click. Must NOT produce an href."""
    out = _inline_md("[click](//attacker.com/capture)")
    assert "<a href=" not in out
    assert 'href="//' not in out

def test_protocol_relative_url_in_heading_rejected() -> None:
    """R5b: same defense via the H3 heading path of _render_md_text."""
    from audit_pipeline.commands.report import _render_md_text
    out = _render_md_text("### [Section](//attacker.com)")
    assert "<a href=" not in out

def test_legitimate_root_relative_path_still_allowed() -> None:
    """R5b: regression-lock — single-slash root-relative paths must
    still resolve. Only `//` (double slash) is the attack."""
    out = _inline_md("[docs](/docs/index.html)")
    assert '<a href="/docs/index.html">docs</a>' in out

def test_uppercase_https_scheme_allowed() -> None:
    """R5b — goober MEDIUM #3: scheme allowlist is now case-insensitive
    so HTTPS://example.com / Https://example.com don't silently
    disappear from operator-authored narratives."""
    out = _inline_md("[RFC](HTTPS://datatracker.ietf.org/rfc/rfc7230)")
    assert "<a href=" in out

def test_uppercase_javascript_still_rejected() -> None:
    """R5b: case-insensitive allowlist must NOT accidentally allow
    case-variant attack schemes. JAVASCRIPT:alert(1) must still be
    rejected (it's not in the allowed set)."""
    out = _inline_md("[click](JAVASCRIPT:alert(1))")
    assert "<a href=" not in out
    assert 'href="JAVASCRIPT' not in out
    assert 'href="javascript' not in out.lower()
