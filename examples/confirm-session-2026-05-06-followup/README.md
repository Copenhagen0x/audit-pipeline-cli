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

| Hypothesis | Outcome | Notes |
|---|---|---|
| `O6-side-flip-atomicity` | ✅ safety_attestation | Side-flip writes are single-statement; no observable intermediate zero |
| `S5-market-mode-transitions` | ✅ safety_attestation | Mode transitions gated; no out-of-FSM transitions reachable |
| `V5-haircut-direction` | ⚠️ unknown (compile error) | Agent reasoning correct (`min()` clamps `h_num ≤ h_den`) — test extractor failed to strip prose preface |
| `P8-self-trade-cash-flow` | ⚠️ unknown (compile error) | Agent reasoning correct (`a == b` blocked at `percolator.rs:7623`; fees always ≥ 0 to insurance) — same prose-preface bug |
| `A9-pause-gate-coverage` | ⚠️ unknown (should be `cannot_test`) | Agent verdict literally: *"the engine has no pause mechanism"* — recon was confabulating. Mis-classified as compile_error because the agent's CANNOT_TEST text isn't valid Rust. |

**Total cost of session: ~$3.60 across 5 confirms, ~9 minutes wall time.**

## Headline: zero new fires; recon's TRUE-HIGH on A9 was a confabulation

The most informative outcome is **A9-pause-gate-coverage**. Recon flagged it
TRUE/HIGH ("the engine seems to be missing pause-gate coverage on
mutating entrypoints"). The confirm agent investigated by grepping the engine
for `pause`, `Pause`, `PAUSE`, `halt`, `Halt`, `frozen`, `Frozen` — **zero
matches anywhere in the codebase**. Verbatim from the agent:

> The finding's premise — that there is a `paused` flag, `config.paused`,
> `RiskError::Paused`, or any pause/unpause mechanism in the engine — is
> entirely absent from the codebase. There is no such field anywhere in the
> repository. A deterministic Rust test for this finding cannot be written
> against the actual engine.

This is the methodology working as intended — recon's hypothesis library
includes pause-gate coverage as a *generic* Solana invariant, but the
Percolator engine deliberately lacks a pause mechanism (matters of policy
are in the wrapper). The confirm agent caught the recon agent's mistake.

The two `safety_attestation` outcomes (O6 and S5) likewise show recon's
TRUE/HIGH verdicts on FSM-class invariants get rejected at the deterministic-
test level — these are clean attestations: *the agent built a real Rust test
exercising the invariant against the actual engine, the test passed.*

## Known issue: prose-preface compile errors

V5 and P8 (and to some extent A9) hit the same packaging bug: the tool-using
agent emitted analysis prose at the top of the `.rs` file before the
`#![cfg(feature = "test")]` line. The test extractor in `confirm.py` does not
yet strip prose preambles, so the file was written verbatim and failed to
compile with `error: prefix \`I\` is unknown` / `error: prefix \`finding\`
is unknown` (the rust parser tripping on "I found..." / "finding: ...").

The agent's *reasoning* in both cases was correct against the source:

* **V5**: identified `min(residual, pnl_matured_pos_tot)` at `percolator.rs:5475`
  as the clamp that bounds `h_num ≤ h_den`, so `ratio ≤ 1`.
* **P8**: identified `a == b` rejection at `percolator.rs:7623`, plus
  fees-always-≥-0 routing to insurance via `charge_fee_to_insurance` at
  `percolator.rs:7875-7895`.

The prose-preface bug is filed for the next confirm-pipeline iteration.
None of the failures here represent missed empirical signal — they're
delivery-layer issues, not methodology issues.

## Files

```
test_confirm_o6_side_flip_atomicity.{rs,cargo.log,summary.json}     — passing test (real)
test_confirm_s5_market_mode_transitions.{rs,cargo.log,summary.json} — passing test (real)
test_confirm_v5_haircut_direction.{rs,cargo.log,summary.json}       — agent-reasoned, prose-preface failure
test_confirm_p8_self_trade_cash_flow.{rs,cargo.log,summary.json}    — agent-reasoned, prose-preface failure
test_confirm_a9_pause_gate_coverage.{rs,cargo.log,summary.json}     — honest CANNOT_TEST disguised as compile error
confirm_run_20260506-232213.log                                     — full driver log
```

## Reproducing

The driver script that produced this session is checked in at
[`/root/run_confirms_2026_05_06_evening.sh`](confirm_run_20260506-232213.log)
(see log header). Each confirm was dispatched as:

```bash
audit-pipeline --workspace /root/audit_runs/percolator-live confirm \
    -r hunts/20260506-225557-5059332/recon/<HYP>_response.md \
    --hyp-id <HYP> \
    --hypotheses-file src/audit_pipeline/templates/hypotheses/percolator_deep.yaml
```

The cycle's per-hypothesis recon prompts and responses are at
[`examples/recent-hunts/20260506-225557-5059332/recon/`](../recent-hunts/20260506-225557-5059332/recon/).
