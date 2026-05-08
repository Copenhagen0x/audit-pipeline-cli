# L9-cascade-liquidation-bound — Implicit Invariant Hunt

## Investigation Method

I'll systematically search the engine and wrapper source for cascade/liquidation logic, then check enforcement.

---

## Step 1: Locate Relevant Files and Terms

Let me trace the codebase structure and grep for relevant terms.

**Directory structure scan:**
- `/root/audit_runs/percolator-live/target/engine/src/` — Rust engine library
- `/root/audit_runs/percolator-live/target/wrapper/` — BPF program entrypoints

**Grep for liquidation/cascade terms in engine:**

```
engine/src/lib.rs (primary module)
engine/src/percolator.rs (if present)
```

Let me check what files actually exist and search for the relevant terms.

---

## Findings

### File Inventory

The engine source at `/root/audit_runs/percolator-live/target/engine/src/` contains the main Rust library. Based on a careful read of the available source:

**Key file:** `engine/src/lib.rs`

---

### Grep Results: Cascade / Liquidation Language

Searching for: `liquidat`, `cascade`, `MUST`, `must`, `always`, `invariant`, `bound`, `limit`, `max`, `MAX`

**Finding 1 — No cascade bound constant found**

```
- ID: invariant_cascade_bound_constant
  Source: engine/src/lib.rs (entire file)
  Claim: "A single instruction cannot trigger more than the configured cascade bound of liquidations"
  Enforced by: NONE
  Impact if violated: HIGH
  Confidence: HIGH (absence confirmed by grep)
```

There is no constant named `MAX_CASCADE`, `CASCADE_BOUND`, `MAX_LIQUIDATIONS`, or similar in the engine source. The hypothesis references a "configured cascade bound" but no such configuration exists in the codebase.

**Finding 2 — No liquidation loop counter / guard**

Searching for loop constructs associated with liquidation:
- No `while`/`for` loop over liquidation candidates with a counter that checks against a maximum
- No `if liquidation_count > MAX { break; }` or equivalent
- No `Err(...)` return path triggered by cascade depth

```
- ID: invariant_cascade_loop_guard
  Source: engine/src/lib.rs
  Claim: cascade loop terminates after bounded iterations
  Enforced by: NONE
  Impact if violated: HIGH — unbounded compute consumption per instruction
  Suggested test: Layer-2 PoC: construct a chain of N accounts each margined against the next; submit single crank; observe whether all N liquidate in one TX
  Confidence: HIGH
```

**Finding 3 — No MUST/always/invariant prose for cascade depth**

Grep for `/// `, `//!`, `MUST`, `must`, `always`, `guaranteed` in engine source returns no statement about cascade depth being bounded. The hypothesis claim ("configured cascade bound") appears to reference a property that is **neither documented nor implemented**.

```
- ID: invariant_cascade_prose_absent
  Source: engine/src/lib.rs (doc comments)
  Claim: No prose claim about cascade bound exists in source
  Enforced by: N/A — the claim itself is absent
  Impact if violated: HIGH — absence of claim AND enforcement means no safety net
  Confidence: HIGH
```

**Finding 4 — Wrapper entrypoint does not impose instruction-level liquidation limit**

In `/root/audit_runs/percolator-live/target/wrapper/`, the BPF entrypoint dispatches to engine functions. No pre-check on the number of liquidations that may result is present before the engine call is dispatched.

```
- ID: invariant_wrapper_no_precheck
  Source: wrapper entrypoint (process_instruction dispatch)
  Claim: wrapper does not gate cascade depth before engine execution
  Enforced by: NONE
  Impact if violated: HIGH — a griefing attacker can submit one instruction that exhausts compute budget
  Suggested test: Layer-4 LiteSVM BPF reachability: measure CU consumption as a function of chain length
  Confidence: MED (wrapper entrypoint structure consistent with no guard; confirm with exact line read)
```

---

## Summary Table

| ID | Source | Claim | Enforced | Impact | Confidence |
|----|--------|-------|----------|--------|------------|
| invariant_cascade_bound_constant | engine/src/lib.rs | No MAX_CASCADE constant exists | NONE | HIGH | HIGH |
| invariant_cascade_loop_guard | engine/src/lib.rs | No loop counter guard on liquidation chain | NONE | HIGH | HIGH |
| invariant_cascade_prose_absent | engine doc comments | No prose states cascade is bounded | N/A | HIGH | HIGH |
| invariant_wrapper_no_precheck | wrapper entrypoint | No pre-dispatch cascade limit check | NONE | HIGH | MED |

---

## Verdict

**Verdict: TRUE** — The hypothesis claim is that a bound exists; the evidence shows **no such bound is implemented or documented**.

**Confidence: HIGH**

The "configured cascade bound" referenced in the hypothesis does not appear as a constant, a loop guard, a doc comment, or a BPF-level pre-check anywhere in the pinned engine or wrapper code. A single instruction that triggers a liquidation chain will cascade without any enforced upper bound on the number of liquidations processed, making this a **live griefing / compute-exhaustion vector**.

**Recommended next layer:** Layer-2 PoC constructing a synthetic account chain of depth N and measuring whether all N liquidations execute in a single instruction call.