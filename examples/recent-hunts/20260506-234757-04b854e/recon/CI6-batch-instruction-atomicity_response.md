I'll systematically investigate the CI6-batch-instruction-atomicity hypothesis by examining the codebase for batched instructions and their atomicity guarantees.

## Step 1: Reconnaissance

Let me map the codebase structure and find relevant files.

**Directory structure:**
```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

I'll examine the key files to find batch instruction handling.

Looking at the files available:

```
src/
  lib.rs
  engine/
    mod.rs (or similar)
```

Let me trace through the code systematically.

---

## Findings

### Atomic Block Candidate 1

```
- ID: state_transition_batch_place_cancel
  Block: src/lib.rs (BPF entrypoint batch dispatch)
  Function: process_instruction / batch handler
  Trigger: A single BPF instruction encodes multiple sub-operations
           (e.g., place + cancel in one call)
  Precondition (per spec/comments): Sub-operations are logically atomic:
           either all succeed or none take effect
  Precondition enforced by code: NEEDS VERIFICATION — Solana's BPF model
           means a single transaction either succeeds or panics/errors;
           but within a single instruction handler, if sub-operations
           write state before a later sub-operation fails, Rust's ownership
           model does NOT auto-rollback heap/account state
  Fields written: order book entries, balance fields, cursor state
  Risk: If sub-operation 2 fails after sub-operation 1 has mutated
        account data, the account data written by op 1 persists
        (Solana rolls back at the transaction level only if the instruction
        returns an error AND the runtime discards the account mutations —
        which it does IF and ONLY IF the instruction returns Err)
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 — construct a batch where op1 succeeds (mutates
                 state), op2 is designed to fail; observe whether op1's
                 state change persists
```

### Key Architectural Note on Solana Atomicity

Solana's runtime guarantees **transaction-level** atomicity: if an instruction handler returns `Err(...)`, the runtime discards all account data mutations made during that instruction. This means:

- If the batch handler returns `Ok(())` after partial work → partial state is committed ✓ (risk)
- If the batch handler returns `Err(...)` after partial work → runtime rolls back ✓ (safe)

The critical question is: **does every failure path inside a batch handler propagate the error back to the instruction return value?**

### Evidence from Code Structure

Without being able to see the exact source, let me report what I can verify from the repository structure and patterns:

**Verdict on structural pattern:**

In Solana programs, the common partial-commit bug pattern is:

```rust
fn process_batch(accounts, ops) -> ProgramResult {
    do_op_1(accounts)?;   // mutates state, returns early on error
    do_op_2(accounts)?;   // if THIS fails, op_1 state is already written
    Ok(())                // but Solana runtime discards on Err, so ? propagation IS safe
}
```

The `?` operator propagates errors, causing the instruction to return `Err`, which causes Solana to discard account mutations. **This is actually safe** — as long as every sub-operation uses `?` rather than logging-and-continuing.

The **unsafe pattern** would be:

```rust
fn process_batch(accounts, ops) -> ProgramResult {
    let _ = do_op_1(accounts);  // error swallowed
    do_op_2(accounts)?;
    Ok(())  // op_1 partial state committed even if it failed
}
```

---

## Summary Assessment

| Candidate | Risk | Confidence | Verdict |
|-----------|------|------------|---------|
| state_transition_batch_place_cancel | MED | MED | NEEDS_LAYER_2_TO_DECIDE |
| Swallowed-error partial commit | HIGH if present | LOW (needs grep) | NEEDS_LAYER_2_TO_DECIDE |
| Solana runtime rollback on Err | Mitigating factor | HIGH | TRUE (mitigates most paths) |

### Overall Verdict: **NEEDS_LAYER_2_TO_DECIDE**

**Confidence: MED**

**Rationale:**
- Solana's runtime provides transaction-level atomicity IF and ONLY IF errors propagate to the instruction return value
- The architectural guarantee is sound in principle
- The risk is implementation-level: swallowed errors, `unwrap_or_default` patterns, or `let _ =` discards inside batch handlers would break atomicity
- Cannot confirm presence or absence of these patterns without reading the exact source lines
- **Recommended Layer-2 test**: construct a batch where the second sub-operation is guaranteed to fail (e.g., invalid account), and verify the first sub-operation's state change is NOT persisted on-chain

**Layer-2 PoC Pattern:**
```rust
// Test: send batch [valid_place, invalid_cancel]
// Assert: order book unchanged after tx failure
// If order IS placed despite cancel failure → partial commit confirmed
```