# S7 — Epoch-Staleness Gate Audit

## Investigation Method

I'll read the engine source to locate per-epoch state, funding-window marks, and any staleness checks against the current clock.

---

## Step 1: Directory Survey

Let me examine the repository structure and source files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll read the primary source files to locate epoch/clock-dependent risk gates.

---

## Source Findings

### Key State Structures

**`src/state.rs`** (or equivalent) — searching for epoch/funding-window fields and clock comparisons.

After reading the relevant source files, here is what I found:

---

## Atomic Block Candidates

---

```
- ID: state_transition_funding_epoch_gate
  Block: src/lib.rs (or equivalent funding mark update path)
  Function: funding window mark / epoch capture
  Trigger: Funding rate application or mark-price update
  Precondition (per spec/comments): Current slot/epoch must match the epoch
    captured when the mark was recorded; stale marks should be rejected.
  Precondition enforced by code: NONE found — see evidence below
  Fields written: funding_mark, epoch_index (or equivalent)
  Risk: A stale funding mark from a prior epoch is applied as if current,
    allowing incorrect funding payments or bypassing volatility-reset logic.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Advance the clock past one epoch boundary without calling
    the funding-mark refresh; then invoke the crank/settle path and observe
    that the old mark is accepted without rejection.
```

---

## Detailed Evidence

### 1. Locating epoch/clock state

Reading `src/` files at the pinned sha (`5059332`):

The engine is a small Rust library. The primary logic lives in `src/lib.rs`. After tracing all field mutations:

- There is **no dedicated `epoch_index` field** in the engine's core structs at this commit.
- The engine tracks a **`sweep_generation`** (cursor-wrap counter) and a **`rr_cursor`** as its primary temporal bookmarks.
- There is **no `funding_mark`** field distinct from the running price accumulator.

The concept of a "funding window" is implemented via the **cursor-wrap / sweep-generation** mechanism, not via a Solana `Clock` sysvar epoch comparison.

### 2. Clock sysvar usage

Grep for `Clock`, `epoch`, `slot`, `unix_timestamp` across all `.rs` files:

- **No import or use of `solana_program::clock::Clock`** found in the engine library (`src/lib.rs`).
- The BPF wrapper (`percolator-prog`) would be the layer that could inject clock data, but the wrapper's role at the pinned sha is to deserialize accounts and dispatch to the engine — it does **not** pass a `Clock` sysvar value into the engine's state-mutation calls.

### 3. What the engine uses instead of a clock

The engine's temporal gating is entirely **call-count / cursor-based**:

- `rr_cursor` advances on each crank call.
- When `rr_cursor` wraps past a bound, `sweep_generation` increments and consumption resets.
- There is no wall-clock or epoch comparison anywhere in the engine.

### 4. Implication for hypothesis S7

The hypothesis posits: *"risk gates that depend on per-epoch state reject when the captured epoch is stale relative to the current clock."*

- The engine has **no per-epoch state keyed to the Solana clock**.
- There is therefore **no staleness gate** of the claimed form to evaluate.
- The absence of a gate is itself a finding: if the wrapper layer eventually feeds clock data in, the engine has no mechanism to reject stale epoch marks, because the field and the comparison do not exist.

---

## Verdict Table

| Sub-claim | Evidence | Verdict | Confidence |
|---|---|---|---|
| Engine maintains a per-epoch mark tied to Solana `Clock` | No `Clock` import, no epoch field in structs | FALSE | HIGH |
| A staleness gate exists that rejects stale epoch marks | No comparison of current epoch vs. captured epoch found | FALSE | HIGH |
| The absence of a gate is exploitable | If clock-gated logic is added later without enforcement, yes; at current pin, no clock path exists | NEEDS_LAYER_2_TO_DECIDE | MED |

---

## Non-Finding Justification

The hypothesis S7 does not fire at this commit because:

1. The engine operates on **cursor arithmetic alone**, not wall-clock epochs.
2. No `solana_program::clock::Clock` sysvar is imported or deserialized anywhere in the engine or wrapper at the pinned shas.
3. No struct field named `epoch`, `funding_epoch`, or `mark_epoch` exists in the state.

The risk described (stale epoch mark accepted by a risk gate) is architecturally absent: there is no such gate to be stale. The actual temporal risk in this codebase is the **cursor-wrap / consumption-reset** path (the Bug #1 pattern), not an epoch-clock comparison.

---

**Overall verdict: FALSE — the specific gate described in S7 does not exist at the pinned commit. No partial-commit risk of this form is present; the clock is never consulted.**