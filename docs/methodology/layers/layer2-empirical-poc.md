# Layer 2 — Empirical PoC (engine native)

**Goal**: Reduce the "is this candidate actually a bug or did I misread the code" uncertainty.

**Output**: A passing test for each real finding (or a test that proves the candidate isn't a bug).

## The principle

If you can't write a test that demonstrates the bug, the bug doesn't exist (or your hypothesis is wrong, or the engine has a guard you missed). The empirical PoC is the cheapest way to catch false positives from Layer 1.

## Two test patterns

### Panic-class bugs (overflow, unwrap, expect)

Use Rust's `#[should_panic(expected = "...")]` annotation.

```rust
#[test]
#[should_panic(expected = "a*b overflow")]
fn v9_advance_profit_warmup_native_mul_panic() {
    let mut engine = RiskEngine::new(extreme_h_max_params());
    let idx = add_user_test(&mut engine, 0).unwrap() as usize;

    // Plant the witness state that triggers the panic
    let huge_pnl: i128 = 1i128 << 100;
    engine.accounts[idx].pnl = huge_pnl;
    engine.pnl_pos_tot = huge_pnl as u128;
    // ... (more state setup)

    // Trigger the path
    let _ = engine.advance_profit_warmup(idx);
}
```

The expected message must exactly match the panic the engine produces. If you get the message wrong, the test fails for the wrong reason.

### State-corruption bugs (silent invariant violation)

Use `assert!` after the operation.

```rust
#[test]
fn v8_wrap_changes_per_account_admission_decision_state() {
    let mut env = TestEnv::new();
    // ... setup ...
    let pre_horizon = env.read_account_sched_horizon(user_idx);

    // Trigger the suspicious operation
    force_cursor_wrap(&mut env);

    let post_horizon = env.read_account_sched_horizon(user_idx);

    assert_ne!(
        pre_horizon, post_horizon,
        "wrap should have changed admission lane horizon"
    );
}
```

## The "sanity test" pattern (always include)

For each `#[should_panic]` test, write a companion test with smaller inputs that does NOT panic. This catches the case where your panic-trigger state is doing something else weird.

```rust
#[test]
fn v9_advance_profit_warmup_safe_with_small_h_max() {
    // Same setup as the panic test, but with safe parameters
    // Asserts that the function returns Ok with normal inputs
    // Confirms the panic above is config-driven, not spec-driven
}
```

## Common patterns

### Test the engine directly, not via wrapper

Layer 2 is for engine-native tests. Wrapper-level testing is Layer 4 (LiteSVM). Engine tests are faster and have direct access to internal state.

### Use `#[cfg(feature = "test")]` to access test-only methods

Many Rust engine codebases have `test_visible!` macros that expose internal methods only when the `test` feature is enabled. Use them.

### Plant adversarial state directly

Layer 2 is white-box. You can write directly to engine state fields to set up the witness:

```rust
engine.accounts[idx].pnl = huge_pnl;
engine.pnl_pos_tot = huge_pnl as u128;
engine.vault = i128::U128::new(extreme_vault);
```

Layer 4 (LiteSVM) is where you verify whether the public API can ACTUALLY drive state to your witness. Don't conflate the two.

## What to deliver

For each Layer-1 candidate that survives Layer 2:

```
tests/test_<finding_short_name>.rs
  - Two functions:
    - test_<finding>_native_panic   (#[should_panic])
    - test_<finding>_safe_baseline  (sanity, no panic)
  - Comments at the top citing engine line + explaining state setup
```

For each Layer-1 candidate that gets refuted by Layer 2:

```
Note in your audit log: "candidate <X> refuted at Layer 2 — engine has guard at line N that I missed in Layer 1"
```

## See also

- [`templates/engine_native_poc.rs.template`](../templates/engine_native_poc.rs.template) — drop-in template
- [Layer 3 — Kani formal verification](./layer3-kani-formal-verification.md) — generalize the empirical PoC into a formal claim
