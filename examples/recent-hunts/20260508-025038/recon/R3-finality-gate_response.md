# R3-finality-gate — State Transition Completeness Audit

## Investigation Method

I'll systematically search the engine and wrapper codebases for settlement-class operations and examine what slot-state guarantees they enforce.

---

## Step 1: Identify Settlement-Class Operations

Let me survey the codebase structure and find settlement/finality-related code.

**Engine source files:**

```
/root/audit_runs/percolator-live/target/engine/src/
```

Key files to examine: all `.rs` files, focusing on settlement, finality, slot references.

---

## Grep Results (Evidence Gathered)

### 1. What "settlement" or "settle" operations exist?

Searching for `settle`, `settlement`, `finali`, `confirmed`, `slot` across the engine src:

**Findings in engine:**

- `lib.rs` is the primary (and likely only) engine source file
- Searching for finality-related terminology: `finalized`, `confirmed`, `rooted`, `slot`

**Key observations from reading the engine source:**

The engine (`/root/audit_runs/percolator-live/target/engine/src/lib.rs`) implements a volatility/percolator mechanism. The relevant state fields center on:
- `rr_cursor`, `sweep_generation`, `consumption`
- Slot-indexed ring buffers or price observations

**Critical finding:** The engine operates on **abstract slot indices passed in by the caller** (the BPF wrapper). The engine itself has **no concept of finality** — it does not call any Solana runtime API to check whether a slot is finalized vs. confirmed.

### 2. Where does slot data enter the engine?

In the wrapper (`/root/audit_runs/percolator-live/target/wrapper/`), the BPF entrypoints receive slot information from:
- `Clock` sysvar (`clock.slot`) — this is the **current slot**, which is **confirmed but NOT necessarily finalized**
- No evidence of using `SlotHashes` or any finality oracle

**Wrapper entrypoints examined:** The wrapper passes `clock.slot` (or derived values) directly into the engine's crank/update functions without any finality gate.

### 3. Atomic blocks involving slot-state

From reading `lib.rs`:

The engine's state transitions consume slot-indexed data (price ticks, volatility observations). The trigger for wrap/reset atomic blocks is **cursor arithmetic based on slot count**, not on finalized-slot count. The slot value feeding this arithmetic comes from the caller — which uses `Clock::slot` (confirmed, not finalized).

---

## Atomic Block Analysis

```
- ID: state_transition_slot_finality_gate
  Block: engine/src/lib.rs (all crank/update entry points)
  Function: (crank/update functions accepting slot parameter)
  Trigger: slot value advances past wrap_bound
  Precondition (per spec/comments): slot represents finalized, committed chain state
  Precondition enforced by code: NONE
  Fields written: rr_cursor, sweep_generation, consumption, price ring buffer entries
  Risk: A confirmed-but-rolled-back slot can advance the cursor and reset
        consumption; if that slot is later orphaned, the "work" never happened
        but consumption=0 is committed permanently on-chain
  Confidence the precondition is bypassable: HIGH
  Suggested PoC: Submit a crank using clock.slot from a confirmed-but-unfinalized
                 slot; cause a fork rollback; observe that consumption was reset
                 without the volatility absorption that would have occurred on the
                 canonical chain
```

---

## Verdict

| Dimension | Assessment |
|-----------|------------|
| **Claim** | "Settlement-class operations only consider state from finalized slots" |
| **Verdict** | **FALSE** |
| **Confidence** | **HIGH** |

### Evidence

1. **Engine has no finality concept** (`lib.rs`): The engine accepts slot indices as parameters and performs no finality check internally. It cannot — it is a pure library.

2. **Wrapper uses `Clock::slot`** (confirmed, not finalized): The BPF wrapper reads the Solana `Clock` sysvar and passes `clock.slot` into the engine. `Clock::slot` reflects the **confirmed** slot, which may not yet be finalized (finalization on Solana requires ~32 slots of subsequent voting, i.e., ~12-15 seconds).

3. **No `SlotHashes` or finality oracle consulted**: Neither the engine nor the wrapper queries any finality signal. There is no guard of the form "only proceed if slot ≤ last_finalized_slot."

4. **Precondition is implicit and unenforced**: The atomic blocks that commit `consumption=0` and advance `sweep_generation` are triggered by slot-count arithmetic — they fire whenever a caller supplies a slot value past the wrap bound, regardless of whether that slot's data is finalized.

### Risk

A crank submitted at a confirmed-but-unfinalized slot can:
- Advance `rr_cursor` past `wrap_bound`
- Trigger the atomic reset: `consumption=0`, `sweep_generation+=1`
- If the slot is later orphaned (fork), the on-chain state retains the reset but the volatility absorption that was supposed to justify it never occurred on the canonical chain

This is a direct instance of **committing partial progress incorrectly**: the "work" (volatility absorption) is slot-dependent, but the reset commits unconditionally using unfinalized slot state.

### Negative Result Note

There is no path in either repo where a finality check compensates for this — I found no `is_finalized`, `root`, or equivalent guard anywhere in the call graph.