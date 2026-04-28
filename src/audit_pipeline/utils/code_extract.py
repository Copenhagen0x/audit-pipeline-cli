"""Extract Rust source code into agent prompts.

The agent's prior weakness: it claims to "read" files but cannot actually
access them. This module fixes that by pulling the relevant source bytes
out of the workspace and embedding them in the prompt as
CODE-GROUNDED CONTEXT.

Two extraction strategies:
  - extract_function(file, name)  : pull a complete `fn <name>(...) {...}` block
  - extract_grep_context(file, p, ctx_lines) : pull lines around grep matches

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


def extract_function(
    file_path: Path,
    function_name: str,
    max_lines: int = 120,
) -> str | None:
    """Extract a single Rust function by name.

    Matches `fn name(`, `fn name<`, `pub fn name(` etc. Returns the function
    body up to the matching close-brace, with line numbers. Returns None if
    not found.
    """
    if not file_path.exists():
        return None
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()

    # Find the line where the function starts
    start_pattern = re.compile(
        rf"\b(pub\s+(?:\(crate\)\s+)?)?(unsafe\s+)?(async\s+)?fn\s+{re.escape(function_name)}\s*[<(]"
    )
    start_idx = None
    for i, ln in enumerate(lines):
        if start_pattern.search(ln):
            start_idx = i
            break
    if start_idx is None:
        return None

    # Walk forward, brace-matching
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
    """Extract context windows around occurrences of `pattern`.

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
    context.

    Returns: dict[target_id -> formatted-code-block-or-empty].
    """
    out: dict[str, str] = {}
    for raw in targets:
        target = raw.strip()
        if not target:
            continue
        # If the target looks identifier-like, try function extraction
        looks_like_ident = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target))
        for f in files:
            block: str | None = None
            if looks_like_ident:
                block = extract_function(f, target, max_lines=max_lines)
            if block is None:
                block = extract_grep_context(f, target, ctx_lines=fallback_grep_ctx)
            if block:
                rel = f.name
                out[target] = f"### `{target}` from `{rel}`\n```rust\n{block}\n```"
                break
        else:
            out[target] = ""  # no hit anywhere
    return out
