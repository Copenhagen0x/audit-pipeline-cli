I'll systematically investigate whether each of the six instruction handlers uses the strict accrue helper appropriately.

## Investigation

Let me trace through the actual source files.

```bash
# First, locate the wrapper file
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9 -name "percolator.rs" | head -20
```

```bash
grep -n "ensure_market_accrued_to_now_for_account_limited_op\|ensure_market_accrued_to_now_with_policy\|reject_account_limited_market_progress\|reject_stuck_target_accrual" \
  /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/percolator.rs | head -80
```

```bash
grep -n "fn withdraw_collateral\|fn trade_no_cpi\|fn trade_cpi\|fn close_account\|fn convert_released_pnl\|fn keeper_crank\|fn catchup_accrue\|WithdrawCollateral\|TradeNoCpi\|TradeCpi\|CloseAccount\|ConvertReleasedPnl\|KeeperCrank\|CatchupAccrue" \
  /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/percolator.rs | head -80
```

Let me read the actual file:

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/
```

```bash
grep -rn "ensure_market_accrued_to_now_for_account_limited_op\|ensure_market_accrued_to_now_with_policy" \
  /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -60
```

```bash
grep -rn "fn ensure_market_accrued_to_now" \
  /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -20
```

```bash
grep -rn "reject_account_limited_market_progress\|reject_stuck_target_accrual" \
  /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -40
```

```bash
# Search wrapper-prog repo too
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9 -name "*.rs" | xargs grep -l "WithdrawCollateral\|TradeNoCpi\|KeeperCrank" 2>/dev/null | head -10
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9 -type f -name "*.rs" | head -30
```

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/
```

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/
```

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/
```

```bash
cat /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/percolator.rs | wc -l
```

```bash
grep -n "ensure_market_accrued" /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/percolator.rs | head -40
```

```bash
grep -n "reject_account_limited\|reject_stuck" /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/percolator.rs | head -40
```

```bash
grep -rn "ensure_market_accrued" /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -60
```

```bash
grep -rn "reject_account_limited_market_progress" /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -30
```

```bash
grep -rn "fn ensure_market_accrued_to_now" /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ | head -20
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/ -name "*.rs" | xargs grep -l "ensure_market_accrued" 2>/dev/null
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/ -name "*.rs" | xargs grep -l "ensure_market_accrued" 2>/dev/null | head -20
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/ -name "*.rs" 2>/dev/null | head -30
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/ -name "*.rs" 2>/dev/null | wc -l
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/ -name "*.rs" 2>/dev/null | xargs grep -l "ensure_market_accrued" 2>/dev/null | head -20
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/ -name "*.rs" 2>/dev/null | xargs grep -l "KeeperCrank\|WithdrawCollateral" 2>/dev/null | head -20
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/ -type d 2>/dev/null | head -30
```

```bash
ls -la /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/ -type f | head -40
```

```bash
find /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/ -type f -name "*.rs" | head -40
```

```bash
find /tmp/ -name "percolator.rs" 2>/dev/null | head -10
```

```bash
find /tmp/ -name "*.rs" 2>/dev/null | head -20
```

```bash
ls /tmp/
```

```bash
ls /tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/
```

It appears the filesystem access is not returning results through these commands. Let me try a different approach and work from the code-grounded context provided, plus reason about what I can determine from the provided excerpt and standard patterns.

---

## Structured Analysis

Based on the provided code excerpt and the hypothesis structure, here is what I can determine with confidence:

---

## Finding: SH1-strict-helper-coverage

### Evidence from Provided Source

From the provided excerpt (`percolator.rs` lines 1034–1112):

- `KeeperCrankRequest` is a **request struct**, not the handler itself (lines 1036–1073).
- `PermissionlessProgressRequest::from_keeper_request` (lines 1103–1112) is gated behind `#[cfg(any(feature = "test", feature = "stress", kani))]` — it is **test/Kani only**, not production code.
- The production handler for `KeeperCrank` is not shown in the provided excerpt.

### What Cannot Be Determined From Provided Context Alone

The provided excerpt does **not** contain:
- The actual `KeeperCrank` instruction handler body
- The `WithdrawCollateral`, `TradeNoCpi`, `TradeCpi`, `CloseAccount`, or `ConvertReleasedPnl` handler bodies
- Any call sites for `ensure_market_accrued_to_now_for_account_limited_op` or `ensure_market_accrued_to_now_with_policy`
- The definition of `reject_account_limited_market_progress`

Without filesystem access returning results, I cannot cite exact line numbers for the handlers or verify call graphs.

---

## Verdict

| Handler | Strict helper used? | Evidence quality |
|---|---|---|
| `WithdrawCollateral` | UNKNOWN | No source in provided context |
| `TradeNoCpi` | UNKNOWN | No source in provided context |
| `TradeCpi` | UNKNOWN | No source in provided context |
| `CloseAccount` | UNKNOWN | No source in provided context |
| `ConvertReleasedPnl` | UNKNOWN | No source in provided context |
| `KeeperCrank` | UNKNOWN | Only struct/test shim shown |

**Verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: LOW** — filesystem queries did not return source content; the provided excerpt covers only the `KeeperCrankRequest` struct and a test-only shim, neither of which contains accrue-helper call sites.

---

## Structural Observation (from provided excerpt)

- `PermissionlessProgressRequest::from_keeper_request` at line 1104 is **cfg-gated** (`test | stress | kani`). This means the Kani harness for `KeeperCrank` goes through a **different code path** than the production handler. If the hypothesis includes "a Kani policy proof binding the chosen helper to the claimed safety property," the cfg-gate means the proof covers a test shim, not the production dispatch path. This is a **latent audit concern** regardless of which helper is used.

```
- ID: state_transition_kani_cfg_gap
  Block: percolator.rs:1104-1112
  Function: PermissionlessProgressRequest::from_keeper_request
  Trigger: cfg(test | stress | kani) only
  Precondition (per spec/comments): maps KeeperCrankRequest → PermissionlessProgressRequest for verification
  Precondition enforced by code: cfg gate at line 1104
  Fields written: now_slot, (remainder not shown)
  Risk: Kani proof exercises test shim, not production KeeperCrank handler; policy binding may not cover real dispatch path
  Confidence the precondition is bypassable: MED (depends on whether production handler shares the same accrual call)
  Suggested PoC: Layer-2 LiteSVM test: invoke KeeperCrank via BPF entrypoint and trace which accrue helper is called
```

---

## Recommended Next Steps

1. **Retrieve full handler bodies** for all six instructions from `percolator.rs` (search for `fn process_withdraw_collateral`, `fn process_trade`, `fn process_keeper_crank`, etc.).
2. **Grep for all call sites** of both `ensure_market_accrued_to_now_for_account_limited_op` and `ensure_market_accrued_to_now_with_policy` to produce a complete call-site map.
3. **Verify the cfg-gate gap**: confirm whether the production `KeeperCrank` path calls the same helper as the Kani shim at line 1105.