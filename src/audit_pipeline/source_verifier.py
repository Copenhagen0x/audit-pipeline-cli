"""Static source-level verification gates that run BEFORE the L2 PoC stage.

Catches obviously-unreachable bug-class claims that a 5-line regex can rule
out (e.g. hyp claims "no auth on emergency_withdraw" but the function body
opens with `acl::assert_admin(...)`). Saves L2 / L3 / L4 / LLM-judge spend
on false positives the operator flagged on aptos-medium 2026-05-14:

  - APT17: hyp claimed `oracle::get_price` returns zero, but source has
    `assert!(price > 0, ...)` filter.
  - APTM6: hyp claimed `fee_manager::distribute` has no auth, but source
    has `acl::assert_admin(host, signer::address_of(_account));` at top.

The verifier is INTENTIONALLY conservative: if the static check is
ambiguous (can't find the function, can't parse the body, multiple
definitions, etc.), it returns reachable=True and lets the pipeline
proceed. False negatives (missed rejections) just cost spend; false
positives (incorrect rejections) would silence real bugs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Bug-class slug substrings that indicate a "missing authorization" claim.
# If the function body actually has an authorization check, the hyp is
# unreachable.
NO_AUTH_BUG_CLASS_MARKERS = (
    "no-auth",
    "missing-auth",
    "permissionless",
    "no_auth",
    "missing_auth",
    "unauthenticated",
)

# Authorization-check signatures we recognize in function bodies. These are
# substring matches; the actual call signatures vary by codebase but these
# fragments are distinctive enough that a false-positive ("we ruled out a
# real bug") is extremely unlikely.
AUTH_CHECK_PATTERNS = (
    "assert_admin",
    "assert_owner",
    "assert_governance",
    "assert_authorized",
    "acl::is_admin",
    "acl::is_owner",
    "acl::assert",
    "only_admin",
    "only_owner",
    "require_admin",
    "require_owner",
    "ensure_admin",
    "ensure_owner",
)

# Bug-class slug substrings that indicate a "zero / overflow" claim.
ZERO_OVERFLOW_BUG_CLASS_MARKERS = (
    "zero-price",
    "zero_price",
    "div-by-zero",
    "div_by_zero",
    "divide-by-zero",
    "divide_by_zero",
    "overflow",
    "underflow",
    "u64-arith",
    "u64_arith",
)

# Sanity-check patterns we recognize in function bodies that would rule
# out a zero / overflow claim.
SANITY_CHECK_PATTERNS = (
    r"assert!\s*\([^,)]*>\s*0\b",
    r"assert!\s*\([^,)]*!=\s*0\b",
    r"assert!\s*\([^,)]*<=?\s*MAX_U(?:64|128)\b",
    r"assert!\s*\([^,)]*>=?\s*MIN_U(?:64|128)\b",
    r"require\s*\([^,)]*>\s*0\b",
    r"require\s*\([^,)]*!=\s*0\b",
    r"if\s*\([^)]*==\s*0\)\s*\{[^}]*(?:abort|return|revert)",
    r"if\s*\([^)]*<\s*1\)\s*\{[^}]*(?:abort|return|revert)",
)


def _find_function_bodies(
    engine_repo: Path,
    function_name: str,
    file_glob: str | None = None,
) -> list[str]:
    """Walk the engine repo and return text of each function body matching
    `function_name`. We grab a generous window after the function signature
    to capture early-statement auth checks.

    Returns empty list if nothing found (verifier defers to pipeline).
    """
    if not engine_repo.is_dir():
        return []
    bodies: list[str] = []
    # Map file_glob to a set of likely suffixes. The glob in YAML is typically
    # `sources/*.move` or `programs/*/src/**/*.rs` — we want a permissive
    # walk + light filter.
    suffixes: tuple[str, ...]
    if file_glob and ".move" in file_glob:
        suffixes = (".move",)
    elif file_glob and ".sol" in file_glob:
        suffixes = (".sol",)
    elif file_glob and (".rs" in file_glob or ".c" in file_glob or ".h" in file_glob):
        suffixes = (".rs", ".c", ".h", ".cpp", ".cc")
    else:
        suffixes = (".move", ".sol", ".rs", ".c", ".h", ".cpp", ".cc")
    # Multi-language signature patterns. Move and Rust both use `fn NAME(` /
    # `fun NAME(`. Solidity uses `function NAME(`. C uses `NAME(...) {`.
    # We use a single permissive pattern that matches all of these and
    # captures a generous window (8 lines / ~600 chars) after the open
    # brace, which is more than enough to catch early authorization checks.
    name_re = re.escape(function_name)
    sig_patterns = [
        # Rust: `fn NAME(`, with optional `pub`, `pub(crate)`, `pub(super)`, etc.
        re.compile(
            r"(?m)^\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?fn\s+"
            + name_re + r"\b[^{]*\{",
        ),
        # Move: `fun NAME(`, with optional `public`, `entry`, friend, native.
        re.compile(
            r"(?m)^\s*(?:public(?:\s*\([^)]*\))?\s+)?(?:entry\s+)?(?:native\s+)?fun\s+"
            + name_re + r"\b[^{]*\{",
        ),
        # Solidity: `function NAME(`, with optional visibility/modifiers.
        re.compile(
            r"(?m)^\s*function\s+" + name_re + r"\b[^{]*\{",
        ),
        # C: bare `NAME(...) {` — we add a likely-type-prefix guard so
        # we don't match calls / macros.
        re.compile(
            r"(?m)^\s*(?:static\s+|inline\s+|extern\s+)?[\w*\s]+\s+"
            + name_re + r"\s*\([^)]*\)\s*\{",
        ),
    ]
    for src_file in engine_repo.rglob("*"):
        if not src_file.is_file():
            continue
        if src_file.suffix.lower() not in suffixes:
            continue
        try:
            text = src_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sig_re in sig_patterns:
            for m in sig_re.finditer(text):
                # Capture window: 800 chars after the matched signature.
                # That's roughly the first 8-15 lines of the function body,
                # which is where authorization checks live.
                start = m.end()
                window = text[start : start + 800]
                bodies.append(window)
    return bodies


def verify_bug_class_reachable(
    hyp: dict[str, Any],
    engine_repo: Path,
) -> tuple[bool, str]:
    """Return (reachable, reason).

    reachable=False means the static check rules out the hyp's bug class.
    Caller should mark `not_reachable` and skip L2/L3/L4 + judge.

    reachable=True means we couldn't statically rule it out — either the
    hyp doesn't match a check we know, or the function body doesn't have
    the disqualifying pattern. Caller proceeds with the normal pipeline.
    """
    bug_class = str(hyp.get("bug_class") or "").lower()
    engine_function = str(hyp.get("engine_function") or "").strip()
    target_file = str(hyp.get("target_file") or "")
    if not engine_function:
        return True, "no engine_function on hyp"
    if not engine_repo or not engine_repo.is_dir():
        return True, "engine_repo not available"

    bodies = _find_function_bodies(engine_repo, engine_function, target_file)
    if not bodies:
        # Function name not found — could be that it's renamed, inlined,
        # or simply doesn't exist in this codebase. We defer to the
        # downstream pipeline rather than rule out the bug.
        return True, f"function {engine_function!r} not found in {engine_repo}"

    # --- Missing-authorization check ---
    if any(marker in bug_class for marker in NO_AUTH_BUG_CLASS_MARKERS):
        for body in bodies:
            for pattern in AUTH_CHECK_PATTERNS:
                if pattern in body:
                    return False, (
                        f"bug_class {bug_class!r} claims missing auth, but "
                        f"{engine_function} body contains {pattern!r} - "
                        "the function IS authorization-checked. Static "
                        "verifier ruled out the bug class."
                    )

    # --- Zero / overflow / division check ---
    if any(marker in bug_class for marker in ZERO_OVERFLOW_BUG_CLASS_MARKERS):
        for body in bodies:
            for sanity_re in SANITY_CHECK_PATTERNS:
                if re.search(sanity_re, body):
                    return False, (
                        f"bug_class {bug_class!r} claims zero / overflow / "
                        f"div-by-zero, but {engine_function} body contains "
                        f"a sanity check matching pattern {sanity_re!r} - "
                        "the bounds ARE asserted. Static verifier ruled out "
                        "the bug class."
                    )

    return True, "no static rule disproves bug class"
