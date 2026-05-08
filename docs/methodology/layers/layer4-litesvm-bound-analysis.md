# Layer 4 — LiteSVM BPF-level reachability + bound analysis

**Goal**: Reduce the "even if Kani says the engine math fails, can the public BPF API actually drive state to that witness in production?" uncertainty.

**Output**: An exploit chain test (live attack works, bug confirmed) OR a bound analysis (witness state is unreachable at production caps in any realistic time horizon → finding downgrades to code defect).

## What LiteSVM is

[LiteSVM](https://github.com/LiteSVM/litesvm) is an embedded Solana VM library. Unlike `solana-test-validator` (which spawns a separate process and is slow), LiteSVM runs the BPF program inside your test binary. Test runs that take 30+ seconds with `solana-test-validator` complete in <1 second with LiteSVM.

For the audit pipeline, LiteSVM is the glue between:
- **What Kani says** (engine math fails on adversarial state, white-box)
- **What the public BPF API can actually do** (driven by real instructions through the wrapper, black-box)

## Two test patterns

### Exploit chain (attacker wins)

Used when the Kani CEX corresponds to a state that's reachable via legitimate trading flow.

```rust
#[test]
fn test_v8_cursor_wrap_resets_consumption_via_natural_drift() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    // Phase 1: drive consumption above threshold via real oracle drift
    let consumption = drive_consumption_above_threshold(&mut env);
    assert!(consumption > 5e11 as u128, "precondition: consumption above slow-lane gate");

    // Phase 2: attacker spam-cranks the cursor
    let attacker = Keypair::new();
    env.svm.airdrop(&attacker.pubkey(), 1_000_000_000).unwrap();
    for _ in 0..48 {
        send_permissionless_crank(&mut env, &attacker);
    }

    // Phase 3: assert consumption was reset by the wrap, not absorbed by volatility
    let post_consumption = read_consumption(&env);
    assert_eq!(post_consumption, 0, "wrap should have reset consumption to zero");
}
```

If this test passes → the attacker wins. The Kani CEX corresponds to a real exploit.

### Bound analysis (attacker can't win in realistic time)

Used when the Kani CEX corresponds to engine state that's bounded away from the BPF-reachable surface.

```rust
#[test]
fn test_v11_l1_warmup_overflow_bound_analysis() {
    // ... setup ...

    // Engine constants from src/percolator.rs
    const MAX_ACCOUNT_POSITIVE_PNL: u128 = 1e32 as u128;
    const PER_SLOT_PNL_GAIN_DEFAULT_CAPS: u128 = 3e13 as u128;
    const SLOTS_PER_SEC: u128 = 2; // Solana mainnet conservative (500ms/slot)

    // For overflow: sched_anchor_q × elapsed > 2^128
    let sched_anchor_unsafe = u128::MAX / (1u128 << 30);
    let slots_to_accumulate = sched_anchor_unsafe / PER_SLOT_PNL_GAIN_DEFAULT_CAPS;
    let years = slots_to_accumulate / (SLOTS_PER_SEC * 86400 * 365);

    println!("Slots to accumulate unsafe sched_anchor_q: {}", slots_to_accumulate);
    println!("Years of wall-clock: {}", years);

    // Print + assert the bound is prohibitive
    assert!(
        years > 1_000_000,
        "if attacker can accumulate in <1M years, this is exploitable"
    );
}
```

If this test passes → the engine math is unsafe (Kani-confirmed) but unreachable in any realistic time. The finding downgrades from "active exploit" to "code defect / defense-in-depth recommendation."

## Reachability skeleton (call-chain proof)

A third pattern: prove that the Kani CEX function is REACHED by a public BPF instruction, even if you can't drive state to the unsafe witness.

```rust
#[test]
fn test_v11_l1_trade_nocpi_reaches_panic_site() {
    let mut env = TestEnv::new();
    env.init_market_with_invert(0);

    let lp = setup_lp(&mut env);
    let user = setup_user(&mut env);

    // Trigger TradeNoCpi with normal-sized inputs
    env.trade(&user, &lp, lp_idx, user_idx, 500_000);

    // Assert the call chain reached the panic-site function (no panic for normal inputs)
    let pnl = env.read_account_pnl(user_idx);
    let _ = pnl; // function executed; if panic site were unreachable, the trade would have failed differently

    println!("TradeNoCpi → execute_trade_with_matcher → ... → account_equity_trade_open_raw EXECUTED");
}
```

This skeleton proves the call chain exists. Combined with the bound analysis, it answers "yes, the panic site is in production code, but you can't drive state there."

## When to use which pattern

| Kani result | Layer 4 pattern |
|---|---|
| CEX, witness state reachable via single trade | Exploit chain |
| CEX, witness state requires unbounded accumulation | Bound analysis + reachability skeleton |
| CEX, witness state requires admin authorization | Note "admin-gated", skip Layer 4 (not exploitable by user) |
| SUCCESSFUL (SAFE proof) | Layer 4 not needed; the property is proven |

## Slab-offset readers

Reading engine state from LiteSVM tests requires byte-level offsets into the program's account data. These offsets are BPF-target-specific (u128 aligns to 8 bytes on SBF, 16 on x86), so they must be observed empirically against the compiled `.so`.

Common pattern:

```rust
const PNL_POS_TOT_OFFSET: usize = ENGINE_OFFSET + 328;

fn read_pnl_pos_tot(env: &TestEnv) -> u128 {
    let slab_data = env.svm.get_account(&env.slab).unwrap().data;
    u128::from_le_bytes(
        slab_data[PNL_POS_TOT_OFFSET..PNL_POS_TOT_OFFSET + 16]
            .try_into()
            .unwrap(),
    )
}
```

Cross-check offsets via two sanity tests when first writing them: read a value at the offset, mutate via a known instruction, read again, assert the change matches expectation.

## BPF artifact compatibility

LiteSVM tests load the BPF artifact (`target/deploy/<program>.so`). Test feature flags MUST match the BPF artifact's feature flags, OR the slab size will mismatch and you'll get cryptic `Custom(4)` errors at `init_market`.

Build the artifact with the same features the test uses:

```bash
cargo build-sbf --features small  # for tests with --features small
cargo build-sbf                    # for tests with default features
```

## Time budget

| Activity | Time |
|---|---|
| First LiteSVM test for new program | 2-4 hours (test scaffolding + offsets discovery) |
| Each subsequent test | 30-90 min |
| Bound analysis (no live exploit needed) | 1-2 hours |
| Cross-platform comparison | trivial (just re-run on the other host) |

## See also

- [`templates/litesvm_exploit_chain.rs.template`](../templates/litesvm_exploit_chain.rs.template)
- [`templates/litesvm_bound_analysis.rs.template`](../templates/litesvm_bound_analysis.rs.template)
- [Layer 5 — Cross-platform reproduction](./layer5-cross-platform-reproduction.md)
