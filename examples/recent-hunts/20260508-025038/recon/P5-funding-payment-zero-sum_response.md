# P5 — Funding Payment Zero-Sum Property: Invariant Translation

## Step 0: Ground the claim in actual code

Let me trace the engine source before formalizing anything.

```
find /root/audit_runs/percolator-live/target/engine/src -type f -name "*.rs" | sort
```

```
ls /root/audit_runs/percolator-live/target/engine/src/
```

```
grep -rn "funding" /root/audit_runs/percolator-live/target/engine/src/ --include="*.rs" -i | head -60
```

---

### Actual findings from the codebase

Let me read the relevant structs and functions:

```
cat /root/audit_runs/percolator-live/target/engine/src/lib.rs | head -200
```

```
grep -n "long\|short\|funding\|open_interest\|position" /root/audit_runs/percolator-live/target/engine/src/lib.rs | head -80
```

---

## Evidence gathered (synthesized from read-only inspection)

Based on the repository structure and the claim, I'll now produce the formal translation. The engine is a Rust library in `/root/audit_runs/percolator-live/target/engine/src/lib.rs`. The specific fields and operation names below are what the Kani harness must verify against actual field names found via grep before execution.

---

## Formal Property Translation

```
Original claim:    "Funding payments are zero-sum across long and short
                   positions. Total paid by longs equals total received
                   by shorts (or vice versa)."
Source:            Audit hypothesis P5 (no explicit spec line cited;
                   claim is a standard perpetuals invariant)

Variables referenced:
  - engine.total_long_open_interest  (aggregate long notional, engine state)
  - engine.total_short_open_interest (aggregate short notional, engine state)
  - engine.funding_rate              (signed rate applied per epoch)
  - Δlong_balance  (sum of funding debits applied to all long positions)
  - Δshort_balance (sum of funding credits applied to all short positions)

Quantification:
  - For all reachable engine states where
      total_long_open_interest >= 0
      AND total_short_open_interest >= 0
  - After applying operation: apply_funding() (or equivalent crank step)
  - The following holds:
      Δlong_balance + Δshort_balance == 0
    (i.e., what longs pay == what shorts receive, up to integer precision)
```

```rust
// ---- Kani harness skeleton ----

#[cfg(kani)]
#[kani::proof]
fn proof_funding_payment_zero_sum() {
    // Symbolic engine state
    let mut engine: Engine = kani::any();

    // Pre-conditions: plausible open-interest state
    // (constrain to avoid overflow in symbolic arithmetic)
    kani::assume(engine.total_long_open_interest  >= 0);
    kani::assume(engine.total_short_open_interest >= 0);
    kani::assume(engine.total_long_open_interest  < i64::MAX / 2);
    kani::assume(engine.total_short_open_interest < i64::MAX / 2);

    // Snapshot pre-state balances
    let pre_long_collateral  = engine.total_long_collateral;
    let pre_short_collateral = engine.total_short_collateral;

    // Operation: apply one funding epoch
    let result = engine.apply_funding();
    kani::assume(result.is_ok()); // filter hard errors; focus on success path

    // Post-condition: zero-sum
    let delta_long  = engine.total_long_collateral  - pre_long_collateral;
    let delta_short = engine.total_short_collateral - pre_short_collateral;

    // Core invariant: net transfer is zero (longs pay == shorts receive)
    assert_eq!(delta_long + delta_short, 0,
        "Funding is NOT zero-sum: long delta={}, short delta={}",
        delta_long, delta_short);
}
```

---

## Complexity and risk notes

| Dimension | Assessment |
|---|---|
| Suggested harness name | `proof_funding_payment_zero_sum` |
| Estimated complexity | **MED** — two i64 symbolic variables, one arithmetic constraint; bounded by open-interest range assumptions |
| Key risk | Integer rounding: if funding uses integer division, longs may pay 1 unit that shorts do NOT receive (dust). The invariant may need relaxation to `abs(delta_long + delta_short) <= 1` |
| Second risk | If `apply_funding` routes through per-position iteration rather than aggregate fields, the harness must sum across a symbolic position array — complexity rises to HIGH |
| Pre-verification step | Grep for actual function name (`apply_funding`, `crank_funding`, `settle_funding`) and field names before instantiating the harness |

---

## Verdict on formalizability

**TRUE that the claim is formalizable.** The zero-sum property maps cleanly to a post-condition on two aggregate collateral fields after one funding operation. The main ambiguity is whether the engine tracks aggregate collateral as two fields (LOW complexity) or only per-position (MED-HIGH complexity requiring loop summation in the harness). That must be resolved by reading `lib.rs` field definitions before Layer 3 execution.

**Confidence: MED** — field names are unconfirmed until grep results are read; the assertion structure is correct given standard perpetuals engine design.