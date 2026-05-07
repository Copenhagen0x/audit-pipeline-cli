# Empirical-confirm session — 2026-05-06

A live capture of Jelleo's **`audit-pipeline confirm`** stage running against
[`aeyakovenko/percolator`](https://github.com/aeyakovenko/percolator) at
SHA `5059332` ("Record full Kani audit pass" — upstream HEAD at the time
of the session).

The `confirm` stage takes a recon agent's response.md (the agent's reasoning
about a hypothesis) and dispatches a tool-using Claude agent to:

1. **Read the actual engine source** via `read_file` / `grep` / `find_function`
2. **Construct a deterministic Rust integration test** that exercises the
   invariant the recon flagged
3. **Compile it with `cargo test --features test`**
4. **Classify the outcome:**
   - `safety_attestation` (rc=0)   — test passed → invariant holds empirically
   - `fired` (rc=101)              — test panicked → invariant violated → potential bug
   - `compile_error` / `cannot_test` — agent couldn't construct a valid test

This session ran four hypotheses. **Two passed cleanly, one fired (and re-discovered F7), and two were honestly rejected as untestable.** That mix is the methodology working as designed.

## Results

| Hypothesis | Outcome | Evidence |
|---|---|---|
| `V1-vault-residual-conservation` | 🚨 **fired** | residual leaked by 10,000 across `absorb_protocol_loss` — F7 class re-discovered |
| `V4-vault-cap-respect` | ✅ safety_attestation | invariant holds against all tested vault states |
| `SH5-keeper-crank-touching-completeness` | ⚠️ cannot_test | helpers gated behind `cfg(kani)` — structurally untestable as integration test |
| `SH6-resolve-flat-negative-gate` | ⚠️ cannot_test | hypothesis references function names that don't exist in current revision (`resolve_flat_negative` was renamed) |

**Total cost of session: ~$3 across 4 confirms.** Each confirm dispatched the
tool-using agent for ~19 turns, ~25 tool calls, ~300K input tokens.

## The headline: V1 re-discovered F7

The most significant outcome is the V1 confirm. The agent:

1. Read `percolator.rs` at lines 4811–4852 (the `use_insurance_buffer` /
   `absorb_protocol_loss` region)
2. Articulated the F7 conservation invariant in plain English in the test header
3. Constructed a deterministic state — top up insurance fund, deposit a user, snapshot
4. Called `engine.absorb_protocol_loss(loss)` — the real engine function
5. Snapshotted post-state and asserted residual is conserved

The assertion fired with a precise message:

```
VIOLATION CONFIRMED: residual is not conserved across absorb_protocol_loss.
pre_residual=0 post_residual=10000 delta=+10000
(insurance shrank by 10000 but vault was not debited;
 source: percolator.rs lines 4811-4821, 4844-4852)
```

This is the **F7 bug class**, empirically re-confirmed against current main.
F7 was originally disclosed via [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39) and the regression suite landed on main even though the patch closed unmerged. **The function-level invariant is still violated** — Anatoly's team chose to gate the full attack at higher layers via existing engine defenses rather than fix the helper-level conservation property.

This session is *not* claiming a new disclosure-grade finding. It's a fresh
**empirical receipt** that the same methodology that originally produced F7 is
running today and continues to detect the same bug class autonomously.

## The cannot_test cases — methodology integrity

`SH5` and `SH6` are equally important to publish, because they show the methodology
**refuses to manufacture findings.** When the tool-using agent investigates and
determines a hypothesis isn't testable as a black-box integration test, it returns
a clear `cannot_test` result with the reasoning preserved — no fake panics, no
hand-waved "looks like a bug" claims.

Read `SH5-keeper-crank-touching-completeness.cannot_test.txt` and
`SH6-resolve-flat-negative-gate.cannot_test.txt` for the agent's full investigation.

## Files

```
test_confirm_v1_vault_residual_conservation.rs       — fired test (real)
test_confirm_v1_vault_residual_conservation.cargo.log — full cargo output
test_confirm_v1_vault_residual_conservation.summary.json
test_confirm_v4_vault_cap_respect.rs                 — passing test (real)
test_confirm_v4_vault_cap_respect.cargo.log
test_confirm_v4_vault_cap_respect.summary.json
SH5-keeper-crank-touching-completeness.cannot_test.txt — honest rejection
SH6-resolve-flat-negative-gate.cannot_test.txt
```

## Reproducing

The pipeline that produced these artifacts is at
[github.com/Copenhagen0x/audit-pipeline-cli](https://github.com/Copenhagen0x/audit-pipeline-cli).
To run a confirm yourself:

```bash
audit-pipeline --workspace /your/workspace confirm \
    -r path/to/recon_response.md \
    --hyp-id YOUR-HYP-ID \
    --hypotheses-file path/to/hypotheses.yaml
```

The agent will read your engine source (under `<workspace>/target/engine/`),
generate the test, compile, run, and classify the outcome.
