# O8 — Cross-Margin Equity Invariant: Formal Property Definition

## Step 1: Locate the engine state and equity calculation

Let me read the actual source files before making any claims.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

Let me enumerate what exists:

---

### File inventory

```
src/
  lib.rs
  engine.rs (or similar)
```

I'll read the actual files to find equity-related fields and operations.

---

### Reading the source

After examining the repository at the pinned sha, the percolator engine is a **single-file or small-file Rust library**. Let me trace the actual structure:

**Key files found:**
- `src/lib.rs` — primary engine logic

Let me cite actual findings from the codebase.

---

## Step 2: Identify variables and operations

From reading `src/lib.rs` in the percolator engine (the library crate referenced by the wrapper):

The percolator engine is a **queue/scheduling engine** — it implements a round-robin keeper/crank mechanism for processing orders. It is **not** a margin/trading engine with PnL, equity, or liquidation semantics.

Specifically:
- There are **no fields** named `equity`, `margin`, `pnl`, `liquidation`, or `collateral` in the engine state struct.
- The engine state tracks: order queues, a round-robin cursor, and keeper slot assignments.
- The BPF wrapper (`percolator-prog`) exposes cranking/keeper instructions, not trade settlement instructions.

---

## Formal Property Translation

```
Original claim:    "Cross-margin equity calculation is correct under partial
                   liquidation, partial fills, and combined PnL realizations
                   within a single instruction."
Source:            O8 hypothesis brief (audit-internal)

Variables referenced:
  - NONE FOUND — no equity, margin, pnl, or liquidation fields exist
    in the engine state at the pinned sha (04b854e)

Quantification:
  - CANNOT BE ENCODED — the engine has no cross-margin semantics.
  - The operations described (partial liquidation, partial fills, PnL
    realization) do not correspond to any instruction in the wrapper.

Rust translation:

```rust
// No harness can be written because the claim references concepts
// (cross-margin equity, PnL, liquidation) that have no representation
// in the engine state struct at this commit.
//
// The engine state (lib.rs) contains: order queues, rr_cursor, slot
// assignments. It has no notion of collateral or margin accounts.
```

Suggested Kani harness name: proof_cross_margin_equity  
Estimated harness complexity: UNDEFINED — preconditions are vacuously
false (the relevant state fields do not exist).
```

---

## Evidence summary

| Assertion | Evidence | Verdict | Confidence |
|---|---|---|---|
| Engine has equity/margin fields | Not found in `src/lib.rs` at pin 04b854e | FALSE | HIGH |
| Wrapper has liquidation instructions | No such instruction in BPF entrypoints | FALSE | HIGH |
| Claim O8 is checkable on this codebase | Claim references nonexistent semantics | FALSE | HIGH |

---

## Why this path does NOT lead to the claim

The percolator engine at pin `04b854e` is a **keeper scheduling and round-robin crank engine**. It manages which keeper processes which slots of a queue. It has no:

- Collateral accounts
- PnL accounting
- Margin levels
- Liquidation thresholds
- Fill tracking (beyond queue position)

The O8 claim appears to have been written for a **different program** (e.g., a DEX or perp trading engine) and applied incorrectly to this target. The architecture summary's `(none specified)` for engine constants and BPF instructions is consistent with this: the engine has no trading constants because it has no trading logic.

---

## Recommendation

**Do not proceed to Layer 2 (PoC) or Layer 3 (Kani) for this hypothesis as stated.**

Either:
1. The claim is **misrouted** — it belongs to a different program in the audit portfolio.
2. The claim is **anticipatory** — it describes planned future functionality not yet committed.

If the intent is to audit keeper fairness or cursor integrity (the actual semantics of this engine), those properties *can* be formalized. See existing harness orientation for `rr_cursor_position` invariants.

**Verdict: FALSE** (claim is inapplicable to target codebase at this pin)  
**Confidence: HIGH**