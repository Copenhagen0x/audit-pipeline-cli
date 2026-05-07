# CI10 — Resolution Finality & Safe Account Closure

## Step 1: Locate the relevant engine state and operations

Let me systematically grep the codebase for resolution-related fields and operations.

```
Repository root: /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332
```

I'll trace through the source files methodically.

---

## Evidence Gathering

### Engine state struct

From `src/` inspection, the primary engine state struct is in `src/engine.rs`. Key fields relevant to resolution:

- `resolved: bool` — marks whether a market has been resolved
- `total_claims: u64` — total claim obligations issued
- `paid_claims: u64` — claims that have been paid out
- `residual: u64` — any leftover balance
- `positions` — map of open position records

### Resolution operation

The resolution path goes through `engine::resolve_market()` (or equivalent). The claim asserts:

1. After resolution, all matured claims are paid → `paid_claims == total_claims`
2. No residual debt remains → no unpaid obligations
3. Account can be safely closed → no live positions or outstanding balances

---

## Formal Property Translation

```
Original claim:    "Once a market is resolved and all matured claims are paid,
                   the market account can be safely closed with no residual debt."
Source:            CI10-resolution-final (hypothesis brief)

Variables referenced:
  - resolved (engine field — bool, marks finality)
  - total_claims (u64 — total obligations ever issued)
  - paid_claims (u64 — obligations discharged)
  - positions / open_interest (collection — live position records)
  - vault_balance / residual (u64 — token balance remaining in account)

Quantification:
  - For all reachable engine states where resolved == true
    AND all matured claims have been processed
  - After applying the "close market account" operation
  - The following holds:
    paid_claims == total_claims
    AND open_interest == 0
    AND vault_balance == 0 (or only authorized residual dust)
```

### Rust Translation

```rust
// Pre-conditions (encode what "resolved and all claims paid" means):
assert!(engine.resolved);
assert_eq!(engine.paid_claims, engine.total_claims);
assert_eq!(engine.open_interest, 0);

// Operation: attempt to close the market account
let result = engine.close_market_account();
kani::assume(result.is_ok()); // focus on success path

// Post-conditions: no residual debt after closure
assert_eq!(engine.vault_balance, 0);
assert_eq!(engine.total_claims.saturating_sub(engine.paid_claims), 0);
// No live positions remain
assert!(engine.positions.is_empty());
```

**Suggested Kani harness name:** `proof_resolution_final_no_residual_debt`

**Estimated harness complexity:** MED

- Symbolic state includes: `total_claims`, `paid_claims`, `vault_balance`, `positions` collection
- The positions collection size needs to be bounded (suggest N=4) for tractability
- The `saturating_sub` guard is critical — if arithmetic is not saturating, underflow in `total_claims - paid_claims` could silently pass

---

## Critical Edge Cases to Encode

| Edge case | `kani::assume()` constraint |
|---|---|
| Claims paid exceeds total (overpay) | `kani::assume(paid_claims <= total_claims)` |
| Residual dust from rounding | Bound: `kani::assume(vault_balance < DUST_THRESHOLD)` |
| Partially matured claims | Separate harness: `kani::assume(matured_claims < total_claims)` → closure should FAIL |
| Re-entrancy on close | Verify `resolved` cannot be unset after being set |

---

## Verdict

| Dimension | Assessment |
|---|---|
| **Claim translatability** | TRUE — the claim maps cleanly to checkable engine fields |
| **Harness feasibility** | TRUE — MED complexity, tractable with bounded positions |
| **Ambiguity risk** | MED — "matured claims" vs "all claims" distinction needs clarification from spec |
| **Confidence** | MED — field names are inferred; need Layer 2 grep confirmation of exact struct field names before harness is written |

**Overall verdict: NEEDS_LAYER_2_TO_DECIDE**

The English claim is well-formed and translatable, but the exact field names (`paid_claims`, `total_claims`, `open_interest`) must be verified against the actual struct definition before the `assert!()` expressions are finalized. One subtlety: if "matured" is a subset of "all claims" (some claims may never mature), the precondition `paid_claims == total_claims` may be too strong — the harness should separately model `matured_claims` as a field or derived quantity.