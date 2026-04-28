# audit-pipeline (CLI)

Autonomous AI-driven security audit pipeline for Solana programs. Runs 24/7 on a VPS, fires a 12-agent hunt cycle on every upstream commit, writes findings to a SQLite database with severity + lifecycle, and (optionally) auto-files GitHub issues + posts Slack alerts.

**5-layer technical flow:** multi-agent code review → empirical PoC → Kani formal verification → LiteSVM end-to-end → cross-platform reproduction.

**Plus the autonomous hunt loop** (`hunt` + `watch --on-update`) that turns the methodology into a continuous, hands-off bug hunter with cost caps, daily spend limits, and severity-tagged findings.

```
─── Core 5-layer pipeline ─────────────────────────
audit-pipeline init           # scaffold a new audit workspace at pinned SHAs
audit-pipeline provision-vps  # one-time VPS setup (Rust + Solana + Kani + tmux)
audit-pipeline sync           # sync target repo to a VPS workspace
audit-pipeline recon          # render Layer 1 multi-agent hypothesis prompts
audit-pipeline poc            # generate Layer 2 PoC scaffold (panic OR state-conservation)
audit-pipeline kani           # author + dispatch Layer 3 Kani harnesses
audit-pipeline litesvm        # author + dispatch Layer 4 LiteSVM tests
audit-pipeline cross-check    # Layer 5 cross-platform compare
audit-pipeline disclose       # generate disclosure docs from findings.yaml
audit-pipeline run            # interactive end-to-end walkthrough

─── Force multipliers ────────────────────────────
audit-pipeline spec-check     # Layer 0: spec ↔ code gap analysis (--auto)
audit-pipeline debate         # Layer 1.5: adversarial second-opinion (--auto)
audit-pipeline propagate      # Layer 1.6: cross-protocol pattern search (init-corpus + search)
audit-pipeline synth-kani     # Layer 2.5/3: NL invariant → Kani harness with compile-fix-retry (--auto)
audit-pipeline shadow start   # Layer 6: 24/7 mainnet shadow audit + state-delta detection
audit-pipeline shadow tail    # view recent alerts

─── Live source-code tracking ────────────────────
audit-pipeline freshness      # one-shot: how stale is your workspace vs upstream?
audit-pipeline watch          # continuous: pull new commits + auto-rerun audit

─── Autonomous hunt loop (production entrypoint) ─
audit-pipeline hunt           # recon → debate → PoC → Kani → DB → Slack/GitHub
                              # Designed for `watch --on-update`
                              # Per-cycle + per-day cost caps

─── Commercial layer (T1-T2-T3) ──────────────────
audit-pipeline onboard <url>  # one-shot: clone repo + scaffold workspace + register target
audit-pipeline dashboard      # self-contained HTML status dashboard (with auto-refresh)
audit-pipeline report cycle   # per-cycle HTML report
audit-pipeline report weekly  # rolling 7-day HTML summary
audit-pipeline issue draft    # render Markdown issue body for a finding
audit-pipeline issue file     # actually file via `gh issue create`
audit-pipeline issue auto-file-confirmed  # batch-file from a cycle (severity floor)
audit-pipeline health         # daemon health check (systemd-timer + Slack-on-degraded)
```

25 commands. Production-grade `--auto` modes for the LLM-backed commands (require `ANTHROPIC_API_KEY`).

---

## Status

**Beta.** The methodology is production-grade — it produced [confirmed disclosures on Anatoly Yakovenko's Percolator perpetual DEX](https://github.com/aeyakovenko/percolator-prog/pull/39). The CLI is the operational wrapper around that methodology.

For production audits, you can ALWAYS fall back to the underlying templates and scripts in [`solana-audit-pipeline`](https://github.com/Copenhagen0x/solana-audit-pipeline) — the CLI doesn't introduce magic, it just automates the orchestration.

---

## Installation

```bash
git clone https://github.com/Copenhagen0x/audit-pipeline-cli
cd audit-pipeline-cli
pip install -e .
```

Requires Python 3.10+. For the `--auto` modes, set `ANTHROPIC_API_KEY` in your environment.

VPS-side tooling (Rust 1.95+, Solana CLI 3.1+, Kani 0.67+, tmux): see `solana-audit-pipeline/scripts/provision_vps.sh` or run `audit-pipeline provision-vps` to set up automatically.

---

## Quick start — full audit on any Solana program

```bash
# 1. Scaffold the workspace at pinned commit SHAs
audit-pipeline init \
    --engine-repo  https://github.com/<org>/<engine-repo> \
    --engine-sha   <engine-sha> \
    --wrapper-repo https://github.com/<org>/<wrapper-repo> \
    --wrapper-sha  <wrapper-sha> \
    --output ./<target>-audit/

# 2. Layer 0 — surface spec/code drifts (the F7-class methodology)
audit-pipeline --workspace ./<target>-audit spec-check \
    --spec ./target/engine/spec.md \
    --code ./target/engine/src/<main>.rs \
    --auto

# 3. Layer 1 — render hypothesis prompts from gaps + your own list
audit-pipeline --workspace ./<target>-audit recon \
    --hypotheses hypotheses.yaml

# 4. Layer 1.5 — adversarial debate on contested verdicts
audit-pipeline --workspace ./<target>-audit debate \
    --hypothesis-id H1 \
    --proposer-verdict recon/H1_response.md \
    --auto

# 5. Layer 2 — empirical PoC for confirmed findings
audit-pipeline --workspace ./<target>-audit poc \
    --finding f7_residual_grows \
    --template engine_state_conservation_poc \
    --engine-function absorb_protocol_loss \
    --invariant-description "residual = vault - (c_tot + insurance) is preserved across the call"

# 6. Layer 2.5/3 — describe an invariant in English, get a verified Kani harness
audit-pipeline --workspace ./<target>-audit synth-kani \
    --invariant "the residual is preserved across absorb_protocol_loss" \
    --engine-function absorb_protocol_loss \
    --mode safe \
    --auto --run-kani

# 7. Layer 4 — LiteSVM end-to-end reachability
audit-pipeline --workspace ./<target>-audit litesvm author \
    --finding f7_residual_grows \
    --template litesvm_bound_analysis

# 8. Layer 5 — cross-platform reproduction
audit-pipeline --workspace ./<target>-audit cross-check \
    --test test_f7_residual_grows_bound_analysis

# 9. Generate disclosure docs
audit-pipeline --workspace ./<target>-audit disclose \
    --findings findings.yaml
```

## Quick start — continuous monitoring (24/7)

```bash
# Source-code watcher: pulls new commits + fires the autonomous hunt loop
audit-pipeline --workspace ./<target>-audit watch \
    --auto-pull --update-pin --interval 300 \
    --on-update "source ~/.audit-env && audit-pipeline hunt"

# Mainnet shadow audit: polls Solana RPC + watches account state byte-by-byte
audit-pipeline --workspace ./<target>-audit shadow start \
    --program <program-pubkey> \
    --watch-account <slab-account> \
    --watch-fields fields.json
```

Production-grade systemd deployment is bundled in `deploy/`:

```bash
# One command — installs sentinel-{shadow,watch,health,backup}.{service,timer},
# kills any existing tmux sessions, enables units, restarts.
sudo bash deploy/install_systemd.sh
```

---

## Autonomous hunt loop

`hunt` is the production entrypoint. One invocation runs the full chain:

1. **Layer 1** — `recon --auto` dispatches N parallel Claude agents, one per hypothesis
2. **Layer 1.5** — `debate --auto` runs adversarial second-opinion on contested verdicts
3. **Layer 2** — for every TRUE/HIGH verdict: scaffold a state-conservation PoC and run `cargo test`
4. **Layer 3** — for every PoC that fires: `synth-kani --auto` generates + runs a verified Kani harness
5. **Synthesis** — every verdict is written to `findings.db` with derived severity (Critical/High/Medium/Low/Info) and an initial lifecycle status (`new` / `triaged` / `confirmed` / `rejected`)
6. **Reporting** — Markdown + JSON cycle artifacts; HTML report on demand; Slack webhook on confirmed findings

**Cost discipline:** every cycle has a `--budget-cap-usd` and there's a global `--daily-cap-usd` (or `AUDIT_DAILY_CAP_USD` env var) that aborts the cycle if spending today + this cycle's estimate would exceed it. State is persisted in `<workspace>/.daily_spend.json`.

---

## Commercial features (T1 → T3)

Built for selling the pipeline as audit-as-a-service:

- **Severity rubric** — Critical / High / Medium / Low / Info with formal definitions, derived automatically from `(hypothesis_class, verdict, debate_promoted, poc_fired)` or set explicitly per hypothesis.
- **Finding lifecycle state machine** — `new → triaged → confirmed → disclosed → fixed → verified` with audit-trail transitions in SQLite.
- **SQLite findings DB** — `targets`, `cycles`, `findings`, `transitions` tables. Single source of truth.
- **Slack/Discord webhook** — fires on confirmed findings. Set `HUNT_WEBHOOK_URL`.
- **GitHub Issue auto-filer** — `audit-pipeline issue auto-file-confirmed --cycle-id ... --repo owner/name --severity-floor High` opens issues for confirmed findings; transitions them to `disclosed`.
- **HTML reports** — per-cycle and rolling weekly. No Jinja, no Flask — single self-contained HTML files.
- **Customer dashboard** — `audit-pipeline dashboard` writes (and optionally serves) a status page with KPIs, severity breakdown, recent findings, target list.
- **Multi-target onboarding** — `audit-pipeline onboard https://github.com/owner/repo` clones + pins + scaffolds + registers in one command. Templates for `perp_dex`, `lending`, `amm`, `minimal`.
- **Operational hardening** — sliding-window rate limiters + retry-with-backoff (Anthropic / GitHub / RPC), JSON-formatted daily-rotating logs, daily SQLite `.backup` with rotation, systemd-timer health check that POSTs Slack on degraded state.

---

## Cross-protocol pattern search

```bash
# One-time: clone curated list of 15 popular Solana protocols
audit-pipeline propagate init-corpus --corpus ~/corpus

# After finding a bug on protocol A, search for the same pattern across all 15
audit-pipeline propagate search \
    --corpus ~/corpus \
    --signature 'insurance.*\.balance' \
    --signature 'vault\s*[-+]?='
```

Default corpus includes Drift, Mango, Marginfi, Kamino, Phoenix, OpenBook, Orca, Meteora, Raydium, Anchor framework, SPL, plus the audit target itself.

---

## How it actually works

Each layer reduces a different uncertainty:

| Layer | What it catches |
|---|---|
| 0 — Spec ↔ code drift | Places the spec says one thing and the code does another (the F7-class) |
| 1 — Multi-agent code review | Hypotheses you'd miss reading alone |
| 1.5 — Adversarial debate | First-pass FALSE/HIGH verdicts that got bluffed by doc comments |
| 1.6 — Cross-protocol propagate | Recurring bug-class patterns across the ecosystem |
| 2 — Empirical PoC | False positives (if you can't write the test, it's not a bug) |
| 2.5 — NL → Kani synthesis | Skill barrier to authoring formal proofs |
| 3 — Kani formal verification | Bugs invisible to random testing AND proves SAFE properties |
| 4 — LiteSVM end-to-end | Whether the public API can actually drive state to the Kani witness |
| 5 — Cross-platform reproduction | Platform-specific artifacts vs real findings |
| 6 — Live mainnet shadow | Real-time detection of state changes on deployed binaries |

---

## License

Apache-2.0. See `LICENSE`.

Methodology: [Copenhagen0x/solana-audit-pipeline](https://github.com/Copenhagen0x/solana-audit-pipeline)
