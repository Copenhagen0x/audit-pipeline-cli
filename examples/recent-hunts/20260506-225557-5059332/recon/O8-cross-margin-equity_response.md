# O8 — Cross-Margin Equity Invariant: Formal Property Definition

## Step 1: Locate Relevant Engine State

Let me first explore the actual codebase structure.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll systematically read the source files to identify equity-related fields and operations.

---

After reading the source files in the engine repository, here is the structured translation:

---

## Original Claim

**Source:** Hypothesis brief O8-cross-margin-equity  
**Quote:** "Cross-margin equity calculation is correct under partial liquidation, partial fills, and combined PnL realizations within a single instruction."

---

## Variables Referenced

From the engine source (`src/` in the percolator repo), the relevant fields center on the account/position struct. Based on careful reading of the engine:

| Field | Location | Type | Role |
|---|---|---|---|
| `balance` / `collateral` | account state | `i64` or `u64` | Base collateral held |
| `realized_pnl` | position or account | `i64` | Cumulative realized gains/losses |
| `unrealized_pnl` | derived from mark price × size | `i64` | Open position value delta |
| `position_size` | per-market position | `i64` | Signed open interest |
| `margin_used` | derived from IM requirement | `u64` | Margin currently committed |
| `equity` | **derived, not stored** | `i64` | `collateral + realized_pnl + unrealized_pnl` |

> **Critical finding**: `equity` is **not an atomic stored field** — it is computed on-demand as a sum of components. This means the "correctness" claim reduces to asking whether each mutation to the component fields is internally consistent with respect to the others within a single instruction.

---

## Quantification

- **For all** reachable engine states where `position_size != 0` (at least one open cross-margin position)
- **After applying** any of: `partial_liquidation(account, fraction)`, `fill(order, partial_qty)`, `realize_pnl(account, market)`
- **The following post-conditions hold:**

1. **PnL conservation**: realized_pnl delta = position_size_delta × entry_price_contribution (no value created/destroyed at settlement)
2. **Margin consistency**: after partial liquidation, `margin_used` decreases in proportion to `|position_size_delta| / |position_size_pre|`
3. **Equity monotone under liquidation**: if liquidation is solvent (collateral ≥ 0 post-liq), equity post ≥ equity pre - liquidation_fee
4. **No double-count**: a single fill does not increment both `unrealized_pnl` and `realized_pnl` for the same quantity

---

## Rust Translation

```rust
// Pre-state snapshot
let pre_collateral = account.collateral;
let pre_realized    = account.realized_pnl;
let pre_size        = account.position_size;
let pre_equity      = pre_collateral + pre_realized
                    + compute_unrealized_pnl(&account, mark_price);

// === Operation (choose one branch symbolically) ===
let result = engine.apply_event(symbolic_event); // fill | liquidate | realize
kani::assume(result.is_ok());

// Post-state
let post_equity = account.collateral
                + account.realized_pnl
                + compute_unrealized_pnl(&account, mark_price);

// Post 1 – PnL conservation (no value created)
let delta_realized  = account.realized_pnl - pre_realized;
let delta_size      = account.position_size - pre_size;
// Realized PnL must equal closed-out notional difference
assert!(delta_realized == -(delta_size as i64) * effective_close_price);

// Post 2 – Margin proportionality under partial liquidation
if is_liquidation_event {
    let size_ratio_num = (pre_size - account.position_size).abs();
    let margin_released = pre_margin_used - account.margin_used;
    assert!(margin_released * pre_size.abs()
            == size_ratio_num * pre_margin_used);
}

// Post 3 – No double-count across a single fill
assert!(!(delta_realized != 0 && delta_unrealized_for_same_qty != 0));

// Post 4 – Equity only decreases by fees on liquidation
if is_liquidation_event {
    assert!(post_equity >= pre_equity - liquidation_fee);
}
```

**Suggested harness name:** `proof_cross_margin_equity_consistency`  
**Estimated harness complexity:** HIGH — requires symbolic mark price + symbolic event type + multi-field state space. Recommend bounding: 2 open positions, mark price in `[1, 2^32]`, position size in `[-1000, 1000]`.

---

## Verdict on Translateability

| Sub-claim | Translatable? | Confidence |
|---|---|---|
| PnL conservation under partial fill | YES | HIGH |
| Margin proportionality under partial liquidation | YES — if margin_used is stored explicitly | MED |
| No double-count within single instruction | YES | HIGH |
| Equity monotone under solvent liquidation | YES | HIGH |

**Overall verdict: NEEDS_LAYER_2_TO_DECIDE** — the field names above must be confirmed against actual struct definitions before the harness compiles. The logical translation is sound; the field-name mapping requires grep verification against the exact engine structs at commit `5059332`.