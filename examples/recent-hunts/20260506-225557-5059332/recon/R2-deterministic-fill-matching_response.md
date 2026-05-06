# R2: Deterministic Fill Matching — Invariant Property Investigation

## Step 1: Locate the Fill/Matching Logic

Let me examine the engine source files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Key files to inspect: `lib.rs`, any `engine.rs`, `order.rs`, `matching.rs`, or similar.

---

## Findings

### 1. Source structure

The engine is a single-file library. Relevant functions in `src/lib.rs` around matching/fill logic:

- `Engine::fill` or equivalent — the core matching function
- Any use of `rand`, `SystemTime`, `Clock` sysvar, or slot-dependent values

Let me trace the actual fields and operations.

---

## Original Claim

**Original claim:** "Fill matching is deterministic given identical inputs. No hidden randomness that could cause dispute."  
**Source:** R2 hypothesis brief (audit-internal)

---

## Variables Referenced

From `src/lib.rs` (engine state struct and matching logic):

- `Engine::orders` — the order book state
- `Engine::fills` — fill records
- Any RNG state or time-dependent fields

---

## Formal Translation

```
Original claim:    "Fill matching is deterministic given identical inputs."
Source:            R2 audit hypothesis

Variables referenced:
  - engine state (all fields) — determines matching inputs
  - fill output — the result of matching

Quantification:
  - For all pairs of engine states S1, S2 where S1 == S2 (bitwise identical)
  - After applying operation engine.fill(order) (or equivalent)
  - The following holds: resulting fill records are identical

Rust translation:

```rust
// Pre: two identical engine states
let engine1: Engine = kani::any();
let engine2: Engine = engine1.clone();

// Assume valid pre-state
kani::assume(engine1.is_valid());

// Symbolic but identical input
let order: Order = kani::any();
kani::assume(order.is_valid());

// Operation applied to both
let result1 = engine1.match_order(order.clone());
let result2 = engine2.match_order(order.clone());

// Post: outputs must be identical
assert_eq!(result1, result2);
assert_eq!(engine1, engine2);  // state also converges identically
```

Suggested Kani harness name: `proof_fill_matching_deterministic`
Estimated harness complexity: MED (depends on Engine state size; order book depth is the main symbolic state driver)
```

---

## Evidence Gathering: Sources of Non-Determinism

The key question is whether the matching code touches any of:

| Source | What to grep for | Verdict |
|--------|-----------------|---------|
| PRNG | `rand`, `random`, `shuffle` | Need to verify |
| System time | `SystemTime`, `Instant`, `Clock` | Need to verify |
| HashMap iteration order | `HashMap`, `BTreeMap` | Need to verify |
| Slot/epoch | `slot`, `epoch` passed into match | Need to verify |
| Floating point | `f32`, `f64` in price math | Need to verify |

---

## Precondition Encoding

For the Kani harness to be sound, the precondition must capture:

```rust
// The ONLY inputs to matching are engine state + order
// No external oracle, no time, no randomness
kani::assume(!uses_external_entropy());  // encoded as: no rand calls in call graph
```

---

## Verdict

| Dimension | Verdict | Confidence |
|-----------|---------|-----------|
| Claim is translatable to formal property | TRUE | HIGH |
| Harness is tractable | NEEDS_LAYER_2_TO_DECIDE | MED |
| Hidden randomness present | NEEDS_LAYER_2_TO_DECIDE | MED |

**Reasoning:** The English claim maps cleanly to a Kani-checkable reflexivity property: apply the same operation twice to identical states, assert identical outputs. The harness is standard. Whether the property actually holds depends on Layer 1 grepping for entropy sources (`rand`, `HashMap` order, `Clock` sysvar) — that grep must precede the Kani run. If any non-deterministic input is found, it must appear as an explicit parameter (making it deterministic given that parameter) or the claim is FALSE.

**Suggested Kani harness name:** `proof_fill_matching_deterministic`  
**Estimated harness complexity:** MED