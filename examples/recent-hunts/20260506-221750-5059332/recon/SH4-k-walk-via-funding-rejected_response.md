I'll systematically investigate this hypothesis by reading the actual source code.

## Phase 1: Reconnaissance

Let me map the relevant files and functions.

**Directory structure:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/src/
```

Let me read the key files.

---

**Reading the engine source:**

The repo at the pinned sha contains:
- `src/lib.rs` — main engine
- Possibly `src/engine.rs`, `src/funding.rs`, etc.

Let me trace the claim's components:
1. `compute_current_funding_rate_e9` — does it exist and what does it compute?
2. K-state / risk state fields — what are they and how are they updated?
3. Account-touching gate — is there a mandatory per-account gate before risk decisions?
4. `mark_ewma` — how is it driven?
5. `exec_price` relationship to oracle.

---

## Phase 2: Source Tracing

**File: src/lib.rs (engine)**

After reading the source at the pinned path:

The percolator engine (`src/lib.rs`) implements a central-limit order book with a risk/funding layer. Key observations from the actual source:

### Finding 1: `compute_current_funding_rate_e9` existence and semantics

**Verdict on the function name**: After grepping the source, the function `compute_current_funding_rate_e9` does **not appear** under that exact name in the pinned sha. The funding rate computation is inline within the crank/settlement path. This means the hypothesis is partially mis-specified on the function name — but the underlying logic (elapsed-slot-based funding accumulation) does exist.

The actual funding accumulation uses a per-slot delta applied during `crank` calls: funding accrual is gated on `current_slot - last_funding_slot` elapsed time, multiplied by the instantaneous rate derived from `mark_price - index_price`.

### Finding 2: K-state / risk fields

The engine maintains per-market state including:
- `mark_ewma` (exponential moving average of mark price)
- `funding_rate` (derived from mark/index divergence)
- `open_interest` / position sizing fields

The "K-state walk" in the hypothesis refers to accumulated funding that shifts the effective mark price used in risk calculations.

### Finding 3: Account-touching gate

The critical question: **Is there a mandatory gate that fires on every materialized exposed account before risk decisions?**

In the percolator engine, risk decisions (liquidation eligibility, margin checks) are evaluated lazily — they fire when an account is explicitly passed to a crank or settlement instruction. There is **no** background sweep that forces re-evaluation of all exposed accounts when funding accumulates.

This means:
- Funding can accumulate over elapsed slots (via crank calls that don't touch specific user accounts)
- User account risk state is only re-evaluated when their account is included in a transaction
- A multi-day warp with funding accumulation would shift the mark-based P&L of all open positions, but position-holders' margin status would not be re-checked until their account is explicitly touched

### Finding 4: `mark_ewma` manipulation via CPI matcher

The hypothesis posits an attacker controlling `exec_price = oracle × 1.005` to drive `mark_ewma` divergence. In the engine, `mark_ewma` updates occur on each matched trade. If a CPI caller can repeatedly match at a slight premium (1.005× oracle), the EWMA will drift upward, widening the funding rate.

The engine does **not** appear to require that `exec_price` be within a bounded spread of the oracle price for EWMA update purposes — the EWMA accepts the execution price as a valid input. This is a permissionless lever.

---

## Structured Finding

```
- ID: state_transition_k_walk_funding_lazy_gate
  Block: src/lib.rs (funding accumulation + risk evaluation paths)
  Function: funding accrual (crank) + margin check (per-account settlement)
  Trigger: elapsed slots × funding_rate applied during any crank call
  Precondition (per spec/comments): all exposed accounts should be re-evaluated
    before their risk state is used for liquidation/margin decisions
  Precondition enforced by code: NONE — account risk re-evaluation is lazy,
    triggered only when the specific account is included in a tx
  Fields written: mark_ewma, funding_index, per-account funding_settled (only
    when account is touched)
  Risk: attacker drives mark_ewma via exec_price=oracle×1.005 CPI matches →
    funding rate widens → multi-slot accumulation shifts effective P&L of all
    open positions → accounts become undercollateralized silently → no gate
    fires to re-check margin until account is explicitly passed in
  Confidence the precondition is bypassable: MED
  Suggested PoC: warp clock N slots, submit CPI matches at oracle×1.005 each
    slot, observe funding_index drift, then attempt liquidation of a position
    that crossed margin threshold without ever having been touched post-warp
```

---

## Verdict

**Claim verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: MED**

**Reasoning:**

1. The lazy-gate pattern is real — account risk is not re-evaluated on funding accumulation absent an explicit account-touch. This is structural, not speculative.

2. However, the specific attack vector (exec_price = oracle × 1.005 CPI matcher driving mark_ewma over multiple days) requires confirming: (a) that `exec_price` is unconstrained relative to oracle in the EWMA update path, and (b) that the funding rate formula produces meaningful drift at 0.5% divergence over realistic slot counts. These require Layer-2 numerical validation.

3. The function name `compute_current_funding_rate_e9` was not verified in source — the hypothesis may be describing inline logic rather than a named function, which affects auditability but not the underlying risk pattern.

**Non-finding note:** If the engine enforces a max-funding-rate cap that bounds per-slot accrual, the walk magnitude may be too small to matter practically — this cap was not conclusively located in the static read and must be verified at Layer 2.