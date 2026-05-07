# Audit Finding: IX8-replay-protection

## Investigation Summary

I'll systematically examine the percolator engine and wrapper for replay protection mechanisms across all state-mutating instruction paths.

---

## Step 1: Repository Structure Survey

Let me trace the actual files present and relevant state mutation points.

**Files examined:**
- `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`

Key files identified:
- `src/lib.rs` — engine core
- `src/state.rs` (if present)
- Wrapper: `percolator-prog` (separate repo, second path listed points to same clone)

---

## Step 2: Identify Instruction Entry Points and Credit/Debit Operations

Searching for functions that credit users, update balances, or process claims.

### Key findings from source grep:

**`src/lib.rs`** — The engine is a single-file library. Relevant state-mutating regions:

#### Atomic Block Candidates

---

```
- ID: state_transition_consume_credits
  Block: src/lib.rs (consumption/credit update regions)
  Function: Functions mutating `consumption`, `credits`, or balance fields
  Trigger: Crank or user instruction with a position/slot argument
  Precondition (per spec/comments): Slot/position has not been previously credited
  Precondition enforced by code: NEEDS VERIFICATION — see below
  Fields written: consumption counter, user credit balance
  Risk: Double-credit if same (user, slot, price) tuple can be submitted twice
  Confidence the precondition is bypassable: MED
  Suggested PoC: Submit identical crank transaction twice in same epoch; check credit balance doubled
```

---

```
- ID: state_transition_rr_cursor_wrap
  Block: src/lib.rs ~6149-6158 (per prior audit context)
  Function: sweep/crank handler
  Trigger: sweep_end >= wrap_bound (cursor arithmetic)
  Precondition (per spec/comments): wrap implies real volatility window expired
  Precondition enforced by code: NONE — cursor advances on call count, not on real work
  Fields written: rr_cursor=0, sweep_generation+=1, consumption=0
  Risk: consumption resets without work done; prior credits can be re-earned
  Confidence the precondition is bypassable: HIGH (previously confirmed Bug #1)
  Suggested PoC: Permissionless cranks at fixed (slot, price) to advance cursor, trigger wrap, re-credit
```

---

```
- ID: state_transition_claim_without_nonce
  Block: src/lib.rs (claim/settlement handler)
  Function: claim or settle function
  Trigger: User submits claim instruction referencing a settled position
  Precondition (per spec/comments): Position has not been claimed before
  Precondition enforced by code: CHECK REQUIRED — is a "claimed" bit/flag set atomically?
  Fields written: user balance += payout, position.claimed (maybe)
  Risk: If claimed flag not set before return, or set after balance update, re-entrant or replayed claim succeeds
  Confidence the precondition is bypassable: MED
  Suggested PoC: Submit claim instruction twice for same position; observe balance
```

---

```
- ID: state_transition_stale_generation_window
  Block: src/lib.rs (generation check on credit application)
  Function: credit application / crank
  Trigger: User submits instruction referencing sweep_generation N
  Precondition (per spec/comments): sweep_generation must match current; stale references rejected
  Precondition enforced by code: If generation check is absent or off-by-one, stale window exploitable
  Fields written: consumption, user credits
  Risk: If wrap resets consumption (Bug #1) AND old generation references remain valid, credits can be replayed across generation boundary
  Confidence the precondition is bypassable: MED (depends on generation validation strictness)
  Suggested PoC: Trigger wrap (cursor reset), then re-submit instruction from previous generation
```

---

```
- ID: state_transition_signer_replay_no_sequence
  Block: BPF wrapper entrypoints
  Function: Any permissionless instruction handler
  Trigger: Any valid signer submits instruction
  Precondition (per spec/comments): Solana's native deduplication (recent blockhash) provides replay protection at network level
  Precondition enforced by code: Solana tx dedup applies ONLY within ~150 slots; no per-instruction nonce visible in engine
  Fields written: All state fields touched by that instruction
  Risk: After ~150 slots, identical transaction can be resubmitted if market state unchanged (stale-state window)
  Confidence the precondition is bypassable: LOW-MED — requires specific timing, but engine adds no application-layer nonce
  Suggested PoC: Record a valid signed transaction, wait >150 slots, resubmit; observe if accepted
```

---

## Verdict Summary

| ID | Verdict | Confidence |
|----|---------|------------|
| state_transition_consume_credits | NEEDS_LAYER_2_TO_DECIDE | MED |
| state_transition_rr_cursor_wrap | TRUE (precondition gap confirmed by prior audit) | HIGH |
| state_transition_claim_without_nonce | NEEDS_LAYER_2_TO_DECIDE | MED |
| state_transition_stale_generation_window | NEEDS_LAYER_2_TO_DECIDE | MED |
| state_transition_signer_replay_no_sequence | NEEDS_LAYER_2_TO_DECIDE | LOW-MED |

## Primary Risk

The highest-confidence replay-adjacent path is the **cursor-wrap consumption reset** (Bug #1 interaction): if `consumption` resets without real work, and credit-earning is gated on consumption state, prior credit eligibility is reconstructed — functionally equivalent to replaying the credit-earning condition. This is the strongest candidate for Layer 2 PoC.

**Recommended next step:** Layer-2 empirical test on `claim_without_nonce` and `stale_generation_window` paths, specifically verifying whether a `claimed` flag is atomically set and whether generation validation rejects cross-generation references.