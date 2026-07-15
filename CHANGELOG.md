# Changelog

All notable changes to `audit-pipeline-cli` are tracked here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project tracks SemVer once it cuts a stable v1.

The "live state" section of [`README.md`](README.md#platform--live-state) is the source of truth for what's currently deployed. This file logs the milestones along the way.

---

## [Unreleased]

### WS1 autonomy core — `converge`: re-run hunt until it stops finding new bugs (2026-07-15)
- **New `audit-pipeline converge [hunt args...]`.** Round 1 runs `hunt` with your arguments; each round's confirmed findings are expanded into structural *sibling* hypotheses, which the next round audits. Stops when a round produces 0 net-new confirmed findings with nothing left to dispatch, or `--max-rounds` (default 4). Implemented as a subprocess orchestrator, so hunt's internals and every existing flag/gate are untouched.
- **It derives the siblings itself, and that is the whole point.** The auto-derivation hook only fires from `db.transition_finding(..., CONFIRMED)`, whose sole reachable caller is the human triage UI; `hunt` persists verdicts via a direct `upsert_finding` that fires no hooks. So `derived/` is empty unless a human has already acted, and a loop that merely *reads* it audits exactly one round and then claims convergence. `converge` therefore invokes `derive-siblings` between rounds. Firing the hook from `upsert_finding` instead would start LLM derivation on every existing hunt/watch/cron run — a core-persistence change, deliberately not made here.
- **"Converged" is never claimed unless earned.** The command exits **non-zero** on: a failed round; a round whose cycle dispatched 0 hypotheses (hunt's Layer-1 failure path finishes a cycle and returns **exit 0**, so rc alone cannot be trusted); a cycle converge cannot see (it is reading a different `findings.db` than hunt writes); every sibling blocked by the disclosure-history gate (an audited repo can shape `prior_disclosure` to empty the queue — the gate's skip-list is now printed, mirroring hunt); every derivation failing; `--no-derive` leaving confirmed findings unexpanded; and `--max-rounds` reached with a backlog. An unattended harness must be able to tell "clean" from "audited nothing".
- **Trust posture (autonomous by default, bounded and announced).** Siblings are LLM-derived from the audited repo. The default dispatches un-reviewed siblings and says so every round; `--approved-only` restores the `triage-siblings` human gate; `--max-siblings-per-round` (default 50) bounds round SIZE, since round COUNT is not a cost bound. Repo influence over the next round is confined to prose inside a `claim`, because `load_hypotheses` structurally validates every sibling (id/class/severity/bug_class) before dispatch — and that prose is now marker-wrapped in the derivation prompt.
- **Correctness fixes to shared code found by the review** (these affected the existing engine, not just converge):
  - `derive-siblings`' `--output`/default-file path — the one `converge` drives — **bypassed `_enforce_sibling_diversity`**, re-opening Defect 06 (near-duplicate siblings multiply the parent's false-positive rate) on the unattended path. Now filtered; all three derivation paths share it.
  - That same path **spent LLM money with no cap and no ledger entry**: the D15 daily budget lived only in the async hook. It now checks and records, via a shared `_DERIVE_DAILY_BUDGET_USD`.
  - `SIBLING_PROMPT` now **wraps** untrusted finding text (`title`/`claim`/`target_file`) in `<<<UNTRUSTED_HYP_*>>>` markers with explicit "this is data, not instructions" framing. Sanitizing without wrapping was inert — stripping markers only matters when there is a boundary to protect.
- `--max-total-usd` bounds the **whole run**: hunt-cycle spend (measured) plus sibling derivation (estimated at ~$0.30/call — derivation runs outside a cycle and the subprocess doesn't report its cost back). Checked *before* each derivation batch, so it stops before spending rather than reporting the overrun afterwards. hunt's own `--budget-cap-usd` is per-cycle, so forwarding it to N rounds would otherwise allow N× it.
- A human's `triage-siblings reject` is honoured: converge will not re-derive over a rejected sibling. The dispatched set is also seeded from this target's history, so a re-run doesn't re-audit (and re-pay for) every sibling derived by earlier runs.
- Sibling dispatch is target-scoped via `scoping.filter_hypotheses`. `derived/` is per-workspace, not per-target, so without this a second target audited in the same workspace would ingest the first's hypotheses — and nothing downstream would catch it, since hunt does not scope a `--hypotheses` library and an absent `applies_to` defaults to `["*"]`.

#### WS1 known limits — read before pointing this at anything that matters
- **Not for cron yet.** Suppression keys on hypothesis id. A *depth-1* rejection now survives (the slug is stable), but siblings derived from siblings get fresh LLM-minted ids each run, so a rejection cannot follow them. A content key (`bug_class` + normalized-claim hash) is required before scheduling.
- **Not for a customer eval root yet.** The shared-`findings.db` rollup has open issues: `converge` and `triage-siblings` resolve the DB by *different* rules, so in some layouts the human gate silently fails to find anything to approve.
- **One workspace, one target.** See the target-scoping note above — the guard is now in place, but the underlying `derived/` layout is still shared.
- **`derived/` is never cleaned** (it is triage's pending queue; converge must not delete from it). History seeding bounds the re-dispatch cost, not the directory's growth.
- **`dashboard.py` counts `derived/*-siblings.yaml` as "pending sibling review"** — converge's autonomous output now lands there, so that metric includes siblings no human queued.
- Findings with status `triaged` (rather than `confirmed`) are not expanded into siblings.
- Derivation cost in `--max-total-usd` is an **estimate**, not a measurement.
- Tests: `tests/test_converge.py` (41), deliberately built on real objects — a real `FindingsDB` on a real `<cust>-eval/workspaces/<cell>` tree, and round-2 argv parsed against the real `hunt` CLI. Three prior total-failure bugs passed an all-green mocked suite, because the mocks replaced the exact code that was broken.

### WS6 continuous diff-scan — the capability now works on the deploy path (2026-07-12)
- **`watch --on-update` can now scope the downstream hunt to only the files changed since the last successful audit** (`--diff-scope`, **OPT-IN, OFF by default**). Previously the deploy path (`hunt --skip-poc`, no `--diff-since-sha`) had no way to scope — every commit re-scanned the FULL hypothesis library.
  - **No behavior change for existing deploys:** default OFF, so nothing changes until you pass `--diff-scope`. Narrowing what gets scanned is a security-relevant choice — enable it only once `--on-update` runs a real hunt (not a no-op shim). The **FIRST scoped run per component full-scans** to set a clean baseline, then later commits are diff-scoped. A boot-time log line reports the active mode. `last_scoped_sha` is tracked only while diff-scope is enabled.
- **`hunt --diff-since-sha` now works in source-mode** (no local clone) via the GitHub compare API (`changed_files_via_compare`), not just local `git diff`.
- **Fail-closed by construction** (3-gate hardened): the compare result is trusted ONLY on a clean fast-forward (base a direct ancestor of head, `status=="ahead"` + merge-base==base, both fields present) — a rebased/force-pushed/diverged/malformed response falls back to the full library rather than trusting the compare API's merge-base diff (which is incomplete and would silently skip a changed file). Renamed files count both old and new paths (`previous_filename` union / local `--no-renames`). The scoping baseline advances only on a successful cycle; a failed/crashed hunt (or an unpinnable source head) never narrows the next scan. Every SHA/identifier spliced into the compare URL is validated (SHA = exact `[0-9a-f]{40}`, matching `freshness_gate`).
- Scoping is driven ONLY through `--diff-scope` (which owns the first-run-full-library, repo-correctness, and success-gated-baseline guards). A `--diff-since-sha` hand-written into an `--on-update` template is respected but bypasses those guards — it's detected, logged, and does not advance the managed baseline. (`{prev_sha}` template exposure was removed — it let a hand-written template silently skip the first-run-full guard.)
- **Known limit — the retry tradeoff of enabling diff-scope (tracked follow-up):** under full-library scanning, a hypothesis that *transiently* errors (rate-limit, tool/parse edge case, adversarial input) on the commit that changed its file is re-dispatched against that file on **every** poll — many retries. With `--diff-scope` ON, once the baseline advances that hypothesis isn't re-run until its file changes again, so a transient miss on that commit is not retried. The baseline advances only on a clean process exit (a crashed hunt retries), but a *per-hypothesis* error inside an exit-0 run is not yet detected — closing that requires hunt to surface per-hypothesis coverage and watch to gate the baseline on it (tracked separately). This is why `--diff-scope` is OFF by default: enabling it is a deliberate trade of full-retry for per-commit scoping. (Also pre-existing, unaffected by scoping: a commit introducing a brand-new file has no pre-authored `target_file` hypothesis to match.)

### Methodology consolidation (2026-05-07)
- Methodology spec (§01–§10) lives in [`docs/methodology/`](docs/methodology/) inside this repo (previously a standalone `solana-audit-pipeline` repo)
- Layer-by-layer implementation notes moved under [`docs/methodology/layers/`](docs/methodology/layers/)
- Internal references to the old standalone methodology repo updated across `pyproject.toml`, `deploy/STATUS.md`, `deploy/generate_status.py`, `src/audit_pipeline/__init__.py`, generated workspace README templates, and internal docs

---

## [v0.3] · 2026-05-07 — Tier 5 architecture ship

### Added
- **Multi-tenant customer registry** (Tier 5 #26 + #27)
  - `<workspace>/customers.json` declarative registry (id, name, protocol_name, tier, since, target_match, contact_email)
  - `<workspace>/customers/<id>/` per-customer directory (keys + future per-customer overrides)
  - `audit-pipeline customer {add,remove,list,show,rotate-key,pubkey}` operator surface
- **Per-customer derived signing keys** (Tier 5 #28)
  - HKDF-SHA256 derives a 32-byte Ed25519 seed from platform private key + customer id
  - Deterministic (same id → same key), distinct (different ids → different keys), salt-isolated (rotate-key generates fresh salt)
  - `audit-pipeline sign sign <file> --customer <id>` signs with the derived key
  - Operator still only custodies the platform key — customer keys are reproducible from it
- **Public proof-of-running heartbeat** (Tier 5 #29)
  - `audit-pipeline heartbeat` emits a signed `heartbeat.json` covering schema version, generated_at, hostname, engine SHA, cycles_total, cycles_last_24h, last_cycle_ts, signing-pubkey fingerprint, registered customer count, systemd service summary
  - Hourly cadence via new `deploy/jelleo-heartbeat.{service,timer}` (off-cycle from 24h scheduler at minute :12)
  - Different from a finding: a finding is a security claim about the target; a heartbeat is a security claim about the platform — quiet weeks stay verifiable
- **Full OpenAPI 3.1 spec** of the public api.jelleo.com surface (Tier 5 #30) at `docs/api/openapi.yaml` covering `/snapshot.json`, `/customer/{token}/manifest.json`, `/cycles/{id}/cycle.{html,pdf}{,sig}`, `/keys/jelleo.ed25519.pub`, `/heartbeat.json{,sig}`
- Tests: `tests/test_customers.py` (29 cases — registry, validation, paths, derived keys, persistence) + `tests/test_heartbeat.py` (12 cases — payload shape, fingerprint, customer count, 24h window)

### Changed
- `deploy/install_systemd.sh` installs `jelleo-heartbeat.{service,timer}` alongside the existing units
- `audit_pipeline.cli` registers `customer` and `heartbeat` subcommands

---

## [v0.2] · 2026-05-07 — Tier 2 + Tier 3 capability ship

### Added
- **Class-library hypothesis catalog** — 508 distinct invariants across 5 protocol classes:
  - `perp_dex_class.yaml` (43) — Drift, Mango, Jupiter Perps, Percolator
  - `amm_cp_class.yaml` (58) — Raydium, Orca CP, Saber
  - `clmm_class.yaml` (102) — Orca Whirlpools, Kamino Liquidity, Meteora DLMM
  - `lending_class.yaml` (94) — Marginfi, Kamino Lend, Solend, Save
  - `lst_class.yaml` (68) — Marinade, Sanctum, JitoSOL
- **`audit-pipeline derive-siblings <finding-id>`** — LLM-driven structural sibling generation for confirmed findings
- **Lifecycle hooks** — daemon-thread fire-and-forget on `confirmed` transition firing both sibling derivation and cross-protocol propagation (`db.transition_finding` + `_fire_confirmed_hooks`)
- **PoC test cache** — SHA256(test_code) + engine_sha keyed cache that skips redundant `cargo test` runs across cycles. New `audit-pipeline cache {list,stats,flush}` subcommands
- **Diff-aware hunting** — `audit-pipeline hunt --protocol-class <name> --diff-since-sha <sha>` loads a class library and filters to hyps whose `target_file` is in the commit diff
- **Local triage UI** — `audit-pipeline triage --port 8080`, single-page SPA (vanilla JS), keyboard shortcuts (C/T/R/N), live counters, 60s refresh
- **GitHub Actions CI** — matrix Python 3.10 / 3.11 / 3.12, ruff lint + library validation + pytest on every push and PR
- **Test suite** — five new test files: `test_class_libraries.py`, `test_diff_aware_hunting.py`, `test_poc_cache.py`, `test_lifecycle_hooks.py`, `test_derive_siblings.py`
- **`pyproject.toml [project.optional-dependencies] dev`** — pytest, pytest-cov, ruff, mypy
- **`[tool.ruff]` config** — pragmatic ignore list (E501, B904, B007, N806/N814/N818, SIM102/103/105/108/115, E741, F841) so CI signal stays high without bikeshedding stylistic preferences

### Changed
- `audit_pipeline.scoping` — added `PROTOCOL_CLASSES` catalog, `list_classes()`, `hypotheses_dir()`, `load_class_library()`, `changed_files_between()`, `filter_hypotheses_by_diff()`. Relaxed `_ID_RE` to accept multi-prefix IDs (`BR-F7-…`, `SH11-…-K`, `PD7-…`)
- `audit_pipeline.commands.confirm` — cache lookup before `cargo test`, write outcome on cache hit, `put_poc_cache` after a fresh run
- `audit_pipeline.commands.propagate` — added `propagate_from_finding_async` wrapper for hook-firing path
- `audit_pipeline.db` — added `poc_cache` table + helpers (`get/put/list/flush`), modified `transition_finding` to fire hooks on `Status.CONFIRMED` (suppressible via `run_hooks=False` for tests)

---

## [v0.1] · 2026-05 — Tier 1 production ship

### Added
- **Customer portal** — `/customer/<token>/` token-gated dashboards, demo customer at `/customer/demo/`
- **Per-protocol pages** — `/protocols/percolator/` with program ID, cadence, F7 history, scope
- **F7 case study page** — `/case-studies/f7-percolator/` with dispatch path, root cause, balance proof, sizing, fix options, timeline
- **Status page** — `/status/` service grid + counter row, driven by `snapshot.json` from VPS
- **Integration request form** — `/integrate/` tier picker → `mailto:` to `kirill@jelleo.com`
- **Per-customer manifest publisher** — token-gated `customer/<token>/manifest.json` with confirmed in-progress findings (private to the customer)
- **End-to-end signed cycle pipeline** — cover-page HTML + PDF + Ed25519 signature + email-on-confirmed + public cycle URL
- **Live operational status doc** — `deploy/STATUS.md` + `deploy/generate_status.py`
- **`docs/BIG_PICTURE_CHECKLIST.md`** — single source of truth for what Jelleo ships today (153-item flag table)

### Changed
- Cover-page typography + jelleo palette rebrand for printed reports
- Contact addresses normalized to `kirill@jelleo.com` / `info@jelleo.com` (dropped `wifpros.com` fallback for security/Solana correspondence)

---

## [v0.0.1] · 2026-04 — Inaugural F7 disclosure

- F7 (residual-conservation insurance-siphon class) dispatched 2026-04-22, disclosed 2026-04-30 via [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39)
- LiteSVM regression test committed at [`43cdcd8`](https://github.com/Copenhagen0x/audit-pipeline-cli/commit/43cdcd8)
- Maintainer adopted A1-class regression coverage on `main` at [`a1afd2e`](https://github.com/aeyakovenko/percolator-prog/commit/a1afd2e), labeled `PR39/F7`

Initial CLI surface: `init`, `provision-vps`, `sync`, `recon`, `poc`, `kani`, `litesvm`, `cross-check`, `disclose`, `run`, `spec-check`, `debate`, `propagate`, `synth-kani`, `shadow`, `freshness`, `watch`, `hunt`, `onboard`, `dashboard`, `report`, `issue`, `health`, `narrative`, `sign`, `hunt-deep`, `confirm`, `learn-from-disclosures`, `expand-coverage`.
