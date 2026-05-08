# Layer 3 — Kani formal verification

**Goal**: Reduce the "does the bug exist for ALL inputs in the symbolic state space, or just the test I wrote" uncertainty.

**Output**: A formal counterexample (CEX) proving the bug, OR a formal SUCCESS proving the property holds universally.

## What Kani is

[Kani](https://github.com/model-checking/kani) is a Rust verifier built on CBMC. You write a `#[kani::proof]` function that asserts a property; Kani symbolically executes the code and either:

- **PROVES the property** (returns SUCCESSFUL) — the property holds for ALL inputs in the bounded state space
- **Returns a counterexample (CEX)** — Kani found inputs where the property fails; CEX is a witness state

For our pipeline, Kani serves two roles:

1. **CEX harness**: assert that a panic-class bug does NOT happen. Kani returns CEX → the bug is formally proven to exist on engine-permitted state.
2. **SAFE harness**: assert that a desired property holds. Kani returns SUCCESSFUL → you have a formal proof of the property.

## CEX harness pattern

Used to formally prove a Layer-2 PoC's bug class generalizes:

```rust
#![cfg(kani)]
use percolator::*;

#[kani::proof]
#[kani::unwind(10)]
fn proof_advance_profit_warmup_does_not_panic() {
    let h_max: u64 = kani::any();
    kani::assume(h_max > 0);
    kani::assume(h_max <= 1u64 << 32);

    let h_min: u64 = kani::any();
    kani::assume(h_min > 0);
    kani::assume(h_min <= h_max);

    let params = canonical_params(h_min, h_max);
    let mut engine = RiskEngine::new(params);

    let dep_result = engine.deposit_not_atomic(0u16, 1, 100);
    kani::assume(dep_result.is_ok());

    let pnl_u: u128 = kani::any();
    kani::assume(pnl_u > 0);
    kani::assume(pnl_u <= MAX_ACCOUNT_POSITIVE_PNL);

    // Plant adversarial-but-engine-valid state
    engine.accounts[0].pnl = pnl_u as i128;
    engine.pnl_pos_tot = pnl_u;
    // ... (more state setup)

    // Property: this should not panic for any valid input
    // Kani returns a CEX when (sched_anchor_q × current_slot) > u128::MAX
    let _ = engine.advance_profit_warmup(0);
}
```

If Kani returns SUCCESSFUL → your finding from Layer 2 is wrong (you missed an invariant).
If Kani returns CEX → your finding generalizes; the bug is formally proven on engine-permitted state.

## SAFE harness pattern

Used to formally prove a desired property holds:

```rust
#![cfg(kani)]
use percolator::*;

#[kani::proof]
#[kani::unwind(10)]
fn proof_finalize_preserves_conservation() {
    let mut engine = symbolic_engine_at_max_risk_params();
    let pre_v = engine.vault.get();
    let pre_i = engine.insurance_fund.balance.get();
    let pre_c = engine.c_tot.get();
    let pre_p = engine.pnl_pos_tot;
    let pre_r = engine.matured_pos_tot;

    let result = engine.finalize_touched_accounts_post_live(&touched_set);
    kani::assume(result.is_ok());

    let post_v = engine.vault.get();
    let post_i = engine.insurance_fund.balance.get();
    let post_c = engine.c_tot.get();
    let post_p = engine.pnl_pos_tot;
    let post_r = engine.matured_pos_tot;

    // Property: conservation V = I + C + P + R holds before AND after
    assert_eq!(pre_v, pre_i + pre_c + pre_p + pre_r);
    assert_eq!(post_v, post_i + post_c + post_p + post_r);
}
```

If Kani returns SUCCESSFUL → conservation is formally proven for all reachable states under this operation.
If Kani returns CEX → there's a state where conservation breaks (would be a real protocol-level bug).

## The unwind bound trap

Kani requires you to bound any loops in the verified code. The annotation `#[kani::unwind(N)]` tells Kani to unroll loops up to N iterations.

**Common mistake**: setting `unwind(2)` when the engine's `RiskEngine::new()` runs an N-iteration init loop. Kani fails with `unwinding assertion loop 0` — looks like a real failure but is actually a "give Kani more rope" issue.

**Rule of thumb**: set `unwind` to AT LEAST `MAX_ACCOUNTS + 2` for harnesses that call `RiskEngine::new()`. For engines that bound `MAX_ACCOUNTS = 4` under `cfg(kani)`, that means `unwind(10)` is safe.

## Solver tuning

Kani uses CaDiCaL by default. For most safety-property harnesses, this is fine.

For harnesses that involve large U256 arithmetic or non-linear constraints, swap to `--solver kissat`:

```rust
#[kani::proof]
#[kani::solver(kissat)]
#[kani::unwind(10)]
fn proof_with_complex_arithmetic() {
    // ...
}
```

## Time budget

| Harness complexity | Typical Kani runtime |
|---|---|
| Simple panic check (single function call) | 1-30 seconds |
| Conservation invariant over a single mutation | 30 seconds - 5 minutes |
| Multi-step state machine (N=2-3 calls) | 5 minutes - 1 hour |
| Full bounded model checking of complex paths | 1-12 hours, may not converge |

If a harness doesn't converge in 30 minutes, the symbolic state space is too large. Tighten `kani::assume` constraints to bound it more aggressively.

## When Kani returns "VERIFICATION FAILED" but it's not a real bug

Common false-positive patterns:

| Pattern | What it really means | Fix |
|---|---|---|
| `unwinding assertion loop 0` at builtin-memcmp | Kani's internal memcmp unrolling | Add `--unwind 10` for memcmp loops |
| `expect_failed.assertion` with no obvious caller | Some `.expect()` deep in std got triggered | Read the trace; usually it's an `option.rs:2184` for a `.expect()` panic in your code |
| `assertion.1` with no message | Kani placeholder for `assert!(...)` panics | Look at the line cited; it's where the assertion is |

The pipeline's lessons-learned doc has the full list of these.

## Output format for the audit

For each harness, capture:

```
Harness:           proof_<short_name>
File:              tests/proofs_<finding>.rs
Verdict:           SUCCESSFUL / FAILED (CEX) / TIMEOUT / FAILED (unwind-noise)
Time:              N seconds
Encodes:           <one-sentence property>
Significance:      <bug confirmed / SAFE proof of invariant / inconclusive>
```

Aggregate into `RAW_KANI_RESULTS.md` for the disclosure.

## See also

- [`templates/kani_cex_panic_class.rs.template`](../templates/kani_cex_panic_class.rs.template)
- [`templates/kani_safe_invariant.rs.template`](../templates/kani_safe_invariant.rs.template)
- [`scripts/dispatch_kani.sh`](../scripts/dispatch_kani.sh) — push harness to VPS, run, fetch results
- [Layer 4 — LiteSVM BPF-level reachability](./layer4-litesvm-bound-analysis.md) — confirm the Kani CEX is reachable from public API
