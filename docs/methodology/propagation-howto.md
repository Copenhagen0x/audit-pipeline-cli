# How cross-protocol propagation works

> Customer-facing explainer for Pillar 2 (P2). When you read on Jelleo's
> dashboard that "propagation found 3 candidates in Drift," this document
> explains exactly how that happened.

## The one-line version

When a finding confirms in any covered protocol, Jelleo's engine
automatically searches every other indexed Solana protocol for the same
**structural bug pattern** — within minutes of the original confirmation.

## The mechanic, in five steps

1. **Confirmed finding** — A hypothesis fires its PoC under `cargo test`
   in the original protocol (e.g., F7 in Percolator). Lifecycle moves
   to `confirmed`.

2. **Bug class extracted** — Every hypothesis carries a `bug_class` field
   — a stable, kebab-case identifier that's protocol-agnostic. F7's class
   is `insurance-counter-vault-divergence`. The class names what failure
   mode the bug exemplifies, not which protocol it lives in.

3. **Sibling derivation** — A daemon-thread hook calls Claude with the
   confirmed finding as context, asking for `N` *structural siblings* —
   variants of the same root cause across adjacent code paths. F7's
   confirmation generated 6 siblings: liquidation-vault skew, settle-PnL
   mismatch, withdraw without vault debit, deposit double-credit, and
   two more. These land at `<workspace>/derived/<hyp-id>-siblings.yaml`.

4. **Cross-protocol corpus sweep** — A second hook walks the indexed
   corpus (today: Drift, Mango, Jupiter Perps; growing) with the regex
   signatures registered for the parent's bug class. Files that match
   one or more signatures rank by score. Output: a Markdown report at
   `api.jelleo.com/cycles/.../propagate/...` listing top candidates.

5. **Layer-1 dispatch (operator-gated)** — Top candidates are queued for
   a Layer-1 hunt cycle against the candidate's protocol, scoped to the
   parent's bug class. Auto-fire is OFF by default (cost discipline);
   the operator runs `audit-pipeline propagate dispatch-pending` to
   actually spawn the hunts.

## What you see as a customer

In your dashboard manifest (`api.jelleo.com/customer/<token>/manifest.json`),
`propagation_stats` reports four counters:

- **`bug_classes_seen`** — How many distinct bug classes have touched
  your protocol (lower bound on attack-surface diversity).
- **`findings_with_bug_class`** — How many of your findings carry a
  bug class — should approach 100% as the catalog matures.
- **`sibling_files`** — How many sibling-derivation YAMLs exist for
  findings against your protocol.
- **`propagation_reports`** — How many cross-protocol sweeps have fired
  on your findings.

## What customers DON'T see

- Other customers' findings (token gating prevents cross-customer
  visibility).
- Pre-disclosure findings against your protocol (filtered at the
  publish boundary; only `disclosed/fixed/verified/rejected` make it
  to the public archive).
- Sibling claim text for in-progress findings (siblings live in
  `<workspace>/derived/` on the operator host, not on the public
  surface).

## Why this matters

Static auditing finds bugs in one protocol and ships a PDF. The bugs in
sibling protocols sit waiting for a separate engagement.

Continuous propagation closes the gap. **The cluster benefits whether
or not the sibling protocols are paid customers** — once F7's class is
catalogued, every covered protocol gets free re-checking on the back
of someone else's confirmation. That's the asymmetry: defenders win
zero-sum on bug classes, and the catalog compounds permanently.

## The bug-class catalog

The full list of registered classes (with regex signatures the engine
applies to corpus sweeps) lives at:
[`docs/methodology/bug-class-catalog.md`](bug-class-catalog.md).

19 classes catalogued today; the catalog grows incrementally as new
classes confirm.

---

**Spec reference:** [`§04 — Cross-protocol propagation`](04-propagation.md)
**CLI reference:** `audit-pipeline propagate {init-corpus, search, auto-fire, status, dispatch-pending, add-target}`
