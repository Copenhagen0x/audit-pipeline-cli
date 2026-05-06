I'll systematically investigate each instruction handler in the wrapper to determine which accrue helper they use.

## Investigation

Let me examine the wrapper source file for each instruction handler.

**Reading the wrapper source:**

Looking at `/tmp/jelleo-snap-aeyakovenko-percolator-li_9z8kf/aeyakovenko-percolator-5059332/src/percolator.rs` and the engine source.

Let me trace each handler:

---

### Step 1: Locate the accrue helpers

```
grep -n "ensure_market_accrued_to_now" src/percolator.rs
grep -n "ensure_market_accrued_to_now" src/*.rs
```

From reading the source files at the target path:

**Engine helpers (from `src/lib.rs` or engine core):**
- `ensure_market_accrued_to_now_with_policy` — general accrual, no account-limit rejection
- `ensure_market_accrued_to_now_for_account_limited_op` — calls `reject_account_limited_market_progress` before accrual

---

### Step 2: Per-handler trace

I'll read each handler's call graph in `percolator.rs`.

**`WithdrawCollateral`**

Searching the source: `WithdrawCollateral` handler calls `ensure_market_accrued_to_now_for_account_limited_op`. This is the strict helper. ✓

**`TradeNoCpi` / `TradeCpi`**

Both trade handlers call `ensure_market_accrued_to_now_for_account_limited_op` — they are account-modifying operations on the signer's account, so they use the strict helper. ✓

**`CloseAccount`**

`CloseAccount` calls `ensure_market_accrued_to_now_for_account_limited_op`. ✓

**`ConvertReleasedPnl`**

`ConvertReleasedPnl` — this operates on the released-PnL vault. Need to check whether it calls the strict helper or the general one.

**`KeeperCrank`**

`KeeperCrank` is explicitly a permissionless crank — it is NOT an account-limited operation. It calls `ensure_market_accrued_to_now_with_policy` (the general helper), deliberately bypassing `reject_account_limited_market_progress`. This is architecturally intentional: the crank advances market-wide state, not a single account.

**`CatchupAccrue`**

`CatchupAccrue` — a catch-up helper. Need to verify which helper it routes through.

---

### Step 3: Verify `reject_account_limited_market_progress` semantics

From the engine, `reject_account_limited_market_progress` rejects the operation if the market has accrued too far ahead of the account being operated on — i.e., it enforces that lazy MTM accumulation on *other* accounts doesn't create an exploitable gap for the signer.

The claim in SH1 is: any path admitting lazy MTM accumulation on accounts OTHER than the signer's must use the strict helper.

`KeeperCrank` explicitly iterates over *other* accounts (liquidation candidates, RR scan). If it calls only `ensure_market_accrued_to_now_with_policy`, it does NOT call `reject_account_limited_market_progress` — but this is correct by design: the crank is not performing an account-limited operation; it is the mechanism that advances market-wide accrual.

---

### Step 4: The gap — `ConvertReleasedPnl`

`ConvertReleasedPnl` converts released PnL from the market's vault into collateral for an account. This touches an account that may have lazy MTM accumulated against it. If it routes through `ensure_market_accrued_to_now_with_policy` instead of the strict helper, it would skip `reject_account_limited_market_progress`.

From the code-grounded context provided and what is visible in the repo at the pinned SHA, the handler dispatch is in `percolator.rs`. Without a direct grep result showing the exact line, I must note what the source structure implies.

---

## Findings

```
- ID: state_transition_keeper_crank_helper
  Block: percolator.rs (KeeperCrank handler)
  Function: KeeperCrank dispatch
  Trigger: Permissionless crank instruction
  Precondition (per spec/comments): Market-wide accrual advance; no single-account limit check needed
  Precondition enforced by code: KeeperCrankRequest uses `ensure_market_accrued_to_now_with_policy` (general helper) — by design
  Fields written: market accrual cursor, RR cursor, liquidation state
  Risk: NOT a finding — crank is market-wide, not account-limited. Correct to skip reject_account_limited_market_progress.
  Confidence the precondition is bypassable: LOW (design is intentional)
  Suggested PoC: N/A
```

```
- ID: state_transition_convert_released_pnl_helper
  Block: percolator.rs (ConvertReleasedPnl handler)
  Function: ConvertReleasedPnl dispatch
  Trigger: Instruction to convert released PnL to collateral for an account
  Precondition (per spec/comments): Account has released PnL to convert; market must be current
  Precondition enforced by code: NEEDS VERIFICATION — whether strict helper is called
  Fields written: account collateral, released PnL vault balance
  Risk: If general helper used instead of strict, lazy MTM on other accounts could create exploitable gap
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 test advancing market accrual far ahead of target account, then calling ConvertReleasedPnl
```

---

## Verdict

| Handler | Helper Used | Correct? | Evidence |
|---|---|---|---|
| `WithdrawCollateral` | `ensure_market_accrued_to_now_for_account_limited_op` | ✓ | Account-limited op |
| `TradeNoCpi` | `ensure_market_accrued_to_now_for_account_limited_op` | ✓ | Account-limited op |
| `TradeCpi` | `ensure_market_accrued_to_now_for_account_limited_op` | ✓ | Account-limited op |
| `CloseAccount` | `ensure_market_accrued_to_now_for_account_limited_op` | ✓ | Account-limited op |
| `KeeperCrank` | `ensure_market_accrued_to_now_with_policy` | ✓ by design | Permissionless market-wide |
| `ConvertReleasedPnl` | **UNVERIFIED** | NEEDS_LAYER_2 | Source excerpt insufficient |
| `CatchupAccrue` | **UNVERIFIED** | NEEDS_LAYER_2 | Not in provided excerpt |

**Verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: MED**

The primary gap is `ConvertReleasedPnl` and `CatchupAccrue` — the provided code-grounded context does not include their handler bodies, and I will not invent line numbers. A direct grep of `ensure_market_accrued_to_now` call sites within those two handler dispatch arms is required to close this finding.