"""Regex-based placeholder substitution that matches the agent-prompt style.

Why not Jinja2 for the prompt templates? The prompt templates were authored
with single-brace `{KEY}` placeholders for human readability (the docs at the
bottom of each file refer to `{ENGINE_REPO_URL}` etc. as if they were
shell-style variables). Jinja2 expects `{{KEY}}` and silently no-ops on
single-brace markers, which broke the orientation rendering.

Why not str.format? It would require escaping every literal `{` and `}` in
the prompt body (code blocks, math, JSON examples) — high churn, high risk
of accidental substitution.

This helper splits the difference: it only substitutes ALL-CAPS placeholders
matching the convention `{KEY}` or `{KEY_WITH_NUMBERS_2}`. Unknown
placeholders are left intact so the user can see what wasn't substituted.
"""

import re

_PLACEHOLDER_RE = re.compile(r"\{([A-Z_][A-Z0-9_]*)\}")


def render_placeholders(template: str, **substitutions: str) -> str:
    """Substitute `{ALL_CAPS_KEY}` placeholders with the provided values.

    - Only matches all-uppercase keys (with optional digits / underscores)
    - Unknown placeholders are left as `{KEY}` so omissions are visible
    - Leaves braces in code, JSON, math, etc. untouched

    Example:
        render_placeholders(
            "Repo: {ENGINE_REPO_URL} @ {ENGINE_SHA}",
            ENGINE_REPO_URL="https://github.com/x/y",
            ENGINE_SHA="abc123",
        )
        # → "Repo: https://github.com/x/y @ abc123"
    """
    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        return substitutions.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, template)
