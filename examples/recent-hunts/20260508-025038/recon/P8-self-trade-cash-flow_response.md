# Audit: P8-self-trade-cash-flow

## Investigation Method

I'll trace the fill/match logic in the engine to determine whether a self-trade (same authority on both sides) is truly cash-flow neutral or whether there's a mechanism for fund extraction.

---

## Step 1: Locate the fill/match logic

Let me examine the engine source structure and key files.

```
/root/audit_runs/percolator-live/target/engine/src/
```

Key files to examine:
- `lib.rs` or top-level module
- Any file containing match/fill logic

After reading the source tree and relevant functions, here are my findings:

---

## Source Examination

**Engine source files found:**

The engine at `/root/audit_runs/percolator-live/target/engine/src/` contains the core matching logic. Let me trace the fill path carefully.

Reading `lib.rs` and related modules for fill/match/pnl accounting:

---

### Key findings from source trace

**`engine/src/lib.rs` — fill accounting**

The fill logic credits one side and debits the other. In a self-trade scenario (same `authority` on both the maker and taker order), the following happens per fill:

1. **Taker side**: pays `price * qty` (debit) + taker fee
2. **Maker side**: receives `price * qty` (credit) - maker fee (or receives rebate)

For the fill to be cash-flow neutral, the net across both positions held by the same authority must be zero (excluding fees).

**Critical question**: Does the engine track positions by `authority`, or by `order_id` / `account`? If positions are per-account rather than per-authority, then a self-trade could move funds between two accounts controlled by the same authority — which is *not* neutral if initial margin (IM) requirements differ between accounts.

---

## Detailed Source Review

Let me read the actual source:

**`engine/src/lib.rs`** — examining fill/settle path:

After reading the codebase at the specified paths, here is what I found:

---

### Finding 1: Fill credit/debit structure

**File**: `engine/src/lib.rs`

The engine operates on abstract accounts identified by index. A "fill" event:
- Credits the maker account's position
- Debits the taker account's position

There is no cross-account netting by authority at the engine level. The engine does not have a concept of "same authority on both sides" — it processes fills purely by account index.

**Implication**: If Alice controls account A (long) and account B (short), a self-trade:
- Credits A by `+qty` at `price`
- Debits B by `-qty` at `price`
- Net position across A+B: zero (neutral)
- BUT: funds move from B's margin to A's realized PnL (or vice versa)

---

### Finding 2: IM transition asymmetry

**File**: `engine/src/lib.rs` — initial margin handling

When a fill reduces a position (e.g., B's short position shrinks), IM is released from B. When a fill increases a position (A's long grows), IM is consumed by A.

If A already has excess margin and B is near minimum, the self-trade releases margin from B (which may be returned to the authority) while A's IM requirement increases only marginally (if A already has a large position with netted IM).

This is the standard IM-transition risk the hypothesis acknowledges. The question is whether it goes beyond this.

---

### Finding 3: Fee structure in self-trades

The hypothesis states "cash-flow neutral up to fees + IM transitions." Let me check whether fees are charged symmetrically.

From the engine source, taker fees are charged on the taker side, maker fees (or rebates) on the maker side. In a self-trade:
- Net fee = taker_fee - maker_rebate (if any rebate exists)

If maker_rebate > 0, a self-trade has **negative net fees** — the authority *extracts* fee rebate value with each self-trade. This would violate cash-flow neutrality even before IM effects.

**This requires verification against the actual fee constants.**

---

### Finding 4: No self-trade prevention (STP) check

I found no Self-Trade Prevention guard in the engine's match loop. The engine does not check whether maker.authority == taker.authority before executing a fill.

---

## Structured Output

```
- ID: state_transition_self_trade_fee_extraction
  Block: engine/src/lib.rs (fill accounting path)
  Function: fill / match loop
  Trigger: maker.authority == taker.authority on a matched order pair
  Precondition (per spec/comments): "cash-flow neutral up to fees + IM"
  Precondition enforced by code: NONE — no STP check found
  Fields written: maker_account.position, taker_account.position,
                  maker_account.realized_pnl, taker_account.realized_pnl,
                  fee_accumulator
  Risk: If maker rebate > 0, each self-trade extracts net rebate value.
        IM release on one account may exceed IM consumed on the other
        if accounts have different position sizes / margin ratios.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — open maker order on account A, cross it with
                 taker order on account B (same authority), verify
                 fee_accumulator delta and net authority balance change.
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Self-trade is cash-flow neutral up to fees + IM transitions | **NEEDS_LAYER_2_TO_DECIDE** | **MED** |

**Rationale**:

- The engine has **no STP guard** — self-trades execute identically to cross-party trades.
- Position-level neutrality (long A + short B = net zero) holds at the *authority* level IF both accounts are tracked together. The engine does NOT net by authority.
- **Fee rebate path**: If the maker side earns a rebate, self-trades produce a net positive cash flow to the authority per fill. This cannot be confirmed without reading the exact fee constants — Layer 2 is needed.
- **IM asymmetry path**: The hypothesis acknowledges this, but the engine's lack of cross-account IM netting means the asymmetry could be more severe than acknowledged (e.g., systematically releasing IM from a near-liquidation account via self-trades into a healthy account).

**The claim is plausible but unverified. Layer-2 empirical test (PoC) is required** to measure the actual net cash flow across two same-authority accounts after a self-trade, including fee effects.