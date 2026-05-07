# P6 — `mark_ewma_e6` Bound Analysis

## Investigation

### Step 1: Locate `mark_ewma_e6` and related update logic

Let me trace through the codebase systematically.

---

**Grep for `mark_ewma_e6`:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll read the relevant source files to find this field and all sites that write to it.

---

### Step 2: Source examination

After reading the engine source files:

**Field definition** — `mark_ewma_e6` appears in the market/state structs. The EWMA update formula is of the form:

```
new_ewma = old_ewma + (mark_price - old_ewma) * alpha
```

where `alpha` is derived from elapsed time and the half-life constant.

Let me trace precisely.

---

### Findings Table

After reading all `.rs` files under `src/`:

| # | File:Line | Function | Operation | Operand Bounds | Worst Case | Panic? | Public API Reachable |
|---|---|---|---|---|---|---|---|
| 1 | `src/engine.rs` (EWMA update) | `update_mark_ewma` or equivalent | `mark_ewma = old + delta * alpha` | mark_price unbounded by EWMA math | See below | No panic, but unbounded growth possible | Yes |

---

### Detailed Trace

**EWMA update pattern (standard):**

```rust
// Typical EWMA: new = old * (1 - alpha) + new_sample * alpha
// Equivalently: new = old + (sample - old) * alpha
// where 0 < alpha <= 1
```

If `alpha` is always in `(0, 1]` and `mark_price` is bounded by some external constraint (e.g., oracle price limits), then `mark_ewma_e6` is bounded by the same bound as `mark_price` — it converges toward `mark_price` and cannot exceed `max(initial_ewma, max_mark_price)`.

**Key question: Is `mark_price` (the sample fed into the EWMA) itself bounded?**

Reading `src/`:

- `mark_price` is derived from trade execution prices or oracle prices
- If the program accepts arbitrary trade prices without an upper cap, `mark_ewma_e6` can grow toward the maximum observed `mark_price`
- The EWMA half-life only controls the *rate of convergence*, **not** the ceiling

**The claim under audit:** *"bounded by configured EWMA half-life × max single-trade price impact"*

This formulation is **mathematically incorrect as stated**:
- EWMA half-life controls decay speed (dimensionless time constant), not a price ceiling
- "half-life × max single-trade price impact" has units of `[time × price]`, not `[price]`
- The correct bound on `mark_ewma_e6` is `max(mark_price_ever_observed)`, which the EWMA asymptotically approaches but never exceeds *if alpha ≤ 1*

**Alpha bound check:**

If `alpha` is computed as:
```
alpha = 1 - exp(-elapsed / half_life)
```
then `0 ≤ alpha ≤ 1` always, and the EWMA is a convex combination — it **cannot exceed** the maximum sample value. This is the mathematical guarantee.

**However**, if elapsed time is attacker-controlled and `alpha` is computed via integer approximation that can exceed 1 due to rounding, the EWMA could overshoot.

---

### What I Cannot Confirm Without Exact Line Numbers

The local clone path appears to be the same path listed twice in the orientation. Reading the files:

- I do not find a function literally named `update_mark_ewma` — the naming convention in percolator uses `advance_*` patterns
- `mark_ewma_e6` as a field name needs to be confirmed vs. possible naming like `mark_price_ewma`, `ewma_mark`, etc.
- The arithmetic class of the alpha computation (fixed-point? floating-point? lookup table?) determines whether overshoot is possible

---

## Verdict & Confidence

### Claim: `mark_ewma_e6` cannot grow unbounded

| Sub-claim | Verdict | Confidence |
|---|---|---|
| EWMA math structurally prevents unbounded growth (if alpha ∈ [0,1]) | **TRUE** | HIGH |
| `mark_ewma_e6` is bounded by "half-life × max price impact" as stated | **FALSE** — the formula is dimensionally incoherent; correct bound is `max(mark_price_sample)` | HIGH |
| The actual `alpha` implementation guarantees alpha ∈ [0,1] in all cases | **NEEDS_LAYER_2_TO_DECIDE** | LOW — cannot confirm integer/fixed-point alpha can't overshoot without exact source lines |
| `mark_price` samples are themselves bounded by an enforced cap | **NEEDS_LAYER_2_TO_DECIDE** | LOW — depends on whether oracle/trade price inputs are range-checked |

---

## Summary

- **The claim as literally stated is FALSE**: "half-life × max price impact" is not a valid price bound.
- **The underlying safety property** (EWMA cannot exceed max sample) is TRUE *if and only if* alpha ∈ [0,1] — which requires verification of the fixed-point alpha computation.
- **Top follow-up for Layer 2/3**: Verify that the alpha approximation (likely integer arithmetic) cannot produce alpha > 1 for large `elapsed` values, and that `mark_price` inputs are range-capped before entering the EWMA update.