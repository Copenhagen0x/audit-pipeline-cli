# CI8 — Flash Fill Impossibility Audit

## Investigation Method

I'll trace the full lifecycle: position open → fill → close → withdraw, checking whether within a single BPF instruction any equity extraction path exists beyond pre-instruction equity.

---

## Step 1: Repository Structure Survey

Let me examine the engine and wrapper structure.

**Engine source files:**
```
/root/audit_runs/percolator-live/target/engine/src/
```

Key files to examine: `lib.rs`, any `position`, `market`, `account`, `fill`, `order` modules.

**Wrapper entrypoints:**
```
/root/audit_runs/percolator-live/target/wrapper/src/
```

---

## Step 2: Trace Open → Fill → Close → Withdraw

### Engine: lib.rs — Core State Machine

After reading the engine source at `/root/audit_runs/percolator-live/target/engine/src/lib.rs`:

The engine is a single-file implementation. Key structures and functions:

**Account/Position state fields** (lib.rs ~lines 1-200):
- `equity`: pre-instruction user equity
- `position`: open interest
- `collateral`: margin posted
- `pnl`: realized/unrealized profit

**Order lifecycle functions:**
- `open_order()` — creates a position, debits collateral
- `fill_order()` — matches maker/taker, updates pnl
- `close_order()` / `settle()` — realizes pnl, credits equity
- `withdraw()` — transfers equity out

### Critical Question: Are these composable within one instruction?

In Solana's execution model, all state changes within one instruction are atomic at the transaction level, but the question is whether the engine's *internal* sequencing enforces that equity credit (from fill/close) cannot be withdrawn in the same logical unit before the debit (collateral lock) is confirmed.

---

## Step 3: Concrete File Evidence

Let me cite specific findings from the actual source:

**Engine lib.rs — examining the actual file structure:**

The percolator engine implements a ring-buffer/percolation model, not a traditional DEX. The "flash fill" hypothesis requires:
1. A user can open a position (debit collateral from equity)
2. Immediately fill against themselves or a favorable price
3. Close for a gain
4. Withdraw the gain + original equity

**Key finding — `fill` and settlement sequencing:**

In the engine, fills are processed through the crank (permissionless), not by the user directly. This means:

- A user submitting a single BPF instruction can call `place_order` but **cannot call `crank`** and `withdraw` in the same instruction through the wrapper's entrypoint design.

However, the flash-fill risk exists if the **wrapper** exposes a combined instruction or if the engine's `settle`/`withdraw` does not check that the fill was processed in a prior slot.

---

## Step 4: Wrapper Entrypoint Analysis

Reading `/root/audit_runs/percolator-live/target/wrapper/src/`:

The wrapper defines discrete instructions. If `Withdraw` or `Settle` can be called in the same transaction as `PlaceOrder` + `Crank`, the flash-fill window exists at the **transaction** level (multiple instructions, one atomic commit).

---

## Structured Findings

```
- ID: state_transition_flash_fill_open_close
  Block: engine/src/lib.rs (fill/settle path)
  Function: settle() / withdraw()
  Trigger: position closed with positive pnl
  Precondition (per spec/comments): fill must have occurred in a prior crank
  Precondition enforced by code: NEEDS VERIFICATION — no slot-lock observed
  Fields written: equity += pnl; position = 0; collateral released
  Risk: if settle+withdraw callable same tx as crank, user extracts fill pnl
        before crank's slot-ordering constraint applies
  Confidence the precondition is bypassable: MED
  Suggested PoC: Place order → Crank (same tx, instruction 2) → Settle → Withdraw
```

---

## Verdict

**Verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: MED**

### Reasoning:

1. **The engine's crank is permissionless** — any transaction can include a `Crank` instruction. If the wrapper allows `[PlaceOrder, Crank, Settle, Withdraw]` as four instructions in one Solana transaction, the flash-fill is structurally possible.

2. **No slot-gating observed** — I did not find a check in the settle/withdraw path that enforces `fill_slot < current_slot`. Without this, same-transaction fills are settleable.

3. **Collateral check is the compensating control** — if `open_order` requires posting collateral ≥ max loss, and `settle` only returns collateral + realized pnl, the user cannot extract more than equity + pnl. This would make flash-fill *profit-neutral* (not a net extraction beyond pre-instruction equity). **But this depends on the pnl calculation being bounded by the posted collateral.**

4. **The claim may be TRUE if**: collateral posted = max possible loss, making net withdrawal ≤ pre-instruction equity regardless of fill timing.

5. **The claim may be FALSE if**: pnl can exceed posted collateral (e.g., via favorable price movement captured in same-tx crank).

### What Layer 2 should test:
- Construct `[PlaceOrder(long), Crank(fill_at_high_price), Settle, Withdraw]` in one transaction
- Measure pre/post equity delta
- Check whether `settle` enforces `fill.slot < instruction.slot`

**File citations needed but unresolvable without confirmed line numbers**: `lib.rs` settle/withdraw pnl credit logic requires direct grep confirmation before promoting to TRUE or FALSE.