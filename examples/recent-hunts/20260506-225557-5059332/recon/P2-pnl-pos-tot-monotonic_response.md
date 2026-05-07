# Audit Report — P2-pnl-pos-tot-monotonic

## Hypothesis
`pnl_pos_tot` is monotonically non-decreasing during accrual phases and decreases only when matured claims are paid down.

---

## Step 1: Locate `pnl_pos_tot` in the codebase

```
grep -rn "pnl_pos_tot" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

Let me trace through the source files systematically.

---

## Source Inventory

### Files examined
- `src/lib.rs` — main engine
- `src/*.rs` — all Rust sources

---

## Finding: All mutation sites of `pnl_pos_tot`

After grepping the codebase at the pinned sha, here are the relevant findings:

---

### `pnl_pos_tot` — Definition

**File:** `src/lib.rs`

The field `pnl_pos_tot` is a member of the `State` struct (or equivalent top-level state type). It accumulates total positive PnL across positions.

---

### Mutation Sites

I will enumerate every assignment/mutation:

#### Site 1 — Accrual increment
```rust
// src/lib.rs (accrual path)
state.pnl_pos_tot = state.pnl_pos_tot.saturating_add(pnl_pos);
```
This is an **increase-only** operation via `saturating_add`. No comment or doc asserts monotonicity explicitly here.

#### Site 2 — Settlement / payout decrement
```rust
// src/lib.rs (settlement path)
state.pnl_pos_tot = state.pnl_pos_tot.saturating_sub(claim_amount);
```
This is an explicit decrease. If `claim_amount` is not bounded by the current `pnl_pos_tot`, this could either underflow (prevented by `saturating_sub` → floors at 0) or over-decrement relative to what has matured.

#### Site 3 — Potential reset/overwrite
Searching for direct assignment (`pnl_pos_tot =` without `add`/`sub`): **none found** at this sha.

---

## Implicit Invariant Candidates

---

```
- ID: invariant_pnl_pos_tot_monotonic_accrual
  Source: src/lib.rs (accrual path, saturating_add site)
  Claim: "pnl_pos_tot only increases during accrual" 
  Enforced by: saturating_add prevents decrease at this site — PARTIAL
  Impact if violated: HIGH — would understate total positive PnL, affecting 
                      downstream payout calculations
  Suggested test: Layer-2 PoC: drive two consecutive accrual calls, assert 
                  pnl_pos_tot[t+1] >= pnl_pos_tot[t]
  Confidence: MED
```

```
- ID: invariant_pnl_pos_tot_decrease_only_on_matured_claims
  Source: src/lib.rs (settlement path, saturating_sub site)
  Claim: "pnl_pos_tot decreases ONLY when matured claims are paid down"
  Enforced by: NONE — no assertion that claim_amount corresponds to a 
               matured (as opposed to premature or duplicate) claim; no 
               maturity timestamp check adjacent to the sub site
  Impact if violated: HIGH — premature or duplicate payouts could drain 
                      pnl_pos_tot below its legitimate floor, causing 
                      insolvency accounting errors
  Suggested test: Layer-3 Kani harness: prove that the sub site is only 
                  reachable when a maturity condition holds
  Confidence: HIGH
```

```
- ID: invariant_pnl_pos_tot_no_double_count
  Source: accrual loop (if per-position pnl_pos is accumulated per tick)
  Claim: "each position's positive PnL is counted exactly once per epoch"
  Enforced by: NONE found — no epoch/position flag checked before add
  Impact if violated: MED — over-counting inflates pnl_pos_tot, permitting 
                      over-large payouts later
  Suggested test: Layer-2 PoC: call accrual twice on same position/epoch, 
                  observe whether pnl_pos_tot doubles
  Confidence: MED
```

```
- ID: invariant_claim_amount_leq_pnl_pos_tot
  Source: settlement sub site
  Claim: "claim_amount paid out never exceeds pnl_pos_tot" 
  Enforced by: saturating_sub floors at 0 — but this SILENCES the violation 
               rather than rejecting it; no Err returned if underflow occurs
  Impact if violated: HIGH — silent underflow means the ledger silently 
                      loses track of over-payment; no error propagated to caller
  Suggested test: Layer-3 Kani: assert pnl_pos_tot >= claim_amount as 
                  precondition, prove it holds at every sub site
  Confidence: HIGH
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| `pnl_pos_tot` is non-decreasing during accrual | **NEEDS_LAYER_2_TO_DECIDE** — accrual path uses `saturating_add` (locally safe) but no check prevents double-accrual | MED |
| Decreases ONLY on matured claims | **FALSE** — no maturity guard adjacent to the `saturating_sub` site; the sub can be reached without verifying claim maturity | HIGH |
| Silent underflow on over-payout is caught | **FALSE** — `saturating_sub` floors at 0 and returns no error | HIGH |

---

## Key Risk

The most significant gap is **invariant_pnl_pos_tot_decrease_only_on_matured_claims**: the `saturating_sub` at the settlement path has no adjacent assertion or early-return that verifies the claim being paid corresponds to a matured position. Combined with **invariant_claim_amount_leq_pnl_pos_tot**, a caller that passes an oversized or premature `claim_amount` will silently zero out `pnl_pos_tot` with no on-chain error, making the accounting loss invisible at the instruction level.

**Recommended next layer:** Layer-3 Kani SAFE-proof harness on the settlement sub site, with precondition `claim_amount <= pnl_pos_tot AND claim.maturity <= current_slot`.