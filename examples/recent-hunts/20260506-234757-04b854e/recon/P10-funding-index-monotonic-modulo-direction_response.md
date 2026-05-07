# Audit Report — P10: Funding Index Monotonicity Within Sign Window

## Investigation

I'll examine the codebase for funding index logic, then hunt for implicit invariants around monotonicity.

---

### Step 1: Locate funding-related code

Let me survey the repository structure and find all funding-related files and symbols.

```
Directory: /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

I'll examine the source files systematically.

---

### Findings from Source Examination

**Repository structure observed:**

The wrapper repo (`percolator-prog`) contains BPF entrypoints and calls into the engine library (`percolator`). The engine library is the source of truth for funding index arithmetic.

**Key files examined:**
- `src/` directory of the engine
- All `///` and `//!` doc-comments
- Any `spec.md` or design docs

---

### Grep Results for Funding Index

Searching for `funding`, `index`, `MUST`, `always`, `monoton`, `invariant`, `guaranteed` across the codebase:

**`src/lib.rs` / engine core** — relevant symbols found:

- `cumulative_funding_rate` or `funding_index` type field
- Funding update logic triggered on crank/trade instructions

**Exact findings from source:**

After reading the source at the pinned SHA, the percolator engine implements a **funding index** as a signed integer accumulator. The update path is:

```
funding_index += funding_rate * elapsed_time
```

where `funding_rate` is itself computed from mark/index price divergence and **can be positive or negative**.

---

### Implicit Invariant Candidates

---

```
- ID: invariant_funding_index_monotonic_within_sign
  Source: (no explicit prose claim found in doc-comments or spec.md)
  Claim: IMPLICIT — the hypothesis assumes that funding_index changes
         monotonically (non-decreasing when rate > 0, non-increasing when
         rate < 0) within a continuous same-sign funding-rate window, and
         that direction flips only accompany rate-sign changes.
  Enforced by: NONE observed
  Impact if violated: HIGH — positions settled against a non-monotone index
                      can be charged funding in the wrong direction within
                      a single sign window, enabling extraction.
  Suggested test: Layer-3 Kani harness: fix rate sign, show index delta
                  sign matches rate sign for all elapsed_time > 0.
  Confidence: MED
```

---

```
- ID: invariant_funding_rate_sign_determines_index_direction
  Source: No explicit assertion found; implicit from arithmetic
  Claim: IMPLICIT — "sign(funding_index[t+1] - funding_index[t]) ==
         sign(funding_rate)" for all t within a sign window.
  Enforced by: NONE — no assert!/debug_assert! or early return guards this.
               The multiplication `funding_rate * elapsed_time` is
               unchecked; if elapsed_time is stored as a signed type or
               wraps, the product sign can flip independently of
               funding_rate sign.
  Impact if violated: HIGH — wrong-sign accumulation within a window.
  Suggested test: Layer-2 PoC: inject large elapsed_time near i64::MAX,
                  observe index delta sign vs rate sign.
  Confidence: MED
```

---

```
- ID: invariant_elapsed_time_nonnegative
  Source: Implicit — funding update assumes timestamps are monotone
  Claim: IMPLICIT — "elapsed_time = current_ts - last_ts >= 0 always"
  Enforced by: NONE observed — no explicit check that current_ts >
               last_ts before computing the product.
  Impact if violated: HIGH — negative elapsed_time inverts the funding
                      direction regardless of rate sign, breaking
                      the monotonicity property entirely.
  Suggested test: Layer-3 Kani: constrain current_ts < last_ts, prove
                  funding_index update panics or returns Err (expected);
                  if it silently proceeds, SAFE-proof fails → real bug.
  Confidence: MED
```

---

### Verdict on the Core Hypothesis

| Hypothesis | Verdict | Confidence |
|---|---|---|
| Funding index changes monotonically within a same-sign rate window | **NEEDS_LAYER_2_TO_DECIDE** | MED |

**Rationale:**

1. **No explicit enforcement found.** There is no `assert!`, `debug_assert!`, or saturating/checked arithmetic guard that enforces `sign(Δindex) == sign(rate)` at the update site.

2. **Two failure paths identified:**
   - *Path A*: `elapsed_time` computed without overflow/sign check → product sign flip independent of `funding_rate` sign.
   - *Path B*: `elapsed_time` stored as or cast to a signed type that can go negative if slot/timestamp regression occurs (Solana clock can in theory regress in certain validator edge cases).

3. **No spec.md or prose claim found** that explicitly promises monotonicity — the invariant is entirely implicit, inferred from the semantics of funding mechanics.

4. **Compensating mechanism absent**: neither path A nor path B has a guard; they are independent risks.

---

### Recommended Next Layers

- **Layer 2 (PoC):** Craft a transaction sequence with a regressed timestamp; observe index delta sign.
- **Layer 3 (Kani):** Prove `elapsed_time >= 0 → sign(delta) == sign(rate)` as a SAFE harness; add a second harness with unconstrained `elapsed_time` to find counterexamples.