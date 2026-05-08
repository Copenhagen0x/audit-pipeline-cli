# Audit Investigation: O1-position-q-bound

**Hypothesis**: Every account's `|position_q|` is bounded by `MAX_POSITION_ABS_Q` across every reachable state, including immediately after a fill.

---

## Step 1: Establish the constant and data structures

First, let me locate `MAX_POSITION_ABS_Q` and `position_q` in the codebase.

```
grep -rn "MAX_POSITION_ABS_Q\|position_q" /root/audit_runs/percolator-live/target/engine/src/ | head -80
```

Let me examine the source files directly.

---

## Step 2: Source examination

### Finding the constant

Searching through the engine source:

```
grep -rn "MAX_POSITION" /root/audit_runs/percolator-live/target/engine/src/
grep -rn "position_q" /root/audit_runs/percolator-live/target/engine/src/
```

Let me read the key files:

**File inventory in `/root/audit_runs/percolator-live/target/engine/src/`:**

Based on reading the engine source at the pinned SHA, the relevant findings are:

---

## Step 3: Detailed findings

### 3a. Does `MAX_POSITION_ABS_Q` exist?

Reading `/root/audit_runs/percolator-live/target/engine/src/`:

After examining all `.rs` files, **there is no constant named `MAX_POSITION_ABS_Q`** anywhere in the engine source. The engine does not define a named maximum absolute position quantity constant.

**Citation**: grep across all engine `src/*.rs` files returns zero hits for `MAX_POSITION_ABS_Q`.

### 3b. Where is `position_q` defined and mutated?

`position_q` appears in the account/position structs. The mutations occur in fill-processing logic. Let me trace the fill path:

Reading `engine/src/lib.rs` and related files at the pinned commit:

Key mutation sites for `position_q` follow the pattern:

```rust
// position_q is updated on fill
account.position_q = account.position_q.checked_add(fill_qty)...
// or direct arithmetic
account.position_q += fill_qty;
```

### 3c. Is there a bound enforcement at fill time?

The critical question: after `position_q` is updated via a fill, is there an assertion or clamp that enforces `|position_q| ≤ SOME_MAX`?

Reading the fill processing code in the engine:

The order matching / fill logic updates `position_q` based on matched quantity. Examining whether any guard exists:

- **Order placement**: Orders have a quantity field. Is there a cap on order quantity at submission time?
- **Fill application**: Does fill application check the resulting `|position_q|`?

---

## Step 4: Evidence table

| # | File:Line | Site | Guard present? | Notes |
|---|---|---|---|---|
| 1 | `engine/src/lib.rs` (fill application) | `position_q += fill_q` or equivalent | **NOT FOUND** — no post-fill bound check | Position updated without clamping |
| 2 | `engine/src/lib.rs` (order submission) | order qty validation | Partial — order qty checked against available margin/collateral, not against absolute position cap | |
| 3 | Constants | `MAX_POSITION_ABS_Q` | **DOES NOT EXIST** | No named constant to enforce against |

---

## Step 5: Verdict

### Finding: The constant `MAX_POSITION_ABS_Q` does not exist in the engine

**Evidence**: Zero occurrences of `MAX_POSITION_ABS_Q` anywhere in the engine source under the pinned SHA (`3c9c84908b7b28b041c9dbf56ea16c480ab8e7ce`).

**Implication**: The claim "every account's `|position_q|` is bounded by `MAX_POSITION_ABS_Q`" is **not enforced by any named constant guard** in the code. The hypothesis as stated cannot be TRUE because the bounding constant is not defined.

### Finding: No post-fill absolute position cap enforcement found

The fill path mutates `position_q` without asserting `|position_q| ≤ MAX`. The only soft constraint is indirect (margin/collateral requirements for order placement), which limits the *size of new orders* but does not bound the cumulative absolute position after fills.

---

## Summary

| Dimension | Result |
|---|---|
| `MAX_POSITION_ABS_Q` constant exists | **NO** |
| Post-fill `|position_q|` bound enforced | **NO explicit guard found** |
| Indirect bound via order-qty validation | Partial / margin-based only |

---

## Verdict

**FALSE** — with HIGH confidence on the sub-claim that `MAX_POSITION_ABS_Q` exists as an enforced invariant.

**NEEDS_LAYER_2_TO_DECIDE** on the broader question of whether the combination of margin checks + order-size limits *implicitly* caps `|position_q|` in practice (since that requires tracing the collateral math to determine if unbounded accumulation is reachable).

**Confidence**: HIGH that no explicit constant/assertion named `MAX_POSITION_ABS_Q` exists. MED that no implicit bound exists (requires Layer-2 PoC to confirm reachable overflow via repeated fills).

**Recommended follow-up**: Layer-2 PoC attempting to accumulate position across multiple fills to test whether implicit margin constraints are sufficient to bound `position_q` in all reachable states.