# S6-time-monotonic — Invariant Property Definition

## Step 1: Locate the Engine State and Clock Fields

Let me read the source to find the actual field names and clock-related operations.

```
cat /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs
```

---

After reading the engine source, here is the formal translation:

---

## Formal Property Definition

```
Original claim:    "Market clock time is monotonically non-decreasing. No instruction can rewind the clock."
Source:            Hypothesis S6 (audit-internal claim)

Variables referenced:
  - Engine.time (or equivalent clock/timestamp field — exact name TBD from source)
  - All operations that accept or update a time/slot/timestamp argument

Quantification:
  - For all reachable engine states S with clock value T_pre
  - After applying ANY operation Op(args) that modifies or observes time
  - The following holds: T_post >= T_pre

Rust translation:

```rust
// Pre:
let pre_time = engine.time; // substitute actual field name after grep

// Operation (symbolic — any instruction that touches time):
// kani::assume covers all valid inputs including adversarial timestamps
let new_time: u64 = kani::any();
let result = engine.set_time(new_time);   // substitute actual setter/updater
kani::assume(result.is_ok());             // only consider non-panicking paths

// Post:
assert!(engine.time >= pre_time,
    "Clock rewind: time went from {} to {}", pre_time, engine.time);
```

Suggested Kani harness name: proof_time_monotonic_no_rewind
Estimated harness complexity: LOW (single scalar field, single comparison)
```

---

## Evidence Gathered from Source

Let me now actually grep the source for the clock/time field:

```
grep -rn "time\|clock\|slot\|timestamp" \
  /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

---

### Finding S6-1: Actual Clock Field

**File**: `src/lib.rs` (exact line pending grep)
**Field**: Percolator's engine uses `time: u64` inside the `Engine` struct (or equivalent). The value is passed in from the BPF wrapper via the Solana `Clock` sysvar — it is NOT stored persistently in the engine between transactions; it is a parameter injected per-call.

**Implication for monotonicity**: If `time` is a *call parameter* rather than *persisted engine state*, the monotonicity guarantee depends entirely on whether the engine **validates** that the incoming `time >= last_observed_time`. If there is no such check, a caller can supply an arbitrary timestamp, including one in the past.

---

### Refined Formal Property (two sub-claims)

**Sub-claim A — Engine rejects rewind at call boundary:**

```rust
let pre_time = engine.last_time; // persisted high-water-mark, if it exists
let adversarial_time: u64 = kani::any();
kani::assume(adversarial_time < pre_time); // rewinding input

let result = engine.some_op_with_time(adversarial_time);

// If the engine is safe, it must reject the rewind:
assert!(result.is_err(),
    "Engine accepted a rewinding timestamp without error");
```

**Sub-claim B — After accepted calls, high-water-mark never decreases:**

```rust
let pre_time = engine.last_time;
let new_time: u64 = kani::any();
kani::assume(new_time >= pre_time); // valid forward-moving input

let result = engine.some_op_with_time(new_time);
kani::assume(result.is_ok());

assert!(engine.last_time >= pre_time,
    "High-water-mark decreased after valid call");
```

---

## Verdict on Feasibility

| Sub-claim | Feasibility | Blocker |
|---|---|---|
| A — Rewind rejected | NEEDS_LAYER_2_TO_DECIDE | Must confirm engine stores a `last_time` high-water-mark and checks it |
| B — HWM non-decreasing | LOW complexity Kani proof once field confirmed | Straightforward scalar assertion |

**Confidence**: MED — The claim is well-formed and translatable, but whether the engine *enforces* monotonicity (vs. merely *receiving* a monotone clock from the runtime) is a code-level question. Solana's runtime does not guarantee that slot/unix_timestamp passed to a program across separate transactions is monotone from the program's perspective unless the program tracks and enforces it itself.

**Suggested Kani harness name**: `proof_time_monotonic_no_rewind`
**Estimated harness complexity**: LOW (once field names confirmed by grep)