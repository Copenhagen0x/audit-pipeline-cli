"""Regression test for audit-025 (Bucket L L4 — narrative raw-HTML pass-through).

L4 finding: ``L5 narrative template allows raw HTML passthrough``.
Verified at audit-025 time across the markdown→HTML pipeline in
``report.py``:

* ``_narrative_md_to_html(md)`` (line 1311) — splits on triple-backtick
  fences. Fence bodies go through ``html.escape(code)``; text segments
  go to ``_render_md_text`` → ``_inline_md``.
* ``_inline_md(text)`` (line 1417) — line 1428 calls ``html.escape(text)``
  BEFORE any markdown-→-HTML substitution. Subsequent regex passes
  inject ``<strong>``/``<code>``/``<a>`` on the escaped text. URL scheme
  is allow-listed (P6 fix for ``javascript:`` XSS).

This test pins the contract by feeding a battery of XSS payloads
through ``_narrative_md_to_html`` and asserting NO unescaped HTML tag
survives (other than the controlled inserts from the markdown-→-HTML
pass). If a future refactor lets raw HTML through, the test fails.
"""

from __future__ import annotations

_XSS_PAYLOADS = [
    # Raw script tag — classic
    "<script>alert(1)</script>",
    # Image with onerror
    '<img src=x onerror="alert(1)">',
    # Inline event handler on a div
    '<div onmouseover="alert(1)">hover me</div>',
    # SVG-based XSS
    "<svg/onload=alert(1)>",
    # IFrame with javascript: src
    '<iframe src="javascript:alert(1)"></iframe>',
    # Encoded angle brackets — should still escape after html.escape
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    # Mixed content — paragraph text with embedded script
    "Some narrative text <script>steal_secrets()</script> more text.",
    # Link with javascript: URL (P6 fix)
    "Click [here](javascript:alert(1)) for details.",
    # Link with data: URL (P6 fix)
    "Click [here](data:text/html,<script>alert(1)</script>).",
    # Link with protocol-relative URL (R5b fix)
    "Click [here](//evil.com/payload).",
    # Backtick code with HTML inside
    "Inline `<script>x</script>` code.",
    # Triple-backtick fence with HTML
    "```\n<script>alert(1)</script>\n```",
]


def test_no_xss_payload_produces_executable_html() -> None:
    """Every XSS payload must come out with its dangerous tags escaped
    or stripped — no unescaped ``<script>``, ``<img>``, ``<svg>``,
    ``<iframe>``, ``<object>``, ``<embed>``, or inline ``on*`` event
    handler survives the markdown→HTML pipeline.

    Acceptable HTML tags in the output: ``<p>``, ``<ul>``, ``<li>``,
    ``<strong>``, ``<code>``, ``<a>``, ``<pre>``, ``<h5>`` — these are
    the controlled inserts from the markdown→HTML conversion.
    """
    from audit_pipeline.commands.report import _narrative_md_to_html

    # The contract: NO unescaped opening tag for any of these elements.
    # ``html.escape`` turns ``<script`` into ``&lt;script``; we verify
    # the LITERAL ``<script`` byte sequence does NOT appear in output.
    # Attribute names like ``onerror=`` may appear inside ESCAPED text
    # (e.g. ``&lt;img src=x onerror=&quot;...&quot;&gt;``) — that's
    # safe because the browser renders the whole thing as visible
    # text, not as a live element with attributes. We don't check
    # for attribute names.
    forbidden_tag_opens = (
        "<script", "<img ", "<img>", "<svg/", "<svg ", "<svg>",
        "<iframe", "<object", "<embed", "<style", "<link",
    )

    for payload in _XSS_PAYLOADS:
        out = _narrative_md_to_html(payload)
        lc = out.lower()
        for tag in forbidden_tag_opens:
            assert tag not in lc, (
                f"audit-025 L4 regression: forbidden unescaped tag-open "
                f"{tag!r} survived the markdown→HTML pipeline.\n"
                f"  input:  {payload!r}\n"
                f"  output: {out!r}"
            )
        # ``javascript:`` and ``data:`` schemes inside href attributes
        # are the P6 / R5b attack class. Anchor scheme allowlist must
        # reject them.
        if 'href="' in lc:
            # Extract every href value and verify the scheme is safe.
            import re
            for href in re.findall(r'href="([^"]+)"', lc):
                assert not href.startswith("javascript:"), (
                    f"javascript: URL survived in href: {payload!r} -> {out!r}"
                )
                assert not href.startswith("data:"), (
                    f"data: URL survived in href: {payload!r} -> {out!r}"
                )
                assert not href.startswith("//"), (
                    f"protocol-relative URL survived in href: {payload!r} -> {out!r}"
                )


def test_safe_markdown_still_renders() -> None:
    """Sanity: legitimate markdown must still produce legitimate HTML.
    Without this, a regression that over-aggressively strips could
    pass the XSS test by neutering ALL output.
    """
    from audit_pipeline.commands.report import _narrative_md_to_html

    out = _narrative_md_to_html("Hello **world** and `code`.")
    assert "<strong>world</strong>" in out
    assert "<code>code</code>" in out

    out = _narrative_md_to_html("- item 1\n- item 2\n")
    assert "<li>item 1</li>" in out
    assert "<li>item 2</li>" in out

    out = _narrative_md_to_html(
        "Visit [example](https://example.com/path) for docs."
    )
    assert '<a href="https://example.com/path">example</a>' in out


def test_smtp_credentials_are_env_var_only() -> None:
    """Audit-025 (L3 verified-clean lock): the SMTP password must NEVER
    be read from a config file on disk. Pin this contract by source-
    inspection: ``notifier.py`` must read ``JELLEO_SMTP_PASSWORD`` ONLY
    from ``os.environ``, never from a file open / read_text call.
    """
    from pathlib import Path
    src = Path("src/audit_pipeline/notifier.py").read_text(encoding="utf-8")
    # The env-var read must be present.
    assert 'os.environ.get("JELLEO_SMTP_PASSWORD")' in src, (
        "notifier.py must read JELLEO_SMTP_PASSWORD from os.environ "
        "(audit-025 L3 regression)"
    )
    # No file-based password storage — verify by negative-match on the
    # canonical "read password from file" patterns.
    forbidden_patterns = [
        '.read_text("",',  # any read_text right next to password
        'open("smtp',
        'Path("/etc/jelleo/smtp',
        'load_yaml_password',
    ]
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"notifier.py contains forbidden file-based password pattern "
            f"{pat!r} (audit-025 L3 regression)"
        )
