"""AST-grep signature engine for cross-protocol propagation (Wave 8a).

Complements the existing regex scanner in `propagate.py` with a tree-sitter
based scanner that matches **structural** patterns rather than literal
text. AST patterns dramatically reduce false positives by ignoring
matches inside comments, strings, and type signatures that aren't
function bodies.

Two scanners now run side-by-side:

  * Regex scanner    (propagate.py:_scan_file_for_signatures)
  * AST scanner      (this module:_scan_file_for_ast_patterns)

Their results are union'd in `run_for_finding_combined` (called from the
auto-fire entry point). Each match is tagged with its source so
downstream tooling can compare regex vs AST hit rates.

## Why tree-sitter

  * Solana programs are Rust. tree-sitter-rust is a mature parser, used
    by GitHub's code search, Linguist, and most modern Rust LSPs.
  * Python bindings (`tree-sitter` + `tree-sitter-rust` packages) are
    pure-pip-install, no system dependencies.
  * Graceful fallback: if tree-sitter isn't installed, the AST scanner
    no-ops and the existing regex flow continues uninterrupted.

## Catalog: BUG_CLASS_AST_PATTERNS

Maps `bug_class` to a list of tree-sitter S-expression queries. For
each declared pattern, the scanner walks the parse tree of every .rs
file in the corpus and reports matches.

A small initial catalog ships covering classes where AST matching
gives clear value over regex (function-body-scoped matches, type-
signature matches, attribute-macro matches). The remaining 40+ classes
in BUG_CLASS_SIGNATURES still use regex via the existing scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AstMatch:
    """A single tree-sitter match: structural location of the pattern."""
    repo: str
    file: str
    line: int
    pattern_name: str
    snippet: str


# ---------------------------------------------------------------------------
# AST pattern catalog
# ---------------------------------------------------------------------------
#
# Each entry is a list of (pattern_name, tree-sitter S-expression) pairs.
# Pattern names should be descriptive — they show up in the propagation
# report as "matched: <pattern_name>".
#
# Tree-sitter Rust grammar reference:
# https://github.com/tree-sitter/tree-sitter-rust/blob/master/src/grammar.json
#
# These complement (not replace) BUG_CLASS_SIGNATURES regex patterns.
# A bug_class with both regex AND AST patterns gets the union of matches.

BUG_CLASS_AST_PATTERNS: dict[str, list[tuple[str, str]]] = {
    # F7's class — match function bodies that mutate insurance counter
    "insurance-counter-vault-divergence": [
        (
            "insurance_balance_mutation",
            "(assignment_expression "
            "  left: (field_expression field: (field_identifier) @field) "
            "  (#match? @field \"^balance$\"))",
        ),
    ],

    # Admin-gate-bypass: function with no signer-check call but writes to admin
    "admin-gate-bypass": [
        (
            "admin_field_assignment",
            "(assignment_expression "
            "  left: (field_expression field: (field_identifier) @field) "
            "  (#match? @field \"^(admin|authority|owner)$\"))",
        ),
    ],

    # Pause-bypass: function that mutates state without checking paused flag
    "pause-bypass": [
        (
            "is_paused_check",
            "(macro_invocation "
            "  macro: (identifier) @macro "
            "  (#eq? @macro \"require\"))",
        ),
    ],

    # Account-discriminator-bypass: deserialize without discriminator check
    "account-discriminator-bypass": [
        (
            "try_from_slice_call",
            "(call_expression "
            "  function: (field_expression field: (field_identifier) @method) "
            "  (#eq? @method \"try_from_slice\"))",
        ),
    ],

    # Authorization-bypass: signer check pattern
    "authorization-bypass": [
        (
            "is_signer_check",
            "(field_expression "
            "  field: (field_identifier) @field "
            "  (#eq? @field \"is_signer\"))",
        ),
    ],

    # Arithmetic-overflow-pnl-mark: unchecked arithmetic on i128
    "arithmetic-overflow-pnl-mark": [
        (
            "i128_binary_op",
            "(binary_expression "
            "  left: (cast_expression type: (primitive_type) @type) "
            "  (#match? @type \"^i128$\"))",
        ),
    ],

    # Constant-product-invariant-violation: x*y multiplications on reserves
    "constant-product-invariant-violation": [
        (
            "reserve_multiplication",
            "(binary_expression "
            "  operator: \"*\" "
            "  left: (field_expression field: (field_identifier) @field) "
            "  (#match? @field \"^(reserve|reserves|x|y)\"))",
        ),
    ],
}


# ---------------------------------------------------------------------------
# tree-sitter loader (graceful fallback when missing)
# ---------------------------------------------------------------------------

_TS_AVAILABLE: bool | None = None  # tri-state cache: None=untried, True/False=tried
_TS_LANGUAGE: Any = None
_TS_PARSER: Any = None


def _try_load_tree_sitter() -> bool:
    """Lazy-load tree-sitter-rust. Returns False if not installed.

    Python tree-sitter has had two incompatible API generations; this
    function tries the newer (0.21+) interface first, then falls back
    to the older API. Either way, we set _TS_PARSER on success or
    leave it None on failure, so the scanner can fail-soft.
    """
    global _TS_AVAILABLE, _TS_LANGUAGE, _TS_PARSER
    if _TS_AVAILABLE is not None:
        return _TS_AVAILABLE
    try:
        import tree_sitter
        try:
            # Newer style (0.21+): tree_sitter_rust ships its own .language()
            import tree_sitter_rust
            lang_obj = tree_sitter_rust.language()
            if hasattr(tree_sitter, "Language"):
                _TS_LANGUAGE = tree_sitter.Language(lang_obj)
            else:
                _TS_LANGUAGE = lang_obj
            _TS_PARSER = tree_sitter.Parser(_TS_LANGUAGE) if hasattr(tree_sitter.Parser, "__init__") else tree_sitter.Parser()
            if hasattr(_TS_PARSER, "language") and not callable(getattr(_TS_PARSER, "language", None)):
                _TS_PARSER.language = _TS_LANGUAGE
            elif hasattr(_TS_PARSER, "set_language"):
                _TS_PARSER.set_language(_TS_LANGUAGE)
        except ImportError:
            # Older style: tree_sitter_languages shipped by community
            from tree_sitter_languages import get_language, get_parser
            _TS_LANGUAGE = get_language("rust")
            _TS_PARSER = get_parser("rust")
        _TS_AVAILABLE = True
        return True
    except Exception:
        _TS_AVAILABLE = False
        _TS_LANGUAGE = None
        _TS_PARSER = None
        return False


def is_ast_available() -> bool:
    """Public: did tree-sitter load successfully?"""
    return _try_load_tree_sitter()


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def patterns_for_bug_class(bug_class: str) -> list[tuple[str, str]]:
    """Return registered AST patterns for a bug_class, or [] if none."""
    return list(BUG_CLASS_AST_PATTERNS.get(bug_class, []))


def scan_file_for_ast_patterns(
    src_text: str,
    patterns: list[tuple[str, str]],
) -> list[tuple[str, int, str]]:
    """Run tree-sitter queries over src_text. Returns (pattern_name, line, snippet).

    Returns [] if tree-sitter isn't available (so callers don't need
    to guard the call site — the scanner just contributes nothing
    in that case).
    """
    if not _try_load_tree_sitter():
        return []
    if not patterns:
        return []
    try:
        src_bytes = src_text.encode("utf-8", errors="replace")
        tree = _TS_PARSER.parse(src_bytes)
        hits: list[tuple[str, int, str]] = []
        for name, query_str in patterns:
            try:
                query = _TS_LANGUAGE.query(query_str)
                for node, _capture_name in query.captures(tree.root_node):
                    line_no = node.start_point[0] + 1  # 1-indexed
                    snippet = src_text.splitlines()[max(0, line_no - 2):line_no + 1]
                    hits.append((name, line_no, "\n".join(snippet)))
            except Exception:
                # Bad query string or ts API mismatch — skip this pattern
                continue
        return hits
    except Exception:
        return []


def scan_corpus_for_ast_patterns(
    corpus_path: Path,
    bug_class: str,
) -> list[AstMatch]:
    """Walk corpus and produce AST matches for the bug_class.

    Mirrors the regex scanner's output shape so results can be merged.
    Returns empty list if tree-sitter isn't installed.
    """
    if not _try_load_tree_sitter():
        return []

    patterns = patterns_for_bug_class(bug_class)
    if not patterns:
        return []

    out: list[AstMatch] = []
    if not corpus_path.is_dir():
        return out

    skip_dirs = {"target", "node_modules", ".git", "build"}
    for repo_dir in sorted(p for p in corpus_path.iterdir() if p.is_dir()):
        for src_path in repo_dir.rglob("*.rs"):
            if not src_path.is_file():
                continue
            if any(part in skip_dirs for part in src_path.parts):
                continue
            try:
                content = src_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = scan_file_for_ast_patterns(content, patterns)
            for name, line_no, snippet in hits:
                out.append(AstMatch(
                    repo=repo_dir.name,
                    file=str(src_path.relative_to(repo_dir)),
                    line=line_no,
                    pattern_name=name,
                    snippet=snippet,
                ))
    return out
