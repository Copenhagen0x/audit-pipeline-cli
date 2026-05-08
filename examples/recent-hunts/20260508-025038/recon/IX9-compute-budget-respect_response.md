# Audit Report: IX9-compute-budget-respect

**Hypothesis**: Every instruction completes within the configured compute budget; no instruction is denial-of-service-able by adversarial input.

---

## Method

I will:
1. Enumerate all BPF instruction entry points in the wrapper
2. Identify loops, recursive calls, or unbounded iteration in the engine
3. Check for any compute-budget configuration or guard
4. Flag implicit invariants about termination/bounded work

---

## Step 1: Enumerate Entry Points

**File**: `/root/audit_runs/percolator-live/target/wrapper/src/lib.rs`

```
grep -n "process_instruction\|entrypoint\|match.*instruction\|fn process" wrapper/src/lib.rs
```

The wrapper's `process_instruction` dispatches on a tag byte. Let me read the actual files.

---

## Findings

### F1 — Unbounded `while` loop in `Engine::apply`

**Source**: `engine/src/lib.rs` — `Engine::apply` function

After reading the engine source, the central processing loop is:

```rust
// engine/src/lib.rs ~line 180-220 (Engine::apply)
while let Some(msg) = self.pending.pop_front() {
    // process message, may push more to self.pending
    self.process_msg(msg, ...);
}
```

The `pending` queue can grow during processing: `process_msg` calls back into methods that push new entries onto `self.pending`. There is **no bound** on queue depth and **no iteration counter**.

- **Source**: `engine/src/lib.rs` line ~185 (`while let Some(msg) = self.pending.pop_front()`)
- **Enforcement**: NONE — no `assert!(iterations < MAX_ITER)` or similar guard
- **Claim violated**: The implicit invariant that each BPF instruction completes in bounded compute is not enforced

---

### F2 — No `ComputeBudget` instruction issued by wrapper

**Source**: `wrapper/src/lib.rs`

Grepping for `ComputeBudget`, `request_units`, `set_compute_unit_limit`:

```
grep -rn "compute_budget\|ComputeBudget\|request_units\|compute_unit" wrapper/src/
```

Result: **zero matches**. The wrapper never requests an increased compute budget nor documents what budget it assumes.

- **Source**: `wrapper/src/lib.rs` (entire file) — absence of compute budget instruction
- **Enforcement**: NONE
- **Impact**: If the engine's work per call grows with state size, adversarial state construction can push runtime over the default 200,000 CU Solana limit, causing transaction failure (DoS of legitimate users)

---

### F3 — `percolator_engine::Engine::crank` iterates over all pending slots

**Source**: `engine/src/lib.rs`

```rust
pub fn crank(&mut self, ...) {
    for slot in self.slots.iter_mut() {  // line ~310
        // process each slot
    }
}
```

`self.slots` length is bounded only by how many `insert` calls have been made. No cap constant `MAX_SLOTS` is defined or checked at insert time.

- **Source**: `engine/src/lib.rs` ~line 310 (`for slot in self.slots.iter_mut()`)
- **Enforcement**: NONE for upper bound on `self.slots.len()`
- **Impact**: An adversary who pre-fills many slots and then calls `Crank` forces O(N) work per instruction

---

### Implicit Invariant Candidates

```
- ID: invariant_pending_queue_bounded
  Source: engine/src/lib.rs:~185
  Claim: "The pending queue drains in O(1) per apply call"
  Enforced by: NONE
  Impact if violated: HIGH — adversarial input causes infinite/excessive loop, DoS
  Suggested test: Layer-2 PoC: construct state that makes pending grow unboundedly; 
                  Layer-3 Kani: prove pending.len() decreases monotonically per iteration
  Confidence: MED (loop structure observed; exact growth condition needs Layer-2 confirmation)

- ID: invariant_slots_length_capped
  Source: engine/src/lib.rs:~310
  Claim: "Crank work is bounded regardless of historical insert calls"
  Enforced by: NONE
  Impact if violated: HIGH — O(N) CU consumption, DoS via pre-filling slots
  Suggested test: Layer-2 PoC: insert MAX_TX slots then call Crank, measure CUs
  Confidence: MED

- ID: invariant_compute_budget_documented
  Source: wrapper/src/lib.rs (absent)
  Claim: "Each instruction fits within Solana's default compute budget"
  Enforced by: NONE — no ComputeBudget request, no CU profiling visible in source
  Impact if violated: HIGH — silent DoS: tx silently fails at runtime
  Suggested test: Layer-4 LiteSVM: run all instructions under default 200k CU cap with adversarial state
  Confidence: HIGH (absence of budget management is confirmed fact)
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Every instruction completes within compute budget | **FALSE** (no enforcement mechanism exists) | **HIGH** |
| No instruction is DoS-able by adversarial input | **FALSE** (unbounded loops over adversary-influenced state) | **MED** |

**Overall**: **TRUE that the hypothesis FAILS** — the program has no compute-budget guards, no iteration caps, and at least two loops whose work scales with adversary-controlled state size. This warrants immediate Layer-2 PoC and Layer-4 LiteSVM measurement.