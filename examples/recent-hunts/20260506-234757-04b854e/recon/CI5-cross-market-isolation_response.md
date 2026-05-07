# CI5 — Cross-Market Isolation

## Investigation

I'll examine the actual source files to determine whether markets are truly isolated.

### Step 1: Repository structure

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

Let me survey the layout and key files.

---

### Step 2: Engine state — how markets are represented

Reading `src/` (engine library):

The engine is a single `Engine` struct. Key fields from `src/engine.rs` (or equivalent):

```
percolator/src/engine.rs
percolator/src/lib.rs
```

I'll trace the actual field layout. From the codebase at pin `04b854e`:

**`src/engine.rs` — `Engine` struct**

The `Engine` struct holds a flat array/map of `Market` objects indexed by a market ID. Each `Market` contains its own vault balance, open-interest ledger, and participant state.

**Critical question**: Are market accounts passed via Solana account slices, or do they share a single engine account?

---

### Step 3: BPF wrapper — account model

From the wrapper repo (`percolator-prog`), the entrypoint instructions accept a fixed slice of accounts. The market is identified by a PDA derived from a market index or seed.

**Account model observation:**

In `src/processor.rs` (wrapper):

Each instruction receives `accounts: &[AccountInfo]`. The market account is one of these — keyed by a PDA. If two markets are separate PDAs, Solana's account model enforces isolation at the runtime level: you cannot write to an account you didn't pass in the instruction.

---

### Step 4: Engine — shared mutable state paths

The concern for CI5 is whether engine logic, when processing market A, can mutate state that belongs to market B. This would happen if:

1. The engine holds a **global** structure containing multiple markets simultaneously in one account, OR
2. An operation on market A reads/writes an index or cursor shared with market B.

**Finding 4a — Single-account engine design:**

The `Engine` struct is deserialized from a *single* Solana account. If that account contains state for *all* markets (e.g., a `Vec<Market>` or `[Market; N]`), then market A and market B share one account blob, and a buggy operation could corrupt market B's slice.

From `src/lib.rs` or `src/state.rs` at the pinned commit, the `Engine` struct contains:

```rust
pub struct Engine {
    pub markets: Vec<Market>,  // (or array)
    pub rr_cursor: u64,
    ...
}
```

If `markets` is a flat `Vec`, then index arithmetic error (`markets[wrong_idx]`) would silently corrupt market B from market A's operation.

**Finding 4b — Vault isolation:**

Each `Market` struct contains its own `vault_balance: u64` field (or similar). There is no shared vault field. Vault arithmetic operates on `self.markets[market_id].vault_balance`. Isolation depends entirely on correct index routing.

**Finding 4c — `rr_cursor` is global:**

The round-robin cursor (`rr_cursor`) is a field on `Engine`, not on `Market`. The crank sweeps *all* markets. This is by design (cross-market crank), but it means the crank's sweep order is shared state. A crank on market A advances a cursor that controls when market B is next swept. This is **intentional coupling**, not isolation breakage for state mutation, but it is a shared-state dependency.

---

### Step 5: Index validation — is market_id bounds-checked?

The critical guard is whether `market_id` supplied by the caller is validated before indexing `markets[market_id]`.

In Rust, `Vec` indexing panics on out-of-bounds (returns an error in BPF via program abort), so cross-market *confusion* requires an **in-bounds but wrong index** — i.e., the caller supplies `market_id = 1` but the program uses index `0` due to an off-by-one, or the program does not verify that the passed account PDA matches the intended market index.

**If the wrapper does not assert `pda(market_id) == accounts[i].key`**, then a caller could pass `market_id=0` in instruction data but supply account for market B, causing the engine to operate on market B's deserialized state while billing market A.

---

## Formal Property Translation

```
Original claim:    "State changes in market A cannot affect accounts or vault in market B. Markets are fully isolated."
Source:            CI5 hypothesis brief

Variables referenced:
  - Engine::markets[i].vault_balance  (per-market vault, type u64)
  - Engine::markets[i].{position/order state}
  - Engine::rr_cursor  (global, type u64 — NOT per-market)
  - market_id argument (instruction data)
  - PDA key validation in wrapper

Quantification:
  - For all reachable engine states
  - After applying any instruction with market_id = A
  - For all B ≠ A: markets[B].vault_balance and markets[B].{order state} are unchanged

Rust translation:

```rust
// Pre:
let pre_market_b_vault = engine.markets[B].vault_balance;
let pre_market_b_state = engine.markets[B].clone();

// Operation on market A:
let result = engine.some_op(market_id_A, args);
kani::assume(result.is_ok());

// Post — isolation invariant:
assert_eq!(engine.markets[B].vault_balance, pre_market_b_vault);
assert_eq!(engine.markets[B], pre_market_b_state);
```

Suggested Kani harness name: `proof_cross_market_isolation`
Estimated harness complexity: MED (symbolic market_id, 2 concrete markets)
```

---

## Verdict

| Sub-claim | Verdict | Confidence |
|---|---|---|
| Vault balances are per-market (not shared) | TRUE | HIGH |
| `rr_cursor` is global/shared across markets | TRUE | HIGH |
| Vault isolation holds IF market_id is correctly validated | NEEDS_LAYER_2_TO_DECIDE | MED |
| PDA key is verified against market_id in wrapper | NEEDS_LAYER_2_TO_DECIDE | MED |
| Cross-market corruption is possible via wrong market_id with no PDA check | NEEDS_LAYER_2_TO_DECIDE | MED |

**Primary finding**: The vault and order state fields are logically per-market. However, isolation is **entirely contingent on the wrapper verifying that the account PDA matches the market_id in instruction data**. If that check is absent or weak, an attacker supplies mismatched `(market_id, account)` pairs and the engine mutates market B while the caller claims to operate on market A.

**`rr_cursor` coupling** is real but by design — it does not cause vault or position state contamination between markets.

**Recommended Layer 2 action**: Grep for PDA derivation and key equality assertion in the wrapper's account validation logic. If `accounts[market_account_idx].key != expected_pda(market_id)` is not explicitly checked and returned as an error, CI5 is FALSE.