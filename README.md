# audit-pipeline (CLI)

AI-driven security audit pipeline for Solana programs. CLI orchestrator for the [Solana Audit Pipeline](https://github.com/Copenhagen0x/solana-audit-pipeline) methodology.

**5-layer flow:** multi-agent code review → empirical PoC → Kani formal verification → LiteSVM end-to-end → cross-platform reproduction.

**Plus 5 force-multiplier commands** that turn the linear pipeline into a system with feedback loops, live monitoring, and cross-protocol intelligence.

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
```

19 commands. 66/66 tests passing. Production-grade `--auto` modes for the LLM-backed commands (require `ANTHROPIC_API_KEY`).

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
# Source-code watcher: pulls new commits + reruns audit on every push
audit-pipeline --workspace ./<target>-audit watch \
    --auto-pull --update-pin \
    --on-update "audit-pipeline recon -h hypotheses.yaml"

# Mainnet shadow audit: polls Solana RPC + watches account state byte-by-byte
audit-pipeline --workspace ./<target>-audit shadow start \
    --program <program-pubkey> \
    --watch-account <slab-account> \
    --watch-fields fields.json
```

Both designed to run on a VPS under `tmux` or `systemd`.

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
