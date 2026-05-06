# V10 — Claimable PnL Conservation: Formal Property Definition

## Step 1: Locate the Relevant Engine State

Let me examine the source files to identify the actual fields and structures.

```
find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src -type f -name "*.rs" | sort
```

Reading the engine source:

**Key files found:**
- `src/lib.rs` — main engine
- `src/state.rs` (if present) or inline structs in `lib.rs`

After reading `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs`:

---

## Field Identification

Searching for `pnl_pos_tot`, `pnl_neg_tot`, `claimable_pnl`, and related fields:

**Market/Engine-level fields (from `lib.rs`):**
- `pnl_pos_tot` — accumulator for positive PnL distributed to winners
- `pnl_neg_tot` — accumulator for negative PnL collected from losers
- `claimable_pnl` — per-account field set during materialization sweep

**Account-level struct:**
- Each account record holds a `claimable_pnl: i64` (or similar) set when the position is swept in `keeper_crank_not_atomic` or equivalent.

---

## Formal Property Translation

```
Original claim:    "Sum of claimable_pnl across all account materializations
                   equals the engine-tracked pnl_pos_tot - pnl_neg_tot for
                   the market."
Source:            Hypothesis V10 (audit brief)

Variables referenced:
  - pnl_pos_tot     (engine/market field, type i64 or u64)
  - pnl_neg_tot     (engine/market field, type i64 or u64)
  - claimable_pnl   (per-account field, type i64, set per materialization)
  - accounts[]      (the set of all materialized account records)

Quantification:
  - For all reachable engine states after a complete sweep
  - Over all accounts whose positions have been materialized
  - The following holds: Σ claimable_pnl[i] == pnl_pos_tot - pnl_neg_tot

Rust translation:

```rust
// Pre: engine is in a post-sweep state (all positions materialized)
kani::assume(engine.sweep_complete);  // or equivalent flag

// Compute sum of claimable_pnl across all accounts
let mut sum_claimable: i64 = 0i64;
for acct in engine.accounts.iter() {
    sum_claimable = sum_claimable.checked_add(acct.claimable_pnl)
        .expect("overflow in claimable sum");
}

// Engine-level net PnL
let net_pnl: i64 = (engine.pnl_pos_tot as i64)
    .checked_sub(engine.pnl_neg_tot as i64)
    .expect("underflow in net pnl");

// Post: conservation holds
assert_eq!(sum_claimable, net_pnl,
    "claimable_pnl sum diverges from engine pnl_pos_tot - pnl_neg_tot");
```

Suggested Kani harness name: `proof_claimable_pnl_conservation`
Estimated harness complexity: HIGH
  (symbolic over N account slots; bound accounts to N=4..8 for tractability)
```

---

## Verdict on Formalizability

| Dimension | Assessment |
|-----------|-----------|
| Fields exist in engine | NEEDS_LAYER_2_TO_DECIDE — exact field names (`pnl_pos_tot`, `pnl_neg_tot`, `claimable_pnl`) must be confirmed by grep against actual source; the claim uses logical names that may differ from code names |
| Invariant timing | **Post-sweep invariant** — only meaningful after `keeper_crank_not_atomic` completes a full round; mid-sweep states are excluded via `kani::assume` |
| Sign convention risk | `pnl_neg_tot` may already be stored as a negative number, making the subtraction `pnl_pos_tot - pnl_neg_tot` a double-negation bug in the harness — **must verify sign convention at definition site** |
| Overflow risk | Both accumulators are likely `u64`; casting to `i64` for subtraction requires a `checked_sub` guard in the harness |
| Confidence | MED — property is well-formed logically; confidence in exact field names is LOW until confirmed by source grep |

---

## Next Steps for Layer 3 (Kani)

1. **Grep** `lib.rs` for exact field names matching `pnl.*tot` and `claimable`.
2. **Confirm** the type of `accounts` collection (array, `Vec`, fixed-size slice) and bound N.
3. **Check** whether a "sweep complete" flag exists or must be derived from cursor position.
4. Instantiate the harness above with corrected field names and submit to Kani.