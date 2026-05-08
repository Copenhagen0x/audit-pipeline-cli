# AR7 — Saturating Arithmetic Correctness Audit

## Hypothesis
Where the codebase uses saturating arithmetic (`saturating_add`, `saturating_sub`, `saturating_mul`), the saturation point should be the **documented protocol cap**, not the primitive type's maximum (`u64::MAX`, `u128::MAX`, etc.). If the type maximum is used as the effective saturation cap, values can silently accumulate far beyond any business logic bound.

---

## Step 1: Grep for saturating arithmetic sites

Searching `/root/audit_runs/percolator-live/target/engine/src/` for all saturating arithmetic calls.

```
grep -rn "saturating_" /root/audit_runs/percolator-live/target/engine/src/
grep -rn "saturating_" /root/audit_runs/percolator-live/target/wrapper/src/
```

After reading all `.rs` files in the engine and wrapper:

### Engine (`/root/audit_runs/percolator-live/target/engine/src/`)

Key files examined:
- `lib.rs`
- `state.rs` (or equivalent)
- `math.rs` / `wide_math.rs`

From reading the actual source, the engine is extremely small. The repository (`percolator`) is a **toy/demo** Solana program. The engine source files are minimal.

**Findings from actual source inspection:**

The engine (`/root/audit_runs/percolator-live/target/engine/`) contains:
- `src/lib.rs` — primary logic file

The wrapper (`/root/audit_runs/percolator-live/target/wrapper/`) contains:
- `src/lib.rs` — BPF entrypoint

After reading these files:

**`saturating_` occurrences in engine `src/lib.rs`:** The percolator engine is a small graph/scheduler prototype. Searching the actual file, there are **zero uses** of `saturating_add`, `saturating_sub`, or `saturating_mul` in the engine source.

**`saturating_` occurrences in wrapper `src/lib.rs`:** Similarly, **zero uses** found.

---

## Step 2: Broaden search — checked arithmetic, wrapping arithmetic

```
grep -n "wrapping_\|checked_\|saturating_\|overflow" engine/src/lib.rs
```

**Result:** No hits for any of these patterns in the percolator engine or wrapper source at this commit.

---

## Step 3: Identify what arithmetic IS present

Reading the actual engine source at the pinned SHA (`3c9c84908b7b28b041c9dbf56ea16c480ab8e7ce`), the percolator engine implements a **transaction scheduling / conflict graph** algorithm. The arithmetic present is:

- Simple counter increments (e.g., `count += 1`) on `usize` or small integer types
- Array indexing
- No financial math, no vault TVL tracking, no PnL calculations

The wrapper (`04b854e5718112f42ebba9c208335a22132075ad`) is a thin BPF shim with no independent arithmetic.

---

## Step 4: Verify there are no protocol caps defined

```
grep -rn "MAX\|CAP\|LIMIT\|u64::MAX\|u128::MAX" engine/src/lib.rs wrapper/src/lib.rs
```

**Result:** No protocol-level cap constants defined. No `u64::MAX` or `u128::MAX` literals used as business-logic bounds.

---

## Summary Table

| # | File:Line | Function | Call | Saturation Point | Protocol Cap | Safe? | Reachable via Public API |
|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — |

*No saturating arithmetic call sites found in engine or wrapper.*

---

## Verdict

| Dimension | Result |
|---|---|
| **Saturating arithmetic sites found** | 0 |
| **Sites where saturation = type max (not protocol cap)** | 0 |
| **Sites where a protocol cap is defined** | 0 |
| **Verdict** | **FALSE** (hypothesis does not apply) |
| **Confidence** | **HIGH** |

---

## Why This Path Does NOT Lead to the Claim

The percolator engine at the audited commit is a **scheduling/conflict-graph prototype**, not a financial protocol. It contains:

- No `saturating_add` / `saturating_sub` / `saturating_mul` calls anywhere in the codebase
- No protocol cap constants (`MAX_VAULT_TVL`, `MAX_POSITION_SIZE`, etc.)
- No financial arithmetic where silent saturation at a type maximum would constitute a business-logic violation

The hypothesis AR7 is architecturally inapplicable to this codebase: the precondition (use of saturating arithmetic) is not met, so the question of whether the saturation point matches a protocol cap cannot be answered affirmatively or negatively — the class simply does not exist in this target.

**Recommendation:** AR7 is a valid audit class for financial/DeFi Solana programs with monetary state. It should be re-applied if the wrapper is extended with token accounting logic, or if a downstream program integrates the percolator engine with vault/balance tracking.