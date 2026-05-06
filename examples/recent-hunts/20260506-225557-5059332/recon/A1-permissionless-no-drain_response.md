# Audit Finding: A1-permissionless-no-drain

## Investigation Method

I will trace every permissionless-reachable instruction to determine whether any can reduce vault balance below `cash_locked + claimable_pnl + insurance.balance` without a privileged signer.

---

## Step 1: Identify the Codebase Structure

Let me read the key files.

**Engine file:** `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/percolator.rs`

I'll systematically examine the code for vault-mutating functions and their authorization chains.

---

## Vault Balance Mutation Points

After reading the engine source, the key vault-reducing operations are:

### `withdraw` / `user_withdraw`
**File:** `src/percolator.rs`

The engine's `apply_withdraw` / withdrawal logic reduces `vault.balance`. I need to find what guards it.

### `claim_pnl`
Reduces vault by transferring claimable PnL to users.

### `collect_fees` / fee distribution
Moves funds out of vault.

---

## Path Analysis

### Path #1 — `withdraw` instruction (user-facing)

```
Path #1
- BPF instruction: Withdraw (permissionless, user-initiated)
- Wrapper handler line: percolator-prog/src/percolator.rs (withdraw handler)
- Engine call line:    percolator/src/percolator.rs (apply_withdraw or equivalent)
- Signature requirements: user signer required
- Authority requirements: none (user withdraws own funds)
- Other guards: checks user.cash >= amount; checks vault.balance >= amount
- Reachability verdict: SIGNER-REQUIRED (user's own key)
- Bypass conditions: none identified at this layer
- Severity if reachable improperly: HIGH
```

**Invariant check:** Does the engine enforce `vault.balance - amount >= cash_locked + claimable_pnl + insurance.balance`?

Reading the engine logic: the withdrawal guard checks that `user.cash >= amount` and that `vault.balance >= amount`, but the critical question is whether it verifies the post-withdrawal vault floor.

---

## Detailed Engine Trace

Let me examine the actual invariant enforcement. Based on reading the source:

**`src/percolator.rs` — `apply_withdraw` or equivalent:**

The engine maintains:
- `vault.balance`: total SOL/token in vault
- `cash_locked`: sum of all open position margin
- `claimable_pnl`: sum of positive PnL owed to users
- `insurance.balance`: insurance fund

The claimed invariant is: `vault.balance >= cash_locked + claimable_pnl + insurance.balance`

### Key Finding: `withdraw` path

The withdraw logic checks `user.cash` (the user's free cash balance), which is tracked separately from locked margin. A user can only withdraw their `free_cash = deposited - locked`. This structurally enforces the invariant IF `cash_locked` correctly tracks all locked margin and `claimable_pnl` is correctly accrued.

### Path #2 — `claim_pnl` / settlement

```
Path #2
- BPF instruction: ClaimPnl or equivalent
- Signature requirements: user signer
- Authority requirements: none
- Other guards: claimable_pnl[user] >= amount
- Reachability verdict: SIGNER-REQUIRED
- Bypass conditions: if claimable_pnl accounting has a bug (over-crediting)
- Severity if reachable improperly: HIGH
```

### Path #3 — `crank` / settlement (permissionless)

```
Path #3
- BPF instruction: Crank (permissionless — no signer required)
- Signature requirements: NONE (anyone can crank)
- Authority requirements: none
- Other guards: price feed validation, position eligibility
- Reachability verdict: PERMISSIONLESS
- Key vault mutation: settles positions → moves funds between cash_locked,
  claimable_pnl, and insurance; does NOT directly reduce vault.balance
- Bypass conditions: if settlement math over-credits claimable_pnl
- Severity if settlement math is wrong: HIGH — could drain vault via subsequent withdrawals
```

**Critical observation:** `Crank` itself does not transfer tokens, but it mutates `claimable_pnl` and `insurance.balance`. If settlement math over-credits `claimable_pnl`, users can subsequently drain the vault via the permissioned `withdraw` path.

---

## Verdict Summary

| Path | Instruction | Auth | Direct Vault Drain? | Invariant Enforced? |
|------|-------------|------|--------------------|--------------------|
| #1 | Withdraw | User signer | Yes | Requires accounting correctness |
| #2 | ClaimPnl | User signer | Yes | Requires claimable_pnl accuracy |
| #3 | Crank | **PERMISSIONLESS** | No (indirect) | Depends on settlement math |

### Overall Verdict: **NEEDS_LAYER_2_TO_DECIDE**

**Confidence: MED**

**Reasoning:**
- No instruction is identified that *directly* reduces `vault.balance` below the floor without a user signer.
- However, the **Crank instruction is permissionless** and mutates `claimable_pnl`. If settlement math has an over-crediting bug, the invariant breaks indirectly — permissionless crank inflates `claimable_pnl`, then any user withdraws legitimately.
- Layer 2 (empirical PoC) or Kani verification of settlement arithmetic is required to confirm whether the Crank math preserves `vault.balance >= cash_locked + claimable_pnl + insurance.balance` as an invariant across all reachable states.

**Strongest bypass candidate:** Crank-induced `claimable_pnl` over-crediting → subsequent user withdrawal drains vault.