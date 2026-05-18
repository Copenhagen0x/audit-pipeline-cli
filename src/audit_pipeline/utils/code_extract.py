"""Extract source code into agent prompts (language-aware).

The agent's prior weakness: it claims to "read" files but cannot actually
access them. This module fixes that by pulling the relevant source bytes
out of the workspace and embedding them in the prompt as
CODE-GROUNDED CONTEXT.

Two extraction strategies:

  * extract_function(file, name)  — pull a complete language-appropriate
    function block matching ``name``. Supports Rust (``fn name``), C
    (``[static] [return-type] name(``), Move (``[public/entry] fun name``),
    and Solidity (``function name``). The language is inferred from the
    file's extension when not provided explicitly.
  * extract_grep_context(file, p, ctx_lines) — pull lines around grep
    matches. Used when ``name`` doesn't resolve to a function (constant,
    struct field, comment marker, etc.).

Both add line numbers (1-indexed) so the agent can cite specific lines in
its verdict, like a real auditor.
"""

from __future__ import annotations

import re
from pathlib import Path


def _format_lines(start: int, lines: list[str]) -> str:
    """Format a span of source lines with 1-indexed line numbers."""
    width = len(str(start + len(lines)))
    return "\n".join(f"{i + start:>{width}}: {ln}" for i, ln in enumerate(lines))


# Per-extension function-start regexes. Each must capture (or be safe to
# scan as) "the line where the function definition starts". Brace-balancing
# from that point is done by the caller.

_RUST_FUNC = (
    r"\b(pub\s+(?:\(crate\)\s+)?)?(unsafe\s+)?(async\s+)?"
    r"fn\s+{NAME}\s*[<(]"
)

# C: return type may be one or more space-separated tokens (incl. pointers
# and qualifiers); then the function name then `(`. Anchor at start of line
# (with leading whitespace allowed) to avoid matching inside calls.
_C_FUNC = (
    r"(?m)^[ \t]*(?:static\s+|inline\s+|extern\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:\s+|\s*\*+\s*))+"
    r"{NAME}\s*\([^;]*\)\s*\{"
)

_MOVE_FUNC = (
    r"\b(public\s*(?:\([^)]*\))?\s+)?(entry\s+)?(native\s+)?"
    r"fun\s+{NAME}\s*[<(]"
)

_SOLIDITY_FUNC = (
    r"\bfunction\s+{NAME}\s*\("
)


def _func_re_for(ext: str, name: str) -> re.Pattern[str]:
    quoted = re.escape(name)
    if ext == ".rs":
        return re.compile(_RUST_FUNC.replace("{NAME}", quoted))
    if ext in (".c", ".h", ".cc", ".cpp", ".hpp"):
        return re.compile(_C_FUNC.replace("{NAME}", quoted))
    if ext == ".move":
        return re.compile(_MOVE_FUNC.replace("{NAME}", quoted))
    if ext == ".sol":
        return re.compile(_SOLIDITY_FUNC.replace("{NAME}", quoted))
    # Unknown ext: try the rust pattern as a fallback (legacy behavior).
    return re.compile(_RUST_FUNC.replace("{NAME}", quoted))


def _markdown_lang_for(ext: str) -> str:
    """Return the markdown fenced-code-block language tag for ``ext``."""
    return {
        ".rs": "rust",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".move": "move",
        ".sol": "solidity",
    }.get(ext, "")


def extract_function(
    file_path: Path,
    function_name: str,
    max_lines: int = 120,
) -> str | None:
    """Extract a single function definition by name (language-aware).

    The language is inferred from ``file_path.suffix``. Returns the function
    body up to the matching close-brace, with line numbers. Returns None if
    no matching definition is found.
    """
    if not file_path.exists():
        return None
    ext = file_path.suffix.lower()
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()

    pattern = _func_re_for(ext, function_name)

    start_idx: int | None = None
    for i, ln in enumerate(lines):
        if pattern.search(ln):
            start_idx = i
            break
    if start_idx is None:
        return None

    # Walk forward, brace-matching from the FIRST `{` after start_idx.
    depth = 0
    started = False
    end_idx = start_idx
    for j in range(start_idx, min(start_idx + max_lines, len(lines))):
        for c in lines[j]:
            if c == "{":
                depth += 1
                started = True
            elif c == "}":
                depth -= 1
                if started and depth == 0:
                    end_idx = j
                    return _format_lines(start_idx + 1, lines[start_idx : end_idx + 1])

    end_idx = min(start_idx + max_lines - 1, len(lines) - 1)
    return _format_lines(start_idx + 1, lines[start_idx : end_idx + 1])


def extract_grep_context(
    file_path: Path,
    pattern: str,
    ctx_lines: int = 12,
    max_matches: int = 3,
) -> str | None:
    """Extract context windows around occurrences of ``pattern``.

    Returns up to max_matches windows of size ctx_lines (centered on match),
    separated by '---'. Useful when the relevant code site isn't a complete
    function (e.g., a const, a constant block, a one-off match arm).
    """
    if not file_path.exists():
        return None
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[str] = []
    for i, ln in enumerate(lines):
        if pattern in ln:
            start = max(0, i - ctx_lines // 2)
            end = min(len(lines), i + ctx_lines // 2 + 1)
            blocks.append(_format_lines(start + 1, lines[start:end]))
            if len(blocks) >= max_matches:
                break
    if not blocks:
        return None
    return "\n\n---\n\n".join(blocks)


# Stop-words that prose tokenizers wrongly pick up as "function names" —
# common English words that happen to be valid identifiers. Filtered at the
# ``collect_grounded_code`` layer so we don't waste prompt tokens on hits
# for "the", "of", "at", etc.
_STOP_TOKENS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "at", "by", "for", "with",
    "and", "or", "not", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "if", "then", "else",
    "as", "from", "into", "out", "over", "under", "between", "above",
    "below", "after", "before", "during", "while", "when", "where",
    "what", "which", "who", "whom", "whose", "why", "how", "use", "uses",
    "used", "using", "must", "should", "may", "can", "will", "would",
    "could", "do", "does", "did", "done", "doing", "fix", "fixes",
    "fixed", "check", "checks", "checked", "checking", "see", "seen",
    "look", "looks", "looked", "looking", "find", "found", "finding",
    "tail", "head", "top", "bottom", "left", "right",
})


def collect_grounded_code(
    targets: list[str],
    files: list[Path],
    *,
    max_lines: int = 120,
    fallback_grep_ctx: int = 14,
) -> dict[str, str]:
    """For each target identifier, search every file and return the first hit.

    The lookup tries function-extraction first; if that fails (target is a
    constant, a struct field, a comment marker, etc.), falls back to grep
    context. Language inferred per-file from extension; markdown fence tag
    matches the file's language.

    Common English words (``the``, ``of``, ``at``, ...) and short
    single-character or numeric tokens are filtered out — they leak through
    naive prose tokenizers and would otherwise produce noise headers.

    Returns: dict[target_id -> formatted-code-block-or-empty].
    """
    out: dict[str, str] = {}
    for raw in targets:
        target = raw.strip()
        if not target:
            continue
        # Filter prose noise: stop-words, pure digits, and <3-char tokens.
        if target.lower() in _STOP_TOKENS:
            continue
        if len(target) < 3:
            continue
        if target.isdigit():
            continue
        looks_like_ident = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target))
        for f in files:
            block: str | None = None
            if looks_like_ident:
                block = extract_function(f, target, max_lines=max_lines)
            if block is None:
                block = extract_grep_context(f, target, ctx_lines=fallback_grep_ctx)
            if block:
                fence_lang = _markdown_lang_for(f.suffix.lower())
                out[target] = (
                    f"### `{target}` from `{f.name}`\n"
                    f"```{fence_lang}\n{block}\n```"
                )
                break
        else:
            out[target] = ""  # no hit anywhere
    return out
