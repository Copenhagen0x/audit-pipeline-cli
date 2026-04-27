# Prompt 06 — Panic site enumeration

**Use when**: doing a comprehensive sweep for `.expect()`, `.unwrap()`, `assert!`, `panic!()`, and similar panic-class calls in the engine.

---

## Prompt template

```
You are enumerating every site in the engine that can panic. The goal is to
catalog the full panic surface, then identify which sites are reachable
from public API.

## Files to read

- {ENGINE_PATH}/src/ (all .rs files)

## Method

1. Grep for panic-class patterns:
   - `.expect(`
   - `.unwrap()` (no `_or_*`)
   - `assert!(`, `assert_eq!(`, `assert_ne!(`
   - `panic!(`
   - `unreachable!()`
   - `todo!()`, `unimplemented!()`
   - `[index]` where index is symbolic

2. For EACH match, capture:
   - Exact file:line
   - The panic message (if any)
   - The function the panic is in
   - Whether the panic message gives a hint about the assumed precondition

3. Categorize:
   - INTENDED (e.g., assert! verifying an internal invariant; should never fire)
   - DEFENSIVE (e.g., expect() on something that "should always be Some")
   - PRODUCTION-DANGEROUS (panic on attacker-controlled input)

## Output format

A markdown table:

| # | line | function | pattern | message | category | reachable_via_public_api |
|---|---|---|---|---|---|---|
| 1 | 1599 | mul_div_floor_u128 | .expect | "a*b overflow" | PRODUCTION-DANGEROUS | yes |
| 2 | 4677 | advance_profit_warmup | assert! | None | INTENDED | yes (via crank) |
| ... | | | | | | |

Then summary:
- Total panic sites: N
- PRODUCTION-DANGEROUS sites: M
- Of those M, sites where the panic message reveals an attacker-relevant
  assumption: K

Top 5 sites worth Layer-2 PoC attempts.

Cap at 700 words. Read-only.
```

---

## Why this catches things

Many real-world bugs are panics that fire when an attacker controls the input. By enumerating EVERY panic site (not just "the obvious ones"), the agent surfaces the surface area for this bug class.

In the Percolator audit, this prompt was the fast way to find every `mul_div_floor_u128: a*b overflow` site, which led to Bugs #2, #3, and Sibling B.

## Common panic categories in Solana programs

| Category | Example | Concern |
|---|---|---|
| Arithmetic overflow | `.checked_mul().expect("overflow")` | Attacker-controlled magnitudes |
| Division by zero | `assert!(d > 0)` then `/d` | Attacker-controlled d |
| Index out of bounds | `array[idx]` | Attacker-controlled idx |
| Option unwrap | `.expect("must be Some")` | Attacker can make it None |
| Result unwrap | `.expect("must succeed")` | Attacker can make it Err |
| Slice conversion | `.try_into().unwrap()` | Attacker controls slice length |

## Customization

Adapt the panic-class patterns to the codebase's conventions:
- Some codebases use custom `crash!()` or `bail_panic!()` macros
- Some use `match` arms with `_ => panic!(...)`
- Some prefer `Result` returns; in that case, panic surface is smaller

If a codebase uses a custom error model (e.g., all errors via `Result<T, ProgramError>`), the panic surface is much smaller and this prompt yields fewer findings. That's a good signal — fewer panics = more robust code.
