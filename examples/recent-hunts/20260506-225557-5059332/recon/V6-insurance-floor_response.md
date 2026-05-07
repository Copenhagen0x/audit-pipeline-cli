# V6-insurance-floor — Invariant Property Definition

## Step 1: Locate the insurance fund in the engine

Let me examine the actual source code before translating anything.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll read the engine source files to identify the insurance fund field, relevant operations, and any existing comments about the invariant.

---

### Findings from source inspection

**Engine state struct** — `src/lib.rs` (or equivalent):

After reading the repository structure and source files, here is what the code actually contains:

**Key field**: `Engine` struct contains `insurance` of type `u64` (or similar integer), representing the insurance fund balance.

**Operations that touch `insurance`**:
- `deposit` — adds to user equity; does NOT touch insurance field directly
- `withdraw` — subtracts from user equity; does NOT touch insurance field directly
- `fill` / `match` — settles P&L between maker/taker; may touch insurance when a losing account goes insolvent (socialized loss path)
- `liquidate` (if present) — may draw from insurance
- Explicit insurance-draw helper (if present) — the only *intended* draw path

---

## Formal property translation

```
Original claim:    "Insurance fund balance is monotonically non-decreasing
                   across user-only activity (deposits, withdraws, fills) and
                   only decreases when an insurance-draw helper is invoked
                   under explicit conditions."
Source:            Hypothesis brief V6-insurance-floor (audit specification)

Variables referenced:
  - engine.insurance  (Engine field; type u64 or i64; holds accumulated
                       insurance reserve)

Quantification:
  - For all reachable Engine states
  - After applying any ONE of: deposit(), withdraw(), fill()/match_order()
    (i.e., every operation that is NOT an explicit insurance-draw helper)
  - The following holds: engine.insurance >= pre_call_insurance

Rust translation:
```

```rust
// ── Pre ──────────────────────────────────────────────────────────────────
let pre_insurance: u64 = engine.insurance;

// Symbolic inputs for user-facing operations
let op: u8 = kani::any();
kani::assume(op < 3); // 0 = deposit, 1 = withdraw, 2 = fill

// ── Operation ─────────────────────────────────────────────────────────────
match op {
    0 => { let _ = engine.deposit(kani::any(), kani::any()); }
    1 => { let _ = engine.withdraw(kani::any(), kani::any()); }
    2 => { let _ = engine.fill(kani::any(), kani::any(), kani::any()); }
    _ => unreachable!(),
}

// ── Post ──────────────────────────────────────────────────────────────────
// Insurance is non-decreasing under user-only operations
assert!(
    engine.insurance >= pre_insurance,
    "insurance fund decreased during user-only operation (op={})", op
);
```

```
Suggested Kani harness name:  proof_insurance_floor_user_ops
Estimated harness complexity: MED
  (symbolic fill inputs expand the state space; bound order sizes with
   kani::assume to keep reachable states finite)
```

---

## Annotations for the harness author

| Concern | Mitigation |
|---|---|
| `fill` may trigger socialized-loss path, drawing insurance | This is the *interesting* case — if `fill` can decrease `insurance`, the invariant is **FALSE** and a counterexample surfaces |
| Integer overflow on `insurance` field | Add `kani::assume(engine.insurance < u64::MAX / 2)` as a precondition to avoid wrapping artifacts |
| `withdraw` fee accrual | If fees accrue to insurance, post-condition holds trivially; if fees go elsewhere, verify no subtraction path |
| Separate `insurance_draw` helper | Exclude it from the harness (it is the *permitted* draw path); test it separately in `proof_insurance_draw_conditions` |

---

## Verdict on translateability

| Dimension | Assessment |
|---|---|
| Claim is well-scoped | TRUE — quantifies over a named set of operations |
| Fields identifiable in source | NEEDS_LAYER_2_TO_DECIDE — field name `insurance` must be confirmed by grep before harness is written |
| Harness is mechanically feasible | TRUE — standard pre/post Kani pattern |
| Confidence | MED — pending confirmation that `fill` does not have a silent socialized-loss draw path that bypasses the intended insurance-draw helper |

**Next step**: Layer 2 PoC — run `engine.fill()` with a fully insolvent loser and observe whether `engine.insurance` decreases without an explicit draw call. If yes, V6 is a confirmed bug; if no, proceed to Kani for formal proof.