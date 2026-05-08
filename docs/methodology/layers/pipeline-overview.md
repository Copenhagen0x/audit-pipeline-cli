# Pipeline overview — the 5 layers

This document is the conceptual entry point. For deep dives into each layer, see the per-layer documents listed at the bottom.

## The principle

The pipeline is opinionated about which uncertainties matter for production-grade security disclosure. Each of the 5 layers reduces a different uncertainty:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 1 — Multi-agent code review                                            │
│   Reduces: "did I miss a hypothesis just by reading the code alone?"         │
│   Output:  prioritized list of candidate findings, each with evidence trail  │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 2 — Empirical PoC (engine native)                                       │
│   Reduces: "is this candidate actually a bug, or did I misread the code?"    │
│   Output:  passing test for each REAL finding (or test that proves the       │
│            candidate isn't actually a bug)                                    │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 3 — Kani formal verification                                           │
│   Reduces: "does the bug exist for ALL inputs in the symbolic state space,   │
│             or just the one I happened to write a test for?"                 │
│   Output:  formal counterexample (CEX) proving the bug, OR formal SUCCESS    │
│            proving the property holds universally                            │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 4 — LiteSVM BPF-level reachability                                      │
│   Reduces: "even if Kani says the engine math fails, can the public BPF      │
│             API actually drive state to that witness in production?"         │
│   Output:  exploit chain test (live attack works) OR bound analysis          │
│            (witness state is unreachable at production caps in any           │
│             realistic time horizon → finding downgrades to code defect)      │
└──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ Layer 5 — Cross-platform reproduction                                        │
│   Reduces: "is this finding a platform-specific artifact (Windows-vs-Linux,  │
│             host-vs-VPS, toolchain version) or a real protocol issue?"       │
│   Output:  bit-identical pass/fail across at least 2 distinct host envs      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Skipping a layer is fine if you accept the corresponding uncertainty. The pipeline is opinionated about which uncertainties matter for **production-grade disclosure to a maintainer of a deployed protocol**.

## When NOT to use the full pipeline

| Scenario | Recommended layers |
|---|---|
| Quick spot-check on a small change | Layers 1 + 2 |
| Hypothesis you want to refute (negative result) | Layers 1 + 3 (Kani SAFE proof) |
| Bug bounty submission for a deployed protocol | All 5 layers |
| Paid audit deliverable | All 5 layers + a written disclosure following `docs/disclosure-template.md` |
| Internal pre-release sanity check | Layers 1 + 2 + 4 |

## The honest cost

For a single non-trivial finding to go through the full pipeline:

| Layer | Time per finding | Compute | Skill needed |
|---|---|---|---|
| 1 — Multi-agent review | 1-2 hours (parallel) | Agent orchestration budget | Hypothesis design |
| 2 — Empirical PoC | 30-90 min | Local Rust toolchain | Writing focused tests |
| 3 — Kani harness | 1-3 hours | 6-core VPS recommended; harnesses can run minutes to hours each | Symbolic execution mental model |
| 4 — LiteSVM PoC + bound | 2-4 hours | Local or VPS; LiteSVM is fast | BPF instruction-level orchestration |
| 5 — Cross-platform | 30 min (parallel re-run) | 2nd host (VPS) | DevOps |

A first-time audit (provisioning + tooling setup) is 1-2 days of upfront work. A repeat audit on a similar codebase is much faster — most of the cost is in Layer 1's hypothesis design.

## Why each layer matters

### Without Layer 1 (no multi-agent review)
You'll find what one human reviewer's attention happens to land on. Multi-agent fans out the search space across hypotheses you wouldn't have prioritized alone.

### Without Layer 2 (no empirical PoC)
Disclosures read as theoretical. Maintainers reasonably push back: "show me." Layer 2 is the show.

### Without Layer 3 (no Kani)
The bug only exists for the specific test inputs you wrote. Maintainer can rationalize that it's a specific edge case rather than a general flaw.

### Without Layer 4 (no LiteSVM bound)
You disclose Kani CEXes that turn out to require state that's unreachable in production. Maintainer correctly says "this isn't actually a bug, you bypassed the public API guards." You lose credibility.

### Without Layer 5 (no cross-platform)
At least one finding will be a Windows-only or x86-only or toolchain-version artifact. Embarrassing on the call.

## Per-layer documents

- [Layer 1 — Multi-agent code review](./layer1-multi-agent-review.md)
- [Layer 2 — Empirical PoC](./layer2-empirical-poc.md)
- [Layer 3 — Kani formal verification](./layer3-kani-formal-verification.md)
- [Layer 4 — LiteSVM BPF-level reachability](./layer4-litesvm-bound-analysis.md)
- [Layer 5 — Cross-platform reproduction](./layer5-cross-platform-reproduction.md)
- [Lessons learned](./lessons-learned.md) — operational gotchas from running the pipeline on Percolator
- [Reusability checklist](./reusability-checklist.md) — what you need to know about your target before starting
