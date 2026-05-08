# §01 · The four pillars

The methodology composes four pillars into one adaptive loop. Each pillar is a distinct product capability — when run together, they replace the static-PDF audit-report model with continuous, on-chain-composable security infrastructure.

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                     One adaptive feedback loop                   │
   │                                                                  │
   │   ┌────────────┐   ┌──────────────┐   ┌────────────┐   ┌──────┐  │
   │   │ P1 detect  │──▶│ P2 propagate │──▶│ P3 fix-bdl │──▶│ P4   │  │
   │   │ (Layer 6)  │   │ (Layer 1.6)  │   │ (Layer 3)  │   │attest│  │
   │   └────────────┘   └──────────────┘   └────────────┘   └──────┘  │
   │          ▲                                                  │     │
   │          └──────────────────────────────────────────────────┘     │
   │                  every cycle compounds the catalog                │
   └──────────────────────────────────────────────────────────────────┘
```

## P1 — Counterfactual mainnet detection

**What:** For every transaction hitting a covered protocol, run a parallel simulation against attack-pattern-instrumented forks of the program state. When counterfactual state diverges from actual state, flag the transaction in real time — before the attack chain finalizes.

**Stack:** RPC subscription + LiteSVM forked-state + per-protocol attack-pattern library + divergence detector.

**Funded-state target:** 23-second median from violation event to flagged disclosure.

**Bug classes it catches:** operational, state-mutation, oracle-driven, cross-program-invocation paths that point-in-time audits structurally cannot detect.

## P2 — Cross-protocol bug-class propagation

**What:** When a finding confirms anywhere in the indexed corpus, the engine extracts the structural pattern and searches every applicable protocol for the same class within minutes.

**Stack:** Bug-class signature extraction + corpus-wide regex/AST search + Layer-1 hypothesis dispatch on candidate matches.

**Why it matters:** F7's "shrink counter, don't debit vault" pattern probably exists in any protocol with insurance accounting. CatchupAccrue's "advance clock without touching accounts" pattern probably exists in any protocol with multi-instruction settlement. The corpus is how we find them.

**Funded-state target:** 5-minute corpus sweep across the indexed cluster.

## P3 — Closed-loop fix bundle

**What:** When a finding confirms, the engine generates the fix, formally proves (via Kani) it preserves all other invariants, validates the test suite, and bundles the bug + fix + proof + tests into one upstream PR.

**Stack:** Patch synthesis (LLM-driven) + Kani harness synthesis (NL-to-Kani with compile-fix-retry) + LiteSVM regression test + signed disclosure package.

**Bug → Fix delta:** F7 disclosure was filed as PR #39 with the patch (insurance-buffer-also-debits-vault) plus a LiteSVM regression test (commit `43cdcd8`). Maintainer-receivable, proof-carrying, single-PR.

## P4 — On-chain attestation registry

**What:** Every audit cycle publishes a cryptographically-signed Merkle root attesting which invariants were verified at which commit SHA. Composable on-chain primitive other protocols can require as a precondition.

**Stack:** Per-cycle receipt signed Ed25519 + Merkle root over per-finding receipts + (funded-state) on-chain registry transaction.

**Why on-chain:** Insurers and partner protocols can require — programmatically — that a counterparty's last attestation is fresh, signed by the right key, and covers the expected invariant set. The audit becomes a composable precondition, not a paywalled PDF.

**Today:** Off-chain Ed25519 signatures published at [`api.jelleo.com/cycles/<id>/cycle.html.sig`](https://api.jelleo.com/cycles/). Public key at [`api.jelleo.com/keys/jelleo.ed25519.pub`](https://api.jelleo.com/keys/jelleo.ed25519.pub).

**Funded-state:** Cycle receipts as Anchor program accounts, indexable by protocol address.

---

## How the pillars compose

The pillars are not independent products — they form one closed feedback loop:

1. **P1 detects** a candidate divergence on mainnet.
2. **The hypothesis library** (see §03) generates testable invariant claims around the divergence; agents dispatch them.
3. Confirmed findings move through **the lifecycle state machine** (§06) to status `confirmed`.
4. **P2 propagates** the confirmed bug class across the corpus, generating sibling hypotheses + auto-firing them.
5. **P3 ships the fix** as a bundled, proof-carrying PR.
6. **P4 attests** the cycle: the engine SHA, the invariant set tested, the verdicts. Off-chain today; on-chain in the funded build.

Each cycle's output feeds the next cycle's input — the hypothesis library compounds, the corpus deepens, the attestation chain extends.

---

**Implementation:** [`Copenhagen0x/audit-pipeline-cli`](https://github.com/Copenhagen0x/audit-pipeline-cli). The CLI commands `recon` (Layer 1), `propagate` (Layer 1.6), `confirm` + `synth-kani` (Layer 2/3), `shadow` (Layer 6), and `sign` (P4) implement the layers under each pillar.
