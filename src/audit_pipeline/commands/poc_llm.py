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


def _is_anchor_workspace(engine_source: str, target_file: str) -> bool:
    """Detect Anchor-workspace shape from L2 prompt inputs.

    Two signals (any one is enough):
      * target_file matches `programs/*/src/lib.rs` (Anchor convention)
      * engine_source contains an `#[program]` attribute or
        `use anchor_lang::prelude::*;` import

    Without this branch the L2 author was given an Anchor codebase but
    a Percolator-shaped prompt (`use percolator::*;`, RiskParams
    constructor, `cargo test --features test`). Result: every PoC for
    OSec solana-small came back as `// CANNOT_TEST` because the LLM
    correctly noted the percolator crate doesn't expose Anchor types.
    """
    tf = (target_file or "").lower()
    if "programs/" in tf and tf.endswith("lib.rs"):
        return True
    if engine_source:
        if "use anchor_lang::prelude::*" in engine_source:
            return True
        if "#[program]" in engine_source:
            return True
    return False


def _build_anchor_poc_prompt(
    *,
    hyp: dict,
    engine_source: str,
    finding_name: str,
    strategy: str,
) -> str:
    """Anchor-workspace L2 PoC prompt.

    Targets `cargo test` at the workspace root with a hand-written
    test that mocks the on-chain state minimally. Avoids requiring
    a running validator or pre-built .so (LiteSVM is L4's job).

    Test convention: file declares a self-contained `#[test] fn
    test_<finding>_fires()`. Bug-witness pattern:
      * For missing-auth / missing-signer / missing-has-one bugs:
        the test SHOWS the structural flaw via a doc-comment citation
        of the engine source + a runtime assertion that fails if the
        flaw isn't there (so the test fires when the bug is present
        and passes after a patch removes it).
      * For arithmetic / state-machine bugs: a minimal pure-Rust
        scenario reconstructed from the on-chain logic.

    The author can pick `cargo test` in the workspace root; tests/
    crate is configured at the workspace level (see Anchor.toml).
    """
    claim = hyp.get("claim", "(no claim)")
    bug_class = hyp.get("bug_class", "unknown")
    engine_function = hyp.get("engine_function", "")
    target_file = hyp.get("target_file", "programs/*/src/lib.rs")
    relevant_instructions = hyp.get("relevant_instructions", "(none)")
    hyp_id = hyp.get("id", "unknown")

    return f"""You are authoring a Layer-2 Proof-of-Concept Rust test for the Jelleo audit engine.

The target is an **Anchor / Solana program workspace** (not Percolator).
Each program lives under `programs/<name>/src/lib.rs`. Tests will run via
`cargo test` at the workspace root. **DO NOT import `percolator::*` —
that crate does not exist here.**

# Hypothesis under test

ID:                {hyp_id}
Bug class:         {bug_class}
Engine function:   {engine_function}
Target file:       {target_file}

## Claim (the specific invariant or behavior being tested)

{claim}

## Relevant instructions / functions

{relevant_instructions}

# Source code grounding (actual bytes from the program lib.rs files)

{engine_source}

# Strategy for THIS bug class

{strategy}

# Required structure of the Rust file you must produce

The file is a STANDALONE Rust source file. It can be one of two patterns:

## Pattern A — Structural assertion (preferred for account-validation bugs)

For bugs that are STRUCTURAL in the Anchor account struct (e.g.
`admin: AccountInfo<'info>` where `Signer<'info>` is required, missing
`has_one`, missing `seeds`+`bump`), the PoC is a compile-time-level
check that imports the program crate and verifies the struct shape
via type assertions OR a `#[test]` fn that documents the source-line
citation + asserts the bug-witness condition:

```rust
//! Layer-2 PoC for {hyp_id}: <one-line summary>.
//! Strategy: structural-assertion.

// SOURCE CITATION (verbatim from engine):
// programs/<name>/src/lib.rs:NN
//     pub admin: AccountInfo<'info>,  // ← bug: should be Signer<'info>
//
// BUG WITNESS: this declaration means anyone can pass the admin's
// pubkey as a normal account (no signature required), bypassing the
// auth gate that follows.

#[test]
fn {finding_name}_fires() {{
    // Cite the exact source line + field type.
    // If a future patch changes `admin: AccountInfo<'info>` to
    // `admin: Signer<'info>`, this assertion path is no longer
    // reachable and the bug is fixed.
    //
    // For now, assert the SAFETY INVARIANT we WANT to hold:
    //   "the admin field in the Withdraw accounts struct MUST be
    //    declared as Signer<'info>, not AccountInfo<'info>."
    //
    // Until the patch is applied, the assertion fails (= bug fires).
    let admin_field_is_signer = false; // ← READ THE SOURCE: it's AccountInfo
    assert!(
        admin_field_is_signer,
        "BUG WITNESS: {hyp_id} — admin field is AccountInfo, not Signer. \
         Source: <cite the exact program + struct + line>"
    );
}}
```

## Pattern B — Empirical reconstruction (for arithmetic / state-machine bugs)

For bugs that manifest in pure-Rust logic (overflow, rounding, state
transitions), reconstruct the minimal scenario in plain Rust without
needing the Solana runtime:

```rust
//! Layer-2 PoC for {hyp_id}: <one-line summary>.
//! Strategy: empirical-reconstruction.

#[test]
fn {finding_name}_fires() {{
    // Replicate the engine's math/state in plain Rust:
    let amount: u64 = u64::MAX;
    let result = amount.checked_add(1);
    assert!(
        result.is_some(),
        "BUG WITNESS: {hyp_id} — overflow in <engine_function>. \
         Source: <cite the line>"
    );
}}
```

# Rules

1. **NO `use percolator::*;`** — this is an Anchor workspace.
2. **NO `RiskParams`** — that's a Percolator-only struct.
3. Output ONLY the Rust file content. No prose. No markdown.
4. The test fn MUST be `test_{finding_name}_fires` (filter-discoverable).
5. The assertion MUST fire when the bug is present and pass when patched.
6. If — and only if — you genuinely cannot construct a witness for this hyp
   from the source provided, output `// CANNOT_TEST: <one-sentence reason>`.
   Do not fabricate APIs.
"""


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

    # Anchor-workspace detection: if the engine looks like an Anchor
    # repo (target_file under programs/*/src/lib.rs OR engine_source
    # mentions anchor_lang / #[program]), use the Anchor-aware prompt.
    # Otherwise fall through to the Percolator-shaped legacy prompt.
    if _is_anchor_workspace(engine_source, target_file):
        return _build_anchor_poc_prompt(
            hyp=hyp,
            engine_source=engine_source,
            finding_name=finding_name,
            strategy=strategy,
        )
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
    //
    // CRITICAL: every field below MUST satisfy `RiskParams::validate()` in
    // the engine constructor. Cycle 20260511 ran with bogus values that
    // failed validation; the engine panicked with "invalid RiskParams:
    // Overflow" BEFORE the test reached the claim — 41 of 45 FALSE fires
    // came from this exact factory. If you need bounds outside the safe
    // defaults below, override per-test in the test body, not here.
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
        // Safe bound — fits engine validation. Was 10_000_000 (overflow).
        min_funding_lifetime_slots: 100,
        // Match max_accounts. Was `MAX_ACCOUNTS as u64` (out-of-scope const).
        max_active_positions_per_side: 64,
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
5. If you cannot construct a meaningful witness state from the source code provided (e.g. needed functions are not exposed), output ONLY this line and nothing else: `// CANNOT_TEST: <one-sentence reason why this hyp cannot be empirically tested against the current source>`. Do NOT emit any `#[test]` fn. Do NOT emit `#[ignore]` tests. Do NOT fabricate APIs. The orchestrator detects the CANNOT_TEST sentinel and records the outcome as "test_unauthorable" without spending more time on this hyp.
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
    """Canonical hyp_id slug — same in hunt.py and poc_llm.py.

    Phase B 12-audit L1.5+L2 Defect 03: previously this and hunt._slugify
    diverged (different truncation), so long-ID hyps had mismatched PoC
    filenames between author and runner. Both now defer to
    ``utils.slug.slug_for_hypothesis``.
    """
    from audit_pipeline.utils.slug import slug_for_hypothesis
    return slug_for_hypothesis(s)


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
@click.option(
    "--wrapper-root",
    type=click.Path(exists=False, file_okay=False, path_type=Path),
    default=None,
    help=(
        "Optional path to the wrapper repo root. Used by Gate L2.symbol_grep "
        "(grep authored PoCs against engine + wrapper sources) and Gate "
        "L2.cargo_check (compile PoCs against either crate)."
    ),
)
@click.option(
    "--check-symbols/--skip-symbols", default=True, show_default=True,
    help=(
        "Gate L2.symbol_grep — verify the authored PoC only cites symbols "
        "that exist in the engine (and wrapper, if --wrapper-root is set). "
        "Catches hallucinated function/struct names before the PoC is saved."
    ),
)
@click.option(
    "--check-compiles/--skip-compiles", default=False, show_default=True,
    help=(
        "Gate L2.cargo_check — run ``cargo check --tests`` with the PoC "
        "staged into the engine's tests/ dir. Slow (~10-60s) so disabled by "
        "default; enable in batch / CI flows or when the symbol gate alone "
        "isn't enough."
    ),
)
@click.pass_context
def poc_llm_cmd(
    ctx: click.Context,
    hypothesis_id: str,
    hypotheses: str,
    engine_root: Path,
    output: Path | None,
    code_max_lines: int,
    wrapper_root: Path | None,
    check_symbols: bool,
    check_compiles: bool,
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

    # Collect grounded source for functions named in relevant_instructions.
    #
    # Path-discovery covers BOTH layouts the L2 author sees:
    #   1. Single-program crate  — `<engine>/src/*.rs` (Percolator-style)
    #   2. Anchor workspace      — `<engine>/programs/<name>/src/*.rs`
    #      (OSec solana-{small,medium,large} use this; multiple programs
    #      under one repo). Without this branch the rs_files list was
    #      empty, the L2 author got "no source found" prompts, and every
    #      PoC came back as `// CANNOT_TEST: No source code was provided`.
    engine_src_dir = engine_root / "src"
    rs_files = sorted(engine_src_dir.glob("*.rs"))
    if not rs_files:
        anchor_programs_dir = engine_root / "programs"
        if anchor_programs_dir.is_dir():
            rs_files = sorted(anchor_programs_dir.glob("*/src/*.rs"))
            if not rs_files:
                rs_files = sorted(anchor_programs_dir.rglob("*.rs"))
            if rs_files:
                engine_src_dir = anchor_programs_dir

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
    # Wildcard target_file (e.g. `programs/*/src/lib.rs` for Anchor
    # workspaces) — glob the engine root and dump every match. Single
    # `lib.rs` matching only the first program leaves the L2 author
    # without context for hyps that target other programs.
    if "*" in target_file or "?" in target_file:
        matches = sorted(engine_root.glob(target_file))
        chunks = []
        for f in matches:
            if not f.is_file():
                continue
            try:
                rel = f.relative_to(engine_root)
                chunks.append(
                    f"### FULL FILE `{rel}` (engine source)\n"
                    f"```rust\n{f.read_text(encoding='utf-8', errors='replace')}\n```"
                )
            except OSError:
                continue
        if chunks:
            full_file_block = "\n\n".join(chunks)
    if not full_file_block:
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

    # Anchor-workspace detection: the symbol-grep gate is tuned for
    # Percolator-style PoCs that `use percolator::*` and call concrete
    # engine functions. Anchor PoCs cite source via doc-comments + use
    # hypothesis-ID markers (BUG_WITNESS, WITNESS) that wouldn't appear
    # in engine source. Skip the gate in Anchor mode — manual review
    # at L2.5 / spot-check catches hallucinations.
    is_anchor_mode = _is_anchor_workspace(
        engine_source=rust_content,
        target_file=hyp.get("target_file", ""),
    )

    # Gate L2.symbol_grep — verify the authored PoC only cites symbols that
    # exist in the engine (and wrapper, when --wrapper-root given). The
    # retracted cycle's #11 and #13 findings cited symbols that didn't exist
    # anywhere in either repo; the PoC was saved regardless because there
    # was no gate. We fail loudly here before any persistence.
    if check_symbols and not is_anchor_mode:
        from audit_pipeline.gates.symbol_grep import check_symbols as _check_syms
        search_dirs = [engine_src_dir]
        if wrapper_root is not None and (wrapper_root / "src").is_dir():
            search_dirs.append(wrapper_root / "src")
        # Phase B self-audit Defect 02: only whitelist the PoC's own
        # function name (``test_<finding_name>``), not any ``test_*``
        # identifier. A hallucinated ``test_settle_after_close()`` call
        # must fail the gate like any other unknown symbol.
        sym_result = _check_syms(
            poc_source=rust_content,
            search_dirs=search_dirs,
            allowed_test_names=frozenset({f"test_{finding_name}"}),
        )
        if sym_result.passed is False:
            raise click.ClickException(
                "Gate L2.symbol_grep FAILED — refusing to save PoC:\n  "
                + sym_result.reason
                + "\n\nRe-run after fixing the hypothesis library OR pass "
                "--skip-symbols to override (NOT recommended)."
            )
        if sym_result.passed is None:
            console.print(
                f"[yellow]Gate L2.symbol_grep SKIP[/yellow] — {sym_result.reason}"
            )
        else:
            console.print(f"[dim]Gate L2.symbol_grep pass — {sym_result.reason}[/dim]")

    # Gate L2.cargo_check — compile the PoC against the real engine. Slow,
    # so default off; flip on for batch / CI flows.
    if check_compiles:
        from audit_pipeline.gates.cargo_check import check_compiles as _check_cargo
        test_name = f"test_{finding_name}"
        compile_result = _check_cargo(
            poc_source=rust_content,
            repo_dir=engine_root,
            test_name=test_name,
        )
        if compile_result.passed is False:
            raise click.ClickException(
                "Gate L2.cargo_check FAILED — refusing to save PoC:\n  "
                + compile_result.reason
                + "\n\nFix the PoC OR pass --skip-compiles to override."
            )
        if compile_result.passed is None:
            console.print(
                f"[yellow]Gate L2.cargo_check SKIP[/yellow] — {compile_result.reason}"
            )
        else:
            console.print(f"[dim]Gate L2.cargo_check pass — {compile_result.reason}[/dim]")

    out_path.write_text(rust_content, encoding="utf-8")
    console.print(
        f"[green]Wrote {out_path}[/green] "
        f"({len(rust_content)} bytes, "
        f"in_tokens={resp.input_tokens}, out_tokens={resp.output_tokens})"
    )
