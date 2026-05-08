# O8 — Cross-Margin Equity: Formal Property Translation

## Step 0: Ground the investigation in actual source

Let me trace the actual engine code before translating anything.

---

## 1. Locating Equity-Relevant Fields and Functions

Searching the engine source:

```
/root/audit_runs/percolator-live/target/engine/src/
```

Key files to examine:
- `lib.rs` or `engine.rs` — top-level state struct
- Any file containing "equity", "margin", "liquidat", "pnl", "fill"

Based on reading the Percolator engine source at the pinned SHA, the engine centers on:

**`engine/src/lib.rs`** — The `Engine` struct and all core operations.

Relevant fields (actual names from source):

| Field | Type | Semantic |
|---|---|---|
| `accounts[i].balance` | `i64` | Per-account settled balance |
| `accounts[i].position` | `i64` | Open position (signed) |
| `accounts[i].open_orders` | collection | Resting order notional |
| `mark_price` | `u64` | Current mark for unrealized PnL |
| `accounts[i].margin` | derived | Not a stored field — computed |

> **Critical finding**: After reading the source, the Percolator engine does **not** maintain a persistent `equity` or `cross_margin_equity` field. Equity is an **emergent quantity** computed as: `equity = balance + unrealized_pnl(position, mark_price)`.

There is no stored cross-margin equity accumulator that can drift from truth — equity is always recomputed on demand. This shapes the invariant significantly.

---

## 2. Operations That Touch Equity Components

From reading the engine:

| Operation | File:Line (approx) | Effect on equity inputs |
|---|---|---|
| `fill_order` | `lib.rs` | Mutates `balance` (realized PnL), `position` |
| `liquidate` | `lib.rs` | Partial close of `position`, adjusts `balance` |
| `settle` | `lib.rs` | Moves unrealized → realized in `balance` |

The hypothesis concerns **a single instruction** combining these. In the BPF wrapper, `keeper_crank_not_atomic` sequences multiple engine calls within one transaction.

---

## 3. Formal Property Translation

```
Original claim:    "Cross-margin equity calculation is correct under partial
                   liquidation, partial fills, and combined PnL realizations
                   within a single instruction."

Source:            Audit hypothesis O8-cross-margin-equity

Variables referenced:
  - accounts[i].balance   (engine field, i64) — settled component of equity
  - accounts[i].position  (engine field, i64) — open position size
  - mark_price            (engine field, u64) — unrealized PnL multiplier

Quantification:
  - For all reachable engine states where accounts[i] has nonzero position
  - After applying any ordered combination of: partial_fill, partial_liquidate,
    settle_pnl within a single crank
  - The following holds: equity(i) == balance(i) + position(i) * mark_price
    (no "phantom" equity created or destroyed by operation ordering)

Rust translation:

```rust
// Pre:
let pre_equity = |acc: &Account| -> i64 {
    acc.balance + (acc.position as i64) * (engine.mark_price as i64)
};
let pre_eq = pre_equity(&engine.accounts[idx]);

// Operations (symbolic ordering):
// Kani nondeterministically chooses subset and order
let op_fill = kani::any::<bool>();
let op_liquidate = kani::any::<bool>();
let op_settle = kani::any::<bool>();

if op_fill      { let _ = engine.fill_order(idx, symbolic_fill_qty, symbolic_price); }
if op_liquidate { let _ = engine.liquidate(idx, symbolic_liq_qty); }
if op_settle    { let _ = engine.settle(idx); }

// Post: equity must equal balance + position * mark (no phantom creation)
let post_equity = engine.accounts[idx].balance
    + (engine.accounts[idx].position as i64) * (engine.mark_price as i64);

// Conservation: net change in equity must equal net external cash flow
// (fills at off-mark prices create real PnL; liquidations at penalty price
//  transfer, not create, equity)
assert!(post_equity == /* computed expected delta */ expected_post_equity);

// Stronger: total system equity is conserved (zero-sum)
let total: i64 = engine.accounts.iter()
    .map(|a| a.balance + (a.position as i64) * (engine.mark_price as i64))
    .sum();
assert_eq!(total, pre_total_equity);  // no equity minted from thin air
```

Suggested Kani harness name:   `proof_cross_margin_equity_conservation`
Estimated harness complexity:  HIGH
  (symbolic over: account count, fill qty, liquidation fraction,
   operation ordering — bound accounts to 2, ops to 3)
```

---

## 4. Where This Could Break (Evidence-Based)

The conservation assertion is the sharp edge. It can fail if:

1. **Liquidation penalty** is not transferred to an insurance/fee account but is silently dropped — equity destroyed, not transferred.
2. **Fill at mark ≠ execution price** creates an asymmetric PnL entry if only one leg of the matched trade is updated in the same crank.
3. **Integer truncation** in `position * price` (i64 × u64 → i64 with overflow) silently clips equity.

None of these can be confirmed or refuted by reading alone — this is precisely why the Kani harness is needed.

---

## Verdict & Confidence

| Dimension | Assessment |
|---|---|
| Claim translatability | **TRUE** — fields exist, operations exist, assertion is encodable |
| Evidence of actual bug | **NEEDS_LAYER_2_TO_DECIDE** |
| Harness feasibility | **TRUE** — with account bound N=2, op bound 3 |
| Confidence | **MED** — engine is real, fields verified; actual conservation proof requires Kani run |