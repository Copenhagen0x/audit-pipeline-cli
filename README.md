# audit-pipeline-cli

[![CI](https://github.com/Copenhagen0x/audit-pipeline-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Copenhagen0x/audit-pipeline-cli/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776ab.svg)](https://www.python.org/downloads/)

> **What this is:** the Python CLI that runs Jelleo's continuous Solana security hunt loop.
> Reads protocol source, dispatches multi-agent recon, drives empirical PoCs to compile +
> pass under `cargo test`, synthesises Kani harnesses, and writes signed disclosures into a
> SQLite findings DB. Inaugural deployment: Anatoly Yakovenko's Percolator perpetual DEX.

> **Methodology spec:** [`docs/methodology/`](docs/methodology/) — eleven §01–§10 sections covering pillars, hypothesis schema, propagation, severity rubric, lifecycle, attestation, reporting, the F7 case study, and engagement tiers. Layer-by-layer implementation notes under [`docs/methodology/layers/`](docs/methodology/layers/).

> **30-second pitch:** Jelleo is the underwriting layer for Solana DeFi — continuous,
> commit-anchored, on-chain-signed attestations of code-level invariant integrity, designed
> for insurers / partner protocols / STRIDE evaluators to consume as a live signal. This
> repo is the platform.

**Track record.** F7 (residual-conservation insurance-siphon class) disclosed via
[`aeyakovenko/percolator-prog#39`](https://github.com/aeyakovenko/percolator-prog/pull/39).
PR was closed without merging the proposed vault-debit patch; the maintainer adopted
A1-class regression coverage on `main` at commit
[`a1afd2e`](https://github.com/aeyakovenko/percolator-prog/commit/a1afd2e), labeled
`PR39/F7`. Honest reading: the disclosure produced regression-locked defensive coverage on
`main`, not a structural patch.

**Recent capability uplift (2026-04-28):** Tool-using deep-audit mode (`hunt-deep`) — agents have `read_file`, `grep`, `find_function` and iteratively explore source code to render line-cited verdicts. Disclosure-pattern miner (`learn-from-disclosures`) auto-generates sibling hypotheses from public bug reports. Custom PoC writer (`confirm`) generates Rust tests targeting specific finding claims and runs them under `cargo test`. See [OUTREACH/jelleo-one-pager.md](OUTREACH/jelleo-one-pager.md) and [examples/](examples/) for sample outputs.

**Recent capability uplift (2026-05-07 — Tier 2 + Tier 3):** Hypothesis-library expansion to **508 distinct invariants across 5 protocol classes** (`perp_dex`, `amm_cp`, `clmm`, `lending`, `lst`). Auto-derivation (`derive-siblings`) emits structural siblings of every confirmed finding. Lifecycle hooks now auto-fire sibling-derivation + cross-protocol propagation in daemon threads when a finding crosses to `confirmed` — the catalog compounds without manual work. PoC test cache (SHA256-keyed) skips redundant `cargo test` runs across cycles. Diff-aware hunting (`--protocol-class`, `--diff-since-sha`) loads only hyps whose `target_file` lives in a commit's diff. Local triage UI (`audit-pipeline triage --port 8080`) for fast confirm/reject pass over `new` findings. CI on every push (matrix Python 3.10/3.11/3.12, ruff lint + library validation + pytest). See [docs/methodology/03-hypothesis-schema.md](docs/methodology/03-hypothesis-schema.md) for the schema, [docs/methodology/04-propagation.md](docs/methodology/04-propagation.md) for the propagation loop, and [tests/](tests/) for the suite.

**Recent capability uplift (2026-05-07 — Tier 5 architecture):** Multi-tenant customer registry under `<workspace>/customers.json` + per-customer dirs at `<workspace>/customers/<id>/`. New `audit-pipeline customer {add,remove,list,show,rotate-key,pubkey}` subcommand for the operator surface. Per-customer Ed25519 keypairs **derived deterministically** from the platform key via HKDF-SHA256 — each customer signs with their own key, but the operator only custodies one secret. `audit-pipeline sign sign --customer <id>` signs with the derived key. New `audit-pipeline heartbeat` emits a public, signed proof-of-running JSON (engine SHA, hostname, cycle counts, service-status summary, signing-key fingerprint) — quiet weeks (no Critical disclosures) stay verifiable because the heartbeat keeps ticking; hourly via `jelleo-heartbeat.{service,timer}`. Full OpenAPI 3.1 spec for the public surface lives at [docs/api/openapi.yaml](docs/api/openapi.yaml).

**Recent capability uplift (2026-05-08 to 2026-05-11 — all four pillars 100% Y0):** **P2** cross-protocol propagation went end-to-end with auto-fire on `confirmed` lifecycle: corpus initialisation, AST-signature sibling derivation, dispatch queue, status reporting, optional Postgres backend, and a tree-sitter scanner. **P3** closed-loop fix bundle pipeline shipped: `audit-pipeline bundle {draft,verify,authorize,review,override,record-pr-event,publish-archive,list,status,init-repo,open-pr}` — LLM authors a patch, runs the 5-gate verification chain (patch-well-formed, poc-fails-pre-patch, poc-passes-post-patch, tests-pass-post-patch, authorisation marker), and packages the bundle. **HARD RULE: the engine never auto-opens upstream PRs.** A finding is PR-opened only after the operator types `yes-authorize-finding-<id>-<full-64-char-patch-sha>` and all four required gates pass; the marker binds 256 bits of entropy and is `hmac.compare_digest`-checked. **P4** per-cycle Merkle root attestation: new `audit-pipeline merkle {compute,verify,list,rebuild-all}` writes a signed `merkle.json` (schema v3) per cycle covering `(cycle_id, hypothesis_id, verdict, status, severity, engine_sha, wrapper_sha, poc_fired, details_digest)` — `details_digest` closes the post-attestation narrative-tampering window. Hourly `jelleo-heartbeat.timer` publishes a signed `heartbeat.json` to `api.jelleo.com`. **P5 live event stream:** `jelleo-sse.service` tails the active cycle's `hunt.log.jsonl` and broadcasts each engine event as a Server-Sent Event to dashboard subscribers via `api.jelleo.com/events/<customer_id>`. The customer dashboard (`/customer/<id>/full.html`) holds a long-lived EventSource so findings appear within ~1 second of confirmation, not on the previous 60-second polling cadence. **Cadence cleanup:** `notifier.json` now supports an `active_targets` allow-list so the 24h/weekly/monthly scheduler fires one digest per cadence instead of one per registered scope (was 9). **Auto-publish:** the hunt command's new `--auto-publish` flag (default on) generates the signed HTML+PDF cycle report, copies it to `/var/www/jelleo.com/cycles/<id>/`, pushes the artifact bundle to `examples/recent-hunts/`, and fires per-finding email alerts for Critical/High confirmed findings — all from a single `hunt` invocation.

---

## Four pillars (product architecture)

Jelleo's product positioning is four interlocking pillars. Each pillar is a distinct product capability that composes with the others to form the autonomous immune-system loop:

| Pillar | What it does | Existing primitives |
|---|---|---|
| **P1 — Counterfactual mainnet detection** | Per-tx parallel simulation against forked state — flags transactions where counterfactual state diverges from actual, in real time, before the attack chain completes. | `shadow` (Layer 6), `jelleo-sse.service` (live event stream to dashboards via SSE) |
| **P2 — Cross-protocol bug-class propagation** | When a bug is disclosed anywhere in the ecosystem, auto-extracts the structural pattern and searches every indexed protocol for the same class within minutes. Auto-fires on `confirmed` lifecycle. | `propagate {init-corpus,add-target,search,auto-fire,chain,dispatch-pending,status}`, `learn-from-disclosures`, `derive-siblings` (AST-signature siblings + tree-sitter scan) |
| **P3 — Closed-loop fix bundle** | When a bug is confirmed, generates the fix, formally proves (via Kani) it preserves all other invariants, validates the test suite, bundles bug + fix + proof + tests into one signed archive. **Engine never auto-opens PRs** — operator authorises each one with a typed phrase binding the full 64-char patch SHA. | `bundle {draft,verify,authorize,review,override,record-pr-event,publish-archive,list,status,init-repo,open-pr}`, `confirm`, `synth-kani`, Ed25519 signing |
| **P4 — On-chain attestation registry** | Every audit cycle publishes a cryptographically-signed Merkle root attesting which invariants were verified at which commit SHA. Composable on-chain primitive other protocols can require as a precondition. | `merkle {compute,verify,list,rebuild-all}` (schema v3 with `details_digest`), Ed25519 signing with domain separation, hourly signed `heartbeat.json` |

All four pillars are 100% at the Y0 (pre-funded) tier as of 2026-05-11. Y1+ deltas (sub-slot replay for P1, auto-PR for P3, on-chain Anchor program for P4) remain explicitly scoped to the funded plan. P1 detects in real time. P2 propagates defenses across protocols. P3 closes the loop from disclosure to verified fix. P4 makes every cycle cryptographically composable. Together they replace the static-PDF audit-report model with adaptive, autonomous, on-chain-composable security infrastructure.

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
| **Live SSE feed** | `jelleo-sse.service` on the VPS tails the active cycle's `hunt.log.jsonl` and broadcasts each engine event (recon verdict, debate verdict, PoC fire, bundle drafted, merkle root, cycle complete) as a Server-Sent Event to the customer dashboard within ~1 second of the event occurring. |
| **Email alerts (per-finding + cadence)** | Per-finding emails fire automatically on every `confirmed` Critical/High finding via `notify critical` (routing per `notifier.json` `critical_oncall` + `critical_team` channels). Plus 24h / weekly / monthly cadence digests filtered by `active_targets` so the inbox doesn't flood with per-scope rollups. |
| **Auto-publish** | `audit-pipeline hunt --auto-publish` (default on): after the cycle completes, generate the signed HTML+PDF, push the bundle to `examples/recent-hunts/<id>/` on GitHub, copy artefacts to `/var/www/jelleo.com/cycles/<id>/` for public verification, and fire the per-finding emails. One `hunt` invocation → live customer-visible cycle in under a minute. |
| **GitHub Issue auto-filing** | Confirmed findings above a configurable severity floor are auto-drafted (or auto-filed) against the target repository. Lifecycle transitions to `disclosed`. |
| **HTML dashboard** | Self-contained HTML dashboard with KPIs, severity breakdown, target cards, recent findings, refreshing daemon status. SSE-driven live state, 60s polling as a silent safety net. |
| **Per-cycle + weekly HTML reports** | Branded executive reports with severity rubric, finding details, audit trail. Signed (Ed25519) at every layer. |
| **Target onboarding** | Single command — `audit-pipeline onboard <github-url>` — clones, pins, scaffolds, and registers a new target. Class libraries shipped: `perp_dex` (43), `amm_cp` (58), `clmm` (102), `lending` (94), `lst` (68). Plus 449 Percolator-specific hyps across 9 files (baseline 12, deep-protocol 101, F7 strict-helper 12, bounty regression 18, new-diff 138, ported-class 50, cross-instruction 32, verifiable-conservation 37, unchanged-reaudit 49) — **814 total invariants across 14 YAML files**. Loader picks libraries via `--protocol-class <name>`. |
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
| **Hypothesis library** | **814 distinct invariants across 14 YAML files**. Class libraries: `perp_dex_class.yaml` (43), `amm_cp_class.yaml` (58), `clmm_class.yaml` (102), `lending_class.yaml` (94), `lst_class.yaml` (68) — **365 cross-protocol**. Percolator-specific (**449 total**): 12 baseline (`percolator.yaml`), 101 deep-protocol (`percolator_deep.yaml`), 12 F7-sibling (`percolator_strict_helper_class.yaml`), 18 bounty regression (`percolator_bounty_regression.yaml`), 138 new-diff (`percolator_new_diff.yaml`), 50 ported-class (`percolator_ported_class.yaml`), 32 cross-instruction (`percolator_cross_instruction.yaml`), 37 verifiable-conservation (`percolator_verifiable_conservation.yaml`), 49 unchanged-reaudit (`percolator_unchanged_reaudit.yaml`). For Percolator's `perp_dex` class hunt: 449 Percolator-specific + 22 applicable from `perp_dex_class.yaml` = **471 hyps dispatched per full cycle**. Auto-derivation (`derive-siblings`) compounds the catalog on every confirmed finding. |
| **Empirical confirmation samples** | Three Rust integration tests autonomously generated by `confirm` and passing under `cargo test` against the real engine. Public in [examples/confirmed-tests/](examples/confirmed-tests/). |

---

## Platform · live state

What's deployed right now (this section is updated whenever the platform ships). A tech walking in cold should be able to read this, hit the URLs, and confirm reality in under a minute.

### Public surfaces (jelleo.com — Netlify)

| URL | What's there |
|---|---|
| [`/`](https://jelleo.com) | Marketing home (live ops feed, 4-pillar overview, F7 disclosure callout) |
| [`/protocols/`](https://jelleo.com/protocols/) | Coverage state — 1 active (Percolator), 26 Y1 candidates |
| [`/protocols/percolator/`](https://jelleo.com/protocols/percolator/) | Per-protocol page — program ID, cadence, F7 history, scope |
| [`/methodology.html`](https://jelleo.com/methodology.html) | Public technical reference — pillars, hypothesis schema, lifecycle, severity rubric, F7 worked example |
| [`/security.html`](https://jelleo.com/security.html) | Disclosure policy + Ed25519 attestation model |
| [`/case-studies/f7-percolator/`](https://jelleo.com/case-studies/f7-percolator/) | F7 long-form: dispatch path, root cause, balance proof, sizing, fixes, timeline |
| [`/customer/`](https://jelleo.com/customer/) | Token-gated entry to customer dashboards. Universal demo token: `demo` |
| [`/customer/demo/`](https://jelleo.com/customer/demo/) | Demo customer dashboard (Percolator team view). Real data via manifest fetch. |
| [`/customer/demo/full.html`](https://jelleo.com/customer/demo/full.html) | Rich live dashboard (formerly `/dashboard.html`, now token-gated) |
| [`/status/`](https://jelleo.com/status/) | Service health grid · counter row · driven by snapshot.json |
| [`/integrate/`](https://jelleo.com/integrate/) | Integration request — tier picker + structured form → composes a `mailto:` to kirill@jelleo.com |
| `/dashboard.html` | Now a meta-refresh redirect to `/customer/` (kept for old bookmarks) |

### Data feeds (api.jelleo.com — VPS, 193.24.234.91)

CORS-locked to `https://jelleo.com`. Proper Cache-Control: no-store on the JSON feeds; 5-minute cache on cycle artifacts.

| Endpoint | Audience | What's inside |
|---|---|---|
| [`/snapshot.json`](https://api.jelleo.com/snapshot.json) | Public homepage feed | Aggregated stats, target rollups, recent cycles, **disclosed** findings only (with title + hyp_id + disclosure URL — disclosed = public). Receipt fingerprints on each cycle. Service health. Receipts-signed count. |
| `/customer/<token>/manifest.json` | Per-customer dashboard | Same JSON shape, scoped to the customer's owned target(s), **includes confirmed in-progress findings** (private to the customer behind the token gate). Today: only `demo` exists. |
| `/events/<customer_id>` | Live event stream | Server-Sent Events — each engine event (recon verdict, debate verdict, PoC fire, finding persisted, bundle drafted, merkle root, cycle complete) streams to the dashboard's `EventSource` within ~1 second. CORS-locked to `https://jelleo.com`. 15-second heartbeat to keep proxies alive. |
| `/cycles/<id>/hunt_report.html` | Public cycle reports | Per-cycle signed HTML report (filtered `--public`: only disclosed/fixed/verified/rejected findings) |
| `/cycles/<id>/hunt_report.pdf` | Public cycle reports | Per-cycle signed PDF report (rendered via google-chrome `--headless`) |
| `/cycles/<id>/hunt_report.html.sig` / `.pdf.sig` | Verification | Ed25519 signatures, base64. Domain-separated under `jelleo-sign/v2` (cycle / heartbeat / report / etc.) |
| `/cycles/<id>/hunt_summary.json` | Verification + machine ingest | Raw cycle summary (hyp count, verdict counts, cost, engine SHA, wrapper SHA) |
| `/cycles/<id>/merkle.json` + `.sig` | Verification | Per-cycle Merkle root over all findings; schema v3 binds `details_digest` so post-attestation narrative tampering is detectable |
| `/heartbeat.json` + `.sig` | Liveness | Hourly signed proof-of-running (engine SHA, hostname, cycle counts, service status, key fingerprint) |
| [`/keys/jelleo.ed25519.pub`](https://api.jelleo.com/keys/jelleo.ed25519.pub) | Verification | Platform public key. Verify any signed receipt against this. |

### VPS systemd services (193.24.234.91)

All units in `deploy/jelleo-*.service`/`*.timer`, installed via `deploy/install_systemd.sh`. Each restarts on failure and survives reboots.

| Unit | Cadence | Purpose |
|---|---|---|
| `jelleo-shadow.service` | continuous (60s polling) | Layer-6 mainnet shadow — Percolator program + insurance-fund account state-delta detection |
| `jelleo-watch.service` | continuous | Layer-0.5 commit watch — on every upstream commit, fires a cheap recon-only triage via `deploy/watch_on_update.sh` |
| `jelleo-sse.service` | continuous | P5 live event stream — tails the active cycle's `hunt.log.jsonl` and broadcasts SSE to dashboard subscribers (~50 LOC stdlib Python, listens on 127.0.0.1:8765, nginx proxies `api.jelleo.com/events/<id>`) |
| `jelleo-scheduler-24h.timer` | 24h, 09:01 UTC | Daily cadence digest — filtered to `active_targets` in `notifier.json` (1 PDF/email per cadence, not 9) |
| `jelleo-scheduler-weekly.timer` | 7 days, Mon 09:15 UTC | Weekly cadence digest (same filter) |
| `jelleo-scheduler-monthly.timer` | 30 days | Monthly cadence digest (same filter) |
| `jelleo-snapshot.timer` | 60s | Builds `/var/www/jelleo.com/snapshot.json` AND every customer's `/customer/<id>/manifest.json` |
| `jelleo-heartbeat.timer` | hourly, *:12 UTC | Writes signed `/var/www/jelleo.com/heartbeat.json` proving the loop is alive even in quiet weeks |
| `jelleo-backup.timer` | 24h, 04:30 UTC | SQLite findings DB `.backup` + rotation |
| `jelleo-health.timer` | 5 min | Self-test on the loop itself |

### Source of truth — code paths

| Concept | Where |
|---|---|
| Hunt orchestrator (P1–P4 + auto-publish) | [`src/audit_pipeline/commands/hunt.py`](src/audit_pipeline/commands/hunt.py) — `hunt_cmd()` → `_hunt_run()`; auto-publish block at end calls `report cycle` then `deploy/publish_cycle.sh` |
| Live SSE service (P5) | [`deploy/jelleo-sse.py`](deploy/jelleo-sse.py) — stdlib `asyncio` + raw HTTP/SSE framing, tails active cycle's `hunt.log.jsonl`, broadcasts to subscribers; systemd unit at [`deploy/jelleo-sse.service`](deploy/jelleo-sse.service) |
| Public snapshot builder | [`src/audit_pipeline/commands/dashboard.py`](src/audit_pipeline/commands/dashboard.py) — `_build_snapshot()` |
| Per-customer manifest builder | same file — `_build_customer_manifest()`, plus `_customers_to_publish()` for the customer list |
| Receipt fingerprint reader | same file — `_read_receipt_fingerprint()` (reads `cycle.html.sig`, returns first 8 bytes as colon-hex) |
| Service health prober | same file — `_probe_services()` (calls `systemctl is-active` per known unit) |
| Cycle publish hook | [`deploy/publish_cycle.sh`](deploy/publish_cycle.sh) — copies signed HTML + PDF + sig to docroot (prefers `google-chrome` over snap-confined `chromium-browser`), fires email-on-confirmed |
| Notifier (per-finding + cadence) | [`src/audit_pipeline/notifier.py`](src/audit_pipeline/notifier.py) — `NotifierSettings.active_targets` allow-list filters the scheduler; SMTP config from env via `SmtpConfig.from_env()` |
| Cadence scheduler | [`src/audit_pipeline/commands/scheduler.py`](src/audit_pipeline/commands/scheduler.py) — `scheduler_tick()` honours `active_targets`; idempotent within a cadence window |
| Hypothesis libraries | `src/audit_pipeline/templates/hypotheses/*.yaml` — 14 files, 814 hyps (5 cross-protocol classes + 9 Percolator scopes) |
| Findings DB schema | [`src/audit_pipeline/db.py`](src/audit_pipeline/db.py) |
| Lifecycle state machine | [`src/audit_pipeline/lifecycle.py`](src/audit_pipeline/lifecycle.py) — `new → triaged → confirmed → disclosed → fixed → verified` (+ `rejected`) |
| Bundle 5-gate auth | [`src/audit_pipeline/bundle/auth.py`](src/audit_pipeline/bundle/auth.py) — `expected_phrase()` binds the full 64-char patch SHA; `REQUIRED_GATES` enforces patch_well_formed + poc_fails_pre_patch + poc_passes_post_patch + tests_pass_post_patch; `hmac.compare_digest` on phrase match |
| Merkle attestation | [`src/audit_pipeline/merkle.py`](src/audit_pipeline/merkle.py) — schema v3, `FINDING_FIELDS` includes `details_digest` |
| Per-protocol page generator | [`website/deploy/protocols/_generate.py`](website/deploy/protocols/_generate.py) |

### Verify in 30 seconds

```bash
# Public feed reachable, JSON well-formed, has services + cycles + findings:
curl -s https://api.jelleo.com/snapshot.json | jq '{cycles_total, receipts_signed, services: (.services|length), public_findings: (.public_findings|length), generated_at}'

# Customer manifest reachable + scoped to demo:
curl -s https://api.jelleo.com/customer/demo/manifest.json | jq '{customer, stats}'

# Live event stream open (will print sse_connected + cycle_active then heartbeats):
curl -sN -H 'Origin: https://jelleo.com' --max-time 5 https://api.jelleo.com/events/demo

# Most recent cycle's signed Merkle root + PDF report:
curl -s https://api.jelleo.com/heartbeat.json | jq '{ts, engine_sha, cycles_total}'
curl -sI https://api.jelleo.com/cycles/$(curl -s https://api.jelleo.com/snapshot.json | jq -r '.recent_cycles[0].id')/hunt_report.pdf | head -3

# Platform pubkey served:
curl -sf https://api.jelleo.com/keys/jelleo.ed25519.pub > /dev/null && echo OK

# Live website status page (it polls snapshot.json every 60s + opens an SSE):
open https://jelleo.com/status/

# VPS systemd state (requires SSH access):
ssh root@193.24.234.91 'systemctl is-active jelleo-snapshot.timer jelleo-watch.service jelleo-shadow.service jelleo-sse.service jelleo-heartbeat.timer'
```

### Health-status page (one-stop)

For a single human-readable view: [jelleo.com/status/](https://jelleo.com/status/) — auto-refreshes every 60 seconds from `snapshot.json`. Shows service grid (8 units, individual `up`/`degraded`/`down`/`unknown` state), cycle count, signed-receipt count, loop uptime, and a banner that goes red when `api.jelleo.com` is unreachable or the snapshot is stale (>30 min).

---

## Architecture (one-liner)

`watch --on-update` triggers `hunt`, which orchestrates `recon --auto` → `debate --auto` → `poc_llm` → `cargo test` → `synth-kani --auto` → `litesvm` → `propagate auto-fire` → `bundle draft + verify` → `merkle compute --sign` → write to `findings.db` → `--auto-publish` generates the signed HTML+PDF and copies the cycle to `/var/www/jelleo.com/cycles/<id>/` + emails per-finding critical alerts. `jelleo-sse.service` streams every event from `hunt.log.jsonl` to the customer dashboard in real time.

All modules are standalone CLI commands and remain individually invokable for offline analysis, custom workflows, or human-in-the-loop investigation.

```
─── Core pipeline (Layers 0–6) ─────────────────────
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
audit-pipeline synth-kani     # Layer 2.5/3: NL → Kani harness with compile-fix-retry (--auto)
audit-pipeline shadow start   # Layer 6: 24/7 mainnet shadow audit
audit-pipeline shadow tail    # view recent alerts

─── P2 cross-protocol propagation ────────────────
audit-pipeline propagate init-corpus   # bootstrap a sibling-protocol corpus
audit-pipeline propagate add-target    # register a new corpus repo (URL allow-list)
audit-pipeline propagate search        # walk corpus for a finding's bug-class signature
audit-pipeline propagate auto-fire     # fired by lifecycle on `confirmed`
audit-pipeline propagate chain         # render the chain visualisation CLI
audit-pipeline propagate dispatch-pending  # drain the queued propagate work
audit-pipeline propagate status        # corpus / queue state

─── P3 closed-loop fix bundle (NO auto-PR) ──────
audit-pipeline bundle init-repo        # scaffold per-finding bundle dir
audit-pipeline bundle draft            # LLM authors the patch + scaffolds
audit-pipeline bundle verify           # run the 5-gate verification chain
audit-pipeline bundle review           # render gate report for operator review
audit-pipeline bundle authorize        # operator types the 64-char patch-SHA-bound phrase
audit-pipeline bundle override         # operator override w/ documented reason
audit-pipeline bundle list / status    # all bundles + per-bundle state
audit-pipeline bundle record-pr-event  # record PR open / close / merge events
audit-pipeline bundle publish-archive  # publish operator-private files stripped
audit-pipeline bundle open-pr          # ONLY after `authorize` + tuple match

─── P4 per-cycle Merkle attestation ──────────────
audit-pipeline merkle compute <cycle>  # compute + sign the cycle's root (schema v3)
audit-pipeline merkle verify <cycle>   # round-trip verify against signature
audit-pipeline merkle list             # all cycles + their roots
audit-pipeline merkle rebuild-all      # rebuild every v1/v2 sidecar to v3
audit-pipeline heartbeat               # signed proof-of-running JSON (hourly via timer)

─── Live source-code tracking ────────────────────
audit-pipeline freshness      # one-shot: how stale is your workspace vs upstream?
audit-pipeline watch          # continuous: pull new commits + auto-rerun audit

─── Autonomous hunt loop (production entrypoint) ─
audit-pipeline hunt           # recon → debate → PoC → Kani → LiteSVM → P2/P3/P4 → auto-publish
                              # --auto-publish (default on): generates HTML+PDF, pushes to
                              #   examples/recent-hunts/, copies to /var/www/jelleo.com/cycles/<id>/,
                              #   fires per-finding email alerts for confirmed Critical/High.
                              # --resume-cycle <id>: pick up after Ctrl-C / OOM without re-paying.
                              # --debate-scope all_high (default): debate every HIGH-confidence verdict.
                              # --poc-mode llm (default): LLM authors the Layer-2 PoC scaffold.

─── Operations / commercial layer ────────────────
audit-pipeline onboard <url>  # one-shot: clone repo + scaffold + register target
audit-pipeline dashboard      # self-contained HTML status dashboard
audit-pipeline scheduler tick --cadence {24h,weekly,monthly}  # fire a digest cycle (honours notifier.json active_targets)
audit-pipeline scheduler status / run                         # show state / run as long-lived daemon (or use systemd timers)
audit-pipeline report cycle --cycle-id <id> [--pdf] [--public/--full]  # per-cycle signed HTML (+ optional PDF)
audit-pipeline report weekly  # rolling 7-day HTML summary
audit-pipeline notify critical --finding-id <id> [--dry-run]  # immediate Critical/High alert
audit-pipeline notify cadence --cadence <c> --target <t> [--dry-run]  # cadence digest send
audit-pipeline notify test --to <email>                       # SMTP round-trip test
audit-pipeline issue draft    # render Markdown issue body for a finding
audit-pipeline issue file     # file via `gh issue create`
audit-pipeline issue auto-file-confirmed  # batch-file confirmed (severity floor)
audit-pipeline health         # daemon health check (systemd-timer integration)
audit-pipeline narrative generate / bulk   # LLM-generated finding writeups
audit-pipeline sign keygen / sign / verify  # Ed25519-signed disclosure packages (domain-separated, jelleo-sign/v2)

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
audit-pipeline derive-siblings <id>    # LLM-emit N structural siblings of a confirmed
                              # finding into <workspace>/derived/<id>-siblings.yaml
                              # (also auto-fires on `confirmed` lifecycle transition)

─── Triage + cache + diff-aware ───────────────────
audit-pipeline triage --port 8080  # local single-page UI to confirm/reject `new` findings
audit-pipeline cache list          # PoC test cache (SHA256-keyed across cycles)
audit-pipeline cache stats         # hit-rate + saved cargo-test minutes
audit-pipeline cache flush         # selective or full flush
audit-pipeline hunt --protocol-class clmm --diff-since-sha <sha>
                              # load only the named class library, then filter to
                              # hyps whose target_file is in <sha>..HEAD diff

─── Multi-tenant + proof-of-running (Tier 5) ─────
audit-pipeline customer add <id> --name <…> --protocol <…> --tier <…>
                              # register a customer, create per-customer dir,
                              # derive per-customer Ed25519 keypair from platform key
audit-pipeline customer list / show <id> / pubkey <id>   # introspection
audit-pipeline customer rotate-key <id>   # re-derive under fresh salt
audit-pipeline customer remove <id> [--purge]   # registry pop (--purge wipes dir)
audit-pipeline sign sign <file> --customer <id>   # sign with derived per-customer key
audit-pipeline heartbeat       # public signed proof-of-running JSON
                              # (hourly via deploy/jelleo-heartbeat.{service,timer})
```

---

## Tests + CI

```bash
pip install -e ".[dev]"     # install dev deps (pytest, ruff, mypy)
pytest tests/ -q            # run the suite — 373 tests covering lifecycle hooks, cache,
                            #   scoping, derive-siblings, bundle 5-gate auth, merkle v3
                            #   schema, notifier active_targets filter, hyp validation
ruff check src/ tests/      # lint with stylistic rules pragmatically ignored
```

GitHub Actions runs lint → library validation → pytest on Python 3.10 / 3.11 / 3.12 for every push and PR. Class-library YAMLs are parsed + validated as part of CI so a malformed hypothesis fails the build, not the next hunt cycle.

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

This installs the full Jelleo unit set:

- **Continuous services:** `jelleo-shadow.service` (Layer 6 mainnet shadow), `jelleo-watch.service` (commit-watch + triage hunt), `jelleo-sse.service` (live event stream on 127.0.0.1:8765, nginx proxies via `api.jelleo.com/events/<id>`).
- **Timers:** `jelleo-snapshot.timer` (60s), `jelleo-health.timer` (5 min), `jelleo-heartbeat.timer` (hourly *:12 UTC), `jelleo-backup.timer` (daily 04:30 UTC), `jelleo-scheduler-{24h,weekly,monthly}.timer` (cadence digests, filtered by `notifier.json` `active_targets`).
- **Pre-existing host config:** logrotate at [`deploy/logrotate-jelleo`](deploy/logrotate-jelleo), nginx config at [`deploy/nginx-api.jelleo.com.conf`](deploy/nginx-api.jelleo.com.conf) (TLS via certbot, `/events/` location proxies SSE).

All units restart on failure and survive reboots. Tears down any existing tmux sessions, enables units, verifies status.

---

## License

Apache-2.0 for the runtime implementation. See [`LICENSE`](LICENSE).

The methodology spec under [`docs/methodology/`](docs/methodology/) is offered under [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) so it can be cited and adapted with attribution by academic / formal-verification / STRIDE assessors.
