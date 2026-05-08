# Jelleo · Big-Picture Checklist

**Source of truth for "what state is Jelleo in right now."**
Updated: 2026-05-07

This document maps every component of the proposal vision against today's reality. Every line is one of:

- ✅ **Done** — exists today, working, in production
- 🟡 **Partial / WIP** — scaffolding exists, needs filling out
- ❌ **Pre-seed buildable** — can ship without funding, just time/effort
- 🔒 **Funded-build only** — requires Toly's seed money to deliver

---

## A · Strategic foundation

| Item | Status | Notes |
|---|---|---|
| Proposal sent to Anatoly | ✅ | Sent 2026-05-04, awaiting response |
| Methodology page (`jelleo.com/methodology.html`) | ✅ | 11 sections, full chrome match |
| Security policy (`jelleo.com/security.html` + `SECURITY.md`) | ✅ | Coordinated disclosure, 30-day default embargo |
| Hypothesis schema spec (`docs/HYPOTHESIS_SCHEMA.md`) | ✅ | v1 schema with applies_to / scope_conditions / bug_class |
| Big-picture checklist (this file) | ✅ | You are here |
| Public roadmap page (`jelleo.com/roadmap`) | ❌ | Tier 4 #25 — 1-2h to lift Y1 timeline from proposal |
| Whitepaper (PDF) on methodology | ❌ | Tier 4 #22 — 12-16h, citable artifact for STRIDE |

---

## B · The 4 Pillars

### P1 · Counterfactual mainnet detection

| Item | Status | Notes |
|---|---|---|
| `shadow` CLI command | ✅ | Layer 6 |
| Commit-triggered shadow (via `jelleo-watch.service`) | ✅ | Active since 2026-04-28 |
| Live mainnet shadow service (`jelleo-shadow.service`) | 🟡 | Unit file installed, service NOT enabled. Tier 1 #1 — 1h to enable |
| Sub-slot replay | 🔒 | Funded-state P1 delta |
| On-call routing (PagerDuty/Opsgenie integration) | 🔒 | Funded-state P1 delta |

### P2 · Cross-protocol propagation

| Item | Status | Notes |
|---|---|---|
| `propagate` CLI (`init-corpus`, `search`, `auto-fire`) | ✅ | All 3 subcommands |
| 15-protocol corpus (Drift, Mango, Marginfi, Kamino, Phoenix, OpenBook, Orca, Meteora, Raydium, Marinade, Anchor, SPL + 3 more) | ✅ | Default in `propagate.py` |
| 17 bug-class signatures registered (`BUG_CLASS_SIGNATURES`) | ✅ | `propagate.py` |
| Propagation auto-fire on confirmed-transition | 🟡 | Wired in `publish_cycle.sh` for emails — propagation auto-trigger TBD. Tier 2 #9 — 3h |
| Auto-bug-class sibling derivation | ❌ | Tier 2 #8 — 4-6h |
| Multi-protocol indexing service | 🔒 | Funded-state P2 delta |
| Disclosure-feed integrations (Sherlock / Code4rena / GHSA) | 🔒 | Funded-state P2 delta |

### P3 · Closed-loop fix bundle

| Item | Status | Notes |
|---|---|---|
| `confirm` CLI (PoC test execution) | ✅ | |
| `synth-kani` CLI (NL invariant → Kani harness) | ✅ | Layer 2.5 |
| `kani` CLI (formal verification dispatch) | ✅ | Layer 3 |
| `litesvm` CLI (BPF reachability) | ✅ | Layer 4 |
| `cross-check` CLI | ✅ | Layer 5 |
| End-to-end fix bundle PR (auto-author + Kani-verified + opens PR) | 🔒 | Funded-state P3 deliverable, MVP at 90-day Tranche 2 trigger |

### P4 · On-chain attestation registry

| Item | Status | Notes |
|---|---|---|
| Off-chain Ed25519 signing (`sign keygen/sign/verify`) | ✅ | |
| Auto-sign every report | ✅ | `report.py` --sign default true |
| Public key published at `jelleo.com/keys/jelleo.ed25519.pub` | ✅ | Also at api.jelleo.com mirror |
| Per-cycle Merkle root | ❌ | Could build pre-seed (~6h) but only useful with on-chain |
| ~500-LOC Anchor program for on-chain registry | 🔒 | Funded P4 deliverable, devnet day 90, mainnet day 180 |
| Public proof-of-running attestations (hourly heartbeat) | ❌ | Tier 5 #29 — 3h |

---

## C · Commercial-grade roadmap (T0-T4)

### T0 — Autonomous hunt loop

| Item | Status | Notes |
|---|---|---|
| `recon --auto` parallel agent dispatch | ✅ | |
| `ANTHROPIC_API_KEY` on VPS with daily cap | ✅ | $10/day cap configured |
| Synthesis script (parses recon, categorizes verdicts) | ✅ | In `hunt.py` |
| Auto-debate trigger | ✅ | `debate.py` |
| Auto-PoC trigger | ✅ | `confirm.py` |
| Auto-Kani trigger | ✅ | `synth_kani.py` |
| `watch --on-update` integration | ✅ | `jelleo-watch.service` |
| Default hypothesis library | ✅ | 143 hyps across 4 files |
| Cost tracking + budget cap | ✅ | $10/day global, $3/cycle |

### T1 — Commercial polish

| Item | Status | Notes |
|---|---|---|
| Severity rubric (5 tiers, formal definitions) | ✅ | `severity.py` |
| Finding lifecycle state machine (7 states) | ✅ | `lifecycle.py` |
| Slack / Discord webhook alerts | 🟡 | Telegram works; Slack/Discord — Tier 3 #19, 2h each |
| Auto-file GitHub Issue | ✅ | `issue.py` |
| Per-finding disclosure repo | 🟡 | `disclose.py` exists, end-to-end auto pending |
| PDF report generator (24h/weekly/monthly cadence) | ✅ | `report.py --pdf` via Chrome-headless on VPS |
| Per-target config (`targets.yaml`) | ✅ | |

### T2 — Multi-customer infrastructure

| Item | Status | Notes |
|---|---|---|
| Multi-target support | ✅ | 8 targets currently being audited |
| SQLite findings DB | ✅ | 487 KB, 627 findings |
| Customer dashboard (HTML) | ✅ | `dashboard.html` with live snapshot fetch |
| Customer onboarding script (`audit-pipeline onboard <repo>`) | ✅ | `onboard.py` with 4 templates |
| Multi-tenant workspace isolation | ❌ | Tier 5 #26 — 8-10h |
| Status page | ❌ | Tier 1 #5 — 3h, biggest trust signal |
| Postgres migration | ❌ | Tier 3 #18 — 6-8h, funded-state spec |
| Auth'd customer portal | ❌ | Tier 1 #4 — 6-8h |
| Per-customer signing keys | ❌ | Tier 5 #28 — 4h |
| `audit-pipeline customer` subcommand (add/remove/list) | ❌ | Tier 5 #27 — 4-6h |

### T3 — Operational hardening

| Item | Status | Notes |
|---|---|---|
| systemd units (survive reboots) | ✅ | 8 jelleo-* units |
| Health checks (5-min cadence) | 🟡 | `jelleo-health.timer` running, but `jelleo-health.service` failed — Tier 3 #13, 30min fix |
| Rate limiters (Anthropic / Solana RPC / GitHub) | 🟡 | Anthropic budget cap exists, no explicit rate limiters |
| Error recovery + exponential backoff | 🟡 | Some in hunt.py, not comprehensive |
| Structured logs to file + rotation | 🟡 | Logs exist (`/root/audit_runs/percolator-live/{shadow,watch,scheduler}/`); rotation TBD |
| Backup of findings DB | 🟡 | `jelleo-backup.timer` daily 04:30 UTC, ON-VPS only — Tier 3 #17 for off-VPS |
| Off-VPS backups (Hetzner / R2 / GitHub) | ❌ | Tier 3 #17 — 1-2h |

### T4 — Trust + brand

| Item | Status | Notes |
|---|---|---|
| Public methodology doc | ✅ | `methodology.html` |
| Public case studies | 🟡 | F7 mentioned but no deep page — Tier 1 #6, 4-6h |
| Cryptographic finding signing | ✅ | Ed25519 |
| `SECURITY.md` (responsible disclosure) | ✅ | |
| Domain + branded URL | ✅ | jelleo.com (Netlify) + api.jelleo.com (VPS) |
| Public methodology repo (standalone) | ❌ | Tier 3 #20 — 4h |
| Whitepaper PDF | ❌ | Tier 4 #22 — 12-16h |
| Blog | ❌ | Tier 4 #21 — 6-8h for 3 launch posts |
| `/pricing` page | ❌ | Tier 4 #23 — 2h |
| `/faq` page | ❌ | Tier 4 #24 — 3h |

---

## D · Customer infrastructure (cadence + delivery)

| Item | Status | Notes |
|---|---|---|
| 24h cadence email (signed PDF attached) | ✅ | `jelleo-scheduler-24h.timer` daily 09:00 UTC |
| Weekly cadence email | ✅ | Mon 09:15 UTC |
| Monthly cadence email | ✅ | 1st of month 09:30 UTC |
| Immediate email on confirmed Critical/High | ✅ | Wired in `publish_cycle.sh` (today) |
| Public dashboard with live numbers | ✅ | `dashboard.html` + `/snapshot.json` every 60s |
| Public cycle reports at `api.jelleo.com/cycles/<id>/` | ✅ | Wired in `publish_cycle.sh` (today) |
| Auth'd customer portal with full findings | ❌ | Tier 1 #4 |
| Web triage UI | ❌ | Tier 2 #12 — 8h |
| Customer-specific recipient routing | ✅ | `notifier.json` with 5 channels |
| SMTP transport (Gmail) | ✅ | `security@jelleo.com` |
| SPF + DKIM (so emails don't land in spam) | ❌ | Tier 3 #14 — 15min |

---

## E · Engine internals

| Item | Status | Notes |
|---|---|---|
| Python package (`audit_pipeline`) | ✅ | 13,320 LoC, 7 top-level modules, 32 CLI subcommands |
| `cli.py` with `--workspace` global flag | ✅ | |
| Hypothesis library (143 hyps in 4 files) | ✅ | percolator + bounty regression + deep + strict-helper |
| Hypothesis library expansion (143 → 500+) | ❌ | Tier 2 #7 — 8-12h pure writing |
| Hypothesis JSON Schema validator | ✅ | `schemas/hypothesis.schema.json` |
| Scoping module (loader filter) | ✅ | `scoping.py` with 12 known predicates |
| Severity rubric module | ✅ | `severity.py` |
| Lifecycle state machine | ✅ | `lifecycle.py` |
| Findings DB (SQLite) | ✅ | `db.py`, 4 tables, idempotent migrations |
| Branding / design system | ✅ | `branding.py` matches jelleo.com aesthetic |
| Notifier (SMTP) | ✅ | `notifier.py` |
| Diff-aware hunting | ❌ | Tier 2 #11 — 6h, ~5× cheaper per cycle |
| PoC test cache | ❌ | Tier 2 #10 — 4h, $2-3 saved per cycle |
| Test suite (pytest) | ❌ | Tier 3 #16 — 8-12h (currently zero tests) |
| `audit-pipeline customer` subcommand | ❌ | Tier 5 #27 — 4-6h |
| OpenAPI spec for `api.jelleo.com` | ❌ | Tier 5 #30 — 4-5h |

---

## F · Public surfaces

### `jelleo.com` (Netlify)

| Item | Status | Notes |
|---|---|---|
| `index.html` (homepage with 4 pillars + dashboard preview + ledger + F7) | ✅ | 235 KB |
| `methodology.html` | ✅ | 51 KB |
| `security.html` | ✅ | 20 KB |
| `dashboard.html` (live snapshot fetch) | ✅ | 50 KB |
| `keys/jelleo.ed25519.pub` | ✅ | 113 B |
| `og.png` for social previews | ✅ | 369 KB |
| `/protocols/` page | ❌ | Tier 1 #2 — 3-4h |
| `/protocols/<name>/` per-protocol pages | ❌ | Tier 1 #3 — 4-6h |
| `/case-studies/f7-percolator/` | ❌ | Tier 1 #6 — 4-6h |
| `/blog/` + 3 launch posts | ❌ | Tier 4 #21 — 6-8h |
| `/pricing` page | ❌ | Tier 4 #23 — 2h |
| `/faq` page | ❌ | Tier 4 #24 — 3h |
| `/roadmap` page | ❌ | Tier 4 #25 — 1-2h |

### `api.jelleo.com` (VPS nginx)

| Item | Status | Notes |
|---|---|---|
| TLS via Let's Encrypt (auto-renew) | ✅ | Expires 2026-08-05 |
| HTTP→HTTPS 301 redirect | ✅ | |
| `/snapshot.json` (privacy-tightened DB dump) | ✅ | 32 KB, refreshes every 60s |
| `/keys/jelleo.ed25519.pub` (mirror) | ✅ | |
| `/cycles/<id>/` (signed HTML + PDF + sig) | ✅ | Wired today via `publish_cycle.sh` |
| CORS locked to `https://jelleo.com` | ✅ | |
| Directory listing off | ✅ | autoindex off |
| All other paths → 403 | ✅ | |
| OpenAPI / API docs page | ❌ | Tier 5 #30 |

### `status.jelleo.com` (status page)

| Item | Status | Notes |
|---|---|---|
| Status page subdomain | ❌ | Tier 1 #5 — 3h |
| Live systemd / timer health | ❌ | |
| Uptime monitoring | ❌ | |

### `Copenhagen0x/audit-pipeline-cli` (GitHub)

| Item | Status | Notes |
|---|---|---|
| Repo public + Apache-2.0 | ✅ | |
| README + SECURITY.md | ✅ | |
| `docs/HYPOTHESIS_SCHEMA.md` | ✅ | |
| Auto-publish recent cycles to `examples/recent-hunts/` | ✅ | 4 cycles published, more incoming |
| GitHub Actions CI | ❌ | Tier 3 #15 — 3-4h |
| GitHub releases / tags | ❌ | |
| CONTRIBUTING.md | ❌ | |
| CODEOWNERS | ❌ | |
| Public methodology spec (`docs/methodology/` inside this repo) | ✅ | 11-section §01–§10 spec + `layers/` subfolder (Tier 3 #20, consolidated 2026-05-07 — single repo over standalone) |

---

## G · Operations + infrastructure

### VPS (`193.24.234.91`)

| Item | Status | Notes |
|---|---|---|
| Ubuntu 22.04 + 23 GB RAM + 146 GB disk | ✅ | |
| `/root/audit_runs/percolator-live/` workspace | ✅ | 540 MB |
| 8 systemd units (`jelleo-*`) installed | ✅ | |
| ufw firewall enabled (22/80/443 open) | ✅ | |
| Snap chromium + Google Chrome stable for PDF | ✅ | google-chrome-stable installed today |
| nginx 1.18 serving api.jelleo.com | ✅ | |
| Certbot auto-renew (snap) | ✅ | |

### Daemons

| Daemon | Status | Notes |
|---|---|---|
| `jelleo-watch.service` (continuous) | ✅ active running |
| `jelleo-snapshot.timer` (every 60s) | ✅ |
| `jelleo-backup.timer` (daily 04:30 UTC) | ✅ |
| `jelleo-health.timer` | ✅ |
| `jelleo-health.service` | ⚠️ **failed** — Tier 3 #13, 30min |
| `jelleo-scheduler-24h.timer` (daily 09:00 UTC) | ✅ |
| `jelleo-scheduler-weekly.timer` (Mon 09:15 UTC) | ✅ |
| `jelleo-scheduler-monthly.timer` (1st 09:30 UTC) | ✅ |
| `jelleo-shadow.service` | 🟡 **not enabled** — Tier 1 #1, 1h |

### Backups

| Item | Status | Notes |
|---|---|---|
| Daily on-VPS findings DB backup (rotates 30) | ✅ | `jelleo-backup.timer` |
| Off-VPS backup (Hetzner / R2 / GitHub) | ❌ | Tier 3 #17 — 1-2h |
| Encrypted backup of `keys/jelleo.ed25519` (private) | ❌ | Critical — lose this and old signatures unverifiable |
| Encrypted backup of `notifier.json` + `/root/.audit-env` | ❌ | |

---

## H · Integrations

| Service | Status | Notes |
|---|---|---|
| Anthropic API (Claude Sonnet 4.6) | ✅ | $10/day cap |
| Gmail SMTP (`security@jelleo.com`) | ✅ | STARTTLS:587 with app password |
| Telegram bot (per-cycle alerts) | ✅ | `HUNT_TELEGRAM_TOKEN` + chat ID |
| Slack webhook | ❌ | Tier 3 #19 — 2h |
| Discord webhook | ❌ | Tier 3 #19 — 2h |
| GitHub repo (`Copenhagen0x/audit-pipeline-cli`) | ✅ | |
| Netlify (jelleo.com hosting) | ✅ | |
| GoDaddy DNS | ✅ | |
| Let's Encrypt | ✅ | |
| Google Workspace (3 jelleo.com users) | ✅ | security@, kirill@, info@ |
| PagerDuty / Opsgenie | 🔒 | Funded-build for on-call SLA |
| Linear / Jira (for triage) | 🔒 | Optional Y1 |
| Solana RPC provider (paid tier for high volume) | 🔒 | Funded P1 delta |

---

## I · Documentation

| Item | Status | Notes |
|---|---|---|
| `README.md` | ✅ | |
| `SECURITY.md` (disclosure policy) | ✅ | |
| `docs/HYPOTHESIS_SCHEMA.md` | ✅ | |
| `docs/BIG_PICTURE_CHECKLIST.md` (this file) | ✅ | |
| Public methodology page | ✅ | `methodology.html` |
| `deploy/README.md` (runbook) | ✅ | |
| Whitepaper (PDF) | ❌ | Tier 4 #22 |
| Blog posts | ❌ | Tier 4 #21 |
| Per-protocol case studies | 🟡 | F7 mentioned, deep page TBD |
| `CONTRIBUTING.md` | ❌ | |
| `CODE_OF_CONDUCT.md` | ❌ | |
| OpenAPI / API documentation | ❌ | Tier 5 #30 |

---

## J · Trust + compliance

| Item | Status | Notes |
|---|---|---|
| Cryptographic signing of every report (Ed25519) | ✅ | Auto-sign default true |
| Public key published at jelleo.com + api.jelleo.com | ✅ | |
| Coordinated disclosure policy (30-day default embargo) | ✅ | `SECURITY.md` + `security.html` |
| STRIDE alignment articulated | ✅ | `methodology.html` §02 |
| Apache-2.0 license | ✅ | All repos |
| Privacy filter on snapshot.json (no titles, no hyp IDs, only public-status findings) | ✅ | |
| Customer notification on cross-protocol re-test | 🟡 | Email transport works, propagation auto-fire on confirmed pending — Tier 2 #9 |
| GPG key for security@ reports (per SECURITY.md) | ❌ | 30 min |
| `Terms of Service` page | ❌ | Pre-customer-contract |
| `Privacy Policy` page | ❌ | Pre-customer-contract |
| STRIDE assessor recognition | 🔒 | Year-1 OKR |

---

## K · Sales + outreach

| Item | Status | Notes |
|---|---|---|
| Funding proposal sent (Toly via Anatoly's office) | ✅ | 2026-05-04, awaiting reply |
| OtterSec demo call | ✅ | 2026-05-07 — they want to test next week |
| Pricing publicly visible | ❌ | Tier 4 #23 — `/pricing` page |
| FAQ publicly available | ❌ | Tier 4 #24 — `/faq` page |
| Roadmap publicly visible | ❌ | Tier 4 #25 |
| Blog with launch posts | ❌ | Tier 4 #21 |
| Public testimonials / logos | 🔒 | Need actual customers first |
| Press / launch announcement | 🔒 | Post-funding announcement |

---

## L · Funded-build deliverables (post-seed)

These are explicitly deferred until Toly's money lands. Listed here so we don't accidentally build them now:

- 🔒 Anchor program for on-chain attestation registry (P4)
- 🔒 Live mainnet ingestion + sub-slot replay infrastructure (P1)
- 🔒 Multi-protocol indexing service (P2)
- 🔒 End-to-end automated fix-bundle PR with Kani proof (P3 MVP at 90-day Tranche 2)
- 🔒 Ceiling-tier compute budget ($50K/mo Anthropic spend)
- 🔒 First engineering hire
- 🔒 Mailgun migration
- 🔒 Solana RPC paid-tier provider
- 🔒 Office / corporate setup
- 🔒 Legal review of customer contracts
- 🔒 STRIDE assessor channel
- 🔒 27-protocol cluster operating cost ($680K/mo per proposal)

---

## Summary counts

- **✅ Done**: 95 items
- **🟡 Partial / WIP**: 12 items
- **❌ Pre-seed buildable**: 30 items (the buildable list)
- **🔒 Funded-build only**: 16 items

**Cost to ship all 30 pre-seed buildables: ~150 hours focused work · $0 API spend.**

After those 30 ship → the platform Toly is funding *already exists* — money goes to **scale**, not **build**.

---

*Last update: 2026-05-07 21:42 UTC. Maintained alongside `memory/jelleo_buildable_pre_seed_2026_05_07.md`.*
