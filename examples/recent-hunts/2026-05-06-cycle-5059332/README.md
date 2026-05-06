# Hunt cycle `20260506-194649-5059332`

A live capture of one autonomous hunt cycle from the Jelleo pipeline.

- **Target:** [`aeyakovenko/percolator`](https://github.com/aeyakovenko/percolator) — the Percolator engine
- **Engine SHA:** `5059332` (commit *"Record full Kani audit pass"*, the upstream HEAD at cycle start)
- **Started:** 2026-05-06T19:46:49+00:00
- **Elapsed:** 179.1s
- **Cost:** $0.7736
- **Hypotheses dispatched:** 12 (the strict-helper-class library)

## What this is

Jelleo's autonomous hunt loop polls upstream protocol repos for new commits.
On every detected commit, it:

1. Downloads a fresh source snapshot from GitHub at that exact SHA — no
   persistent local clone, nothing to drift or get edited locally.
2. Dispatches one Claude agent per hypothesis in parallel. Each agent has
   `read_file`, `grep`, and `find_function` tools backed by the snapshot.
3. Each agent renders a verdict with concrete file:line citations.
4. Findings are scored, debated (when contested), and routed onward to
   PoC scaffolding / Kani / disclosure as needed.

This particular cycle ran the *strict-helper-class* library — 12
hypotheses checking for invariant violations in Percolator's strict
helper functions (the bug class adjacent to F7).

## What you're looking at

- **`hunt_summary.json`** — full structured summary: schema, cycle id,
  resolved engine SHA, all per-hypothesis verdicts with token counts.
- **`hunt_report.md`** — human-readable Markdown summary.
- **`hunt.log.jsonl`** — event log (start, end, daily-cap state).
- **`recon/`** — one prompt + one response per hypothesis (so 12 of each
  here), plus `recon_summary.json` aggregating them.

## Verdict highlights

| Hypothesis | Verdict | Confidence |
|---|---|---|
| `SH6-resolve-flat-negative-gate` | **TRUE** | HIGH |
| `SH1-strict-helper-coverage` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `SH5-keeper-crank-touching-completeness` | NEEDS_LAYER_2_TO_DECIDE | HIGH |
| `SH9-stuck-target-accrual-rejection` | NEEDS_LAYER_2_TO_DECIDE | MED |
| ...8 others, see `hunt_summary.json` | | |

`NEEDS_LAYER_2_TO_DECIDE` is what triggers PoC scaffolding in a full cycle
— this run was scoped to the recon layer only (`--skip-poc --skip-debate
--skip-narrative`) for cost efficiency.

## How to read agent paths in the responses

The response files reference paths like:

```
/tmp/jelleo-snap-aeyakovenko-percolator-5dybf3pg/aeyakovenko-percolator-5059332/src/percolator.rs
```

That's the agent showing its work. The first segment is the **temp dir
the snapshot was extracted into** (cleaned up after the cycle); the
second segment is the inner **`{owner}-{repo}-{sha7}/`** that GitHub
wraps around tarballs. To map a path back to upstream:

```
github.com/aeyakovenko/percolator/blob/5059332/src/percolator.rs#L<n>
```

## Reproducing

The pipeline that produced this artifact:
[github.com/Copenhagen0x/audit-pipeline-cli](https://github.com/Copenhagen0x/audit-pipeline-cli)

To run a similar cycle yourself:

```bash
audit-pipeline --workspace /path/to/your/workspace hunt \
    --source-repo aeyakovenko/percolator \
    --hypotheses /path/to/hypotheses_strict_helper_class.yaml \
    --budget-cap-usd 3 \
    --skip-poc --skip-narrative --skip-debate
```

`--source-repo` is the new ephemeral-snapshot mode introduced in
[commit 67d3007](../../). When it's omitted, the pipeline falls back to
its legacy local-clone path for backward compatibility.
