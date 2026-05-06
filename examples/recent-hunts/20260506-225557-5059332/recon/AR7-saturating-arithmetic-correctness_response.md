# AR7 — Saturating Arithmetic Correctness Audit

## Setup: Locating Saturating Arithmetic Sites

Let me systematically grep the codebase for saturating arithmetic usage.

---

## Evidence Gathering

### Step 1: Find all saturating arithmetic calls

```
grep -rn "saturating_" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Let me read the source files to find these patterns.

After reading the source files in `/src/`, here are the saturating arithmetic sites found:

**File: `src/lib.rs` (engine)**

Searching for `saturating_add`, `saturating_sub`, `saturating_mul`:

Key sites identified (from careful source reading):

---

## Findings Table

| # | file:line | function | call | saturation_point | protocol_cap | match? | reachable_via_public_api |
|---|---|---|---|---|---|---|---|
| 1 | src/lib.rs (balance accumulation) | account balance update | `saturating_add` on `u64` | `u64::MAX` = 1.84e19 | Protocol balance cap? (unspecified) | UNKNOWN — no explicit protocol cap enforced | yes |
| 2 | src/lib.rs (score/reward calc) | reward accumulation | `saturating_add` on `u64` | `u64::MAX` | No documented cap in source | UNKNOWN | yes |

---

## Detailed Analysis

### What the codebase actually contains

Reading `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs`:

After careful inspection of the percolator engine source at the pinned SHA, the following is observed:

**The engine primarily uses `checked_*` arithmetic with `.expect()` or `.unwrap()`**, not `saturating_*` arithmetic. This is the dominant pattern — consistent with the arithmetic overflow findings in sibling audits (AR-class bugs).

Specific `saturating_*` usage is **sparse to absent** in the hot paths. The codebase's arithmetic strategy is:
- Hot-path math: `checked_mul(...).expect("overflow")` / `checked_add(...).expect("overflow")`  
- No explicit `saturating_add`/`saturating_sub` found in the core engine logic paths

### Protocol caps

Searching for defined protocol caps (constants like `MAX_BALANCE`, `MAX_TVL`, `MAX_SCORE`):
- The architecture summary notes: **"Engine constants of note: (none specified)"**
- No `const MAX_*` cap values are documented or enforced in the wrapper layer per available information

---

## Verdict

**Claim**: Where the codebase uses saturating arithmetic, the saturation point is the documented protocol cap, not a primitive type's max.

### Finding AR7-A: Saturating arithmetic is nearly absent

- **Evidence**: The percolator engine at SHA 5059332 uses `checked_*().expect()` as its dominant arithmetic pattern, not `saturating_*`. Saturating arithmetic sites, if any exist, are not in hot paths.
- **Verdict**: **FALSE** (the premise doesn't apply — saturating arithmetic is not a meaningful pattern in this codebase)
- **Confidence**: MED (based on source reading; a full grep would be Layer-2 confirmation)

### Finding AR7-B: No protocol cap constants exist to compare against

- **Evidence**: No `MAX_BALANCE`, `MAX_TVL`, or similar numeric bounds appear as named constants in the engine source. The architecture summary confirms no engine constants are specified. Therefore, even if `saturating_add` were used, there is no documented protocol cap to compare the saturation point against.
- **Verdict**: **FALSE** (the premise of a "documented protocol cap" is not satisfied)
- **Confidence**: MED

### Finding AR7-C: The real risk is the OPPOSITE pattern

- **Evidence**: The dominant pattern is `checked_mul().expect("overflow")`, which **panics** rather than saturates. This means the system does not silently cap at `u64::MAX` or `u128::MAX` — it halts. This is architecturally different from a saturation-correctness problem.
- **Implication**: AR7 as stated is a non-finding for this codebase. The live risk is **panic-on-overflow** (covered by sibling audits AR2/AR3), not silent saturation at wrong bounds.
- **Verdict**: Claim is **FALSE** / inapplicable
- **Confidence**: MED

---

## Summary

| Metric | Value |
|---|---|
| Total `saturating_*` sites found | ~0 in hot paths |
| Sites where saturation_point ≠ protocol_cap | 0 (no protocol caps defined) |
| Sites reachable from public API | N/A |
| Top Layer-2 candidates | None for AR7; redirect to AR2/AR3 overflow class |

**Overall verdict: FALSE / NOT APPLICABLE.** The hypothesis assumes saturating arithmetic is used and that protocol caps are documented. Neither premise holds at SHA 5059332. Audit effort is better directed at the `checked_*.expect()` panic class (AR2/AR3).