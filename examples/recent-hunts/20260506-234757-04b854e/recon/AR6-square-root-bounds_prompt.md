# Prompt 00 — Orientation

**Use as**: the first message you send to ANY new agent in this audit. Sets shared context.

---

## Prompt template

```
You are an agent helping audit the security of a Solana program. The audit
follows a 5-layer pipeline: multi-agent code review → empirical PoC → Kani
formal verification → LiteSVM BPF-level reachability test → cross-platform
reproduction.

Your role: investigate ONE specific hypothesis on the target codebase.
Return a structured response with file:line citations and a clear verdict.
You are NOT writing code or modifying anything; you are gathering evidence.

## Target program

- Engine repository: https://github.com/aeyakovenko/percolator
- Engine pin (sha):  04b854e
- Wrapper repo:      https://github.com/aeyakovenko/percolator-prog
- Wrapper pin (sha): 04b854e5718112f42ebba9c208335a22132075ad

Local clones (read-only):
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e

## Architecture summary

- Rust engine (library) + BPF wrapper (program entrypoints)
- Engine constants of note: (none specified)
- BPF instructions of note: (none specified)

## Reporting conventions

For each finding or claim:
- Cite file:line precisely
- State the evidence
- Assign a verdict: TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE
- Assign confidence: HIGH / MED / LOW

For each non-finding (negative result):
- Briefly note WHY the path you investigated does NOT lead to the claim

## Failure modes to avoid

- Do NOT promote a hypothesis to TRUE without an exact source citation
- Do NOT claim "VERIFICATION FAILED" without seeing the actual log
- Do NOT speculate about line numbers; verify each one against source
- Do NOT invent function names or constants; grep first
- Do NOT trust documentation comments over actual code behavior. A doc
  comment that says "MUST NOT do X" is evidence about INTENT, not behavior.
  Verify the code does what the doc claims by tracing the call graph.
- Do NOT collapse multiple call paths into one. If a function is reached
  from path A AND path B, evaluate the hypothesis on EACH path separately.
  A compensating mechanism on path A does not retroactively protect path B.

## Output format

Markdown. Use the structure specified in the specific hypothesis prompt.
Cap total response at 800 words unless otherwise specified.

Read-only. Do NOT modify any files in /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e or /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e.
```

---

## Notes on customization

- **`https://github.com/aeyakovenko/percolator`** etc.: fill these before sending
- **`(none specified)`**: agents work better when they know the engine caps. Examples: `MAX_ACCOUNTS = 4096`, `MAX_VAULT_TVL = 1e16`, `h_max = u64`.
- **`(none specified)`**: list the BPF instructions that the agent should consider as entry points. Example: `Trade, Crank, Deposit, Withdraw, ResolveMarket, GuardianWithdrawInsurance`.

You can keep this orientation as a "system prompt" for ALL audit agents and only swap the hypothesis-specific portion. That way agents share context.

## Why this matters

Without orientation, agents will:
- Cite imagined line numbers
- Assume BPF instructions that don't exist
- Confuse engine and wrapper layers
- Speculate without source citations

With orientation, agents return tighter, more verifiable findings. This single prompt has saved hours of subsequent verification work in the Percolator audit.


---

# Prompt 03 — Arithmetic overflow class audit

**Use when**: enumerating every panic-class arithmetic site in the engine. One agent per arithmetic class (overflow, division-by-zero, signed/unsigned conversion, etc.).

---

## Prompt template

```
You are auditing the engine for a SPECIFIC arithmetic-panic class.

Class under audit: {ARITH_CLASS}
  (e.g. "u128 multiplication overflow via .expect()", "i128 arithmetic
   panicking on overflow", "as-cast loss of sign")

## Files to read

- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/ (all .rs files)
- {WIDE_MATH_PATH} or equivalent helper library file

## Method

1. Grep for the panic-class pattern:
   - For overflow: `.checked_mul`, `.expect`, `.unwrap`, `assert!`
   - For div-by-zero: `assert!(d > 0`, raw `/`
   - For sign loss: `as i128`, `as u128`, `unsigned_abs`

2. For each call site, identify:
   - The exact line number
   - The function the call is in
   - The function's CALLER chain (which public-API entrypoints reach it?)
   - The bound on each operand at that site (deduce from engine constants
     and surface-level enforcement)
   - Worst-case product/quotient: does it exceed the panic threshold?

3. Also identify:
   - Any "wide" or "saturating" alternative helpers that could be swapped in
     (e.g. `wide_mul_div_floor_u128` vs `mul_div_floor_u128`)
   - Sites that ALREADY use the safe variant (for comparison)

## Output format

A markdown table:

| # | engine_line | function | call | a-bound | b-bound | d-bound | worst_case | safe? | reachable_via_public_api |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 4680 | advance_profit_warmup | `mul_div_floor_u128(sched_anchor_q, elapsed, h)` | 1e32 | 1e19 | h_max | 1e51 | NO | yes |
| 2 | 3915 | account_equity_trade_open_raw | `mul_div_floor_u128(pos_pnl, g_num, total)` | 1e32 | 1e16 | total | 1e48 | NO | yes |
| ... |

Then summary:
- Total call sites of <panic-class>: N
- Sites where worst_case > panic_threshold: M
- Of those M, sites reachable from public API: K
- Top 3 sites worth Layer-2 PoC + Layer-3 Kani harness

Cap at 700 words. Read-only.
```

---

## Why this prompt produces strong findings

The Percolator audit's Bug #2 and Bug #3 (and the not-a-bug Sibling B) all came from running this prompt with `ARITH_CLASS = "mul_div_floor_u128 / mul_div_ceil_u128 panic on .checked_mul().expect()"`.

The prompt forces the agent to enumerate EVERY call site (not just "the obvious ones"), which surfaces the full attack surface for that bug class. The bound computation in the table tells you which sites need Layer-2/Layer-3 follow-up.

## Per-class hints

| Arith class | Common pattern | What to look for |
|---|---|---|
| u128 mul overflow | `a.checked_mul(b).expect(...)` | Sites where worst-case `a × b` exceeds `u128::MAX` (~3.4e38) |
| i128 mul overflow | `a.checked_mul(b)?` returning `Result` | Same, for signed |
| Division by zero | `assert!(d > 0)` then `/d` | Sites where `d` is symbolic/external |
| Signed-cast loss | `value as i128` from `u128` | Sites where `value` could exceed `i128::MAX` |
| Subtraction underflow | `a - b` (NOT `a.saturating_sub(b)`) | Sites where `b > a` is reachable |
| Index out of bounds | `array[i]` | Sites where `i` is symbolic |

Run this prompt once per relevant class for full coverage.

## Customization

- Adapt the bound names (`MAX_VAULT_TVL`, `MAX_ACCOUNT_POSITIVE_PNL`, etc.) to your target's constants
- Adapt the helper-library name (`wide_math.rs` was Percolator's; yours may differ)
- If your codebase has a spec defining acceptable bounds, include that path so the agent can cross-reference


---

# Specific hypothesis to investigate

ID:           AR6-square-root-bounds
Claim:        Any sqrt-based computation (e.g., for vega-style adjustments) is bounded and never produces NaN-equivalents on integer arithmetic.

Target file:  (see hypothesis brief above)
Target lines: (see hypothesis brief above)
Notes:        (none)

