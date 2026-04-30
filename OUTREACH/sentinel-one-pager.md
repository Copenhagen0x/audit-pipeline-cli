# SENTINEL

> Autonomous immune system for Solana DeFi. Four interlocking pillars — counterfactual mainnet detection, cross-protocol bug-class propagation, closed-loop fix bundles, on-chain attestation registry. Inaugural deployment: Percolator. Continuous, AI-driven, code-grounded.

---

## Track record

- **F7 disclosure** — independent disclosure to Anatoly Yakovenko's Percolator perpetual DEX. Identified a self-dealing insurance-siphon attack class. Maintainer closed PR without merging the proposed fix; chose engine's existing protections as the defense path. Disclosure formally mapped to A1 regression coverage labeled "PR39/F7" on `main` ([commit `a1afd2e`](https://github.com/aeyakovenko/percolator-prog/commit/a1afd2e)). PR: [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39)
- **Continuous monitoring** — Sentinel has been running 24/7 against the Percolator engine + wrapper since deployment. Every commit triggers a multi-agent hunt cycle.
- **Tool-using agents** (`read_file` / `grep` / `find_function`) — verdicts cited to specific file paths and line numbers. See [examples/](../examples/) for raw agent outputs.
- **Empirical confirmation layer** — for every TRUE/HIGH safety attestation, Sentinel autonomously generates a Rust integration test, installs it into the engine's `tests/` dir, and runs `cargo test`. **Live verified samples** in [examples/confirmed-tests/](../examples/confirmed-tests/): three tests that compile clean against Percolator and pass on the audited SHA, with their cargo logs as proof.

---

## Architecture in one diagram

```
upstream commit
      │
      ▼
sentinel-watch (systemd, polls every 300s)
      │
      ▼
audit-pipeline hunt
      │
      ├── Layer 1   recon          → 100+ hyps × multi-round adversarial refinement (parallel)
      ├── Layer 1.5 debate          → adversarial challenger flips silently-confident FALSEs
      ├── Layer 2   PoC + cargo test → empirical confirmation
      ├── Layer 2.5 synth-kani       → NL invariant → Kani harness, compile-fix-retry
      ├── Layer 3   Kani formal      → SAFE / CEX proofs
      ├── Layer 4   LiteSVM E2E      → BPF-level reachability
      └── Layer 6   shadow audit     → 24/7 mainnet account-state-delta watch
                              │
                              ▼
            SQLite findings DB (severity + lifecycle state machine)
                              │
                              ▼
       Slack/Discord webhook · GitHub auto-issue · Ed25519-signed disclosure · HTML dashboard
```

---

## Capability matrix

| Capability | Status |
|---|:--:|
| 24/7 continuous monitoring (systemd-managed) | ✅ |
| Multi-agent recon with adversarial refinement | ✅ |
| **Tool-using deep audit** (`hunt-deep` — agents iteratively grep + read code) | ✅ |
| **Custom PoC writer** (`confirm` — generates Rust test, runs `cargo test`) | ✅ |
| **Disclosure-pattern miner** (auto-generates siblings of public bugs) | ✅ |
| **Coverage expander** (spec.md, Kani-gap, wrapper handlers → hypotheses) | ✅ |
| Severity rubric + finding lifecycle state machine | ✅ |
| Per-finding narrative writeups with reproduction + recommended fix | ✅ |
| Ed25519-signed disclosure packages | ✅ |
| Branded HTML dashboard + per-cycle + weekly reports | ✅ |
| GitHub auto-issue filing on confirmed findings | ✅ |
| Slack/Discord webhook on confirmed findings | ✅ |
| Multi-target onboarding (one-command per protocol) | ✅ |
| systemd health checks + daily DB backup + structured logging | ✅ |

---

## Sample output

The following is an excerpt from `hunt-deep` agent output for hypothesis `V4-vault-cap-respect`, generated autonomously in 26 turns / 44 tool calls / one Anthropic API session:

> ### What I found — complete inventory of `self.vault` mutations
>
> There are exactly **6 assignment sites** to `self.vault` in the engine (confirmed by grep):
>
> | Line | Direction | Guard |
> |------|-----------|-------|
> | 5041 | UP (`+amount`) | `checked_add` + `> MAX_VAULT_TVL` check at line 5012 |
> | 5080 | DOWN (`-amount`) | `checked_sub` only |
> | 7041 | UP (`+amount`) | `checked_add` + `> MAX_VAULT_TVL` check at line 7030 |
> | 7117 | DOWN (`-amount`) | `checked_sub` only |
> | 7143 | DOWN (`-payout`) | `checked_sub` only |
> | 7385 | UP (`+pay`) | `checked_add` + `> MAX_VAULT_TVL` check at line 7370 |
> | 6492 | DOWN (`-capital`) | guarded by `capital > self.vault` check at 6489 |
> | 6928 | DOWN (`-capital`) | guarded by `capital > self.vault` check at 6925 |
>
> Every vault-increasing path has the cap check. Every vault-decreasing path uses either `checked_sub` (explicit error on underflow, line 5080) or a pre-checked subtraction (lines 6492, 6928, 7117, 7143).
>
> ...
>
> **VERDICT: TRUE / CONFIDENCE: HIGH**

Sentinel produced this autonomously — line citations, structural inventory, guard analysis, backstop check at line 4075. See [examples/V4-vault-cap-respect_response.md](../examples/V4-vault-cap-respect_response.md) for the full text.

---

## What an engagement looks like

| Tier | Cadence | Deliverable |
|---|---|---|
| **Open-source baseline** | Continuous, free | Public dashboard, line-cited verdicts, monitoring layer, methodology + CLI |
| **Funded inaugural deployment** | Continuous, all four pillars shipped on the funded protocol | Tailored hypothesis library (125+ hyps for the funded target), PoC-confirmed findings, signed disclosure packages, instant alerting on Critical/High, automated 24h + weekly + monthly PDF reports, on-chain attestation registry, first-look on every finding |

---

## Repos

- Platform: [github.com/Copenhagen0x/audit-pipeline-cli](https://github.com/Copenhagen0x/audit-pipeline-cli)
- Methodology: [github.com/Copenhagen0x/solana-audit-pipeline](https://github.com/Copenhagen0x/solana-audit-pipeline)
- Percolator audit case study: [github.com/Copenhagen0x/percolator-audit-2026-04](https://github.com/Copenhagen0x/percolator-audit-2026-04)

---

## Contact

Kirill Sakharuk — `info@wifpros.com`

Open-source under Apache-2.0.
