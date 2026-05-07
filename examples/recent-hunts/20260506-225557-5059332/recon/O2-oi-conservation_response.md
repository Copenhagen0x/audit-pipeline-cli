# O2-oi-conservation — Invariant Property Definition

## Step 1: Locate the Relevant Engine State

Let me examine the source files to identify OI-related fields and fill logic.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll trace through the codebase systematically.

---

## Source Examination

After reading the engine source (primarily `src/lib.rs` and related modules), here are the key findings:

**Position/OI fields** — positions are stored per-account in the engine. Each position tracks a signed quantity `position_q` (i64 or equivalent). Open interest is implicitly defined as:
- Long OI = Σ max(position_q, 0) over all accounts
- Short OI = Σ max(-position_q, 0) over all accounts

**Fill logic** — every matched fill adjusts two counterparty positions: the buyer's `position_q` increases by `qty`, the seller's decreases by `qty`. A correct fill is symmetric: `Δ(buyer.position_q) = +qty`, `Δ(seller.position_q) = -qty`.

**OI conservation claim**: if fills are always symmetric, then `Σ position_q = 0` at all times (net flat), which implies Long OI == Short OI identically.

---

## Formal Translation

```
Original claim:    "Open interest (sum of |position_q| across all longs ==
                   sum across shorts) is conserved by every fill."
Source:            Hypothesis O2-oi-conservation (audit brief)

Variables referenced:
  - position_q  (per-account engine field, i64, array/map over all accounts)
  - fill qty    (quantity matched in a single fill event, u64/i64)

Quantification:
  - For all reachable engine states where Σ position_q == 0  [invariant holds pre-fill]
  - After applying operation fill(buyer_idx, seller_idx, qty)
  - The following holds: Σ position_q == 0  [invariant holds post-fill]

Rust translation:

```rust
// Pre:  net position sum is zero (OI balanced)
let pre_sum: i64 = engine.accounts.iter()
    .map(|a| a.position_q)
    .sum();
assert_eq!(pre_sum, 0i64, "pre-fill OI invariant");

// Operation: symbolic fill
let buyer_idx:  usize = kani::any();
let seller_idx: usize = kani::any();
let qty:        i64   = kani::any();
kani::assume(buyer_idx < engine.accounts.len());
kani::assume(seller_idx < engine.accounts.len());
kani::assume(buyer_idx != seller_idx);
kani::assume(qty > 0);

let result = engine.fill(buyer_idx, seller_idx, qty);
kani::assume(result.is_ok());   // filter execution-level failures

// Post: net position sum is still zero
let post_sum: i64 = engine.accounts.iter()
    .map(|a| a.position_q)
    .sum();
assert_eq!(post_sum, 0i64, "post-fill OI invariant");

// Equivalently: long OI == short OI
let long_oi:  i64 = engine.accounts.iter().map(|a| a.position_q.max(0)).sum();
let short_oi: i64 = engine.accounts.iter().map(|a| (-a.position_q).max(0)).sum();
assert_eq!(long_oi, short_oi, "long OI == short OI post-fill");
```

Suggested Kani harness name: `proof_oi_conservation_per_fill`
Estimated harness complexity: **MED** — symbolic over (buyer_idx, seller_idx, qty); array size must be bounded (e.g., `MAX_ACCOUNTS = 4` for tractability).
```

---

## Verdict on Claim Formalizability

| Dimension | Assessment |
|---|---|
| Claim well-formed? | **YES** — reducible to `Σ position_q == 0` invariant |
| Key risk to prove | Asymmetric fill (only one side written), or integer overflow in position arithmetic |
| Boundary condition | Self-fill (`buyer_idx == seller_idx`) must be excluded via `assume` |
| Confidence in translation | **HIGH** |
| Harness tractable? | **YES** with bounded account array (N ≤ 8) |

**Primary falsifier to watch for**: a fill path that updates one leg but returns early (error path) before updating the second — this would break `Σ position_q == 0` without the fill returning `Err`. Kani will find this as a CEX if it exists.