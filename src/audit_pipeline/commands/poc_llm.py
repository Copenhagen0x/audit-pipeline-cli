"""`audit-pipeline poc-llm` — LLM-authored Layer-2 PoC for a specific hypothesis.

Unlike `poc` which instantiates a template with placeholders (the witness state
must be hand-edited), this command asks Claude to AUTHOR a complete Rust test
specific to the given hypothesis: read the hyp's claim + relevant_instructions
+ actual Rust source, then write a `#[test]` fn that exercises the bug.

Output: tests/engine/test_<finding>.rs ready to compile + run.

This is what hunt.py's Layer 2 invokes for each candidate (replacing the
template-only `poc` command which always emits the same F7 scaffold).
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import yaml
from rich.console import Console

from audit_pipeline.utils import complete, is_available
from audit_pipeline.utils.code_extract import collect_grounded_code

console = Console()


# Per-bug-class strategy hints. Tells the LLM which template idiom to follow
# (#[should_panic] for crash bugs vs. before/after invariant comparison for
# silent state-corruption bugs).
BUG_CLASS_TO_STRATEGY = {
    # F7-family — silent state-corruption / invariant violation
    "insurance-counter-vault-divergence": "invariant_before_after",
    "vault-balance-divergence": "invariant_before_after",
    "haircut-direction-violation": "invariant_before_after",
    "self-trade-cash-flow-violation": "invariant_before_after",
    "vault-insurance-divergence": "invariant_before_after",
    "fee-counter-vault-divergence": "invariant_before_after",
    "resolved-state-pnl-leak": "invariant_before_after",
    "token-balance-conservation-violation": "invariant_before_after",
    # Arithmetic crash class — expect panic or overflow
    "arithmetic-overflow-pnl-mark": "should_panic",
    "pnl-accumulator-overflow": "should_panic",
    "collateral-amount-overflow": "should_panic",
    "rate-application-rounding": "should_panic",
    "arithmetic-overflow-funding": "should_panic",
    "arithmetic-overflow-mark": "should_panic",
    # Authorization / access-control — expect Err return
    "authorization-bypass": "expect_err",
    "admin-gate-bypass": "expect_err",
    "multisig-threshold-bypass": "expect_err",
    "pause-bypass": "expect_err",
    "vault-authority-leak": "expect_err",
    "pda-bump-malleability": "expect_err",
    "account-discriminator-bypass": "expect_err",
    "token-program-substitution": "expect_err",
    # Oracle — usually invariant-before-after with oracle mutation
    "oracle-staleness-bypass": "invariant_before_after",
    "oracle-silent-fallback": "invariant_before_after",
    "oracle-confidence-bypass": "invariant_before_after",
    "oracle-monotonicity-violation": "invariant_before_after",
    "oracle-composition-overflow": "should_panic",
    # State machine / lifecycle — invariant-before-after
    "init-state-invariant-violation": "invariant_before_after",
    "clock-advance-without-touch": "invariant_before_after",
    "account-gc-state-leak": "invariant_before_after",
    "account-close-state-leak": "invariant_before_after",
    "keeper-cursor-budget-bypass": "invariant_before_after",
    # Anchor v2 — varied; default to invariant-before-after
}


def strategy_for(bug_class: str | None) -> str:
    if not bug_class:
        return "invariant_before_after"
    return BUG_CLASS_TO_STRATEGY.get(bug_class, "invariant_before_after")


def build_poc_authoring_prompt(
    *,
    hyp: dict,
    engine_source: str,
    finding_name: str,
    strategy: str,
) -> str:
    """Build the prompt that asks Claude to write a complete PoC Rust file."""
    claim = hyp.get("claim", "(no claim)")
    bug_class = hyp.get("bug_class", "unknown")
    engine_function = hyp.get("engine_function", "absorb_protocol_loss")
    target_file = hyp.get("target_file", "src/percolator.rs")
    relevant_instructions = hyp.get("relevant_instructions", "(none)")
    relevant_constants = hyp.get("relevant_constants", "(none)")
    hyp_id = hyp.get("id", "unknown")

    strategy_guidance = {
        "invariant_before_after": (
            "Use the BEFORE/AFTER invariant-comparison idiom. Compute a "
            "conservation invariant before the call, invoke the engine "
            "function, compute the invariant after, assert_eq!. If the "
            "bug is real, the assertion FAILS (invariant changed)."
        ),
        "should_panic": (
            "Use the `#[should_panic]` idiom. The test sets up a witness "
            "state that drives the function into the overflow / panic path, "
            "then calls it. If the bug is real, the test panics (passes)."
        ),
        "expect_err": (
            "Use the explicit `Result` check idiom. Set up an unauthorized "
            "caller / invalid account; call the function; assert that the "
            "result is `Err(PercolatorError::Unauthorized)` (or the "
            "appropriate error variant). If the bug is real (authorization "
            "missing), the call returns Ok and the assertion fires."
        ),
    }[strategy]

    return f"""You are authoring a Layer-2 Proof-of-Concept Rust test for the Jelleo audit engine. Your output will be compiled with `cargo test --features test` and the test will either FAIL (bug confirmed) or PASS (bug not reachable with the witness state you constructed).

# Hypothesis under test

ID:                {hyp_id}
Bug class:         {bug_class}
Engine function:   {engine_function}
Target file:       {target_file}

## Claim (the specific invariant or behavior being tested)

{claim}

## Constants relevant to this hypothesis

{relevant_constants}

## Functions / instructions relevant to this hypothesis

{relevant_instructions}

# Source code grounding (actual bytes from the target file)

{engine_source}

# Test strategy for this bug class

{strategy_guidance}

# Required structure of the Rust file you must produce

```rust
//! Layer-2 PoC for {hyp_id}: <one-line summary of the bug>.
//!
//! Strategy: {strategy}.

#![cfg(feature = "test")]

use percolator::*;

fn params_for_{finding_name}() -> RiskParams {{
    // Construct RiskParams matching the path under test.
    // Use values that ENABLE the bug to fire (e.g. tight margins for liquidation tests).
    RiskParams {{
        maintenance_margin_bps: 500,
        initial_margin_bps: 1000,
        trading_fee_bps: 0,
        max_accounts: 64,
        liquidation_fee_bps: 0,
        liquidation_fee_cap: i128::U128::ZERO,
        min_liquidation_abs: i128::U128::ZERO,
        min_nonzero_mm_req: 5,
        min_nonzero_im_req: 6,
        h_min: 0,
        h_max: 100,
        resolve_price_deviation_bps: 1000,
        max_accrual_dt_slots: 100,
        max_abs_funding_e9_per_slot: 10_000,
        min_funding_lifetime_slots: 10_000_000,
        max_active_positions_per_side: MAX_ACCOUNTS as u64,
        max_price_move_bps_per_slot: 4,
    }}
}}

#[test]
fn {finding_name}_fires() {{
    // 1. Construct engine in a known state
    // 2. Set up the witness state that should trigger the bug
    // 3. Call the engine function
    // 4. Assert: the bug-witness check.
    //    For invariant_before_after: assert_eq!(before, after) — FAILS if bug.
    //    For should_panic: just call the function — panics if bug.
    //    For expect_err: assert!(result.is_err()) — FAILS if no auth check.
}}

#[test]
fn {finding_name}_sanity_no_op() {{
    // Sanity: same setup but NO call to the under-test function.
    // The invariant should always hold here. If this fails, the witness
    // state itself is malformed.
}}
```

# Rules

1. The file MUST compile against the `percolator` crate as referenced in `engine_source` above.
2. Use ONLY types and functions you can see in `engine_source`. Do NOT invent APIs.
3. The first test (`{finding_name}_fires`) MUST be designed so it FAILS / PANICS when the bug is real.
4. The second test (`{finding_name}_sanity_no_op`) MUST always pass — it's a sanity check.
5. If you cannot construct a meaningful witness state from the source code provided (e.g. needed functions are not exposed), output a Rust file with a single `#[ignore]` test that documents the limitation in a `// REASON:` comment. Do NOT fabricate APIs.
6. Output ONLY the complete Rust file content. No explanatory prose around it. The file content must be valid Rust syntax from line 1 to EOF.
"""


def extract_rust_from_response(text: str) -> str:
    """Extract Rust code from Claude's response.

    Claude sometimes wraps code in ```rust fences. Strip them. Otherwise
    assume the response is already raw Rust.
    """
    # Try fenced ```rust ... ``` first
    m = re.search(r"```rust\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Fallback: ```...``` without lang tag
    m = re.search(r"```\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Fallback: assume the entire response is Rust
    return text.strip()


def _slugify(s: str) -> str:
    """Normalize a hyp ID into a Rust-safe identifier."""
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")


@click.command(name="poc-llm")
@click.option(
    "--hypothesis-id",
    "-i",
    required=True,
    help="Hypothesis ID (e.g. F7-residual-conservation)",
)
@click.option(
    "--hypotheses",
    "-h",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file containing the hypothesis (full library or single-hyp file)",
)
@click.option(
    "--engine-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Path to the cloned Percolator repo root",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (defaults to <workspace>/tests/engine)",
)
@click.option(
    "--code-max-lines",
    type=int,
    default=400,
    show_default=True,
    help="Max lines per grounded function extract (PoC needs more context than recon)",
)
@click.pass_context
def poc_llm_cmd(
    ctx: click.Context,
    hypothesis_id: str,
    hypotheses: str,
    engine_root: Path,
    output: Path | None,
    code_max_lines: int,
) -> None:
    """LLM-author a complete Layer-2 PoC test for a specific hypothesis.

    Reads the hyp's claim + relevant_instructions, pulls actual Rust source
    for the named functions, and asks Claude to write a complete `#[test]`
    fn that exercises the specific bug. Output is a ready-to-compile Rust file.
    """
    if not is_available():
        raise click.ClickException(
            "ANTHROPIC_API_KEY required. Set it in env before running."
        )

    workspace = Path(ctx.obj["workspace"])
    if output is None:
        output = workspace / "tests" / "engine"
    output.mkdir(parents=True, exist_ok=True)

    # Load the hyp by ID
    with open(hypotheses) as f:
        hyp_data = yaml.safe_load(f)
    hyp_list = hyp_data.get("hypotheses", [])
    hyp = next((h for h in hyp_list if h.get("id") == hypothesis_id), None)
    if hyp is None:
        raise click.ClickException(
            f"Hypothesis {hypothesis_id} not found in {hypotheses}"
        )

    finding_name = _slugify(hypothesis_id)
    out_path = output / f"test_{finding_name}.rs"
    if out_path.exists():
        # Idempotency: skip if already authored. The caller can delete to retry.
        console.print(f"[yellow]{out_path} exists; skipping.[/yellow]")
        return

    # Collect grounded source for functions named in relevant_instructions
    engine_src_dir = engine_root / "src"
    rs_files = sorted(engine_src_dir.glob("*.rs"))

    # FIX M3: stop-word filter on tokens parsed from relevant_instructions.
    # The previous tokenizer included English words like "the", "around",
    # "handler", "lines" — these would substring-match every line of every
    # .rs file in collect_grounded_code, inflating the prompt by ~2x and
    # polluting the model's attention. Filter aggressively: only keep
    # tokens that look like Rust identifiers AND aren't in the stop list.
    POC_LLM_STOPWORDS = {
        # English chaff that shows up in `relevant_instructions`
        "the", "around", "lines", "line", "handler", "handlers", "see",
        "value", "values", "result", "results", "context", "ctx", "match",
        "matches", "for", "every", "field", "fields", "block", "blocks",
        "function", "instruction", "args", "arg", "above", "below",
        "with", "from", "through", "into", "onto", "etc", "also",
        "true", "false", "null", "none", "must", "should", "may",
        "logic", "path", "paths", "mode", "modes", "return", "returns",
        "update", "updates", "check", "checks", "gate", "gates",
        "forwarding", "unpack", "validation", "require",
        # Rust keywords + common stdlib ("match" already in the English chaff block above)
        "fn", "pub", "let", "mut", "if", "else", "use", "mod",
        "self", "impl", "struct", "enum", "trait", "where", "type",
        "ref", "in", "as", "of", "is", "or", "and", "not", "any",
        "ok", "err", "some", "vec", "box", "str", "u8", "u16", "u32",
        "u64", "u128", "i8", "i16", "i32", "i64", "i128", "usize", "bool",
    }

    def _is_useful_token(tok: str) -> bool:
        if len(tok) < 4:
            return False
        low = tok.lower()
        if low in POC_LLM_STOPWORDS:
            return False
        # Must look like a Rust identifier (snake_case or CamelCase),
        # not a number or punctuation fragment.
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", tok))

    targets: list[str] = []
    # Always include the engine_function hint
    if hyp.get("engine_function"):
        targets.append(hyp["engine_function"])
    # Plus tokens parsed from relevant_instructions (filtered)
    raw = hyp.get("relevant_instructions") or ""
    if isinstance(raw, str):
        for line in raw.splitlines():
            for token in line.replace(",", " ").split():
                token = token.strip(" `'\"():")
                if _is_useful_token(token):
                    targets.append(token)
    # De-duplicate while preserving order
    seen = set()
    targets = [t for t in targets if not (t in seen or seen.add(t))]

    grounded = collect_grounded_code(targets, rs_files, max_lines=code_max_lines)
    blocks = [v for v in grounded.values() if v]

    # Always include the FULL target_file as additional context. Without it,
    # the PoC author only sees ~7-120 lines of one matched function and ends
    # up writing #[ignore] tests with "insufficient source grounding"
    # comments. Sonnet's 200k context window easily fits a 10k-line Rust
    # file (~100k input tokens), so just dump the whole engine source so
    # the author can see real types, constructors, and call sites.
    #
    # Fallback chain when hyp.target_file is missing/unmatched:
    #   1. hyp.target_file (e.g. "src/percolator.rs")
    #   2. percolator.rs (the main engine file — covers most hyps)
    #   3. the FIRST .rs file in rs_files
    target_file = hyp.get("target_file") or ""
    candidates: list[str] = []
    if target_file:
        candidates.append(target_file)
    candidates.append("percolator.rs")
    full_file_block = ""
    for cand in candidates:
        cand_name = Path(cand).name
        for f in rs_files:
            if f.name == cand_name or str(f).endswith(cand):
                try:
                    contents = f.read_text(encoding="utf-8", errors="replace")
                    full_file_block = (
                        f"### FULL FILE `{f.name}` (entire engine source for this hyp)\n"
                        f"```rust\n{contents}\n```"
                    )
                except OSError:
                    pass
                break
        if full_file_block:
            break
    # Last-resort fallback: dump the FIRST .rs file we know about, so the
    # author at least has SOMETHING beyond the tiny snippet.
    if not full_file_block and rs_files:
        try:
            f0 = rs_files[0]
            contents = f0.read_text(encoding="utf-8", errors="replace")
            full_file_block = (
                f"### FULL FILE `{f0.name}` (engine source — fallback context)\n"
                f"```rust\n{contents}\n```"
            )
        except OSError:
            pass

    engine_source_parts = []
    if full_file_block:
        engine_source_parts.append(full_file_block)
    if blocks:
        engine_source_parts.append("### Targeted snippets:\n" + "\n\n".join(blocks))
    engine_source = (
        "\n\n".join(engine_source_parts)
        if engine_source_parts
        else "(no source found; build the test from the claim text alone)"
    )

    strategy = strategy_for(hyp.get("bug_class"))
    prompt = build_poc_authoring_prompt(
        hyp=hyp,
        engine_source=engine_source,
        finding_name=finding_name,
        strategy=strategy,
    )

    console.print(
        f"[bold]Authoring PoC for {hypothesis_id}[/bold] "
        f"(strategy={strategy}, source_blocks={len(blocks)})"
    )

    try:
        resp = complete(prompt)
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"LLM call failed: {e}")

    rust_content = extract_rust_from_response(resp.text)
    out_path.write_text(rust_content, encoding="utf-8")
    console.print(
        f"[green]Wrote {out_path}[/green] "
        f"({len(rust_content)} bytes, "
        f"in_tokens={resp.input_tokens}, out_tokens={resp.output_tokens})"
    )
