# JELLEO

**Autonomous immune system for Solana DeFi.** Four interlocking pillars — counterfactual mainnet detection, cross-protocol bug-class propagation, closed-loop fix bundles, and on-chain attestation registry — that together replace static-PDF audit reports with adaptive, autonomous, ecosystem-composable security. Built on the F7-disclosure methodology. Inaugural deployment: Anatoly Yakovenko's Percolator perpetual DEX.

Track record: F7 disclosure to [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39) — public record, regression coverage formally labeled "PR39/F7" in `main` of `aeyakovenko/percolator-prog` (commit [`a1afd2e`](https://github.com/aeyakovenko/percolator-prog/commit/a1afd2e)).

**Recent capability uplift (2026-04-28):** Tool-using deep-audit mode (`hunt-deep`) — agents have `read_file`, `grep`, `find_function` and iteratively explore source code to render line-cited verdicts. Disclosure-pattern miner (`learn-from-disclosures`) auto-generates sibling hypotheses from public bug reports. Custom PoC writer (`confirm`) generates Rust tests targeting specific finding claims and runs them under `cargo test`. See [OUTREACH/jelleo-one-pager.md](OUTREACH/jelleo-one-pager.md) and [examples/](examples/) for sample outputs.

---

## Four pillars (product architecture)

Jelleo's product positioning is four interlocking pillars. Each pillar is a distinct product capability that composes with the others to form the autonomous immune-system loop:

| Pillar | What it does | Existing primitives |
|---|---|---|
| **P1 — Counterfactual mainnet detection** | Per-tx parallel simulation against forked state — flags transactions where counterfactual state diverges from actual, in real time, before the attack chain completes. | `shadow` (Layer 6) |
| **P2 — Cross-protocol bug-class propagation** | When a bug is disclosed anywhere in the ecosystem, auto-extracts the structural pattern and searches every indexed protocol for the same class within minutes. | `propagate`, `learn-from-disclosures` |
| **P3 — Closed-loop fix bundle** | When a bug is confirmed, generates the fix, formally proves (via Kani) it preserves all other invariants, validates the test suite, bundles bug + fix + proof + tests into one PR. | `confirm`, `synth-kani`, Ed25519 signing |
| **P4 — On-chain attestation registry** | Every audit cycle publishes a cryptographically-signed Merkle root attesting which invariants were verified at which commit SHA. Composable on-chain primitive other protocols can require as a precondition. | Ed25519 signing, signed disclosure packages |

P1 detects in real time. P2 propagates defenses across protocols. P3 closes the loop from disclosure to verified fix. P4 makes every cycle cryptographically composable. Together they replace the static-PDF audit-report model with adaptive, autonomous, on-chain-composable security infrastructure.

---

## Implementation pipeline

The 4 pillars above are implemented as a layered hunt cycle dispatched on every upstream commit. Layers compose into pillars; pillars are the product, layers are the technical architecture:

| Layer | Capability |
|---|---|
| **0** — Spec/code drift | Continuous detection of where the protocol's spec and implementation diverge (the F7-class). |
| **1** — Multi-agent recon | N parallel Claude agents, one per hypothesis. Per-target hypothesis libraries with severity tagging. |
| **1.5** — Adversarial debate | Second-opinion challenger against every contested verdict. Promotes silently-bluffed FALSEs back into the candidate set. |
| **1.6** — Cross-protocol propagation | When a finding lands, the same pattern is searched across the indexed corpus. (Backs Pillar 2.) |
| **2** — Empirical PoC | Auto-scaffolded state-conservation tests run under `cargo test`. PoCs that fire confirm the finding empirically. (Backs Pillar 3.) |
| **2.5 / 3** — Kani formal verification | NL-to-Kani harness synthesis with compile-fix-retry loop. SAFE proofs for invariants; CEX proofs for violations. (Backs Pillar 3.) |
| **4** — LiteSVM end-to-end | BPF-level reachability + bound analysis. Verifies that the public API can drive state to the verified witness. |
| **5** — Cross-platform reproduction | Diff test outputs between local + mainnet-equivalent VPS to eliminate platform artifacts. |
| **6** — Live mainnet shadow | 24/7 RPC polling + byte-level account-state-delta detection on deployed binaries. (Foundation for Pillar 1.) |

Every verdict — confirmed, refuted, or escalated — is written to a SQLite findings database with derived severity (Critical / High / Medium / Low / Info), an enforced lifecycle state machine (`new → triaged → confirmed → disclosed → fixed → verified`), and a full audit trail of state transitions.

---

## Operations layer

| Capability | Detail |
|---|---|
| **Slack / Discord alerts** | Real-time webhook on confirmed findings, severity-tagged with cycle metadata. |
| **GitHub Issue auto-filing** | Confirmed findings above a configurable severity floor are auto-drafted (or auto-filed) against the target repository. Lifecycle transitions to `disclosed`. |
| **HTML dashboard** | Self-contained HTML dashboard with KPIs, severity breakdown, target cards, recent findings, refreshing daemon status. Can be served from the deployment host or attached to email. |
| **Per-cycle + weekly HTML reports** | Branded executive reports with severity rubric, finding details, audit trail. |
| **Target onboarding** | Single command — `audit-pipeline onboard <github-url>` — clones, pins, scaffolds, and registers a new target. Baseline hypothesis libraries available for `perp_dex` (Percolator-tailored, 125 hyps), with `lending` / `amm` corpora as Year 2+ multi-protocol scaffolding. |
| **Multi-target capable** | Architecture supports N programs in parallel with full per-target isolation. Inaugural deployment is Percolator-only; multi-protocol scaling is the Year 2+ path. |

---

## Production hardening

| | |
|---|---|
| **systemd-managed services** | `jelleo-shadow`, `jelleo-watch`, `jelleo-health`, `jelleo-backup` — auto-restart on failure, survive reboots. |
| **Health monitoring** | systemd-timer health check every 5 minutes; alerts to Slack on degraded state (stale daemon logs, DB corruption, missed cycles). |
| **Daily DB backup** | SQLite `.backup` daily at 04:30 UTC, configurable rotation (default 30 copies). |
| **Rate limiters + retry-with-backoff** | Sliding-window rate limiters per external surface (Anthropic, GitHub, Solana RPC). Exponential-backoff retry on transient failures (429/502/503/504, timeouts, connection errors). |
| **Structured logging** | JSON-formatted, daily-rotating, 14-day retention by default. Pipe-able into any log-shipping pipeline. |
| **Hard spend caps** | Per-cycle and per-day spend caps enforced at the framework level. Cycles abort cleanly when caps would be exceeded. |
| **Reproducibility** | Every finding pinned to engine + wrapper SHA. Re-running against the same SHA yields the same verdict (modulo LLM nondeterminism — debate compensates). |

---

## Track record

| | |
|---|---|
| **F7 — Percolator residual-conservation** | [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39). Disclosed April 2026 — identified a self-dealing insurance-siphon attack class. Maintainer closed the PR without merging the proposed vault-debit fix; chose the engine's existing protections (bounded dt, bounded price movement, solvency-envelope validation, A1 regression suite) as the defense path. Disclosure formally mapped to A1 regression coverage labeled "PR39/F7" on `main` ([commit `a1afd2e`](https://github.com/aeyakovenko/percolator-prog/commit/a1afd2e)). |
| **Continuous Percolator coverage** | Jelleo runs against the Percolator engine + wrapper repos 24/7. Every commit triggers a full hunt cycle. |
| **Hypothesis library** | 125 hand-curated invariants on disk: 12 baseline (`percolator.yaml`), 101 deep-protocol (`percolator_deep.yaml`), 12 sibling-derived from F7 (`percolator_strict_helper_class.yaml`). |
| **Empirical confirmation samples** | Three Rust integration tests autonomously generated by `confirm` and passing under `cargo test` against the real engine. Public in [examples/confirmed-tests/](examples/confirmed-tests/). |

---

## Architecture (one-liner)

`watch --on-update` triggers `hunt`, which orchestrates `recon --auto` → `debate --auto` → `poc` → `synth-kani --auto` → write to `findings.db` → emit cycle artifacts → optionally alert/file.

All modules are standalone CLI commands and remain individually invokable for offline analysis, custom workflows, or human-in-the-loop investigation.

```
─── Core 5-layer pipeline ─────────────────────────
audit-pipeline init           # scaffold a new audit workspace at pinned SHAs
audit-pipeline provision-vps  # one-time VPS setup (Rust + Solana + Kani + tmux)
audit-pipeline sync           # sync target repo to a VPS workspace
audit-pipeline recon          # render Layer 1 multi-agent hypothesis prompts
audit-pipeline poc            # generate Layer 2 PoC scaffold
audit-pipeline kani           # author + dispatch Layer 3 Kani harnesses
audit-pipeline litesvm        # author + dispatch Layer 4 LiteSVM tests
audit-pipeline cross-check    # Layer 5 cross-platform compare
audit-pipeline disclose       # generate disclosure docs from findings.yaml
audit-pipeline run            # interactive end-to-end walkthrough

─── Force multipliers ────────────────────────────
audit-pipeline spec-check     # Layer 0: spec ↔ code gap analysis (--auto)
audit-pipeline debate         # Layer 1.5: adversarial second-opinion (--auto)
audit-pipeline propagate      # Layer 1.6: cross-protocol pattern search
audit-pipeline synth-kani     # Layer 2.5/3: NL → Kani harness with compile-fix-retry (--auto)
audit-pipeline shadow start   # Layer 6: 24/7 mainnet shadow audit
audit-pipeline shadow tail    # view recent alerts

─── Live source-code tracking ────────────────────
audit-pipeline freshness      # one-shot: how stale is your workspace vs upstream?
audit-pipeline watch          # continuous: pull new commits + auto-rerun audit

─── Autonomous hunt loop (production entrypoint) ─
audit-pipeline hunt           # recon → debate → PoC → Kani → DB → Slack/GitHub

─── Operations / commercial layer ────────────────
audit-pipeline onboard <url>  # one-shot: clone repo + scaffold + register target
audit-pipeline dashboard      # self-contained HTML status dashboard
audit-pipeline report cycle   # per-cycle HTML report
audit-pipeline report weekly  # rolling 7-day HTML summary
audit-pipeline issue draft    # render Markdown issue body for a finding
audit-pipeline issue file     # file via `gh issue create`
audit-pipeline issue auto-file-confirmed  # batch-file confirmed (severity floor)
audit-pipeline health         # daemon health check (systemd-timer integration)
audit-pipeline narrative generate  # LLM-generated finding writeups (description + reproduction + fix)
audit-pipeline sign keygen / sign / verify  # Ed25519-signed disclosure packages

─── Deep-audit layer (tool-using agents) ─────────
audit-pipeline hunt-deep      # tool-using agent loop: read_file + grep + find_function
                              # produces line-cited verdicts
audit-pipeline confirm        # write custom Rust PoC test, compile, cargo test, classify
                              # converts NEEDS_LAYER_2 leads -> confirmed/refuted

─── Hypothesis generation (autonomous) ────────────
audit-pipeline learn-from-disclosures  # extract attack patterns from public GH issues,
                              # generate sibling hypotheses
audit-pipeline expand-coverage         # generate hyps from spec.md, Kani-coverage gaps,
                              # and wrapper public instruction handlers
```

---

## Engagement

For continuous-audit engagements, deeper hypothesis libraries, custom invariant work, or platform partnerships: open an issue on this repository or reach out via the contact channels in [Copenhagen0x](https://github.com/Copenhagen0x).

---

## Engineer install (for contributors and self-hosted operators)

```bash
git clone https://github.com/Copenhagen0x/audit-pipeline-cli
cd audit-pipeline-cli
pip install -e .
```

Requires Python 3.10+. For LLM-backed `--auto` modes, set `ANTHROPIC_API_KEY`. For VPS-side tooling (Rust 1.95+, Solana 3.1+, Kani 0.67+): `audit-pipeline provision-vps`.

Production deployment (systemd-managed):

```bash
sudo bash deploy/install_systemd.sh
```

This installs `jelleo-{shadow,watch,health,backup}.{service,timer}`, tears down any existing tmux sessions, enables units, and verifies status.

---

## License

Apache-2.0. See `LICENSE`.

Methodology repository: [Copenhagen0x/solana-audit-pipeline](https://github.com/Copenhagen0x/solana-audit-pipeline)
