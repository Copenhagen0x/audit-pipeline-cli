# Empirically-verified safety attestations

Each `test_confirm_*.rs` file in this directory was **autonomously generated** by
Jelleo's `confirm` command and **compiles + passes against the actual
Percolator engine** at the audited SHA.

The flow:

1. Jelleo's tool-using `hunt-deep` agent investigated a hypothesis and
   produced a line-cited verdict (see `../V4-vault-cap-respect_response.md`).
2. The `confirm` command then dispatched a *second* tool-using agent with
   `read_file`, `grep`, and `find_function` access. That agent studied the
   existing test files in `engine/tests/`, learned the actual `RiskEngine` API
   surface, and wrote a custom integration test asserting the invariant from
   the finding.
3. The test was installed into `engine/tests/` and run via
   `cargo test --features test --test <name>`.
4. The cargo log + outcome JSON are saved alongside the test for verification.

**Outcome semantics:**

- `safety_attestation` (cargo rc=0): the test compiled and all assertions held —
  the invariant is empirically verified
- `fired` (cargo rc!=0, panicked): the test compiled but an assertion failed —
  potential confirmed bug (worth disclosing)
- `compile_error`: the test failed to compile — manual harness fix needed

## Files in this directory

| Test | Hypothesis | Outcome | Run time |
|---|---|---|---|
| `test_confirm_v4_vault_cap_respect.rs` | `MAX_VAULT_TVL` cap is enforced at every reachable mutation site | safety_attestation | 0.3s |
| `test_confirm_o7_position_zero_clears_basis.rs` | When `position_q` reaches 0, basis-related fields zero atomically | safety_attestation | 0.3s |
| `test_confirm_o9_position_bedge_correct.rs` | Bedge accounting on partial closes correctly apportions realized PnL | safety_attestation | 0.3s |

## Sample cargo output

```
Compiling: cargo test --features test --test test_confirm_v4_vault_cap_respect
Compiling percolator v0.1.0 (/root/audit_runs/percolator-live/target/engine)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 5.42s
     Running tests/test_confirm_v4_vault_cap_respect.rs
running 1 test
test test_confirm_v4_vault_cap_respect ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

## Reproduction

To reproduce locally:

```bash
audit-pipeline confirm \
    --response-md hunt_deep/<cycle-id>/<hyp-id>_response.md \
    --hyp-id <hyp-id> \
    --hypotheses-file hypotheses.yaml \
    --max-turns 18 --timeout 240
```

The agent will study the codebase iteratively, write the test, install it,
compile it, run it, and classify the outcome.
