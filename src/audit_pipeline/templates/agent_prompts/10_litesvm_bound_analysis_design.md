# Prompt 10 — LiteSVM bound analysis design

**Use when**: a Kani CEX has been confirmed but you need to determine whether the witness state is reachable via legitimate flow at production caps.

This prompt produces a numeric derivation of the wall-clock cost, plus the LiteSVM test scaffold to print and assert on it.

---

## Prompt template

```
You are designing a numeric bound analysis for a Kani-CEX finding.

## Finding context

Kani CEX harness: {HARNESS_NAME}
Witness state (from CEX trace):
{WITNESS_STATE_DESCRIPTION}

Public-API path that reaches the witness function:
{CALL_CHAIN}

## Engine constants (fill in for the target)

- MAX_VAULT_TVL: {value}
- MAX_ACCOUNT_POSITIVE_PNL: {value}
- MAX_POSITION_ABS_Q: {value}
- max_price_move_bps_per_slot (default): {value}
- IM_bps (default): {value}
- {add other constants relevant to the finding}

## Method

Step 1: Restate the panic condition as a math inequality.
  Example: For Bug #3 in Percolator, panic fires when
  pos_pnl × g_num > 2^128 (≈ 3.4e38).

Step 2: For each variable in the panic condition, identify the engine cap:
  Example: g_num ≤ MAX_VAULT_TVL = 1e16; pos_pnl ≤ MAX_ACCOUNT_POSITIVE_PNL = 1e32.

Step 3: Solve for the variable that's HARDEST to drive to its cap:
  Example: with g_num at MAX_VAULT_TVL, need pos_pnl > 3.4e22.
  pos_pnl is engine-permitted up to 1e32 (much higher), so the threshold IS
  reachable in principle.

Step 4: Compute the per-slot rate at which the variable can grow under
legitimate flow:
  Example: pos_pnl can grow by at most notional × max_price_move_per_slot
  per slot. With notional capped by MAX_VAULT_TVL/IM_fraction = 1e17 and
  max move = 3 bps = 3e-4, per-slot gain ≤ 3e13.

Step 5: Compute slots needed:
  threshold / per-slot rate = slots
  Example: 3.4e22 / 3e13 = 1.13e9 slots.

Step 6: Convert to wall-clock:
  slots × (slot_duration_seconds) / (seconds_per_year) = years
  Example: 1.13e9 × 0.5 / (86400 × 365) ≈ 18 years.

Step 7: Categorize the finding:
  - < 1 year: ACTIVE — write LiteSVM exploit chain test
  - 1-10 years: BORDERLINE — investigate config-conditional triggers
  - 10+ years: CODE-DEFECT — write bound analysis test, downgrade in disclosure

## Output format

```
## Numeric bound analysis: {FINDING_NAME}

Panic condition: {INEQUALITY}

Variables:
  {var}: cap = {value}, source: {engine constant + line}
  ...

Reachability calculation:
  Threshold: {value}
  Per-slot growth rate: {value}
  Slots needed: {value}
  Wall-clock at {SLOT_DURATION} ms/slot: {N years}

Categorization: {ACTIVE | BORDERLINE | CODE-DEFECT}

LiteSVM test scaffold (drop into tests/<finding>_bound.rs):
{paste test code based on templates/litesvm_bound_analysis.rs.template,
 with the constants and assertion specifically derived from the analysis above}

Disclosure framing recommendation:
"Bug #N: code defect (Kani-confirmed math failure on engine-permitted state).
 LiteSVM bound analysis: required state takes ~{N years} of wall-clock to
 accumulate via legitimate flow at default caps. Disclosed as defense-in-depth."
```

Cap at 800 words. Read-only on source.
```

---

## Why this matters

Without this analysis, you publish Kani CEXes that the maintainer correctly downgrades on the call. With it, YOU do the downgrade in the disclosure itself, preserving credibility AND giving the maintainer the math.

## Common gotchas

- **Slot duration assumption**: Solana mainnet target is ~400 ms (2.5 slots/sec); use 500 ms (2 slots/sec) for conservative bounds
- **Per-slot rate must be CONSERVATIVE upper bound on attacker capability**: assume max position, max price move, max funding rate, etc.
- **Don't forget compound effects**: some state grows multiplicatively (e.g., funding rate accrual), not additively per slot. Adjust the formula.

## Output use

The analysis output goes into TWO places:
1. The LiteSVM bound test (Layer 4 deliverable)
2. The disclosure document's per-bug "Reachability bound analysis" section

Both should cite the same numbers, derived from the same engine constants. Inconsistency between the two is a credibility hit.
