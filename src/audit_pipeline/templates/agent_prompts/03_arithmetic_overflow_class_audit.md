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

- {ENGINE_PATH}/src/ (all .rs files)
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
