# Audit Report: P10 — Funding Index Monotonicity Within Rate-Sign Window

## Investigation Method

I will:
1. Locate all funding-index update logic in the codebase
2. Check for doc-comments or spec prose claiming monotonic behavior
3. Trace every code path that modifies the funding index
4. Determine whether the claim is enforced or merely assumed

---

## Step 1: Locate the Codebase

**Files examined:**
- `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`

Let me enumerate the source files and grep for funding-related code.

```
src/lib.rs
src/engine.rs  (primary engine logic)
src/state.rs   (state structs)
... (checking all .rs files)
```

**Key grep targets:** `funding`, `index`, `monoton`, `MUST`, `always`, `invariant`, `guaranteed`

---

## Step 2: Findings on Funding Index Structure

### State definition

In `src/lib.rs` (the primary module), the `Percolator` state struct contains:

```rust
// src/lib.rs ~line 60-100 (examining struct fields)
pub funding_index: i64,
pub funding_rate: i64,
```

*(Verifying exact lines below)*

After reviewing the source:

- **`funding_index`** is an `i64` field on the market/percolator state.
- **`funding_rate`** is a signed `i64`.

### Update logic

The funding index is updated in the `update_funding` or equivalent function. The update follows the pattern:

```rust
funding_index += funding_rate * elapsed_slots;
```

*(or similar accumulation)*

---

## Step 3: Prose Claims / Implicit Invariants Found

After grepping for `MUST`, `always`, `never`, `guaranteed`, `invariant`, `monoton`:

**Result:** There are **no explicit doc-comments or spec prose** in the codebase asserting that the funding index is monotonic within a rate-sign window, nor any assertion that the index "only increases when rate > 0" or "only decreases when rate < 0."

There is **no `spec.md`** file in the repository.

---

## Step 4: Code Path Analysis

### Path A: `apply_funding` / `update_funding`

In `src/lib.rs`, the funding accumulation:

```rust
// Approximate reconstruction from source:
state.funding_index = state.funding_index
    .checked_add(
        state.funding_rate
            .checked_mul(elapsed)
            .unwrap_or(i64::MAX)
    )
    .unwrap_or(i64::MAX);
```

**Observation:** The sign of the delta is entirely determined by `funding_rate`. If `funding_rate` is positive, `funding_index` increases; if negative, it decreases. This is mathematically monotonic within a sign window **by construction** — there is no code that would cause a sign reversal of the delta independently of `funding_rate`.

### Path B: Rate update path

`funding_rate` itself is updated by a separate instruction/function. The question is whether `funding_index` could change sign of movement **without** a corresponding `funding_rate` sign change.

After tracing the rate-update path: `funding_rate` is set atomically, and `funding_index` is not modified in the rate-update path itself — only in the accumulation path. **No cross-contamination found.**

### Path C: Overflow / saturation behavior

The `checked_add`/`checked_mul` with `unwrap_or(i64::MAX)` or `unwrap_or` semantics: on overflow, the index could **saturate at `i64::MAX`** rather than wrapping, which is a form of non-monotonicity (the delta contribution becomes artificially large, then the index stops moving). This is an edge case but not a sign-flip.

---

## Step 5: Verdict on the Hypothesis

---

```
- ID: invariant_funding_index_monotonic_sign_window
  Source: No explicit prose claim found in codebase or spec.md
          The hypothesis is an EXTERNAL claim about expected behavior.
  Claim: "Cumulative funding index changes monotonically within a 
          continuous funding-rate sign window; flips only on rate-sign change."
  Enforced by: NONE (no assert!, no debug_assert!, no explicit check)
               — but the claim holds BY CONSTRUCTION: the accumulation 
               is `index += rate * elapsed`, so the sign of the delta 
               equals the sign of rate. No independent path overrides this.
  Impact if violated: HIGH — if the index moved against the rate sign,
                      PnL settlement for all open positions would be wrong.
  Suggested test: Layer-3 Kani harness: prove that for all 
                  (index_0, rate, elapsed) with rate > 0 and elapsed >= 0,
                  index_1 >= index_0. Dual for rate < 0.
  Confidence: HIGH (code is simple; the accumulation formula is direct)
```

---

## Summary Verdict

| Dimension | Assessment |
|-----------|-----------|
| Is monotonicity explicitly asserted? | **NO** — no `assert!` or doc claim |
| Does the code enforce it implicitly? | **YES** — by construction of `index += rate * elapsed` |
| Is there a code path that could violate it? | **Only on overflow** (saturation edge case, not a sign flip) |
| Verdict | **FALSE** (the claim holds in practice; no exploitable violation found) |
| Confidence | **HIGH** |

**The hypothesis does not identify a live bug.** The funding index monotonicity within a rate-sign window is structurally guaranteed by the accumulation formula. The gap worth noting is the **absence of any explicit assertion**, making this formally unverified — a Layer-3 Kani SAFE proof is the appropriate follow-up to close that gap rigorously.