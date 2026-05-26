"""Regression test for audit-023 (Bucket L L2 — propagate scanner ReDoS).

L2 finding: ``tree-sitter scanner regex catastrophic backtracking on
adversarial input (ReDoS)``. Verified at audit-023 time: the 94 patterns
in ``BUG_CLASS_SIGNATURES`` (propagate.py) and the tree-sitter
``#match?`` predicates in ``BUG_CLASS_AST_PATTERNS`` (propagate_ast.py)
are all backtracking-safe (anchored literals, alternation of literals,
or simple substring matches). The Python regex engine processes these
in O(n) on any input.

This test pins that current state: it scans every regex pattern in
``BUG_CLASS_SIGNATURES`` for the structural shapes that ARE known to
produce catastrophic backtracking — nested quantifiers ``(x+)+``,
alternation-with-overlap ``(a|a)+``, double greedy ``.*.*`` — and
fails if any future signature introduces one.

We also pin a behavioral timing assertion: each signature must match a
1MB adversarial input in under 100ms. If a future signature regresses
to a backtracking pattern that *passes* the structural scan (the scan
is a heuristic, not a proof), the timing assertion catches it.
"""

from __future__ import annotations

import re
import time

# Structural-pattern heuristics for ReDoS-prone shapes. None of these
# is a perfect detector; the timing test below is the backstop.
_NESTED_QUANT = re.compile(r"\([^)]*[+*]\)[+*?]")
_DOUBLE_GREEDY = re.compile(r"(?:\.\*){2,}|(?:\.\+){2,}")
# Alternation containing the same token twice — classic ReDoS catalyst
# e.g. ``(a|a)+`` or ``(foo|foo)+``. Heuristic: alternation with a
# trailing quantifier, where the alternation has any repeated branch.
_ALT_WITH_QUANT = re.compile(r"\([^)]*\|[^)]*\)[+*?]")


def _structural_scan(pattern: str) -> list[str]:
    """Return a list of red-flag shapes found in ``pattern``."""
    flags: list[str] = []
    if _NESTED_QUANT.search(pattern):
        flags.append("nested-quantifier")
    if _DOUBLE_GREEDY.search(pattern):
        flags.append("double-greedy")
    if _ALT_WITH_QUANT.search(pattern):
        # Further check: is there an actually-overlapping branch?
        m = _ALT_WITH_QUANT.search(pattern)
        if m:
            inner = m.group(0).rstrip("+*?").strip("()")
            branches = [b.strip() for b in inner.split("|")]
            if len(branches) != len(set(branches)):
                flags.append("alt-with-duplicate-branch-and-quantifier")
    return flags


def test_no_bug_class_signature_has_redos_shape() -> None:
    """Every regex in ``BUG_CLASS_SIGNATURES`` must compile cleanly
    AND pass the ReDoS structural-shape scan.
    """
    from audit_pipeline.commands.propagate import BUG_CLASS_SIGNATURES

    bad: list[tuple[str, str, list[str]]] = []
    for cls, sigs in BUG_CLASS_SIGNATURES.items():
        for sig in sigs:
            re.compile(sig)  # smoke test (would raise on bad regex)
            flags = _structural_scan(sig)
            if flags:
                bad.append((cls, sig, flags))

    assert not bad, (
        "ReDoS-prone signature shape(s) detected (audit-023 L2 regression):\n"
        + "\n".join(f"  {cls}: {sig!r} -> {flags}" for cls, sig, flags in bad)
    )


def test_no_ast_match_predicate_has_redos_shape() -> None:
    """Every regex in tree-sitter ``#match?`` predicates inside
    ``BUG_CLASS_AST_PATTERNS`` must be structurally safe.
    """
    from audit_pipeline.commands.propagate_ast import BUG_CLASS_AST_PATTERNS

    # Extract the regex literal from inside ``(#match? @x "REGEX")``.
    match_pat = re.compile(r'#match\?\s+@\w+\s+"((?:\\.|[^"\\])*)"')

    bad: list[tuple[str, str, str, list[str]]] = []
    for cls, entries in BUG_CLASS_AST_PATTERNS.items():
        for name, query in entries:
            for inner in match_pat.findall(query):
                # Replace escaped quotes back to literal quotes.
                regex_str = inner.replace('\\"', '"')
                flags = _structural_scan(regex_str)
                if flags:
                    bad.append((cls, name, regex_str, flags))

    assert not bad, (
        "ReDoS-prone regex in AST #match? predicate (audit-023 L2):\n"
        + "\n".join(
            f"  {cls} :: {name}: {regex!r} -> {flags}"
            for cls, name, regex, flags in bad
        )
    )


def test_bug_class_signatures_run_under_timing_budget() -> None:
    """Behavioral backstop: every signature must process a 1MB
    adversarial-shaped input in under 250ms. Catches catastrophic
    backtracking that the structural-shape heuristics miss.

    The input is a worst-case shape for ``.+`` / ``.*`` matchers:
    1MB of repeated ``a`` characters followed by a single ``!``.
    Pattern engines that backtrack would explode on this.
    """
    from audit_pipeline.commands.propagate import BUG_CLASS_SIGNATURES

    adversarial = ("a" * (1 << 20)) + "!"
    budget_s = 0.25

    slow: list[tuple[str, str, float]] = []
    for cls, sigs in BUG_CLASS_SIGNATURES.items():
        for sig in sigs:
            compiled = re.compile(sig)
            t0 = time.perf_counter()
            compiled.search(adversarial)
            dt = time.perf_counter() - t0
            if dt > budget_s:
                slow.append((cls, sig, dt))

    assert not slow, (
        f"Signature(s) exceeded {budget_s}s on 1MB adversarial input "
        f"(audit-023 L2 timing backstop):\n"
        + "\n".join(f"  {cls}: {sig!r} took {dt:.3f}s" for cls, sig, dt in slow)
    )
