I'll investigate this hypothesis by directly examining the source code for the `WithdrawCollateral` handler and the two helper functions mentioned.

## Step 1: Locate the WithdrawCollateral handler

Let me examine the relevant source files.

```bash
grep -n "WithdrawCollateral\|ensure_market_accrued_to_now_for_account_limited_op\|ensure_market_accrued_to_now_with_policy" /tmp/jelleo-snap-aeyakovenko-percolator-li_9z8kf/aeyakovenko-percolator-5059332/src/percolator.rs | head -80
```

Let me search the actual files:

```
find /tmp/jelleo-snap-aeyakovenko-percolator-li_9z8kf/ -name "*.rs" | xargs grep -l "WithdrawCollateral" 2>/dev/null
```

```
grep -n "withdraw_collateral\|WithdrawCollateral\|ensure_market_accrued" /tmp/jelleo-snap-aeyakovenko-percolator-li_9z8kf/aeyakovenko-percolator-5059332/src/percolator.rs | head -100
```

Since I must work from the code-grounded context provided and the repository structure, let me proceed with what's available and note what I can verify.

---

## Findings

### Verification of Helper Function Names

The hypothesis references two specific functions:
- `ensure_market_accrued_to_now_for_account_limited_op` (claimed: strict helper)
- `ensure_market_accrued_to_now_with_policy` (claimed: lazy/policy-based helper)

The code-grounded context provided does **not** include excerpts from the `WithdrawCollateral` handler body or either of these helper functions. The only source bytes provided are from `i128.rs`, which is unrelated to state-transition logic.

Without verified source excerpts for these functions, I cannot confirm:
- Which helper `WithdrawCollateral` actually calls
- Whether either function name exists in the codebase as stated
- Whether "decoy walk" accounts bypass strict MTM accrual

---

## Verdict

```
- ID: state_transition_withdraw_collateral_helper_choice
  Block: src/percolator.rs (around line 5756, per architecture summary)
  Function: WithdrawCollateral handler
  Trigger: User invokes WithdrawCollateral instruction
  Precondition (per spec/comments): All user accounts must have MTM
    accrued-to-now before collateral withdrawal is permitted (strict helper)
  Precondition enforced by code: UNVERIFIABLE from provided excerpts
  Fields written: UNVERIFIABLE from provided excerpts
  Risk: If the policy-based (lazy) helper is used instead of the strict
    helper, accounts walked as "decoys" may have stale MTM, allowing
    collateral withdrawal against under-accrued positions
  Confidence the precondition is bypassable: LOW (cannot confirm
    without source excerpt of handler body and helper definitions)
  Suggested PoC: Layer-2 test: construct a WithdrawCollateral call with
    a decoy account that has accumulated MTM liability; confirm whether
    the engine rejects or permits the withdrawal without accruing the
    decoy's liability first.
```

---

## Why I Cannot Promote This to TRUE or FALSE

**Verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: LOW**

### Evidence gap:
The code-grounded context supplies only `i128.rs` alignment boilerplate. The hypothesis requires:

1. **The handler body** at or near `src/percolator.rs:5756` — not provided
2. **The definition of `ensure_market_accrued_to_now_for_account_limited_op`** — not provided
3. **The definition of `ensure_market_accrued_to_now_with_policy`** — not provided

Without these, any claim about which helper is called would violate the audit's core convention: *"Do NOT promote a hypothesis to TRUE without an exact source citation."*

### What would resolve this:

1. **Grep for both function names** in `src/percolator.rs` to confirm they exist
2. **Extract lines ~5730–5820** of `src/percolator.rs` to see the actual call site
3. **Trace the call graph**: does `WithdrawCollateral` call one or both helpers? Are there multiple paths (e.g., signer's own account vs. other accounts in the walk)?
4. **Check the policy parameter**: if `ensure_market_accrued_to_now_with_policy` accepts a policy enum, verify which variant is passed for non-signer accounts

### Risk if TRUE:
If `WithdrawCollateral` uses the lazy/policy helper for non-signer accounts, an attacker could construct a walk where decoy accounts carry stale (under-accrued) MTM liability, withdraw collateral against artificially favorable state, then let MTM catch up — extracting value from the protocol.

**This hypothesis warrants Layer-2 empirical testing before dismissal.**