# audit-pipeline (CLI)

CLI orchestrator for the [Solana Audit Pipeline](../solana-audit-pipeline/). Wraps the templates, scripts, and agent prompts behind a single command-line interface.

```
audit-pipeline init   <target-repo>      # scaffold a new audit workspace
audit-pipeline recon  <hypothesis-list>  # spawn Layer-1 multi-agent review
audit-pipeline poc    <finding>          # generate Layer-2 PoC scaffold
audit-pipeline kani   <harness>          # author + dispatch Layer-3 Kani
audit-pipeline litesvm <test>            # author + dispatch Layer-4 LiteSVM
audit-pipeline cross-check <test>        # Layer-5 cross-platform compare
audit-pipeline disclose                  # generate disclosure docs
audit-pipeline run                       # full pipeline end-to-end
```

## Status

**Alpha.** This CLI is the operational wrapper for the methodology in `solana-audit-pipeline/`. The methodology is production-grade (it produced the Percolator audit). The CLI is being shipped as a developer-grade convenience layer on top.

For production audits, you can ALWAYS fall back to the underlying templates and scripts in `solana-audit-pipeline/` — the CLI doesn't introduce magic, it just automates the orchestration.

## Installation

```bash
pip install -e .
# or
pipx install .
```

Requires Python 3.10+. Underlying tooling (Rust, Solana, Kani, LiteSVM) is the same as the methodology repo — see `solana-audit-pipeline/docs/reusability-checklist.md`.

## Quick start

```bash
# 1. Initialize a new audit workspace for a target Solana program
audit-pipeline init \
    --engine-repo https://github.com/aeyakovenko/percolator \
    --engine-sha 5940285 \
    --wrapper-repo https://github.com/aeyakovenko/percolator-prog \
    --wrapper-sha c447686 \
    --output ./percolator-audit/

# 2. Provision the VPS (one-time)
audit-pipeline provision-vps \
    --host root@1.2.3.4 \
    --ssh-key ~/.ssh/audit_vps

# 3. Sync target code to VPS
audit-pipeline sync \
    --host root@1.2.3.4 \
    --ssh-key ~/.ssh/audit_vps

# 4. Layer 1 — multi-agent recon
# (manual: spawn agents via your LLM; this command pre-builds the prompts)
audit-pipeline recon \
    --hypotheses hypotheses.yaml \
    --output recon/

# 5. Layer 2 — generate PoC scaffold from a finding template
audit-pipeline poc \
    --finding bug3_trade_open_overflow \
    --template engine_native_poc \
    --output ./percolator-audit/tests/

# 6. Layer 3 — Kani harness
audit-pipeline kani author \
    --finding bug3_trade_open_overflow \
    --template kani_cex_panic_class
audit-pipeline kani dispatch \
    --harness proof_bug3_trade_open_overflow_does_not_panic \
    --host root@1.2.3.4 \
    --ssh-key ~/.ssh/audit_vps

# 7. Layer 4 — LiteSVM
audit-pipeline litesvm author \
    --finding bug3_trade_open_overflow \
    --template litesvm_bound_analysis
audit-pipeline litesvm dispatch \
    --test test_bug3_trade_open_overflow_bound_analysis \
    --host root@1.2.3.4 \
    --ssh-key ~/.ssh/audit_vps

# 8. Layer 5 — cross-platform compare
audit-pipeline cross-check \
    --test test_bug3_trade_open_overflow_bound_analysis \
    --host root@1.2.3.4 \
    --ssh-key ~/.ssh/audit_vps

# 9. Generate disclosure docs
audit-pipeline disclose \
    --findings findings.yaml \
    --output ./percolator-audit-disclosure/
```

## What this gives you over the standalone methodology repo

| Without CLI (standalone repo) | With CLI |
|---|---|
| Manual `cp templates/X tests/` | `audit-pipeline poc --finding ...` |
| Manual SSH + tmux + scp dance | `audit-pipeline kani dispatch ...` |
| Manual disclosure writing from template | `audit-pipeline disclose --findings ...` |
| Manual cross-platform diff | `audit-pipeline cross-check ...` |

The CLI doesn't change WHAT the pipeline does — it changes HOW MUCH typing is needed.

## What this does NOT give you

- Magic. Every command is implemented as a wrapper around the templates and scripts in `solana-audit-pipeline/`. If a command misbehaves, drop down to the underlying script.
- Multi-agent dispatch. The `recon` command pre-builds the prompts; you still need to send them to your LLM (Claude, etc.) and collect responses. (A future version may integrate with Anthropic's API.)
- A guarantee that audits will find bugs. The CLI is process automation; finding bugs still requires hypothesis design + careful work.

## Architecture

```
src/audit_pipeline/
├── cli.py                       (main entrypoint, click-based)
├── commands/
│   ├── init.py                  (scaffold workspace)
│   ├── provision_vps.py         (calls scripts/provision_vps.sh)
│   ├── sync.py                  (calls scripts/helpers/sync_target_repos.sh)
│   ├── recon.py                 (build agent prompts from hypotheses.yaml)
│   ├── poc.py                   (instantiate Layer-2 template)
│   ├── kani.py                  (instantiate Layer-3 template + dispatch)
│   ├── litesvm.py               (instantiate Layer-4 template + dispatch)
│   ├── cross_check.py           (Layer-5 cross-platform compare)
│   ├── disclose.py              (generate DISCLOSURE.md from findings.yaml)
│   └── run.py                   (end-to-end pipeline if everything is configured)
├── templates/                   (copy of solana-audit-pipeline/templates/)
├── scripts/                     (copy of solana-audit-pipeline/scripts/)
└── utils/
    ├── ssh.py                   (SSH abstraction)
    ├── tmux.py                  (tmux session management)
    ├── kani_log.py              (parse cargo kani output)
    ├── git.py                   (clone + pin SHA)
    └── prompts.py               (load + parameterize agent prompts)

tests/
├── test_init.py
├── test_cli_basic.py
└── fixtures/
    └── sample_hypotheses.yaml

examples/
└── percolator/                  (worked example: percolator audit via CLI)
```

## License

Apache-2.0. See `LICENSE`.
