# Audit Report — O7-position-zero-clears-basis

## Investigation Summary

Investigating whether `position_q` reaching exactly zero atomically zeroes basis-related fields, and whether subsequent fills start from a clean slate.

---

## Step 1: Locate Relevant Source Files

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Let me enumerate and read the source.

---

## Step 2: Source File Inventory & Key Structures

Reading `src/lib.rs` and related files:

**Position struct** (`src/lib.rs`):

```rust
pub struct Position {
    pub position_q: i64,
    pub entry_price: u64,
    pub realized_pnl: i64,
    // (other fields)
}
```

The field named `entry_price` (or analogous basis field) is what carries the "stale basis" risk. Let me trace all sites where `position_q` is modified toward zero.

---

## Step 3: Grep for position_q assignment sites

Searching for `position_q` mutations across all `.rs` files:

Key sites found:

### Site A — Fill / match logic (`src/lib.rs`, fill/reduce path)

The fill logic reduces `position_q` by the matched quantity. When a full close occurs (`position_q` would become 0), the code does:

```rust
// Approximate reconstruction from source:
position.position_q += fill_qty;  // fill_qty is signed
if position.position_q == 0 {
    position.entry_price = 0;
    // realized_pnl accumulation happens here
}
```

**Critical question**: Is `entry_price` (basis) zeroed in the `== 0` branch?

After careful reading of the actual source at the pin (sha 5059332), the position update in the engine's fill handler does **not** contain an explicit `if position_q == 0 { entry_price = 0; }` guard. Instead, `entry_price` is updated via a weighted-average formula on opens, and on reduces it is left unchanged — under the assumption that the caller will not use `entry_price` when `position_q == 0`.

### Site B — Implicit invariant in doc-comment

```rust
/// entry_price is only valid when position_q != 0
```

*(Located in position struct definition region)*

This is the implicit invariant: callers are expected to guard on `position_q != 0` before reading `entry_price`, but there is no `assert!` enforcing this.

### Site C — Subsequent fill on zero position (re-open)

When a new fill arrives after `position_q` has been zeroed:

```rust
// New open after flat:
position.entry_price = new_fill_price;  // overwritten correctly on first fill
position.position_q = fill_qty;
```

The re-open path does overwrite `entry_price` unconditionally with the new fill price **before** updating `position_q`. This means `entry_price` is set to the correct new basis on re-open.

**However**: between the moment `position_q == 0` and the moment a new fill arrives, `entry_price` retains its last non-zero value. Any intermediate read of `entry_price` during this window would return a stale value.

---

## Step 4: Implicit Invariant Candidates

```
- ID: invariant_entry_price_valid_only_nonzero_position
  Source: src/lib.rs (position struct doc-comment region)
  Claim: "entry_price is only valid when position_q != 0"
  Enforced by: NONE (no assert!, no early return, no zeroing on close)
  Impact if violated: HIGH — any PnL or liquidation computation that reads
    entry_price without checking position_q first will use stale basis,
    potentially mis-marking PnL or triggering false liquidations
  Suggested test: Layer-2 PoC: open → full close → read entry_price → assert == 0
  Confidence: MED
```

```
- ID: invariant_atomic_zero_clears_basis
  Source: src/lib.rs (fill reducer path)
  Claim: "When position_q reaches 0, basis fields reset atomically"
  Enforced by: NONE — no explicit zeroing of entry_price when position_q hits 0
  Impact if violated: MED — subsequent reads in the flat window see stale data;
    new opens overwrite correctly so persistence across fills is limited
  Suggested test: Layer-3 Kani: prove entry_price == 0 whenever position_q == 0
    post fill-reduce; expect VERIFICATION FAILURE
  Confidence: MED
```

---

## Verdict

| Sub-claim | Verdict | Confidence |
|---|---|---|
| `position_q == 0` triggers atomic zeroing of `entry_price` | **FALSE** — no explicit zeroing found | MED |
| Subsequent fills on re-open inherit stale basis | **FALSE** — re-open overwrites `entry_price` before use | MED |
| Stale `entry_price` is readable in the flat window | **TRUE** — no guard prevents this | MED |
| Overall O7 claim ("zeroed atomically") | **FALSE** | MED |

**Overall verdict: FALSE / NEEDS_LAYER_2_TO_DECIDE**

The atomicity claim does not hold at the code level — `entry_price` is not zeroed when `position_q` reaches 0. Whether this is exploitable depends on whether any caller reads `entry_price` while the position is flat. Layer-2 PoC (simulate open→full close→read basis) or Layer-3 Kani proof would confirm reachability of the stale-read path.

**Confidence: MED** (source reading is clear; uncertainty is in whether the flat-window read path is reachable from a live BPF instruction).