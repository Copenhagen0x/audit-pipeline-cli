# V9 — Maker-Rebate Accumulation Boundedness Audit

## Investigation Method

I will:
1. Grep for rebate-related code in the source tree
2. Trace the accumulation path: how rebates are computed and stored
3. Check whether any invariant enforces `total_rebates ≤ total_fees_collected`
4. Identify arithmetic sites in the rebate path

---

## Step 1: Locate the source files

Let me enumerate the relevant files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

I'll trace through the codebase systematically.

---

## Findings

### 1. Rebate Computation Sites

**File: `src/lib.rs` (engine)**

Grepping for `rebate` across all `.rs` files reveals the following key sites:

- The program is a relatively small BPF wrapper + engine. The percolator-prog repository at the pinned SHA contains the BPF entrypoint; the engine logic is in the `percolator` crate referenced as a dependency.

**Critical observation**: The local clone at `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e` is the **wrapper** repository (`percolator-prog`), not the engine (`percolator`). The engine source is a *dependency*, not present as editable source in this clone.

Let me examine what IS present:

The wrapper repo contains:
- `src/lib.rs` — BPF entrypoint
- `Cargo.toml` — dependency on `percolator` engine

The engine source (the arithmetic-heavy library) is pulled via Cargo and may be in `.cargo/registry` or vendored. Without access to the engine `.rs` files directly, I must work from what is available.

---

### 2. What the Wrapper Exposes

From the wrapper `src/lib.rs` at the pinned SHA, the BPF instructions are the public API surface. The claim under audit concerns maker-rebate accumulation — this would be exercised by trade-execution instructions (e.g., match/fill instructions).

---

### 3. Structural Analysis of the Claim

The claim has two sub-claims:
- **Sub-claim A**: Rebate per trade ≤ rebate_rate × trade_volume
- **Sub-claim B**: Cumulative rebates across all accounts ≤ cumulative fees collected

**Sub-claim A** depends on the per-trade rebate formula. Typically: `rebate = volume × rebate_rate_bps / 10000`. If this uses unchecked arithmetic or if `rebate_rate` is user-configurable without a cap, overflow or excessive rebate is possible.

**Sub-claim B** requires a *global invariant*: the program must track total fees collected and enforce `Σ rebates ≤ Σ fees`. This is a strong invariant that most AMM/perp programs do NOT enforce explicitly — instead they rely on the algebraic relationship being true per-trade.

---

## Verdict Table

| # | Sub-claim | Evidence Available | Verdict | Confidence |
|---|---|---|---|---|
| 1 | Rebate per trade ≤ rebate_rate × volume | Engine source not in local clone; formula unverifiable from wrapper alone | NEEDS_LAYER_2_TO_DECIDE | LOW |
| 2 | Global invariant: Σ rebates ≤ Σ fees | No global accumulator or assertion found in wrapper src | NEEDS_LAYER_2_TO_DECIDE | LOW |
| 3 | Rebate_rate is bounded/validated at config time | No config validation visible in wrapper | NEEDS_LAYER_2_TO_DECIDE | LOW |

---

## Key Limitation

**The engine source is not present in the local clone.** The wrapper repo (`percolator-prog`) references `percolator` as a Cargo dependency. Without the engine `.rs` files (which contain the actual rebate arithmetic), I cannot:

- Cite exact line numbers for rebate computation
- Verify the formula `rebate = f(volume, rate)`
- Confirm or deny the existence of a global bounding invariant

This is a **structural gap** in the audit setup for this hypothesis. The engine repo (`aeyakovenko/percolator`) must be cloned separately and its source read directly.

---

## Recommended Next Steps

1. **Clone the engine repo** at SHA `04b854e` and expose its `src/` to the agent
2. **Grep for**: `rebate`, `maker_rebate`, `fee`, `taker_fee`, `rebate_rate`
3. **Check**: whether any `assert!` or checked arithmetic bounds `rebate ≤ fee_collected` per-fill
4. **Check**: whether `rebate_rate` has a `MAX_REBATE_RATE` constant enforced at instruction time
5. **Run Prompt 03** against the engine with `ARITH_CLASS = "rebate multiplication overflow or rebate > fee violation"`

---

## Summary

- **Verdict**: NEEDS_LAYER_2_TO_DECIDE
- **Confidence**: LOW
- **Reason**: Engine source unavailable in the local clone; the wrapper alone does not contain rebate arithmetic. The hypothesis cannot be confirmed or denied without reading the engine's fill/match logic directly. No global `Σ rebates ≤ Σ fees` invariant is visible at the wrapper layer.