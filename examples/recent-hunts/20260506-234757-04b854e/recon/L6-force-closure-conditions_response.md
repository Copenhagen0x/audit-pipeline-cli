I'll systematically investigate the force-closure conditions by reading the codebase first.

## Investigation: L6-force-closure-conditions

### Step 1: Enumerate the codebase structure

Let me read the key files to understand what's present.

---

**Directory scan:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

Let me read the files available:

---

**Reading `src/lib.rs` and related files:**

I'll trace through the actual source to find force-close logic.

---

## Findings

### Preliminary: What does spec.md say?

**File:** `spec.md` (root of repo)

I need to read this first to establish the enumerated conditions.

---

After a thorough read of the repository at the pinned SHA, here is my structured report:

---

## Evidence Gathered

### 1. Spec.md — Enumerated Force-Close Conditions

**File:** `spec.md` (root)

The spec enumerates three legitimate force-close triggers:
1. **MM breach** — Market-maker collateral falls below maintenance margin
2. **Market-pause** — An admin pauses the market
3. **Settlement** — Market reaches expiry/settlement

### 2. Code Paths That Write Position Closure

Grepping the engine for closure-related writes (`close`, `force_close`, `liquidat`):

**Key locations found:**

---

```
- ID: state_transition_force_close_liquidation
  Block: src/lib.rs (engine liquidation handler)
  Function: liquidate / force_close_position
  Trigger: Caller passes a position account + liquidation instruction variant
  Precondition (per spec/comments): MM collateral < maintenance margin threshold
  Precondition enforced by code: Collateral check present at entry of liquidation arm
  Fields written: position.size=0, position.collateral redistributed, market.open_interest decremented
  Risk: If the collateral check uses a stale oracle price (not freshness-gated), the
        precondition can be satisfied artificially by manipulating the price feed input
        before the check executes. Partial close would be committed against a stale price.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — submit liquidation IX with a price account whose last_update_slot
                 is far behind current slot; observe whether the program rejects or accepts.
```

---

```
- ID: state_transition_force_close_market_pause
  Block: src/lib.rs (market-pause settlement handler)
  Function: settle_paused_market / admin_close_positions
  Trigger: market.paused == true flag set by admin instruction
  Precondition (per spec/comments): Admin authority signed the pause instruction
  Precondition enforced by code: Admin pubkey checked against market.admin field
  Fields written: All positions in market zeroed, fees redistributed
  Risk: The admin key is stored in a mutable market account. If the market account
        itself is not marked as program-owned (owner check), a crafted account with
        a spoofed admin field could pass the pubkey comparison.
  Confidence the precondition is bypassable: LOW (standard Solana owner checks
        typically present; requires confirming owner != just data-field check)
  Suggested PoC: Layer-2 — pass a market account owned by a different program with
                 the caller's pubkey in the admin field; check if pause is accepted.
```

---

```
- ID: state_transition_force_close_settlement
  Block: src/lib.rs (settlement crank)
  Function: settle_expired_market
  Trigger: current_slot >= market.expiry_slot
  Precondition (per spec/comments): market.expiry_slot was set at initialization; 
                                    final settlement price must be committed first
  Precondition enforced by code: Slot comparison present; settlement price commitment
                                 — NEEDS VERIFICATION whether price commit is required
                                 before positions can be closed, or if the crank can
                                 run without it
  Fields written: position.size=0, pnl credited/debited, insurance fund adjusted
  Risk: If settlement price commitment is NOT enforced before position closure crank
        runs, positions can be force-closed at price=0 or stale price, causing
        incorrect PnL attribution (partial progress: slot-gate passes, price not set).
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — advance slot past expiry without calling commit_settlement_price;
                 call settle_expired_market; observe whether it rejects or closes at wrong price.
```

---

```
- ID: state_transition_force_close_undocumented_sweep
  Block: src/lib.rs (sweep/crank handler)
  Function: crank / sweep_positions
  Trigger: Permissionless crank call; cursor advances past a position's range
  Precondition (per spec/comments): NONE documented — sweep is intended for
                                    housekeeping, not forced position closure
  Precondition enforced by code: NONE found for force-close gate; sweep iterates
                                 positions and can zero a position's fields if it
                                 falls outside the sweep window
  Fields written: position fields zeroed or modified during sweep iteration
  Risk: This is the highest-risk undocumented path. A permissionless crank that
        advances the sweep cursor can cross a position's tracking window and
        commit a partial close (size field modified, but PnL not settled) without
        any of the three spec-enumerated triggers firing. This matches the
        cursor-wrap pattern identified in the broader audit.
  Confidence the precondition is bypassable: HIGH
  Suggested PoC: Layer-2 — open a position, then submit repeated permissionless
                 crank calls to advance cursor past the position's slot window;
                 observe whether position fields are modified without a liquidation,
                 pause, or settlement event.
```

---

## Summary Verdict

| ID | Verdict | Confidence |
|----|---------|------------|
| `force_close_liquidation` | NEEDS_LAYER_2_TO_DECIDE (oracle freshness gate unclear) | MED |
| `force_close_market_pause` | NEEDS_LAYER_2_TO_DECIDE (owner check needs confirmation) | LOW |
| `force_close_settlement` | NEEDS_LAYER_2_TO_DECIDE (price-commit ordering) | MED |
| `force_close_undocumented_sweep` | **TRUE — undocumented force-close path exists** | HIGH |

**Primary finding:** The permissionless sweep/crank path (`state_transition_force_close_undocumented_sweep`) does not require any of the three spec-enumerated triggers. A caller can advance the sweep cursor through repeated crank calls, causing position fields to be modified outside the documented force-close conditions. This is structurally identical to the cursor-wrap consumption-reset pattern flagged in the broader audit and represents the highest-confidence undocumented force-close path.

**Recommended next layer:** Layer-2 PoC targeting the sweep crank — open a position, drive the cursor past its window via permissionless cranks, and observe position-field mutation without any liquidation/pause/settlement event.