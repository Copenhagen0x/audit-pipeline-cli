# Changelog

All notable changes to `audit-pipeline-cli` are tracked here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project tracks SemVer once it cuts a stable v1.

The "live state" section of [`README.md`](README.md#platform--live-state) is the source of truth for what's currently deployed. This file logs the milestones along the way.

---

## [Unreleased]

### Methodology consolidation (2026-05-07)
- Methodology spec (§01–§10) lives in [`docs/methodology/`](docs/methodology/) inside this repo (previously a standalone `solana-audit-pipeline` repo)
- Layer-by-layer implementation notes moved under [`docs/methodology/layers/`](docs/methodology/layers/)
- Internal references to the old standalone methodology repo updated across `pyproject.toml`, `deploy/STATUS.md`, `deploy/generate_status.py`, `src/audit_pipeline/__init__.py`, generated workspace README templates, and internal docs

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
