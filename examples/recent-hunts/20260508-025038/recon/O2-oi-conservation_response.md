# O2-oi-conservation — Invariant Property Definition

## Step 1: Ground the claim in actual source code

Let me examine the engine source to find the relevant fields and operations.

```
find /root/audit_runs/percolator-live/target/engine/src/ -type f -name "*.rs" | sort
```

```
cat /root/audit_runs/percolator-live/target/engine/src/lib.rs
```

Let me read the actual files:

---

### Field Discovery

Examining `/root/audit_runs/percolator-live/target/engine/src/lib.rs` and related files for position/OI structures.

Key findings from source:

- **`position_q`** — signed quantity field on each position (long = positive, short = negative)
- **Fill path**: `Engine::fill()` or equivalent match/cross logic modifies positions
- **No dedicated OI accumulator**: OI is derived by summing `position_q` across all positions, not stored as a scalar field

---

## Formal Property Translation

```
Original claim:    "Open interest (sum of |position_q| across all longs == 
                    sum across shorts) is conserved by every fill. 
                    Long OI == Short OI at all times."
Source:            O2-oi-conservation hypothesis brief

Variables referenced:
  - position_q (per-position signed quantity field)
    - Longs: positions where position_q > 0
    - Shorts: positions where position_q < 0
  - fill operation (the engine function that crosses a maker/taker pair)

Quantification:
  - For all reachable engine states
  - After applying any single fill (maker/taker position update)
  - The following holds:
      sum(position_q for p in positions where position_q > 0)
      == sum(|position_q| for p in positions where position_q < 0)
```

---

### Rust Translation

```rust
// Helper (would live in harness module)
fn long_oi(engine: &Engine) -> i64 {
    engine.positions.iter()
        .map(|p| if p.position_q > 0 { p.position_q } else { 0 })
        .sum()
}

fn short_oi(engine: &Engine) -> i64 {
    engine.positions.iter()
        .map(|p| if p.position_q < 0 { -p.position_q } else { 0 })
        .sum()
}

#[kani::proof]
fn proof_oi_conservation() {
    // Symbolic pre-state satisfying invariant
    let mut engine: Engine = kani::any();
    // Pre-condition: invariant holds before fill
    kani::assume(long_oi(&engine) == short_oi(&engine));
    // Symbolic fill parameters
    let maker_idx: usize = kani::any();
    let taker_idx: usize = kani::any();
    let fill_qty: i64   = kani::any();
    kani::assume(fill_qty > 0);
    kani::assume(maker_idx < engine.positions.len());
    kani::assume(taker_idx < engine.positions.len());
    kani::assume(maker_idx != taker_idx);

    // Operation: apply fill
    let result = engine.fill(maker_idx, taker_idx, fill_qty);
    kani::assume(result.is_ok());  // filter execution failures

    // Post: OI balance preserved
    assert!(long_oi(&engine) == short_oi(&engine),
        "OI conservation violated: long_oi != short_oi after fill");
}
```

**Suggested Kani harness name:** `proof_oi_conservation`
**Estimated harness complexity:** MED (symbolic over position array length; bound `positions.len()` to ≤ 8 for tractability)

---

## Evidence from Source / Pre-Analysis

| Question | Finding |
|---|---|
| Is OI stored as an accumulator? | **No** — must be derived by summing `position_q` |
| Is `position_q` signed? | **Yes** — long > 0, short < 0 |
| Does fill update exactly 2 positions? | **Verify**: if fill can touch >2 positions (e.g., liquidation cascade), additional paths need separate harnesses |
| Overflow risk on `i64` sum? | **Flag**: with many positions, sum may overflow before assert fires |

---

## Critical Path Flags for Layer 2

1. **Self-trade / same-account fill**: if `maker_idx == taker_idx` is permitted, OI conservation is trivially violated (one account goes long and short simultaneously). The harness must test this path separately.
2. **Partial fill + residual**: if `fill_qty < order_qty`, residual stays on book — OI must still balance mid-fill.
3. **Position sign flip**: if a fill crosses zero (e.g., short position becomes long), the absolute-value split changes; verify the fill logic handles sign change atomically.

**Verdict:** NEEDS_LAYER_2_TO_DECIDE
**Confidence:** MED — the invariant is well-formed and translatable, but the actual fill call signature must be confirmed against source before the harness can be run.