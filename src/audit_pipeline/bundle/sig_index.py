"""Cross-module function signature index for LLM patch authorship.

The patcher passes the target file's full source. But for structural
fixes (the kind we want), the LLM often needs to call helpers in other
modules — and without seeing their signatures, it invents them, producing
diffs that reference non-existent functions or wrong arities.

This module walks the engine repo and extracts public function /
struct / type signatures from every .rs file. The result is a compact
prompt section that lets the LLM cite real helpers instead of making
them up.

Scope kept tight on purpose:
  * Only `pub fn` / `pub(crate) fn` (private fns aren't callable cross-module)
  * Only `pub struct` / `pub enum` (private types aren't usable cross-module)
  * Strip function bodies — only the signature line is kept
  * Skip the target file (its full source already goes into the prompt)
  * Skip vendored / generated / test directories
  * Hard token-budget cap (~8K chars) so the prompt stays small
"""

from __future__ import annotations

import re
from pathlib import Path

# Signature patterns. Single-line only — multi-line signatures (rare in Solana
# programs) get the first line; the LLM can still see the function name + return
# context, which is the load-bearing part of the index.
_PUB_FN = re.compile(r"^\s*pub(\s*\([^)]+\))?\s+(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]")
_PUB_TYPE = re.compile(r"^\s*pub(\s*\([^)]+\))?\s+(struct|enum|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)")

_SKIP_DIR_NAMES = {
    "target", "node_modules", ".git", "tests", "test", "test_utils",
    "fixtures", "examples", "benches", "vendor", "third_party",
}


def _should_skip(p: Path) -> bool:
    return any(part in _SKIP_DIR_NAMES for part in p.parts)


def _extract_signatures(path: Path) -> list[tuple[int, str]]:
    """Return [(line_number, signature_text), ...] for one file."""
    out: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.rstrip()
        if _PUB_FN.match(stripped) or _PUB_TYPE.match(stripped):
            # Trim trailing `{` and excess whitespace for compactness
            sig = stripped.rstrip(" {").rstrip()
            out.append((i, sig))
    return out


def build_sig_index(
    engine_repo: Path,
    target_file_path: str | None = None,
    max_chars: int = 8000,
) -> str:
    """Build a compact cross-module signature index for the LLM patcher.

    Args:
      engine_repo: root of the repo whose .rs files to scan.
      target_file_path: relative path of the file the patcher is editing
                        (skipped to avoid duplication with `target_source`).
      max_chars: hard cap so the prompt stays under context budget.

    Returns:
      A string like:

        ## Cross-module signatures (other files in the repo)

        ### programs/foo/src/state.rs
        - L42: pub fn admin_only(ctx: Context<AdminOnly>) -> Result<()>
        - L88: pub struct VaultState

        ### programs/bar/src/lib.rs
        - L10: pub fn settle(ctx: Context<Settle>, amt: u64) -> Result<()>

      Empty string if no .rs files found or engine_repo is invalid.
    """
    if not engine_repo or not engine_repo.is_dir():
        return ""

    target_abs: Path | None = None
    if target_file_path:
        cand = (engine_repo / target_file_path).resolve()
        if cand.is_file():
            target_abs = cand

    sections: list[str] = []
    total = 0
    for rs_file in sorted(engine_repo.rglob("*.rs")):
        if _should_skip(rs_file.relative_to(engine_repo)):
            continue
        if target_abs and rs_file.resolve() == target_abs:
            continue
        sigs = _extract_signatures(rs_file)
        if not sigs:
            continue
        rel = rs_file.relative_to(engine_repo).as_posix()
        section = "### " + rel + "\n"
        for ln, sig in sigs:
            section += "- L" + str(ln) + ": " + sig + "\n"
        sections.append(section)
        total += len(section)
        if total > max_chars:
            sections.append("\n(signature index truncated — limit reached)\n")
            break

    if not sections:
        return ""

    header = (
        "## Cross-module signatures (other files in the repo)\n\n"
        "These are public function / type signatures from OTHER files in the\n"
        "engine repo. Use them to call real helpers in your patch instead of\n"
        "inventing function names or arities. **Do NOT** assume a function\n"
        "exists unless it appears below or in the target file source above.\n\n"
    )
    return header + "\n".join(sections)
