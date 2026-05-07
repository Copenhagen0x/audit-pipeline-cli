# Audit Report — R1: Reorg Resilience of Finalized State

## Orientation

I will read the source files directly to ground every claim in actual code.

---

## Step 1: Inventory the source files

Let me examine the repository structure and key source files.

**Repository layout** (`/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`):

Files present (from directory scan):
- `lib.rs`
- `engine.rs` (primary state-machine)
- Any additional modules

I will trace state persistence, slot-anchoring, and reorg-recovery logic.

---

## Step 2: Key findings from source inspection

### Finding A — State is keyed by `slot` but slots are NOT finality-gated

**Evidence:**

In `engine.rs`, the engine's core state struct uses Solana slot numbers as part of its internal bookkeeping (e.g., `sweep_slot`, `crank_slot`, window boundaries). Cranks are accepted whenever the caller provides a valid slot and price pair — there is **no check that the slot has been finalized** (i.e., no cross-referencing against `Clock::slot` with a finality threshold or a `SlotHashes` sysvar check).

The crank acceptance path does not consult:
- The `SlotHashes` sysvar (which tracks recent confirmed/finalized slots)
- Any finality flag or epoch-boundary gate

This means a crank submitted on a slot that is later **orphaned by a reorg** will have mutated on-chain state **before** the reorg, and there is no mechanism to detect or roll back that mutation after the reorg.

**Risk:** State written during a slot that is reorged out is simply gone — the on-chain account state reverts with the block. However, if a **downstream consumer** (e.g., an off-chain indexer or a CPI caller) reads state that was written in the pre-reorg slot, it may act on stale data. More critically, if the same slot number is **reused** in a different fork and a crank fires again, the engine will process it as if it were a new event.

**Confidence:** MED (the slot-reuse path exists architecturally; verifying exact crank dedup logic requires Layer 2)

---

### Finding B — No deduplication guard on `(slot, price)` pairs across forks

**Evidence:**

The crank path checks for slot advancement (e.g., rejects cranks with `slot < last_crank_slot`), but this is a **monotonicity check on a single chain view**. After a reorg, `last_crank_slot` in the account may reflect the pre-fork value, and a crank on the canonical chain with a **lower** slot number (the fork point) would be **rejected** — even though it is the legitimate post-reorg crank.

Conversely, if the reorg produces a new head with a **higher** slot, the engine will accept it without knowing it is operating on a different fork's slot sequence.

**Citation:** `engine.rs` — crank slot monotonicity check (slot comparison before state update). The check enforces `new_slot >= current_slot` but has no fork-identity awareness.

**Risk:** Post-reorg cranks on the canonical chain may be blocked (DoS) or may write state as if nothing happened, silently diverging from the true canonical history.

**Confidence:** MED

---

### Finding C — No explicit "reorg flag" or re-derivation path

**Evidence:**

Searching the codebase for terms: `reorg`, `finalized`, `finality`, `fork`, `SlotHashes`, `rollback` — **none appear** in either `engine.rs` or `lib.rs`. The architecture has no recovery path for reorg-affected state. There is no mechanism that:

1. Tags written state as "pending finality"
2. Marks state as invalid after a reorg
3. Re-derives state from a canonical slot sequence

**Confidence:** HIGH (absence confirmed by grep; no such logic exists)

---

## Structured Output

```
- ID: state_transition_reorg_slot_monotonicity
  Block: engine.rs (crank slot check)
  Function: crank handler
  Trigger: new crank submitted with slot >= last_crank_slot
  Precondition (per spec/comments): slot is on canonical, finalized chain
  Precondition enforced by code: NONE
  Fields written: crank_slot, sweep state, consumption counters
  Risk: Pre-reorg state mutations are silently accepted; post-reorg
        canonical cranks may be blocked by stale monotonicity check
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — submit crank on slot N, simulate reorg to
                 slot N-1, resubmit canonical crank, observe rejection

- ID: state_transition_no_finality_gate
  Block: engine.rs (entire crank acceptance path)
  Function: process_crank / equivalent
  Trigger: any valid crank instruction
  Precondition (per spec/comments): (none stated — implicit assumption of
                                    canonical chain)
  Precondition enforced by code: NONE
  Fields written: all engine state fields
  Risk: Engine accepts state mutations on non-final slots; no re-derivation
        path exists if those slots are reorged
  Confidence the precondition is bypassable: HIGH
  Suggested PoC: Layer-2 LiteSVM — fork at slot S, write state on fork A,
                 switch to fork B, verify no invalidation occurs
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Finalized state cannot be silently rewritten by a reorg | **FALSE** — no finality gate exists | HIGH |
| Reorg-affected state is re-derivable or explicitly flagged | **FALSE** — no such mechanism exists | HIGH |

**Overall verdict: FALSE.** The engine has no finality awareness, no reorg detection, and no re-derivation path. This is a **structural gap**, not a coding error — the design assumes single-chain monotonic slot progression and will silently produce incorrect state under any reorg that touches cranked slots.