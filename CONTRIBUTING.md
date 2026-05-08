# Contributing

`audit-pipeline-cli` is the runtime engine for [Jelleo](https://jelleo.com) — a continuous, hypothesis-driven Solana audit platform. Engagement is currently small + direct; PRs and issues are welcome but expect them to be discussed before merge.

## Setup

```bash
git clone https://github.com/Copenhagen0x/audit-pipeline-cli
cd audit-pipeline-cli
pip install -e ".[dev]"
```

Python 3.10+ required. Optional: `ANTHROPIC_API_KEY` for the LLM-backed `--auto` modes (recon, debate, narrative, derive-siblings, etc.). VPS-side tooling (Rust 1.95+, Solana 3.1+, Kani 0.67+) is installed via `audit-pipeline provision-vps` if you're standing up your own deployment.

## Running the suite

```bash
pytest tests/ -v             # full test suite
pytest tests/test_class_libraries.py -v   # one file at a time
ruff check src/ tests/       # lint
```

CI runs the same on every push and PR across Python 3.10 / 3.11 / 3.12. If CI is red, fix the underlying issue rather than skipping hooks or downgrading rules.

## Hypothesis library contributions

To add a hypothesis to an existing class library:

1. Open the appropriate file under `src/audit_pipeline/templates/hypotheses/` (`perp_dex_class.yaml`, `amm_cp_class.yaml`, `clmm_class.yaml`, `lending_class.yaml`, `lst_class.yaml`)
2. Append a new entry following the schema in [`docs/methodology/03-hypothesis-schema.md`](docs/methodology/03-hypothesis-schema.md)
3. Required fields: `id`, `class`, `claim`. Recommended: `applies_to`, `scope_conditions`, `bug_class`, `target_file`, `severity`
4. Run `pytest tests/test_class_libraries.py -v` to confirm the loader parses + validates the new entry

To add a new class library entirely:

1. Drop `<class>_class.yaml` in `src/audit_pipeline/templates/hypotheses/`
2. Add `<class>` to `PROTOCOL_CLASSES` in `src/audit_pipeline/scoping.py`
3. The loader picks it up automatically (`audit-pipeline hunt --protocol-class <class>`)

Aim for invariants that are **falsifiable** — phrased so a clean negative result strengthens the disclosure. Avoid speculative "should be safe" language.

## Code style

`ruff` is configured in `pyproject.toml`. Stylistic-preference rules are pragmatically disabled (B904, N8xx, SIM*, E741, F841) so CI gates on bug-finding rules (F8xx undefined names, F4xx unused imports, etc.) rather than on naming-convention bikeshedding. If you find a rule that catches real bugs being silenced, open an issue.

## Methodology changes

The methodology spec lives in [`docs/methodology/`](docs/methodology/) and is licensed CC-BY-4.0 (separate from the runtime's Apache-2.0). Changes there should preserve citability — version stamps, stable section numbering, deliberate mirror with [`jelleo.com/methodology.html`](https://jelleo.com/methodology.html). Don't break links.

## Disclosing a security issue in this CLI itself

See [`SECURITY.md`](SECURITY.md). Short version: don't open a public issue — email `kirill@jelleo.com` directly.

## What "good" looks like

- Tests for new behavior (append to the relevant `tests/test_*.py`)
- A line in [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]`
- If touching the public CLI surface or hypothesis schema: a corresponding update under [`docs/methodology/`](docs/methodology/)
- Small, single-purpose commits — easier to revert if something downstream breaks

That's it. Open the PR and we'll go from there.
