# Audit Report: R1-reorg-resilience

## Hypothesis
A finalized state cannot be silently rewritten by a Solana reorg. Any reorg-affected state is either re-derivable or explicitly flagged.

---

## Step 1: Repository Survey

Let me examine the actual codebase structure.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

### Files found:

- `lib.rs` — BPF entrypoints / wrapper
- `engine.rs` — core state machine (primary target)
- Any additional modules

Let me trace the key state fields and how slot/block identity is recorded.

---

## Step 2: Evidence Gathering

### 2a. Slot anchoring — how does the engine record "which slot produced this state"?

Searching for slot tracking in engine state:

**engine.rs** — key observations:

The engine tracks state via fields like `sweep_generation`, `rr_cursor`, `consumption`, and related counters. Critically, the engine does **not** store the Solana slot number in any persistent state account field as an anchor. The state machine advances based on **call counts and cursor arithmetic**, not on verified slot finality.

From `lib.rs` (BPF wrapper): the instruction handlers call into the engine passing `Clock` sysvar data (slot, epoch), but the engine's state struct does not record `last_committed_slot` or any slot hash as a commit anchor.

### 2b. What does a Solana reorg actually do to this program?

On a Solana reorg (fork switch), transactions on the abandoned fork are **not replayed** on the canonical fork. Any state mutations committed in those transactions persist only on the abandoned fork's accounts. The canonical fork's account state simply never received those writes.

The question is: does the engine leave any "partial progress" footprint that assumes the abandoned-fork transactions happened?

### 2c. Cursor and consumption state — reorg attack surface

The most sensitive state transitions (per the prompt's Bug #1 pattern) are:

```
- ID: state_transition_cursor_wrap
  Block: engine.rs (cursor wrap / sweep generation increment)
  Trigger: rr_cursor >= wrap_bound (call-count arithmetic)
  Fields written: rr_cursor=0, sweep_generation+=1, consumption=0
```

**Reorg scenario**: Suppose crank transactions on fork A advance `rr_cursor` from 900 → 1000 (wrap), triggering `consumption=0` and `sweep_generation+=1`. Fork A is orphaned. The canonical fork B never saw those cranks. Fork B's account state still has `rr_cursor=900`, `sweep_generation=N`, `consumption=X`. **No partial commit problem from reorg** — the account on fork B is simply at the pre-reorg state.

This is Solana's account model working correctly: account state is per-fork until finalized.

### 2d. Is there any cross-account or cross-instruction ordering that reorg could corrupt?

The dangerous pattern would be: instruction I₁ writes account A, instruction I₂ reads account A and writes account B, and a reorg orphans I₁ but not I₂. On Solana, this is impossible within a single program's accounts **because both transactions are on the same fork** — either both are canonical or both are orphaned.

However, if the program **reads an external oracle or CPI result** that was written in a prior transaction on the same fork, and that prior transaction gets orphaned while the consuming transaction somehow lands on the canonical fork — this would be a cross-fork dependency. Reviewing `lib.rs` for CPI calls or oracle reads that are not re-validated at instruction time.

**Finding**: The engine does not appear to CPI into other programs or read oracle accounts whose finality is separately tracked. State is self-contained within the engine's own accounts.

### 2e. Slot-based time windows — is `Clock::slot` used for expiry logic?

If the engine uses `slot` from `Clock` to gate time windows (e.g., "volatility window = 100 slots"), a reorg that re-uses slot numbers could fire a window boundary twice. **Checking**: The engine uses call-count cursor arithmetic (`rr_cursor`) rather than slot-based windowing for its core loop. `Clock` data may be passed but does not appear to be stored as a commit anchor that could be silently overwritten.

---

## Step 3: Structured Findings

```
- ID: state_transition_reorg_slot_anchor
  Block: engine.rs (entire state struct)
  Function: all mutating functions
  Trigger: any crank/instruction call
  Precondition (per spec/comments): implicit — caller assumed to be on canonical fork
  Precondition enforced by code: NONE (no slot-hash or finality anchor stored)
  Fields written: rr_cursor, sweep_generation, consumption, and related
  Risk: LOW in practice — Solana account model isolates forks; no cross-fork
        partial commit is possible within a self-contained account set.
        Risk would be HIGH if external oracle accounts were consumed without
        re-validation, but no such pattern is present.
  Confidence the precondition is bypassable: LOW
  Suggested PoC: N/A — no exploitable path identified
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Finalized state cannot be silently rewritten by reorg | **TRUE** | **MED** |
| Reorg-affected state is re-derivable or explicitly flagged | **TRUE (by Solana account model, not by explicit engine logic)** | **MED** |

**Reasoning**: The engine stores no cross-fork dependency (no oracle CPI, no stored slot anchor that could be "wrong" after a reorg). Solana's fork-isolated account model means abandoned-fork writes simply never appear on the canonical fork. The engine does **not** add its own reorg protection, but it also does not need to given the self-contained account design. The absence of explicit reorg flagging is **not a bug** here — it would only become one if external oracle state were consumed non-idempotently.

**Residual risk (LOW)**: If a future instruction is added that reads an external account written in the same block, the lack of any finality anchor becomes a latent vulnerability. Recommend adding a `last_slot` field to the state struct as a defensive anchor.