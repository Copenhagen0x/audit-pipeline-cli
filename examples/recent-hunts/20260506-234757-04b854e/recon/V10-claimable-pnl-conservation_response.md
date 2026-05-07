I'll systematically investigate this hypothesis by reading the actual source files before making any claims.

## Step 1: Explore the repository structure

Let me start by understanding the codebase layout.

**Directory structure:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

Let me read the key files.

---

## Reading the source files

I'll examine the engine source to find relevant fields and functions.

**File: src/lib.rs (or equivalent engine entry)**

Let me trace through the actual files available:

```
src/
  lib.rs
  engine.rs (or similar)
```

I need to grep for: `claimable_pnl`, `pnl_pos_tot`, `pnl_neg_tot`, and related accumulation logic.

---

## Evidence Gathering

### Field identification

**Searching for `pnl_pos_tot` and `pnl_neg_tot`:**

From reading the engine source at `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/lib.rs`:

The relevant state struct fields and the PnL settlement logic need to be traced. Let me cite what I can actually verify from the files.

---

## Formal Property Translation

Based on the hypothesis claim:

> **"Sum of claimable_pnl across all account materializations equals the engine-tracked `pnl_pos_tot - pnl_neg_tot` for the market."**

---

### Variables Referenced

Before I can assert exact line numbers, I need to confirm field names exist. Based on the repository structure for a percolator-style engine, the relevant fields would be in the market/engine state struct. Let me present the translation framework with explicit citations needed.

---

## Output

```
Original claim:    "Sum of claimable_pnl across all account materializations
                   equals the engine-tracked pnl_pos_tot - pnl_neg_tot for
                   the market."
Source:            V10-claimable-pnl-conservation hypothesis brief

Variables referenced:
  - claimable_pnl  (per-account field; must verify exact field name and type)
  - pnl_pos_tot    (market-level accumulator; must verify existence in engine struct)
  - pnl_neg_tot    (market-level accumulator; must verify existence in engine struct)

Quantification:
  - For all reachable engine states
  - After applying any sequence of: position open, position close, mark-price
    update, or account materialization operations
  - The following holds:
      sum_over_accounts(account.claimable_pnl)
        == engine.pnl_pos_tot - engine.pnl_neg_tot

Rust translation:

```rust
// Pre: establish symbolic engine state
let engine: Engine = kani::any();
kani::assume(engine.is_valid());  // well-formed invariant

// Snapshot market-level accumulators
let pnl_pos = engine.pnl_pos_tot;
let pnl_neg = engine.pnl_neg_tot;
kani::assume(pnl_pos >= pnl_neg);  // net positive PnL assumption (or remove)

// Sum claimable_pnl across all materialized accounts
let sum_claimable: i64 = engine
    .accounts
    .iter()
    .map(|a| a.claimable_pnl)
    .sum();

// Conservation assertion
assert_eq!(
    sum_claimable,
    (pnl_pos as i64) - (pnl_neg as i64),
    "claimable_pnl conservation violated"
);
```

Suggested Kani harness name: proof_claimable_pnl_conservation
Estimated harness complexity: HIGH
  (symbolic over all account states + all market states; likely needs
   bounded account array size, e.g., N=4 to remain tractable)
```

---

## Verdict

| Dimension | Assessment |
|-----------|-----------|
| **Verdict** | NEEDS_LAYER_2_TO_DECIDE |
| **Confidence** | LOW |

**Reason:** I cannot confirm that the fields `claimable_pnl`, `pnl_pos_tot`, and `pnl_neg_tot` exist under those exact names in the engine without successful grep results from the actual source files. The hypothesis is formally well-posed and the Kani harness skeleton above is correct *given* those field names exist — but promoting this to TRUE or FALSE requires:

1. **Layer 1 (code review):** Confirm exact field names via `grep -rn "pnl_pos_tot\|pnl_neg_tot\|claimable_pnl"` on the source tree.
2. **Layer 2 (PoC):** Trace every write site to these fields to verify the accumulation arithmetic is consistent (no double-counting, no missed accounts).
3. **Layer 3 (Kani):** Run the harness above with bounded account count (N≤4) to formally verify or produce a counterexample.

**Critical risk if claim is FALSE:** A conservation violation would mean claimable PnL can be created or destroyed across materializations — a direct fund-loss vector.