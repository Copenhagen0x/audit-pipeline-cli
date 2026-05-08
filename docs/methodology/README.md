# Solana Audit Methodology

> **Continuous, hypothesis-driven, on-chain-attested security analysis for Solana DeFi.**
> The methodology that produced [F7](https://github.com/aeyakovenko/percolator-prog/pull/39) — a Critical insurance-residual drain on Anatoly Yakovenko's Percolator perpetual DEX — written down so security teams, foundation evaluators, and protocol maintainers can read exactly how it works.

This subtree is the **canonical, citable reference** for the four-pillar autonomous-audit methodology used by [Jelleo](https://jelleo.com). It lives alongside the implementation in this repo so a single clone gives you the spec and the runtime side-by-side.

**Live reference:** [jelleo.com/methodology.html](https://jelleo.com/methodology.html)
**Implementation:** the rest of this repo — see [`../../README.md`](../../README.md)
**Inaugural disclosure:** [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39) (F7, 2026-04)

---

## What this is

Static-PDF audit reports are stale the day they ship. The protocol moves; the audit doesn't. This methodology replaces the audit-report cadence with **continuous hypothesis-driven analysis** — every commit on the target repo triggers a re-run of the protocol's invariant library, every confirmed finding propagates as siblings across the cluster, and every cycle ships with an Ed25519-signed receipt that any third party can verify.

Four interlocking pillars:

| Pillar | What it does |
|---|---|
| **P1 — Counterfactual mainnet detection** | Per-tx parallel simulation against forked state; flags transactions where counterfactual state diverges from actual, in real time, before the attack chain completes. |
| **P2 — Cross-protocol bug-class propagation** | When a bug confirms anywhere in the ecosystem, auto-extracts the structural pattern and searches every indexed protocol for the same class within minutes. |
| **P3 — Closed-loop fix bundle** | When a bug confirms, generates the fix, formally proves (Kani) it preserves all other invariants, validates the test suite, bundles bug + fix + proof + tests into one PR. |
| **P4 — On-chain attestation registry** | Every audit cycle publishes a cryptographically-signed Merkle root attesting which invariants were verified at which commit SHA. Composable on-chain primitive other protocols can require as a precondition. |

P1 detects in real time. P2 propagates defenses across protocols. P3 closes the loop from disclosure to verified fix. P4 makes every cycle cryptographically composable.

---

## Table of contents — methodology spec (§01–§10)

| § | File | Topic |
|---|---|---|
| 01 | [`01-four-pillars.md`](01-four-pillars.md) | Pillar architecture · how the four compose into one adaptive loop |
| 02 | [`02-stride-alignment.md`](02-stride-alignment.md) | How the methodology fits inside Solana Foundation's STRIDE program |
| 03 | [`03-hypothesis-schema.md`](03-hypothesis-schema.md) | The hypothesis library schema (id, applies_to, scope_conditions, bug_class, …) |
| 04 | [`04-propagation.md`](04-propagation.md) | Cross-protocol propagation — auto-firing siblings of a confirmed finding |
| 05 | [`05-severity-rubric.md`](05-severity-rubric.md) | How findings are assigned Critical / High / Medium / Low / Info |
| 06 | [`06-lifecycle.md`](06-lifecycle.md) | Finding lifecycle — `new → triaged → confirmed → disclosed → fixed → verified` |
| 07 | [`07-attestation.md`](07-attestation.md) | Ed25519 cycle receipts + the on-chain attestation registry |
| 08 | [`08-reporting.md`](08-reporting.md) | 24h / weekly / monthly cadence, immediate notifications, public disclosures |
| 09 | [`09-f7-case-study.md`](09-f7-case-study.md) | F7 worked example — dispatch path, root cause, balance proof, fix, timeline |
| 10 | [`10-engagement-tiers.md`](10-engagement-tiers.md) | Foundation / Production / Ceiling — depths, mixes, expected outputs |

---

## Layer-by-layer implementation notes ([`layers/`](layers/))

The §01–§10 spec above describes the methodology at the pillar level. The [`layers/`](layers/) subfolder collects deeper layer-by-layer implementation write-ups — useful when reading the engine source or extending a specific layer:

| File | Topic |
|---|---|
| [`layers/pipeline-overview.md`](layers/pipeline-overview.md) | High-level traversal of the layered hunt cycle |
| [`layers/layer1-multi-agent-review.md`](layers/layer1-multi-agent-review.md) | Layer 1 — multi-agent recon detail |
| [`layers/layer2-empirical-poc.md`](layers/layer2-empirical-poc.md) | Layer 2 — empirical PoC under `cargo test` |
| [`layers/layer3-kani-formal-verification.md`](layers/layer3-kani-formal-verification.md) | Layer 3 — Kani formal verification |
| [`layers/layer4-litesvm-bound-analysis.md`](layers/layer4-litesvm-bound-analysis.md) | Layer 4 — LiteSVM bound analysis |
| [`layers/layer5-cross-platform-reproduction.md`](layers/layer5-cross-platform-reproduction.md) | Layer 5 — cross-platform reproduction |
| [`layers/disclosure-template.md`](layers/disclosure-template.md) | Standard disclosure-package template |
| [`layers/lessons-learned.md`](layers/lessons-learned.md) | Lessons learned through F7 |
| [`layers/reusability-checklist.md`](layers/reusability-checklist.md) | Checklist for porting the loop to a new protocol class |

---

## How to cite

Academic / formal-verification / STRIDE assessments should cite the repo at a pinned commit — it is the stable, versioned reference:

```
Sakharuk, K. (2026). Solana Audit Methodology — continuous,
hypothesis-driven, on-chain-attested security analysis for Solana DeFi.
GitHub: Copenhagen0x/audit-pipeline-cli, docs/methodology/ (v1).
```

For specific sections, link to a permalink at the relevant commit, e.g.
`https://github.com/Copenhagen0x/audit-pipeline-cli/blob/<commit>/docs/methodology/03-hypothesis-schema.md`.

---

## Status

**v1 · 2026-05** — first public version of the methodology. Mirrors the
content of [jelleo.com/methodology.html](https://jelleo.com/methodology.html)
as of the same date.

This subtree is a *specification*. The runnable code lives in the parent repo.
The deployed loop is at [`api.jelleo.com`](https://api.jelleo.com), with
public cycle receipts at [`api.jelleo.com/cycles/`](https://api.jelleo.com/cycles/)
and the platform Ed25519 public key at [`api.jelleo.com/keys/jelleo.ed25519.pub`](https://api.jelleo.com/keys/jelleo.ed25519.pub).

---

## License

The methodology spec (`docs/methodology/`) is offered under [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) — share and adapt with attribution, including commercially.

The runtime implementation (rest of the repo) is Apache-2.0. See the top-level [`LICENSE`](../../LICENSE).
