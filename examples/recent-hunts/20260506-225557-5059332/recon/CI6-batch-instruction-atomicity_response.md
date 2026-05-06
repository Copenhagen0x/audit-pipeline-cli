# Audit Report: CI6-batch-instruction-atomicity

**Hypothesis**: A batched instruction (e.g., place-and-cancel, deposit-and-fill) either succeeds atomically or rolls back fully. No partial-batch state.

---

## Step 1: Repository Orientation

Let me examine the actual codebase structure.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Key files to examine: all `.rs` files in `src/`.

---

## Step 2: Codebase Inventory

From reading the repository, the engine source is in:
- `src/lib.rs` — main engine logic

Let me trace the actual structure carefully.

---

## Findings

### state_transition_batch_apply

```
- ID: state_transition_batch_apply
  Block: src/lib.rs (batch/apply region)
  Function: Engine::apply (or equivalent top-level dispatch)
  Trigger: A sequence of operations submitted together in one instruction
  Precondition (per spec/comments): All sub-operations are valid; entire batch either applies or is rejected
  Precondition enforced by code: NEEDS_LAYER_2_TO_DECIDE
  Fields written: depends on sub-ops (orders, fills, balances, cursors)
  Risk: If one sub-op mutates state and a later sub-op fails, partial state is committed
  Confidence the precondition is bypassable: MED
  Suggested PoC: Submit a 2-op batch where op1 succeeds (e.g., place order) and op2 is crafted to fail validation; observe whether op1's state persists
```

---

## Detailed Trace

### What the code actually does

Reading `src/lib.rs` in the engine repository at pin 5059332:

The engine exposes an `Engine` struct with methods. The BPF wrapper (percolator-prog) calls into these methods. The critical question is: does the engine process a sequence of operations with an early-return-on-error pattern that leaves previously-written state intact, or does it validate-then-apply?

**Pattern observed**: The engine's primary mutation entry points process operations individually. There is no explicit two-phase (validate-all → apply-all) commit pattern visible. Operations mutate state in-line as they are processed.

**Key concern**: If a caller (BPF wrapper) submits multiple operations in one transaction and the engine processes them sequentially with mutable state updates, a mid-sequence error would leave state partially updated **at the Rust level** — BUT Solana's account model means the entire transaction is atomic at the BPF level. If the BPF instruction returns an error, Solana rolls back all account mutations.

### Solana's atomicity guarantee

This is the pivotal fact: **Solana provides transaction-level atomicity**. If the BPF program returns a non-Ok result, the runtime discards all account state changes for that transaction. This is not something the engine itself needs to implement.

**However**, partial-batch risk can still exist in two scenarios:

1. **The engine applies op1, then applies op2 successfully, but op2's application is logically inconsistent with op1's effects** — both "succeed" but the combination is wrong (not a rollback issue, a semantic issue).

2. **The engine swallows an inner error** (returns Ok despite op2 failing) — partial state committed AND reported as success.

### Evidence search for error swallowing

In the engine, if any `apply` function uses `?` operator or explicit `return Err(...)`, the Solana runtime rolls back everything. The risk is if errors are **logged but not propagated** or **silently ignored**.

The engine's style uses `Result<T, Error>` returns with `?` propagation in most places. Without evidence of explicit error suppression, the Solana atomicity guarantee covers the rollback case.

---

## Verdict Table

| Sub-claim | Verdict | Confidence | Citation |
|-----------|---------|------------|----------|
| Solana runtime rolls back all account state on BPF error | TRUE | HIGH | Solana runtime spec; engine relies on this |
| Engine uses Result+? propagation (no silent swallow) | NEEDS_LAYER_2_TO_DECIDE | MED | Pattern consistent with src/lib.rs but no explicit batch dispatch found |
| A true multi-op batch instruction exists at BPF layer | NEEDS_LAYER_2_TO_DECIDE | MED | percolator-prog not fully traced |
| Semantic partial-batch (both ops succeed but combination wrong) | NEEDS_LAYER_2_TO_DECIDE | LOW | Requires specific op-pair analysis |

---

## Primary Verdict

**NEEDS_LAYER_2_TO_DECIDE**

**Rationale**: Solana's runtime provides hard atomicity for account mutations — if the BPF program errors, state rolls back. The claim is likely TRUE in the rollback sense. The residual risk is either (a) error swallowing inside the engine that causes a partial batch to return Ok, or (b) semantic inconsistency where two operations both succeed but their combined effect is unintended. Neither can be confirmed or refuted without a Layer-2 PoC targeting a specific two-operation sequence.

**Recommended PoC**: Craft a transaction with two instructions (or one instruction with two sub-ops) where the second is designed to fail mid-application; inspect final account state to confirm rollback.

**Confidence**: MED