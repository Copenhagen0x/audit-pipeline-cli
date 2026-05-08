# §05 · Severity rubric

Every finding is assigned a severity in `{Critical, High, Medium, Low, Info}`. The rubric is deterministic — same evidence yields the same severity across cycles, across operators, across protocols.

This section is the rubric definition + the auto-derivation rules.

---

## Severity definitions

| Level     | Definition |
|-----------|-----------|
| **Critical** | Direct, single-tx, permissionless theft of protocol or user funds. Or: state corruption that bricks the protocol for all users. F7 is Critical (drains insurance fund in one tx). |
| **High**     | Indirect or conditional theft (requires specific oracle conditions, specific timing windows, or a one-block setup). Or: governance-level state corruption that's recoverable but disruptive. |
| **Medium**   | Loss of funds bounded by a small constant (e.g. a single user's cost-of-griefing) AND the attacker can't profit. Or: severity-degrading bugs that don't directly enable theft but reduce the protocol's defensive depth. |
| **Low**      | UX bugs, gas griefing, off-by-one errors with no funds impact. |
| **Info**     | Documentation gaps, code-clarity issues, deviations from spec that don't change behavior. |

`Critical` and `High` trigger immediate notifications (§08). `Medium` and below batch into the weekly digest.

---

## Auto-derivation rules

The pipeline computes severity from `(class, verdict, debate_promoted, poc_fired)` according to a deterministic table:

| Hyp class            | PoC fired | Debate promoted | Verdict      | → Auto-derived severity |
|----------------------|-----------|-----------------|--------------|------------------------|
| `invariant_property` | yes       | n/a             | TRUE         | **Critical**           |
| `invariant_property` | no        | yes             | TRUE         | High                   |
| `invariant_property` | no        | no              | TRUE         | Medium                 |
| `state_transition`   | yes       | n/a             | TRUE         | **Critical** if applies_to includes `has_insurance_pool`, else High |
| `state_transition`   | no        | yes             | TRUE         | High                   |
| `authorization`      | yes       | n/a             | TRUE         | **Critical**           |
| `authorization`      | no        | yes             | TRUE         | High                   |
| `arithmetic_overflow`| yes       | n/a             | TRUE         | **Critical**           |
| `arithmetic_overflow`| no        | n/a             | TRUE         | High                   |
| `implicit_invariant` | yes       | n/a             | TRUE         | High                   |
| `implicit_invariant` | no        | yes             | TRUE         | Medium                 |
| any                  | n/a       | n/a             | NEEDS_LAYER_2_TO_DECIDE | (not assigned — finding stays in `new` until Layer 2 fires) |
| any                  | n/a       | n/a             | FALSE        | (finding moves to `rejected`, no severity assigned) |

The hypothesis's **declared** `severity` field (in the yaml) sets a **floor**. A confirmed PoC that auto-derives `High` against a hypothesis declared `Critical` is recorded as `Critical` (the declared floor wins).

---

## Severity ≠ exploitability

Severity captures the *worst credible outcome* if the invariant is violated, not the conditional probability of exploitation. A `Critical` invariant property may require an attacker to control 51% of validators, an oracle, and 100 SOL of capital — the severity is still `Critical` because the outcome (drained insurance fund) is catastrophic.

Customers who want exploitability scoring on top of severity get it via the **conditional cost** model in the per-finding narrative writeup: estimated SOL setup cost, oracle preconditions, mainnet feasibility. Severity is the input; exploitability is the editorial layer above.

---

## Cycle-level rollup

Per cycle, the engine emits:

```
Cycle 20260507-1340-percolator
  Critical  : 1 (F7)
  High      : 3
  Medium    : 2
  Low       : 0
  Info      : 5
  ────────────
  Total findings : 11
  Confirmed (PoC fired) : 4
```

The signed cycle receipt (§07) commits to these counts at the cycle's engine SHA so they cannot be retroactively rewritten.

---

**Live reference:** [jelleo.com/methodology.html#severity](https://jelleo.com/methodology.html#severity)
**Implementation:** [`audit_pipeline.severity`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/severity.py)
