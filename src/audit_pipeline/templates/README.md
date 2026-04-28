# Templates

Drop-in Rust file scaffolds for each layer of the audit pipeline. Copy a template into your target's `tests/` directory, replace the placeholder tokens, and run.

## What's here

| Template | Layer | When to use |
|---|---|---|
| [`engine_native_poc.rs.template`](./engine_native_poc.rs.template) | 2 | **Crash-class** finding — `#[should_panic]` test + companion sanity test (overflow, unwrap, divide-by-zero) |
| [`engine_state_conservation_poc.rs.template`](./engine_state_conservation_poc.rs.template) | 2 | **Silent state-corruption** finding — call returns Ok(()) but a conservation invariant is violated. Uses BEFORE/AFTER invariant comparison instead of `#[should_panic]`. F7 (residual growth on insurance absorption) is the canonical example. |
| [`kani_cex_panic_class.rs.template`](./kani_cex_panic_class.rs.template) | 3 | Formally generalize a panic-class finding via Kani |
| [`kani_safe_invariant.rs.template`](./kani_safe_invariant.rs.template) | 3 | Formally prove a desired invariant holds under symbolic state |
| [`litesvm_reachability_test.rs.template`](./litesvm_reachability_test.rs.template) | 4 | Confirm a public BPF instruction's call chain reaches the function under verification |
| [`litesvm_bound_analysis.rs.template`](./litesvm_bound_analysis.rs.template) | 4 | Numerically derive the wall-clock cost of reaching a Kani-CEX witness via legitimate flow |

### Choosing between the two Layer-2 templates

| Symptom | Template |
|---|---|
| Engine call panics / returns `Err(...)` on adversarial input | `engine_native_poc` |
| Engine call returns `Ok(())` but post-state violates a conservation rule | `engine_state_conservation_poc` |
| Not sure which | Start with `engine_state_conservation_poc` — invariant violations are a strict superset of crashes once you wire up `assert_eq!` on the right state. |

## Placeholder tokens to replace

Each template has tokens you must replace before the file compiles:

| Token | Meaning |
|---|---|
| `<FINDING_NAME>` | Short snake_case identifier for the finding (e.g. `bug3_trade_open_overflow`) |
| `<INVARIANT_NAME>` | Short snake_case identifier for the safety property (e.g. `finalize_preserves_conservation`) |
| `<engine_function_name>` | Actual engine function under test (e.g. `advance_profit_warmup`) |
| `<INSTRUCTION_NAME>` | Actual BPF instruction name (e.g. `trade`, `crank`) |
| `<EXPECTED_PANIC_MSG>` | EXACT panic message the engine produces (verify by running once without the annotation) |
| `<INVARIANT_DESCRIPTION>` | One-line description of the conservation rule (e.g. `"residual = vault - (c_tot + insurance) is preserved across the call"`) |

Use your editor's find-and-replace; each token is wrapped in `<>` for easy spotting.

## Conventions

- Each template includes `USAGE NOTES` at the bottom — read these before customizing
- Each template has at least one example for the most common adaptation (Percolator-flavored)
- All templates use `#![cfg(feature = "test")]` or `#![cfg(kani)]` to gate compilation appropriately
- All templates target Rust 1.95+ syntax; no nightly features unless explicitly noted

## What to do after replacing tokens

1. Move the file into the target program's `tests/` directory
2. Run `cargo check --tests --features test` to confirm it compiles
3. Run the test:
   - Layer 2 PoC: `cargo test --test <filename> --features test`
   - Layer 3 Kani: `cargo kani --tests --features test --harness <harness_name>`
   - Layer 4 LiteSVM: `cargo test --test <filename>` (after `cargo build-sbf`)
4. Capture the output for the disclosure

## Cross-template idioms

A few patterns appear across multiple templates; learn them once:

### Bounded symbolic state (Kani)

```rust
let value: u128 = kani::any();
kani::assume(value > 0);
kani::assume(value <= ENGINE_CAP);
```

Always bound `kani::any()` with `kani::assume()`. Unbounded symbolic state explodes the solver's search space and the harness won't converge.

### Filter setup failures (Kani)

```rust
let result = engine.deposit_not_atomic(0u16, 1, 100);
kani::assume(result.is_ok());
```

Use `kani::assume(result.is_ok())` to filter out solver paths where setup fails for unrelated reasons. Without this, Kani may surface CEXes that are about your setup, not your finding.

### Slab-offset read (LiteSVM)

```rust
const FIELD_OFFSET: usize = ENGINE_OFFSET + N;
fn read_field(env: &TestEnv) -> u128 {
    let slab_data = env.svm.get_account(&env.slab).unwrap().data;
    u128::from_le_bytes(
        slab_data[FIELD_OFFSET..FIELD_OFFSET + 16]
            .try_into()
            .unwrap(),
    )
}
```

Offsets are BPF-target-specific. If a `read_*` function returns garbage, your offset is wrong; cross-check against a known mutation.

## See also

- [`../docs/`](../docs/) for layer-by-layer methodology
- [`../scripts/`](../scripts/) for orchestration helpers (VPS dispatch, cross-platform compare)
- [`../examples/percolator-audit/`](../examples/percolator-audit/) for fully worked examples of these templates in production use
