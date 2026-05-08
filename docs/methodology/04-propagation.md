# §04 · Cross-protocol propagation

When a finding confirms anywhere in the cluster, its **bug class** generalizes — there are typically N sibling attack patterns sharing the same structural shape across (a) other call paths in the same protocol and (b) other protocols of the same class. Propagation auto-fires those siblings without a human in the loop.

This is what makes the catalog compound. F7 didn't just produce a Percolator disclosure — it produced 12 sibling hypotheses, 4 of which are now scoped against Drift, Mango, MarginFi.

---

## The bug-class abstraction

Every hypothesis carries a `bug_class` field — a stable, kebab-case identifier that's protocol-agnostic. Examples:

```
insurance-counter-vault-divergence       (F7's class)
oracle-staleness-bypass
liquidation-discount-stacking
swap-rounding-direction
fee-growth-accumulator-regression
position-nft-authority-leak
```

Two hypotheses share a bug class iff they target the same root-cause pattern. The class is what propagation pivots on — when finding A in protocol X confirms with bug_class B, every other protocol that has any hyp with bug_class B (or its prefix) gets re-tested.

---

## Auto-fire on confirmed-transition

The lifecycle hook (§06) fires two parallel actions on `confirmed`:

1. **Sibling derivation** ([`derive-siblings`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/commands/derive_siblings.py)): the LLM reads the confirmed finding, extracts the structural pattern, and emits N additional hypotheses targeting the same class in adjacent code paths. Output → `<workspace>/derived/<hyp-id>-siblings.yaml`.
2. **Cross-protocol propagation** ([`propagate-auto-fire`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/commands/propagate.py)): the engine walks the indexed corpus (today: 15 popular Solana programs) with bug-class signatures, emits a Markdown report ranking files by signature match score. Top hits get auto-dispatched as Layer-1 hypotheses on the next cycle.

Both run as fire-and-forget background threads so the lifecycle transition itself is never blocked.

---

## The corpus

The propagation corpus is a curated set of Solana programs cloned at pinned commits. Default 15:

```
percolator                  drift-protocol-v2
percolator-prog             mango-v4
anchor                      marginfi-v2
solana-program-library      kamino-lending
phoenix-v1                  openbook-v2
orca-whirlpools             meteora-dlmm
raydium-amm                 jupiter-swap-api-client
marinade-finance-onchain-sdk
```

Custom corpus lists are supported via `--list-file <json>`. All clones are shallow (`--depth 1`) to save disk + bandwidth.

---

## Signature library

`bug_class` → list of regex / AST-grep signatures. Maintained per-class as the catalog grows.

Example excerpt for `insurance-counter-vault-divergence` (F7's class):

```
insurance_fund\.balance\s*[-+]?=
use_insurance_buffer
sub_assign\(
\.insurance_counter\b
```

Files that match ≥ N distinct signatures (default `N=1`) surface in the propagation report. Higher score = stronger candidate.

The signature library is intentionally **regex-first** (not AST). The trade-off: a few false positives, much faster corpus sweep. The Layer-1 hypothesis dispatch on top hits filters the false positives.

---

## What propagation produces

A Markdown report at `<workspace>/recon/propagate/auto-fire/propagation_finding_<id>_<bug_class>.md`:

```
# Propagation report — F7 (insurance-counter-vault-divergence)

Source finding: F7 (Percolator)
Signatures used: 4
Files scanned: 1,247 across 15 repos

## Top candidates

  1. mango-v4 / programs/mango-v4/src/state/perp_market.rs:412 (score 4)
  2. drift-protocol-v2 / programs/drift/src/state/perp_market.rs:189 (score 3)
  3. marginfi-v2 / programs/marginfi/src/state/marginfi_account.rs:778 (score 2)
  ...
```

The `audit-pipeline` operator reviews the top candidates, enrolls them as new targets, and schedules a fresh hunt cycle against each — using class-library hyps with bug_class matching the propagation source.

---

## Why this matters

Three properties this gives protocols and assessors:

1. **Defenders win zero-sum on bug classes.** Once F7-class is confirmed and patched in Percolator, the same class is auto-checked across the cluster within minutes. Protocols that haven't audited recently get free coverage on the back of someone else's disclosure.
2. **The library compounds without manual work.** Every confirmed finding generates derived hyps + propagation hits. The catalog is a function of (cycles run × confirmations × class breadth) — not (engineer hours).
3. **STRIDE assessors get a cross-protocol monitoring artifact.** Pillar 4 of STRIDE (cross-protocol monitoring) is satisfied by the propagation reports without additional work.

---

**Live reference:** [jelleo.com/methodology.html#propagation](https://jelleo.com/methodology.html#propagation)
**Implementation:** [`audit_pipeline.commands.propagate`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/commands/propagate.py)
