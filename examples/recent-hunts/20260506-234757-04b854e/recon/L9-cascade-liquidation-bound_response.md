I'll systematically investigate whether a single instruction can trigger unbounded cascade liquidations in the percolator codebase.

## Step 1: Explore the repository structure

Let me first understand what files exist.

**Directory listing of the target repo:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

Let me read the key files.

---

## Investigation

### File structure scan

Reading the source tree at `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/`:

I'll grep for cascade/liquidation-related terms, then trace the enforcement.

---

### Grep results

**Searching for "cascade", "liquidat", "bound", "MAX", "MUST", "must", "always", "never", "guaranteed", "invariant":**

After reading the available files in the repository, here is the structured finding:

---

## Findings

---

### Candidate Implicit Invariant

```
- ID: invariant_cascade_liquidation_bound
  Source: NEEDS_LAYER_2_TO_DECIDE — see analysis below
  Claim: "A single instruction cannot trigger more than the configured
          cascade bound of liquidations"
  Enforced by: NONE found via static analysis
  Impact if violated: HIGH
  Confidence: LOW (codebase lacks accessible source confirming or denying
               the bound enforcement)
```

---

## Detailed Analysis

### What I found

After attempting to read all Rust source files under:

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

The repository (`percolator-prog`) is a **BPF wrapper** around the engine library (`percolator`). The wrapper repo at the pinned SHA contains BPF entrypoint glue. The engine logic lives in the separate `percolator` engine repo.

**Key observation:** The hypothesis references a "configured cascade bound." I searched for:

- `cascade` — no matches in wrapper source
- `liquidat` — no matches in wrapper source
- `MAX_LIQUIDAT`, `LIQUIDATION_BOUND`, `cascade_limit`, `cascade_bound` — no matches
- `MUST`, `must`, `always`, `never`, `guaranteed` — no doc-comment enforcement clauses found that reference liquidation cascades

### What this means

The `percolator-prog` wrapper is thin. It delegates to the engine. **If a cascade bound exists, it would be enforced in the engine library (`percolator`), not in the wrapper.** The wrapper repo alone is insufficient to verify or falsify this hypothesis.

### Implicit invariant candidates from what IS present

From reading available source in the wrapper:

```
- ID: invariant_cascade_liquidation_bound
  Source: hypothesis statement (no spec.md found in wrapper repo)
  Claim: "cascade is bounded per instruction"
  Enforced by: NONE visible in wrapper layer
  Impact if violated: HIGH — an attacker could craft an account state
                      that triggers O(N) liquidations in a single CPI
                      chain, consuming all compute units or corrupting
                      intermediate state
  Suggested test: Layer-2 PoC: construct a position graph where N accounts
                  are each just below liquidation threshold, submit one
                  price-update instruction, observe whether the runtime
                  aborts after a fixed bound or processes all N
  Confidence: LOW (engine source not directly accessible for verification)
```

---

## Verdict

| Attribute | Value |
|-----------|-------|
| **Verdict** | `NEEDS_LAYER_2_TO_DECIDE` |
| **Confidence** | `LOW` |
| **Reason** | The wrapper repo contains no cascade liquidation logic or bound enforcement. The engine repo source must be inspected at the pinned SHA. No `spec.md` was found. No `assert!`, `debug_assert!`, or `return Err(...)` guarding a cascade count was located in the wrapper. |

---

## Recommended Next Steps

1. **Read engine source** at `https://github.com/aeyakovenko/percolator` pin `04b854e` — specifically grep for `cascade`, `liquidat`, `MAX_`, `loop`, `while` in the core settlement/matching logic.
2. **Layer-2 PoC**: Set up a LiteSVM test with N=100 accounts each marginally under-collateralized, trigger one liquidation-initiating instruction, observe whether exactly 1 or all N liquidations execute.
3. **Layer-3 Kani**: If a `cascade_limit: usize` field exists, write a harness asserting `liquidations_processed <= cascade_limit` as a SAFE property.
4. **Check for loop bounds** — any `for account in accounts` or `while let Some(...)` without an explicit iteration counter is a red flag for unbounded cascade.

---

## Non-finding note

The absence of cascade bound enforcement in the **wrapper** layer is **not** evidence of a bug — this logic appropriately belongs in the engine. The finding is that **no compensating mechanism exists at the wrapper boundary** (e.g., no pre-check on account count before delegating to engine), which means the wrapper provides zero independent protection if the engine bound is absent or wrong.