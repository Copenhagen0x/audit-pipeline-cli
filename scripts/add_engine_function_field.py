#!/usr/bin/env python3
"""One-time migration: add `engine_function` field to all Percolator hyps.

Reads each YAML hyp file under templates/hypotheses/, extracts a likely
engine function name from each hyp's `relevant_instructions` text, and
adds an `engine_function:` line below `bug_class:`.

Heuristic for extraction (in priority order):
  1. Snake_case identifier followed by `(` → likely Rust function call
  2. Snake_case identifier ending with _not_atomic, _to, _with_matcher,
     or _internal → Percolator naming convention
  3. CamelCase identifier near start of relevant_instructions → likely
     Anchor v2 instruction name (TradeCpi, KeeperCrank, etc.)
  4. Fallback: "absorb_protocol_loss" (the F7 function — matches engine
     default per hunt.py logic)

This is a best-effort routing hint. The LLM-authored PoC (poc_llm.py)
reads the full hyp anyway; engine_function is for template scaffolding
fallback path.
"""
from __future__ import annotations

import re
from pathlib import Path

HYPS_DIR = Path(__file__).parent.parent / "src" / "audit_pipeline" / "templates" / "hypotheses"
FALLBACK = "absorb_protocol_loss"

# Rust function pattern: snake_case word ending with _<verb> or _not_atomic
RUST_FN_PATTERNS = [
    re.compile(r"\b([a-z_][a-z0-9_]*_not_atomic)\b"),
    re.compile(r"\b([a-z_][a-z0-9_]*_internal)\b"),
    re.compile(r"\b([a-z_][a-z0-9_]*_with_matcher)\b"),
    re.compile(r"\b([a-z_][a-z0-9_]*_with_request_not_atomic)\b"),
    re.compile(r"\b([a-z_][a-z0-9_]*)\s*\("),
]
ANCHOR_IX_PATTERN = re.compile(r"\b(InitMarket|InitUser|InitLP|DepositCollateral|WithdrawCollateral|TradeCpi|TradeNoCpi|KeeperCrank|CloseAccount|CloseSlab|TopUpInsurance|UpdateConfig|PushHyperpMark|ResolveMarket|WithdrawInsurance|WithdrawInsuranceLimited|AdminForceCloseAccount|DepositFeeCredits|ConvertReleasedPnl|ResolvePermissionless|ForceCloseResolved|UpdateAuthority)\b")


def extract_function(relevant_instructions: str | None) -> str:
    """Extract a likely engine function name from the relevant_instructions text."""
    if not relevant_instructions or not isinstance(relevant_instructions, str):
        return FALLBACK
    text = relevant_instructions

    # FIX M4: expanded stop-word list. The greedy `<word>(` regex pattern
    # was matching control-flow keywords + generic identifiers + common
    # nouns. Result: live library has hyps with engine_function values
    # like `mode`, `path`, `return`, `update`, `logic`, `validation`,
    # `gate`, `forwarding`, `check`. These break Kani synthesis (no fn
    # named `mode` exists). Expanded skip-list catches them.
    GENERIC_STOPWORDS = {
        "line", "lines", "engine", "config", "around", "value", "result",
        "true", "false", "ok", "err", "some", "none",
        "mode", "modes", "path", "paths", "return", "returns",
        "update", "updates", "updating", "logic", "validation",
        "gate", "gates", "forwarding", "check", "checks",
        "unpack", "require_initialized", "match", "fn",
        "let", "mut", "impl", "self", "ref", "the", "and", "for",
        "see", "around", "handler", "handlers", "lines",
        "every", "field", "fields", "block", "blocks", "panicked",
        "deserialize", "serialize", "with",
        # Anchor v2 internals that aren't real entry points
        "instruction", "process_instruction", "process",
    }

    # Try snake_case function patterns first
    for pat in RUST_FN_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group(1)
            if name.lower() in GENERIC_STOPWORDS:
                continue
            return name

    # Try Anchor instruction names
    m = ANCHOR_IX_PATTERN.search(text)
    if m:
        return m.group(1)

    return FALLBACK


def process_yaml_file(path: Path) -> int:
    """Insert `engine_function: <name>` after `bug_class:` for each hyp.

    Preserves YAML formatting / comments by operating on raw lines.
    Returns count of hyps modified.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    modified = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)

        # Detect bug_class line at hyp level (4-space indent)
        if re.match(r"^    bug_class:\s", line):
            # Check if engine_function already present in this hyp block
            # by scanning forward until next "  - id:" or end of file
            already_has = False
            j = i + 1
            while j < len(lines):
                if re.match(r"^  - id:", lines[j]) or re.match(r"^\S", lines[j]):
                    break
                if re.match(r"^    engine_function:\s", lines[j]):
                    already_has = True
                    break
                j += 1

            if not already_has:
                # Scan forward for relevant_instructions to extract function name
                relevant: list[str] = []
                in_relevant = False
                j = i + 1
                while j < len(lines):
                    sub = lines[j]
                    if re.match(r"^  - id:", sub) or re.match(r"^\S", sub):
                        break
                    if re.match(r"^    relevant_instructions:\s*\|", sub):
                        in_relevant = True
                        j += 1
                        continue
                    if in_relevant:
                        if re.match(r"^    \w", sub):  # next field at 4-space indent
                            in_relevant = False
                            continue
                        relevant.append(sub)
                    j += 1

                fn_name = extract_function("".join(relevant))
                out.append(f"    engine_function: {fn_name}\n")
                modified += 1

        i += 1

    path.write_text("".join(out), encoding="utf-8")
    return modified


def main() -> None:
    yamls = sorted(HYPS_DIR.glob("percolator*.yaml")) + [HYPS_DIR / "perp_dex_class.yaml"]
    print(f"Scanning {len(yamls)} hyp files...")
    total_modified = 0
    for y in yamls:
        n = process_yaml_file(y)
        total_modified += n
        print(f"  {y.name}: {n} hyps annotated")
    print(f"\nTotal hyps annotated with engine_function: {total_modified}")


if __name__ == "__main__":
    main()
