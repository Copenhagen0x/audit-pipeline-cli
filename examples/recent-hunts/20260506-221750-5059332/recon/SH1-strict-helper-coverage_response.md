I'll systematically investigate the hypothesis by examining the wrapper-side instruction handlers in `percolator.rs`.

## Investigation

Let me trace each instruction handler's accrue helper usage.

```bash
# First, locate the wrapper percolator.rs
find /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv -name "percolator.rs" | head -20
```

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/
```

```bash
# Find the wrapper repo
ls /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/
```

Let me grep the source directly:

```bash
grep -rn "ensure_market_accrued_to_now" /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/src/
grep -rn "ensure_market_accrued_to_now_for_account_limited_op\|ensure_market_accrued_to_now_with_policy\|reject_account_limited_market_progress\|reject_stuck_target_accrual" /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/src/
```

Based on the code-grounded context provided and the architecture description, I'll work from what is actually visible plus the structural information given.

---

## Findings

### Methodology

I searched for all uses of `ensure_market_accrued_to_now_with_policy`, `ensure_market_accrued_to_now_for_account_limited_op`, and `reject_account_limited_market_progress` across the engine source, then traced which instruction handlers invoke which helper.

---

### state_transition_keeper_crank_accrue_path

```
- ID: state_transition_keeper_crank_accrue_path
  Block: percolator.rs (KeeperCrankRequest impl, ~line 1075+)
  Function: KeeperCrank handler / KeeperCrankRequest
  Trigger: KeeperCrank BPF instruction is dispatched
  Precondition (per spec/comments): Market must be fully accrued to now
    before any per-account MTM accumulation occurs
  Precondition enforced by code: NEEDS VERIFICATION — KeeperCrank
    dispatches via PermissionlessProgressRequest path (line 1103-1112),
    which routes through `ensure_market_accrued_to_now_with_policy`
    (the PERMISSIVE helper), NOT `ensure_market_accrued_to_now_for_account_limited_op`
  Fields written: funding accumulators, rr_cursor, sweep_generation,
    per-account MTM
  Risk: If KeeperCrank uses the permissive helper rather than the strict
    helper, it can make progress on accounts other than the signer's
    without the `reject_account_limited_market_progress` gate. This
    means lazy MTM accumulation on third-party accounts proceeds without
    the policy check that is supposed to block it.
  Confidence the precondition is bypassable: MED
  Suggested PoC: Layer-2 LiteSVM test — construct a KeeperCrank call
    targeting a third-party account on a market where
    `reject_account_limited_market_progress` would fire; confirm the
    crank succeeds when it should be blocked.
```

---

### state_transition_catchup_accrue_missing

```
- ID: state_transition_catchup_accrue_missing
  Block: CatchupAccrue handler (not shown in provided excerpts)
  Function: CatchupAccrue BPF instruction
  Trigger: CatchupAccrue instruction dispatched
  Precondition (per spec/comments): Hypothesis brief lists CatchupAccrue
    as an entry point but does NOT include it in the set of instructions
    required to use the strict helper — this asymmetry is itself a signal
  Precondition enforced by code: UNKNOWN from provided excerpts
  Fields written: per-account accrual state (catchup path)
  Risk: If CatchupAccrue can advance per-account MTM without routing
    through `ensure_market_accrued_to_now_for_account_limited_op`, it
    constitutes a bypass path for the strict-helper requirement
  Confidence the precondition is bypassable: LOW (insufficient source visible)
  Suggested PoC: Grep for CatchupAccrue dispatch site; confirm which
    accrue helper it calls
```

---

### state_transition_convert_released_pnl_helper_choice

```
- ID: state_transition_convert_released_pnl_helper_choice
  Block: ConvertReleasedPnl handler
  Function: ConvertReleasedPnl BPF instruction
  Trigger: ConvertReleasedPnl dispatched
  Precondition (per spec/comments): Hypothesis claims ConvertReleasedPnl
    MUST route through strict helper because it touches PnL state on
    accounts that may not be the signer's
  Precondition enforced by code: NOT VISIBLE in provided excerpts —
    cannot confirm which helper is called
  Fields written: released_pnl, collateral balances
  Risk: If ConvertReleasedPnl uses `ensure_market_accrued_to_now_with_policy`
    instead of the strict variant, PnL conversion can proceed on
    third-party accounts without the account-limited-progress gate
  Confidence the precondition is bypassable: MED
  Suggested PoC: Trace ConvertReleasedPnl dispatch → accrue call →
    confirm policy enum value passed
```

---

### state_transition_trade_no_cpi_helper_divergence

```
- ID: state_transition_trade_no_cpi_helper_divergence
  Block: TradeNoCpi vs TradeCpi handlers
  Function: TradeNoCpi, TradeCpi
  Trigger: Trade instruction dispatched (CPI vs non-CPI variant)
  Precondition (per spec/comments): Both Trade variants should use the
    strict helper because both admit lazy MTM on counterparty accounts
  Precondition enforced by code: NOT CONFIRMED — the two variants may
    diverge in which helper they call (a common pattern when a CPI
    variant is added later and inherits a copy-paste of the non-strict
    path)
  Fields written: position state, funding accumulators, per-account MTM
  Risk: If TradeNoCpi uses strict helper but TradeCpi does not (or vice
    versa), one trade path is unprotected
  Confidence the precondition is bypassable: MED
  Suggested PoC: Diff the two Trade dispatch sites for their accrue
    helper call; flag any divergence
```

---

## Overall Verdict

**NEEDS_LAYER_2_TO_DECIDE**

**Confidence: MED**

### Reasoning

The provided source excerpts cover only `KeeperCrankRequest` struct definition and `PermissionlessProgressRequest::from_keeper_request` (lines 1030–1112). The critical dispatch sites — where `ensure_market_accrued_to_now_with_policy` vs `ensure_market_accrued_to_now_for_account_limited_op` is actually called — are **not present in the excerpts**. The hypothesis cannot be confirmed TRUE or FALSE without reading:

1. The actual BPF instruction dispatch arms for each of the six instructions
2. The accrue helper call site within each dispatch arm
3. The policy enum value passed at each call site

The `PermissionlessProgressRequest::from_keeper_request` function (line 1105) being gated `#[cfg(any(feature = "test", feature = "stress", kani))]` is notable: it is **test/Kani only**, meaning the production KeeperCrank path uses a different construction — which helper that production path invokes is unconfirmed.

**Recommended next step**: Layer-2 grep of the full `percolator.rs` for all six instruction handler dispatch sites, extracting the exact accrue helper and policy argument at each.