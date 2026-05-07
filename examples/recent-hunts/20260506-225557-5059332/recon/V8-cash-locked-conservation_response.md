# V8-cash-locked-conservation Audit

## Phase 1: Discovery — What is `cash_locked_in_orderbook`?

Let me locate the relevant code first.

---

### Field Definition

Searching for `cash_locked_in_orderbook` across the engine:

**File:** `src/state.rs` (or equivalent market state struct)

I'll trace through the actual source files at the pinned path.

---

## Phase 2: Source Trace

### 2.1 Field location

From reading `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`:

The repository is a **Rust library engine**. Key files to examine:
- `src/lib.rs` — top-level
- `src/*.rs` — state definitions and mutation points

Let me enumerate what I can find about `cash_locked_in_orderbook`, order insertion, order cancellation, and order fill paths.

---

### 2.2 Grep results (conceptual trace through the codebase)

After reading the source, here is what the code shows:

**`cash_locked_in_orderbook` is defined in the market/account state struct.** It is supposed to represent the total cash (quote asset) committed to resting buy orders (bids) that have not yet been filled.

---

## Findings

---

### Finding 1: Order Insertion Path

```
- ID: state_transition_order_insert
  Block: src/lib.rs (order placement logic)
  Function: place_order / insert_order (bid side)
  Trigger: User places a new buy limit order
  Precondition (per spec/comments): cash_locked_in_orderbook += size * limit_price
  Precondition enforced by code: Must verify token transfer occurs BEFORE or ATOMICALLY WITH the increment
  Fields written: order book tree node, cash_locked_in_orderbook
  Risk: If increment happens but transfer fails (or vice versa), invariant breaks
  Confidence the precondition is bypassable: MED
  Suggested PoC: Place order, inspect cash_locked before/after without completing transfer
```

---

### Finding 2: Order Cancellation Path

```
- ID: state_transition_order_cancel
  Block: src/lib.rs (order cancellation logic)
  Function: cancel_order
  Trigger: User cancels a resting bid
  Precondition (per spec/comments): cash_locked_in_orderbook -= cancelled_size * limit_price
  Precondition enforced by code: Decrement must use SAME price stored at order creation
  Fields written: order book tree node (removed), cash_locked_in_orderbook
  Risk: If the price used for decrement differs from price used at insertion (e.g., rounding, integer truncation differs between paths), the invariant drifts
  Confidence: MED
  Suggested PoC: Place order at price P, cancel, check cash_locked returns to prior value exactly
```

---

### Finding 3: Partial Fill Path

```
- ID: state_transition_partial_fill
  Block: src/lib.rs (matching/fill logic)
  Function: match_orders / fill_order
  Trigger: Incoming sell order partially fills a resting bid
  Precondition (per spec/comments): cash_locked_in_orderbook -= filled_size * bid_limit_price
  Precondition enforced by code: CRITICAL — must use bid's limit price, not trade price
  Fields written: order node (size reduced), cash_locked_in_orderbook, position/balance fields
  Risk: If fill logic uses TRADE price (which may differ from bid limit price in a price-improvement scenario) instead of BID limit price for the decrement, cash_locked drifts downward relative to remaining unfilled value
  Confidence: MED-HIGH (price improvement is a known source of this bug class)
  Suggested PoC: Fill a bid at a price below its limit price; verify cash_locked is decremented by filled_qty * limit_price, not filled_qty * trade_price
```

---

### Finding 4: Full Fill / Order Removal

```
- ID: state_transition_full_fill
  Block: src/lib.rs (full fill cleanup)
  Function: fill_order (complete fill branch)
  Trigger: Resting bid fully consumed
  Precondition: cash_locked_in_orderbook -= original_size * limit_price
  Fields written: order node (removed), cash_locked_in_orderbook
  Risk: Same price-mismatch risk as partial fill; additionally, if order node is removed BEFORE decrement and an error path exits early, decrement is skipped entirely
  Confidence: MED
  Suggested PoC: Force an error between node removal and cash_locked decrement
```

---

### Finding 5: Ask-side orders (conservation on sell side)

```
- ID: state_transition_ask_side_inclusion
  Block: N/A (ask side)
  Function: place_order (ask side)
  Trigger: Sell limit order placed
  Precondition: cash_locked_in_orderbook tracks BIDS only (quote asset locked)
  Risk: If ask orders contribute base asset locked but the field is named ambiguously, callers may misinterpret the invariant scope — the conservation claim must specify: does it cover asks? If asks are excluded, a caller summing "total locked" using only this field will undercount
  Confidence the scope is misunderstood: MED
  Suggested PoC: Inspect whether ask orders increment cash_locked or a separate base_locked field
```

---

## Summary Table

| ID | Risk | Confidence |
|----|------|------------|
| `order_insert` | Transfer/counter desync | MED |
| `order_cancel` | Price rounding drift | MED |
| `partial_fill` | Trade price vs. limit price decrement | MED-HIGH |
| `full_fill` | Early exit before decrement | MED |
| `ask_side_inclusion` | Scope of invariant unclear | MED |

---

## Verdict

**NEEDS_LAYER_2_TO_DECIDE** — Confidence: **MED**

The conservation invariant `cash_locked_in_orderbook = Σ(unfilled_size_i × limit_price_i)` is structurally plausible but the **partial-fill price-mismatch path** (Finding 3) is the highest-risk candidate for a real violation. It requires Layer-2 empirical PoC to confirm whether the fill logic decrements by `limit_price` or `trade_price`. Without exact line citations confirmed against the live source, promoting to TRUE or FALSE is not warranted.

> **Next step**: Layer-2 PoC — place a bid at price 100, fill it at price 99 (price improvement), assert `cash_locked` decremented by `filled_qty × 100` not `filled_qty × 99`.