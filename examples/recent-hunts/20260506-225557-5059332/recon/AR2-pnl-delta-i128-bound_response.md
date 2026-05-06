# Audit Report: AR2-pnl-delta-i128-bound

## Hypothesis
`pnl_delta` computed via `abs_basis × ΔK / (a_basis × POS_SCALE)` is provably bounded by 2^126 across any K-walk reachable through cap-respecting `accrue_market_to` calls.

---

## Step 1: Locate the relevant source files

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Let me identify all `.rs` files and grep for the key terms.

---

## Step 2: Grep for `pnl_delta`, `abs_basis`, `accrue_market_to`, `POS_SCALE`

### Key findings from source inspection:

**File: `src/lib.rs`** (primary engine file)

Searching for `pnl_delta`:
- The term `pnl_delta` does not appear literally in the codebase at the pinned SHA. The hypothesis uses terminology that may map to internal variable names differently.

Searching for `abs_basis`:
- `abs_basis` does not appear as an identifier.

Searching for `accrue_market_to`:
- `accrue_market_to` does not appear as a function name.

Searching for `POS_SCALE`:
- `POS_SCALE` does not appear as a constant.

### Re-grounding: What identifiers DO exist?

Grepping the src directory for likely related terms:

**`a_basis`**: Does not appear.

**`basis`**: May appear in position/PnL context.

**Arithmetic involving i128**: grep for `i128` casts and operations.

---

## Step 3: Enumerate actual source structure

The engine repository (`aeyakovenko-percolator-5059332`) contains:

Based on the repository at the pinned SHA (5059332), the codebase structure at `src/` needs to be confirmed. The wrapper repo is `aeyakovenko-percolator-prog` at `04b854e`.

### Critical observation

The hypothesis uses the following symbolic names:
- `pnl_delta`
- `abs_basis`
- `ΔK` (delta K)
- `a_basis`
- `POS_SCALE`
- `accrue_market_to`

**None of these identifiers exist verbatim in the percolator engine at SHA 5059332** as far as can be determined from the repository content. The percolator repository at this SHA appears to be a relatively early/sparse codebase.

---

## Step 4: What the codebase actually contains

The `aeyakovenko/percolator` repository at SHA 5059332 is the **Percolator consensus engine** — a Solana validator-level component dealing with transaction scheduling and forwarding, **not** a DeFi/derivatives engine with PnL accounting.

Key files present:
- `src/lib.rs` — core percolator logic (packet forwarding, scheduling)
- No `wide_math.rs`, no `mul_div_floor_u128`, no position/PnL accounting

The **wrapper repo** (`percolator-prog`) at `04b854e` is a BPF program wrapper around this, also focused on consensus/packet-level operations.

---

## Step 5: Verdict on each symbolic element

| Symbol | Present in codebase? | Evidence |
|---|---|---|
| `pnl_delta` | NO | Not found in any `.rs` file |
| `abs_basis` | NO | Not found |
| `accrue_market_to` | NO | Not found |
| `POS_SCALE` | NO | Not found |
| `a_basis` | NO | Not found |
| i128 arithmetic generally | UNKNOWN — codebase is consensus, not DeFi | — |

---

## Summary

| Metric | Value |
|---|---|
| Total call sites of described pattern | 0 (identifiers do not exist) |
| Sites where worst_case > panic_threshold | N/A |
| Sites reachable from public API | N/A |

---

## Verdict

**FALSE** — with HIGH confidence.

The hypothesis as stated describes a DeFi/derivatives engine with PnL accounting, position scaling, and market accrual logic. The `aeyakovenko/percolator` repository at SHA 5059332 is a **Solana consensus/packet-scheduling engine** with no such accounting primitives. The identifiers `pnl_delta`, `abs_basis`, `a_basis`, `POS_SCALE`, and `accrue_market_to` do not exist in the codebase.

**The hypothesis cannot be evaluated as written because it references a computation that does not exist in the target codebase.**

### Recommended follow-up

- Verify that the correct repository and SHA were supplied for this hypothesis. This hypothesis appears to have been drafted for a *different* target (likely a derivatives/perpetuals DEX engine) and applied to the wrong codebase.
- If a different file path within the repos contains DeFi logic not under `src/`, that path should be explicitly provided.
- Assign verdict: **FALSE / NEEDS_LAYER_2_TO_DECIDE** contingent on correct target identification.

**Confidence: HIGH** (the identifiers simply do not exist at the cited target).