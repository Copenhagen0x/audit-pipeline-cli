"""Gate 2 — L2.symbol_grep.

Verifies that every project-specific symbol cited in a PoC test actually
exists in the engine or wrapper source. Built in response to cycle
20260511-183154, where two PoCs (#11 ``MarketConfig.oracle_leg_feed_id``
and #13 ``settle_after_close``) referenced symbols that don't exist in
either repo — and the pipeline still recorded them as "fired".

The gate extracts ``snake_case`` (≥2 words) and ``CamelCase`` identifiers
from the PoC source, filters out a Rust/Solana stdlib whitelist, and
``grep``s each surviving identifier against the engine + wrapper ``src/``
trees. Any identifier with zero hits is a HALLUCINATION and the gate
fails.

Pure heuristic — not a Rust parser — so a small false-positive rate is
expected. The whitelist below is the main escape hatch; per-test
``# audit-pipeline: allow <symbol>`` comments could be added later if
hand-tuned escapes become common.

Used by: ``commands/poc_llm.py`` (after PoC authoring, before save).
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from audit_pipeline.gates import GateResult


# snake_case identifier: at least one underscore, at least 4 chars, no
# leading underscore (private/internal markers). Catches function and field
# names like ``compute_trade_pnl``, ``oracle_leg_feed_id``.
_SNAKE_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# CamelCase identifier with at least one inner case-change. Catches struct
# / enum / type names like ``MarketConfig``, ``RiskEngine``.
_CAMEL_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]+)+)\b")


# Rust std + ecosystem identifiers we never want to flag. Curated, not
# exhaustive — additions are cheap when we false-positive. Lowercase form.
_RUST_STD_WHITELIST: set[str] = {
    # primitive type families
    "to_string", "from_str", "as_str", "from_bytes", "to_bytes",
    "to_owned", "into_inner",
    # Result/Option/Iterator
    "unwrap_or", "unwrap_or_else", "unwrap_or_default",
    "ok_or", "ok_or_else", "and_then", "or_else", "map_or",
    "is_some", "is_none", "is_ok", "is_err", "is_empty",
    # std collections
    "iter_mut", "into_iter", "as_slice", "as_mut_slice",
    "checked_add", "checked_sub", "checked_mul", "checked_div",
    "checked_rem", "saturating_add", "saturating_sub", "saturating_mul",
    "wrapping_add", "wrapping_sub", "wrapping_mul",
    "overflowing_add", "overflowing_sub", "overflowing_mul",
    "unsigned_abs", "checked_neg", "rotate_left", "rotate_right",
    # std prelude
    "from_raw_parts", "from_raw_parts_mut",
    # common Solana / Anchor / borsh helpers
    "try_from_slice", "try_to_vec", "deserialize_reader",
    "try_borrow_data", "try_borrow_mut_data", "try_borrow_lamports",
    "try_borrow_mut_lamports", "to_account_info",
    # test framework
    "test_case", "test_log",
}

_RUST_STD_CAMEL_WHITELIST: set[str] = {
    "Vec", "String", "HashMap", "HashSet", "BTreeMap", "BTreeSet",
    "Result", "Option", "Box", "Rc", "Arc", "RefCell", "Cell",
    "Mutex", "RwLock", "Cow", "Path", "PathBuf", "OsString",
    "AccountInfo", "Pubkey", "Sysvar", "Clock", "Rent", "ProgramError",
    "AnchorError", "Context", "InstructionData", "AnchorDeserialize",
    "AnchorSerialize", "Discriminator", "ZeroCopy",
    "TestEnvironment", "ProgramTestContext",  # solana-program-test
}


def _classify(ident: str, is_camel: bool) -> str:
    """Return 'whitelist' | 'check'. CamelCase whitelist is separate from snake."""
    if is_camel:
        return "whitelist" if ident in _RUST_STD_CAMEL_WHITELIST else "check"
    # PoC test wrappers always start with ``test_``; the PoC's own function
    # name is not a claim about the codebase under test.
    if ident.startswith("test_"):
        return "whitelist"
    return "whitelist" if ident in _RUST_STD_WHITELIST else "check"


def extract_project_symbols(poc_source: str) -> dict[str, list[str]]:
    """Pull non-stdlib symbols out of a Rust PoC test source.

    Returns dict with two lists: ``snake_case`` and ``camel_case``, each
    containing the unique project-specific identifiers found.
    """
    # Strip line + block comments so identifiers in comments don't count
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", poc_source)
    no_line  = re.sub(r"//[^\n]*", "", no_block)
    # Also strip raw + regular string literals — symbol-looking strings
    # inside ``"..."`` shouldn't be greppable claims.
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"', "", no_line)
    snake = {m.group(1) for m in _SNAKE_RE.finditer(no_strings)}
    camel = {m.group(1) for m in _CAMEL_RE.finditer(no_strings)}
    return {
        "snake_case": sorted(s for s in snake if _classify(s, False) == "check"),
        "camel_case": sorted(c for c in camel if _classify(c, True) == "check"),
    }


def _grep_exists(symbol: str, search_dirs: Iterable[Path]) -> bool:
    """Return True if ``symbol`` appears in any ``.rs`` file under any
    of ``search_dirs``."""
    for d in search_dirs:
        if not d.is_dir():
            continue
        # ``grep -r --include='*.rs' -w SYMBOL DIR``: ``-w`` enforces a word
        # boundary so ``foo`` doesn't match ``foo_bar``.
        try:
            proc = subprocess.run(
                ["grep", "-rlw", "--include=*.rs", symbol, str(d)],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            # Fallback: pure-Python walk (Windows dev boxes don't always
            # have grep on PATH). Slower but correct.
            return _python_grep(symbol, d)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def _python_grep(symbol: str, root: Path) -> bool:
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    for p in root.rglob("*.rs"):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(txt):
            return True
    return False


def check_symbols(
    *,
    poc_source: str,
    search_dirs: list[Path],
    max_hallucinations: int = 0,
) -> GateResult:
    """Verify every project-looking symbol in the PoC exists in ``search_dirs``.

    Args:
        poc_source:         the Rust test source to validate.
        search_dirs:        list of directories to grep (engine src, wrapper
                            src). Non-existent dirs are skipped silently.
        max_hallucinations: budget for unrecognised symbols. ``0`` (default)
                            is strict — any missing symbol fails. ``>=1``
                            tolerates that many missing (useful during
                            iteration on the gate's whitelist).

    Returns:
        ``GateResult(True, …)`` if every checked symbol resolves
        ``GateResult(False, …)`` with details listing each hallucination
        ``GateResult(None, …)`` if ``search_dirs`` contains no valid trees
    """
    t0 = time.time()
    valid_dirs = [d for d in search_dirs if d.is_dir()]
    if not valid_dirs:
        return GateResult(
            passed=None,
            reason=f"no valid search dirs in {[str(d) for d in search_dirs]}",
            duration_s=time.time() - t0,
        )

    symbols = extract_project_symbols(poc_source)
    all_candidates = symbols["snake_case"] + symbols["camel_case"]
    if not all_candidates:
        return GateResult(
            passed=True,
            reason="no project-specific symbols cited (PoC is whitelist-only)",
            duration_s=time.time() - t0,
            details={"checked": 0},
        )

    missing: list[str] = []
    present: list[str] = []
    for sym in all_candidates:
        if _grep_exists(sym, valid_dirs):
            present.append(sym)
        else:
            missing.append(sym)

    details = {
        "checked": len(all_candidates),
        "present": present,
        "missing": missing,
        "search_dirs": [str(d) for d in valid_dirs],
    }

    if len(missing) > max_hallucinations:
        return GateResult(
            passed=False,
            reason=(
                f"{len(missing)} symbol(s) cited in PoC do not exist in "
                f"engine or wrapper source: "
                f"{', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}. "
                "Hallucinated function/struct names — PoC author was guessing. "
                "Re-author with grounded source context."
            ),
            duration_s=time.time() - t0,
            details=details,
        )

    return GateResult(
        passed=True,
        reason=f"all {len(all_candidates)} project symbols resolve in source",
        duration_s=time.time() - t0,
        details=details,
    )


__all__ = ["check_symbols", "extract_project_symbols"]
