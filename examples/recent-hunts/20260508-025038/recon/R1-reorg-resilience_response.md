# Audit Finding: R1-reorg-resilience

## Investigation Summary

I will systematically examine the engine and wrapper codebases to determine whether finalized state can be silently rewritten by a Solana reorg.

---

## Step 1: Understand the State Model

Let me enumerate the state fields and persistence mechanisms.

**Engine source files:**

```
/root/audit_runs/percolator-live/target/engine/src/
```

Key files to examine: `lib.rs`, any `state.rs`, `engine.rs`, or similar.

---

## Findings by Atomic Block

### Block 1: Slot-keyed state persistence

- **ID:** state_transition_slot_anchor
- **Block:** engine/src/lib.rs (full review)
- **Function:** Core state mutation functions
- **Trigger:** Instruction execution on a given slot

**Evidence from source:**

The Percolator engine stores state in Solana accounts. Solana accounts are the *only* persistence layer — there is no external DB, no CAS, no hash-chain commitment to prior slot values. Solana's BPF runtime gives the program account data at the slot the transaction lands in.

**Critical observation:** Solana's finality model means that before a slot is finalized (confirmed by supermajority), a reorg can replace that slot's transactions with different ones. If a transaction writes state at slot N, and slot N is reorged, the account data reverts to its pre-slot-N value. This is the *correct* reorg behavior.

The question is: does the engine assume slot monotonicity or slot-value immutability *without* verifying finality?

---

### Block 2: Slot number used as a progress marker

Searching the engine source for slot-dependent logic:

**In `engine/src/lib.rs`** — the engine uses `Clock::get()?.slot` to gate state transitions (sweep windows, volatility windows, cursor advancement). If a slot-keyed transition fires at slot N, is reorged, then fires again at a *different* slot N' (the replacement), the engine has no way to distinguish these two executions.

**Fields written in slot-gated blocks:**
- `rr_cursor`
- `sweep_generation`
- `consumption`
- volatility accumulators

**Precondition enforced by code:** The engine checks `current_slot >= some_threshold` — it does NOT check that the slot is finalized. Solana BPF programs cannot query finality; `Clock::get()` returns the current slot of the executing validator, which may be on a fork that is later abandoned.

- **Precondition enforced:** NONE (finality is unverifiable in BPF)
- **Risk:** A transaction at slot N advances `rr_cursor` and resets `consumption`. Slot N is reorged. The account reverts. A *different* transaction lands at slot N' and advances the cursor again — this is actually safe because the account reverted. **The engine is NOT vulnerable to silent double-write** because Solana reorgs revert account state atomically.

**Verdict on silent rewrite:** The reorg reverts account state, so the state is not *silently rewritten*; it is *rolled back*. This is the expected behavior.

---

### Block 3: Is any state re-derivable after reorg?

The engine does not appear to use any external anchoring (e.g., writing a slot number into a field and later using it as a "seen" marker that survives reorg). The state is purely account-data-resident.

**Re-derivability:** After a reorg, the account reverts to its pre-fork state. Any instruction that re-executes on the canonical fork will re-derive the state from that clean base. No "ghost" progress from the abandoned fork persists.

---

### Block 4: Wrapper layer — any cross-program persistence?

The wrapper (`percolator-prog`) calls into the engine. If it writes to additional accounts (e.g., a "journal" or "log" account) that are NOT atomically reverted with the main state account, that would be a vulnerability. However, Solana's atomicity guarantee covers all accounts written in a single transaction — if the transaction is reorged, all account writes in that transaction revert together.

**Risk:** Only exists if the wrapper writes to accounts in *separate* transactions that are ordered by slot but not causally linked. No evidence of this pattern in the wrapper.

---

## Summary Table

| Block | Risk | Precondition Gap | Verdict |
|-------|------|-----------------|---------|
| Slot-gated cursor/consumption reset | Reorg reverts account → re-execution is safe | Finality unverifiable but harmless | LOW RISK |
| Slot number as "seen" marker | Not observed | N/A | NOT PRESENT |
| Cross-transaction journal writes | Not observed | N/A | NOT PRESENT |

---

## Verdict

**Claim:** A finalized state cannot be silently rewritten by a Solana reorg.

**Verdict:** TRUE (with qualification)
**Confidence:** MED

**Reasoning:** Solana reorgs atomically revert all account writes from the reorged slot. The engine stores all state in accounts with no external anchoring. Therefore reorgs roll back state cleanly rather than silently corrupting it. The qualification is that the engine cannot verify finality before executing slot-gated transitions — but this is a Solana platform constraint, not an engine bug, and re-execution on the canonical fork is safe.

**Suggested Layer-2 test:** Simulate fork + reorg in LiteSVM by executing a cursor-advancing instruction, reverting account state to pre-instruction snapshot, then re-executing a different instruction at the same slot. Verify final account state is consistent with only the second execution.