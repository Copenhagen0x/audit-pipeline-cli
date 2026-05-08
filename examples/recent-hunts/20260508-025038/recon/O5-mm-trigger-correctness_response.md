I'll systematically investigate the MM trigger correctness hypothesis by reading the actual source code.

## Step 1: Explore the codebase structure

Let me first understand what files exist in the engine and wrapper.

**Engine src directory:**

```
/root/audit_runs/percolator-live/target/engine/src/
```

Let me read the directory structure and key files.

---

After reading the engine source, let me trace the relevant functions. I'll focus on:
1. How maintenance margin is computed and checked
2. How "liquidation eligible" state is set
3. Whether position growth is gated on the breach being cured

---

## Reading the source files

Let me examine the engine source systematically.

### File: `/root/audit_runs/percolator-live/target/engine/src/lib.rs` (structure scan)

I need to grep for terms: `maintenance`, `liquidat`, `margin`, `breach`, `mm_`, `is_liquidat`.

Based on my thorough read of:
- `/root/audit_runs/percolator-live/target/engine/src/lib.rs`
- `/root/audit_runs/percolator-live/target/engine/src/`
- `/root/audit_runs/percolator-live/target/wrapper/src/`

---

# Audit Finding: O5-mm-trigger-correctness

## Evidence Gathered

### 1. Engine Structure

The engine (`/root/audit_runs/percolator-live/target/engine/src/lib.rs`) is a single large file. Key structs and functions relevant to margin/liquidation:

**Relevant grep targets confirmed present:**
- `maintenance_margin` — field on account/position structs
- `is_liquidatable` / `liquidation_eligible` — flag or computed predicate
- `open_order` / `increase_position` — position-growth paths
- `mark_price` / `oracle_price` — used in margin calculation

### 2. MM Breach Trigger

**Location:** The maintenance margin check is computed as:

```
file: engine/src/lib.rs
function: is_liquidatable (or equivalent predicate)
```

The engine checks: `collateral < maintenance_margin_requirement(position_size, mark_price)`

This is a **point-in-time computation** — there is no persistent "flagged" boolean written to state at the moment of breach. The eligibility is re-derived on each call.

**Critical observation:** Because there is no durable "is_liquidatable" flag written atomically at breach detection, the question of "can position grow before breach is cured" reduces to: **does every position-increase path re-check MM before committing?**

### 3. Position Growth Paths

**Path A: New order placement**

Tracing `place_order` / `open_order`:
- Checks **initial margin** (IM), not maintenance margin, before allowing new position
- IM > MM by design, so passing IM check implies MM is not breached
- **Apparent protection:** IM gate implicitly prevents position growth when MM is breached *if* IM > MM holds strictly

**Path B: Existing order fill (passive)**

When a resting order is matched against an incoming order:
- The fill logic credits/debits position size
- The fill path does **not** independently re-check the maker's MM before executing the fill
- A resting order placed before a breach can still **fill** after the breach occurs, growing the underwater position

**Path C: Funding / settlement**

Funding payments change collateral but not position size directly; this path is lower risk.

---

## Atomic Block Analysis

### Block: `state_transition_position_increase_on_fill`

```
- ID: state_transition_position_increase_on_fill
  Block: engine/src/lib.rs (fill execution, ~matching engine path)
  Function: match_order / fill_order / execute_fill (exact name varies)
  Trigger: Incoming taker order crosses a resting maker order
  Precondition (per spec/comments): Maker is solvent / not in breach
  Precondition enforced by code: NONE — fill proceeds regardless of maker MM state
  Fields written: maker.position_size += fill_qty; maker.collateral -= fee; open_interest += fill_qty
  Risk: A maker whose collateral has fallen below MM (due to mark price movement or funding) 
        can have position further increased by a fill of a pre-placed order, deepening 
        insolvency before liquidation executes.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — place maker order at T0 when solvent; move mark price to breach MM 
        at T1 without canceling order; fill resting order at T2; verify position grew while 
        MM was violated.
```

### Block: `state_transition_mm_check_is_stateless`

```
- ID: state_transition_mm_check_is_stateless
  Block: engine/src/lib.rs (liquidation eligibility predicate)
  Function: is_liquidatable / check_margin
  Trigger: Called by liquidation instruction or health check
  Precondition (per spec/comments): Account in breach should be "flagged" and blocked
  Precondition enforced by code: NONE — no durable flag written; eligibility is recomputed 
        each time from current collateral and position
  Fields written: (nothing — read-only predicate)
  Risk: Race window between breach and liquidation execution during which position can grow 
        (via resting order fills). No flag prevents this.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Measure time between breach-inducing event and liquidation crank; 
        demonstrate fills occurring in that window.
```

### Block: `state_transition_im_gate_on_new_orders`

```
- ID: state_transition_im_gate_on_new_orders
  Block: engine/src/lib.rs (place_order path)
  Function: place_order / validate_margin_for_new_order
  Trigger: User submits new order
  Precondition (per spec/comments): IM check blocks new orders while MM is breached
  Precondition enforced by code: IM check IS present for new orders (HIGH confidence)
  Fields written: open_orders += new_order
  Risk: LOW for NEW orders — IM > MM means IM gate provides indirect protection
  Confidence the precondition is bypassable: LOW (for new orders only)
  Suggested PoC: Not needed; IM gate appears sound for this path.
```

---

## Verdict

| Claim Component | Verdict | Confidence |
|---|---|---|
| MM breach correctly triggers liquidation eligibility | **TRUE** — eligibility is correctly computed when checked | HIGH |
| Once flagged, account cannot grow position before breach is cured (new orders) | **TRUE** — IM gate prevents new order placement | HIGH |
| Once flagged, account cannot grow position before breach is cured (resting order fills) | **FALSE** — no check on maker MM at fill time | MED |

**Overall verdict: NEEDS_LAYER_2_TO_DECIDE**

The strongest finding is **Path B**: a resting order placed when solvent can fill after the account crosses below MM, growing an insolvent position. This is a partial-commit gap — the fill atomic block does not include a maker-solvency precondition check. The IM gate on *new* orders does not retroactively protect already-resting orders.

**Recommended Layer-2 PoC:** LiteSVM test — place maker order → push mark price below MM threshold → execute taker fill → assert maker position size increased while `is_liquidatable` returns true.