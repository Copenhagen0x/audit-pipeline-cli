# Empirical-confirm follow-up — 2026-05-06 (evening)

A second `audit-pipeline confirm` session, dispatched against five candidates
selected from the **101-hypothesis recon cycle**
[`20260506-225557-5059332`](../recent-hunts/20260506-225557-5059332/) which ran
against [`aeyakovenko/percolator`](https://github.com/aeyakovenko/percolator)
at SHA `5059332` ("Record full Kani audit pass" — current upstream HEAD).

The earlier session published at
[`examples/confirm-session-2026-05-06/`](../confirm-session-2026-05-06/) ran
the four hypotheses from the 12-hyp default library. **This follow-up walks
the longer 101-hyp library and confirms five candidates the recon agent
flagged with the strongest signals.**

## Selection

From the 101-hyp recon, five candidates were picked:

| Hyp ID | Recon verdict / confidence | Why selected |
|---|---|---|
| `O6-side-flip-atomicity` | TRUE / HIGH | Position-flip state machine — classic atomicity bug class |
| `S5-market-mode-transitions` | TRUE / HIGH | Market mode FSM — unauthorised-transition risk |
| `V5-haircut-direction` | LAYER_2 / HIGH | F7 sibling — haircut helper invariant |
| `P8-self-trade-cash-flow` | LAYER_2 / HIGH | F7 attack vector — self-trade cash-flow |
| `A9-pause-gate-coverage` | TRUE / HIGH | Missing pause gates → trivial drain primitive |

## Results

| Hypothesis | Outcome | Evidence |
|---|---|---|
| `O6-side-flip-atomicity` | ✅ safety_attestation | Side-flip writes are single-statement (`set_position_basis_q_inner` line 2705); no observable intermediate zero |
| `S5-market-mode-transitions` | ✅ safety_attestation | Mode transitions gated; no out-of-FSM transitions reachable from public surface |
| `V5-haircut-direction` | ✅ safety_attestation | `min(residual, pnl_matured_pos_tot)` clamp at `percolator.rs:5475` proven over 4 scenarios (under/exact/over/zero) |
| `P8-self-trade-cash-flow` | ✅ safety_attestation | Same-owner self-trade is cash-flow-neutral; vault unchanged, combined capital decreases by exactly the fees, insurance grows by the same; OI balanced; conservation preserved |
| `A9-pause-gate-coverage` | ⚠️ cannot_test | The Percolator engine has **no pause mechanism** (zero matches for `pause`/`Pause`/`halt`/`frozen`/etc. across the engine tree). Recon's TRUE-HIGH was a confabulation — pausing is a wrapper-layer concern, not an engine invariant |

**Total cost of session: ~$3.60 across 5 confirms, ~9 minutes wall time.**

## Headline: zero new fires; recon's TRUE-HIGH on A9 was a confabulation

The most informative outcome is **A9-pause-gate-coverage**. Recon flagged it
TRUE/HIGH ("the engine seems to be missing pause-gate coverage on
mutating entrypoints"). The confirm agent investigated by grepping the engine
for `pause`, `Pause`, `PAUSE`, `halt`, `Halt`, `frozen`, `Frozen` — **zero
matches anywhere in the codebase**.

This is the methodology working as intended: recon's hypothesis library
includes pause-gate coverage as a *generic* Solana invariant, but the
Percolator engine deliberately lacks a pause mechanism (matters of policy
live in the wrapper at `aeyakovenko/percolator-prog`). The confirm agent
caught the recon agent's mistake and recorded a clean `cannot_test`
classification — see `A9-pause-gate-coverage.cannot_test.txt`.

The four `safety_attestation` outcomes (O6, S5, V5, P8) likewise show
recon's TRUE/HIGH and LAYER_2/HIGH verdicts collapse cleanly when subjected
to deterministic-test scrutiny: in each case a real Rust test was written
that exercises the invariant against the actual engine, and the test
passed.

## Tooling note: prose-preface bug found and worked around

The original confirm-pipeline run for V5/P8/A9 hit a packaging bug — the
tool-using agent emitted analysis prose at the top of the `.rs` file before
the `#![cfg(feature = "test")]` line, and the test extractor in
`confirm.py` does not currently strip prose preambles. The result was three
"unknown" outcomes from the automated dispatch even though the underlying
agent reasoning was correct.

This README represents the **manual fix-up** of those three runs:

* **V5** and **P8**: stripped the prose preamble; the resulting Rust
  test files compiled cleanly under `cargo test --features test
  --release` and both passed (test result: `ok. 1 passed; 0 failed`).
* **A9**: the agent's actual response was an honest `CANNOT_TEST` text,
  rewritten here as `A9-pause-gate-coverage.cannot_test.txt` to match
  the established pattern (see `SH5-keeper-crank-touching-completeness.cannot_test.txt`
  in the earlier session).

The prose-preface stripping is filed for the next confirm-pipeline
iteration. **None of the failures here represent missed empirical signal**
— they were delivery-layer artefacts that disappear after one round of
hand-cleaning.

## Files

```
test_confirm_o6_side_flip_atomicity.{rs,cargo.log,summary.json}     — passing test (real)
test_confirm_s5_market_mode_transitions.{rs,cargo.log,summary.json} — passing test (real)
test_confirm_v5_haircut_direction.{rs,cargo.log,summary.json}       — passing test (real, manually unwrapped)
test_confirm_p8_self_trade_cash_flow.{rs,cargo.log,summary.json}    — passing test (real, manually unwrapped)
A9-pause-gate-coverage.cannot_test.txt                              — engine has no pause mechanism
confirm_run_20260506-232213.log                                     — full driver log of the original 5 confirms
```

## Reproducing

The driver script that produced this session ran:

```bash
audit-pipeline --workspace /root/audit_runs/percolator-live confirm \
    -r hunts/20260506-225557-5059332/recon/<HYP>_response.md \
    --hyp-id <HYP> \
    --hypotheses-file src/audit_pipeline/templates/hypotheses/percolator_deep.yaml
```

To re-verify the V5 / P8 attestations directly:

```bash
cd target/engine
cargo test --features test --test test_confirm_v5_haircut_direction --release
cargo test --features test --test test_confirm_p8_self_trade_cash_flow --release
```

Both should print `test result: ok. 1 passed; 0 failed`.

The cycle's per-hypothesis recon prompts and responses are at
[`examples/recent-hunts/20260506-225557-5059332/recon/`](../recent-hunts/20260506-225557-5059332/recon/).
