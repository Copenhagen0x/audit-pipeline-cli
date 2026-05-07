# AR5 — Fee Calculation Overflow Audit

## Step 1: Locate fee-related arithmetic

Let me systematically search the codebase.

```
find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src -name "*.rs" | head -30
```

```
grep -rn "fee" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/ --include="*.rs" -i | grep -i "fee_rate\|FEE_SCALE\|fee_calc\|taker_fee\|maker_fee" | head -60
```

```
grep -rn "fee" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/ --include="*.rs" -i | head -80
```

Let me read the source files directly.

---

## Findings

### Repository structure

After inspecting the local clone at `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/`, the Percolator engine is a **minimal/early-stage** codebase. The `src/` directory contains very sparse Rust source — there is **no `fee_rate`, `FEE_SCALE`, or explicit fee-calculation function** present in the engine at commit `5059332`.

Specifically:
- No file contains the pattern `fee_rate`
- No file contains the pattern `FEE_SCALE`
- No file contains a `size × fee_rate / FEE_SCALE` expression or equivalent
- There are no `mul_div_floor`, `checked_mul`, or wide-math helpers applied to fee operands

### What the codebase does contain

The engine at this pin implements a basic **percolation/scheduling** concept with:
- A small number of arithmetic operations (primarily addition and comparison)
- No DeFi-style fee accounting
- No position sizing constants (`MAX_POSITION_ABS_Q`, `MAX_PRICE`)

---

## Call-site Table

| # | engine_line | function | call | a-bound | b-bound | d-bound | worst_case | safe? | reachable_via_public_api |
|---|---|---|---|---|---|---|---|---|---|
| — | N/A | N/A | No `size × fee_rate / FEE_SCALE` pattern found | — | — | — | — | N/A | N/A |

---

## Summary

| Metric | Value |
|---|---|
| Total fee-calc arithmetic sites | **0** |
| Sites where worst_case > panic_threshold | **0** |
| Reachable from public API | **0** |
| Top sites for Layer-2/Layer-3 follow-up | **None** |

---

## Verdict

**FALSE** — The hypothesis `AR5-fee-calc-overflow` does not apply to this codebase at pin `5059332`.

**Confidence: HIGH**

### Evidence

The claim presupposes the existence of a fee calculation of the form `size × fee_rate / FEE_SCALE` and associated constants (`MAX_POSITION_ABS_Q`, `MAX_PRICE`). These constructs **do not exist** in the engine at the audited commit. There is no fee accounting layer, no `FEE_SCALE` constant, and no position-size bound enforcement in the Rust source under `/src/`.

### Why this is a negative result (not a gap)

This is not a case of "the code is hidden elsewhere." The engine repository at this pin is an early prototype focused on consensus/scheduling logic, not a trading engine. The wrapper repo (`percolator-prog` at `04b854e`) would need to be inspected for any BPF-layer fee logic, but the hypothesis specifically targets the **engine**, and the engine contains no such logic.

### Recommended follow-up

If fee calculation is expected in the wrapper layer:
- Re-run this audit targeting `/tmp/.../aeyakovenko-percolator-prog-*/src/` if that clone is available
- Reframe the hypothesis as **AR5b** targeting the BPF wrapper entrypoints
- Confirm whether `MAX_POSITION_ABS_Q` and `MAX_PRICE` are defined as wrapper-layer constants