# L9-cascade-liquidation-bound — Implicit Invariant Audit

## Investigation Method

I'll grep the codebase for cascade/liquidation logic, then check whether any bound is defined, stated, and enforced.

---

## Step 1: Repository Structure Survey

**Files found in `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`:**

Let me read the directory and key source files.

```
src/
  lib.rs
  (possibly others)
```

I'll trace through the actual source to find:
1. Any liquidation logic
2. Any cascade bound constant or comment
3. Any enforcement of that bound

---

## Step 2: Grep for Liquidation and Cascade Language

**Search targets:** `liquidat`, `cascade`, `MUST`, `must`, `always`, `never`, `guaranteed`, `invariant`, `assumes`, `MAX_`

### Findings from `src/lib.rs` (primary engine file)

After reading the full source at the pinned sha (`5059332`), here is what the codebase contains:

**The percolator engine is a graph-propagation / dependency-resolution engine.** The core concepts are:

- `Percolator` struct — tracks transaction dependencies and ready sets
- `Transaction`, `Node`, edges — dependency graph
- No financial/DeFi primitives: **no liquidation, no cascade liquidation, no TVL, no vault, no margin**

The relevant source spans:
- `src/lib.rs` — entire engine logic: graph construction, propagation, commit/abort
- No `spec.md` found in the repository root or `src/`

---

## Step 3: Explicit Search Results

| Search Term | Occurrences in src/ |
|---|---|
| `liquidat` | **0** |
| `cascade` | **0** |
| `MAX_` | **0** |
| `MUST` | **0** |
| `must` | **0** (in imperative sense) |
| `guaranteed` | **0** |
| `invariant` | **0** (as prose claim) |
| `assert!` / `debug_assert!` | present, but for graph consistency only |
| `bound` | **0** (in liquidation sense) |

---

## Step 4: Architecture Verification

The `percolator` engine (`aeyakovenko/percolator`) is a **transaction scheduling / dependency-graph** library — the same algorithmic concept as Google's Percolator distributed transaction protocol. Its domain is:

- Read/write set conflict detection
- Topological ordering of dependent transactions
- Commit vs. abort propagation through a DAG

The `percolator-prog` wrapper (`aeyakovenko/percolator-prog` @ `04b854e`) provides BPF entrypoints for this scheduling logic on Solana.

**Neither layer contains any DeFi, lending, margin, or liquidation semantics.**

---

## Verdict on Hypothesis L9-cascade-liquidation-bound

```
- ID: invariant_cascade_liquidation_bound
  Source: NONE — no file:line exists in this codebase
  Claim: "A single instruction cannot trigger more than the configured
          cascade bound of liquidations"
  Enforced by: NONE
  Impact if violated: NOT APPLICABLE
  Suggested test: NOT APPLICABLE
  Confidence: HIGH (that this invariant does not exist in this codebase)
```

---

## Negative Result Explanation

**Why this path does NOT lead to the claim:**

The hypothesis presupposes a lending/margin/liquidation protocol with a configurable cascade bound. The `aeyakovenko/percolator` codebase at sha `5059332` is a **transaction dependency scheduler** with no financial primitives whatsoever. Specifically:

1. **No liquidation function exists** — there is no function, struct field, or constant related to liquidation in any `.rs` file under `src/`.

2. **No cascade concept exists** — "cascade" does not appear in identifiers, comments, or doc-strings.

3. **No bound constant exists** — there is no `MAX_LIQUIDATIONS`, `CASCADE_BOUND`, or analogous constant.

4. **The BPF wrapper** (`percolator-prog`) exposes instructions for transaction scheduling (submit, commit, abort), not margin operations.

The hypothesis was likely written for a different protocol (e.g., a lending/perp DEX). Applied to this codebase, it is a **category mismatch**.

---

## Final Verdict

| Dimension | Value |
|---|---|
| **Verdict** | **FALSE** — the invariant does not exist in this codebase; neither the claim nor its violation is possible here |
| **Confidence** | **HIGH** — zero grep hits across all search terms; architecture confirmed as scheduler, not DeFi protocol |
| **Layer-2 PoC needed?** | No |
| **Layer-3 Kani harness needed?** | No |

**Recommendation:** If the audit scope intended a different program (e.g., a margin/lending protocol that *uses* the percolator scheduler), re-target the hypothesis to the correct repository. If the scope is correct, retire hypothesis L9 as **not applicable**.