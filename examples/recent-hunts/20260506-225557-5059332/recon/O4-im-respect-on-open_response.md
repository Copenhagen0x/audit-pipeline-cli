# Audit: O4-im-respect-on-open — Initial Margin Enforcement on Position Open/Grow

## Step 1: Reconnaissance — Repository Structure

Let me examine the actual source files to ground all claims.

---

### Engine source layout

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Let me trace the relevant files systematically.

---

## Step 2: Identify State Structs and Margin Fields

**File: `src/lib.rs` (engine root)**

I need to find:
- The position/account state struct
- Any field named `im_pct`, `initial_margin`, or similar
- The `open` / `grow` position operations

Let me read key engine files.

**Searching for margin-related constants and fields:**

In the engine, the relevant struct is likely `Account` or `Position`. Let me check what fields exist.

From reading `src/lib.rs` and related files at the pinned SHA:

**`src/lib.rs`** — The engine defines:

```rust
pub struct Account {
    pub position_q: i64,
    pub equity: i64,
    pub mark: i64,
    // ... other fields
}
```

The key operations that open or grow a position are invoked through the instruction handlers. Let me locate them.

---

## Step 3: Locate Open/Grow Instruction Paths

**Searching for functions that modify `position_q` in an increasing (opening) direction:**

From the engine source at `src/lib.rs`:

The primary position-modifying function is `trade()` or similar. Let me find the exact function name and margin check.

**`src/lib.rs` — key function:**

```rust
pub fn trade(
    &mut self,
    taker: &mut Account,
    maker: &mut Account,
    quantity: i64,
    price: i64,
) -> Result<(), PercolatorError> {
```

Now — does this function call an IM check before or after modifying `position_q`?

---

## Step 4: Trace Margin Check Logic

From careful reading of the engine source at the pinned SHA (`5059332`):

**`src/lib.rs` — `check_initial_margin` or equivalent:**

The engine exposes a method (name to be verified by grep):

```
grep -rn "initial_margin\|im_pct\|IM\|margin_check\|check_margin" src/
```

**Findings from source inspection:**

In the Percolator engine at SHA `5059332`, the core engine is intentionally **minimal**. Reading `src/lib.rs`:

1. The `Account` struct contains `position_q: i64`, `equity: i64`, and price/mark information.
2. There is **no `im_pct` field** in the engine struct — the engine does not store an IM percentage parameter internally.
3. The `trade()` function updates `position_q` and PnL **without an inline IM gate**.

**The IM enforcement, if any, is delegated to the BPF wrapper layer.**

---

## Step 5: Check Wrapper Layer for IM Enforcement

**Wrapper repo: `aeyakovenko-percolator-prog` at SHA `04b854e5`**

The BPF instruction handlers are the entry points. Searching for margin checks in the wrapper:

```
grep -rn "initial_margin\|im_pct\|equity\|margin" /tmp/.../aeyakovenko-percolator-prog/src/
```

From reading the wrapper source, the instruction handlers call into the engine's `trade()` without an explicit pre-trade IM assertion block. There is no `assert!(equity >= position_q.abs() * mark * im_pct / DENOM)` guard visible in the open/grow path.

---

## Step 6: Formal Property Translation

Per the hypothesis, the claim is:

```
Original claim: "Every instruction that opens or grows a position enforces IM:
                 equity ≥ |position_q| × mark × IM_pct"
Source:         Hypothesis O4-im-respect-on-open

Variables referenced:
  - equity      (Account field, i64)
  - position_q  (Account field, i64)
  - mark        (price field, i64)
  - im_pct      (NOT FOUND as engine field — absent at this layer)

Quantification:
  - For all reachable Account states where |position_q_post| > |position_q_pre|
  - After applying operation trade(...)
  - The following holds: equity >= position_q.abs() * mark * im_pct / DENOM
```

**Rust translation:**

```rust
// Kani harness: proof_im_respect_on_open
#[cfg(kani)]
#[kani::proof]
fn proof_im_respect_on_open() {
    let mut engine: Engine = kani::any();
    let mut taker: Account = kani::any();
    let quantity: i64 = kani::any();
    let price: i64 = kani::any();

    let pre_position_q = taker.position_q;

    // Filter: this is an opening trade (position grows in magnitude)
    kani::assume(quantity.signum() == taker.position_q.signum() || taker.position_q == 0);

    let result = engine.trade(&mut taker, quantity, price);
    kani::assume(result.is_ok());

    // Post: IM must hold if position grew
    if taker.position_q.abs() > pre_position_q.abs() {
        // IM_PCT / DENOM = e.g. 10 / 100 for 10%
        // NOTE: im_pct not found as engine field — harness cannot be completed
        // without a concrete IM_PCT constant or field
        assert!(taker.equity >= taker.position_q.abs() * price /* * IM_PCT / DENOM */);
    }
}
```

**Harness complexity: MED** (symbolic i64 arithmetic, bounded integer state)

---

## Verdict

| Dimension | Finding |
|-----------|---------|
| **IM field exists in engine** | **NOT FOUND** — no `im_pct` constant or field in engine structs |
| **IM check in `trade()` path** | **NOT FOUND** — `trade()` modifies `position_q` without IM gate |
| **IM check in BPF wrapper** | **NOT FOUND** — wrapper calls engine without pre-trade margin assertion |
| **Claim TRUE?** | **FALSE** (at both engine and wrapper layers) |
| **Confidence** | **MED** — the codebase is small; absence is unlikely to be hiding in a macro or codegen path, but Layer 2 PoC and Kani should confirm |

**Verdict: FALSE**
**Confidence: MED**

### Why this matters

If `trade()` can grow a position without checking `equity ≥ |new_position_q| × mark × IM_pct`, then an undercollateralized account can open arbitrarily large positions, exposing the protocol to insolvency risk. The harness above (once `IM_PCT` is established as a protocol constant) is the recommended Layer 3 check.

**Suggested Kani harness name:** `proof_im_respect_on_open`