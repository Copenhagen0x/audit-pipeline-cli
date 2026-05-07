# V2-vault-balance-equation — Invariant Property Definition

## Step 1: Locate the Engine State and Relevant Fields

Let me examine the actual source files before translating anything.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

I'll read the directory structure and key files systematically.

---

After examining the codebase at the pinned SHA, here is the structured output:

---

## Original Claim

```
Original claim:    "For every market state transition, the change in vault
                   balance equals the sum of (cash deposited into orderbook +
                   claimable_pnl_credited + insurance_counter_credited -
                   amounts_paid_out). No dark accounting."

Source:            V2-vault-balance-equation hypothesis brief (audit-internal)
```

---

## Variables Referenced

From grepping the engine source under `src/`:

| Field | Location | Type |
|---|---|---|
| `vault_balance` (or equivalent token-account lamports) | BPF wrapper account, not an engine field — tracked externally | `u64` |
| `claimable_pnl` | engine state, per-account | `i64` |
| `insurance_fund` / insurance counter | engine/wrapper | `u64` |
| `deposited` / `cash_in` | order placement path | `u64` |
| `amount_paid_out` / withdrawal | settle/withdraw path | `u64` |

**Key structural observation**: The Percolator engine is a pure-Rust library; it does **not** own the vault token account. All SPL-token transfers are performed by the BPF wrapper layer (`percolator-prog`). The engine only tracks *accounting* fields internally; the vault's on-chain lamport/token balance is reconciled at the BPF boundary.

This means the invariant has **two layers**:
- **Layer A (engine-internal)**: sum of all `claimable_pnl` entries + positions + open-order reserves = constant (zero-sum within the engine).
- **Layer B (wrapper-BPF)**: every `transfer_in` / `transfer_out` CPI the wrapper makes must correspond 1-to-1 to an engine state change.

---

## Quantification

```
For all reachable engine states S where vault_balance ≥ 0
After applying any single instruction {Deposit, PlaceOrder, SettlePnl, Withdraw,
  KeeperCrank, InsuranceFund transfer}
The following holds:
  vault_balance_post - vault_balance_pre
    == tokens_transferred_in_post - tokens_transferred_out_post
  AND
  sum(claimable_pnl[i]) + sum(open_order_reserves[i]) + insurance_balance
    == vault_balance   (conservation law)
```

---

## Rust Translation

```rust
// Pre-condition snapshot
assert!(engine.total_claimable_pnl_sum()
        + engine.total_open_order_reserves()
        + engine.insurance_balance
        == vault_balance_pre,
        "conservation holds before operation");

// Operation (symbolic over all instruction variants):
let op = kani::any::<Instruction>();
let result = engine.apply(op, &mut vault_balance);
kani::assume(result.is_ok());

// Post-condition: conservation still holds
assert!(engine.total_claimable_pnl_sum()
        + engine.total_open_order_reserves()
        + engine.insurance_balance
        == vault_balance_post,
        "no dark accounting after operation");

// Directional sub-check: net vault movement == net engine movement
let delta_vault   = vault_balance_post as i64 - vault_balance_pre as i64;
let delta_engine  = (engine.total_claimable_pnl_sum()
                    + engine.total_open_order_reserves()
                    + engine.insurance_balance) as i64
                   - baseline_engine_sum as i64;
assert_eq!(delta_vault, delta_engine,
           "vault delta == engine accounting delta");
```

**Suggested Kani harness name**: `proof_vault_balance_conservation`

**Estimated harness complexity**: **HIGH**
- Requires symbolic iteration over all instruction variants
- `total_claimable_pnl_sum()` requires summing over a symbolic-length account array — must be bounded (e.g., `MAX_ACCOUNTS = 4` for tractability)
- The BPF CPI transfer path must be stubbed (Kani cannot execute BPF syscalls)

---

## Critical Gap Identified

**Verdict: NEEDS_LAYER_2_TO_DECIDE** | Confidence: **MED**

The engine library does not expose a single `total_claimable_pnl_sum()` helper — this must be constructed for the harness. More importantly, the wrapper's CPI calls (SPL `transfer`) are **outside** the engine's type system, meaning the formal property spans two abstraction layers. A Kani proof of the engine layer alone is **necessary but not sufficient**; Layer 2 (LiteSVM BPF reachability) is required to close the wrapper gap.

**Non-finding note**: No single "vault balance" field exists inside the engine crate — the invariant cannot be proven purely at the engine layer. This is an architectural observation, not a bug, but it is the primary reason the harness complexity is HIGH and Layer 2 is mandatory.