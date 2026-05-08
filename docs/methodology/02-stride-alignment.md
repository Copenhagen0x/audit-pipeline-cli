# §02 · STRIDE alignment

[STRIDE](https://solana.com/news/stride) is the Solana Foundation's tiered, foundation-funded security program covering eight pillars across the ecosystem. It launched in the wake of the Drift ~$285M exploit (April 2026) with a depth-tier mandate that goes well beyond traditional point-in-time audits.

This methodology is **complementary to STRIDE** — STRIDE evaluates programs holistically; this methodology produces the depth artifacts protocols need to demonstrate the smart-contract integrity pillar at higher tiers.

---

## STRIDE's eight pillars

```
1. Smart-contract integrity   ← this methodology produces the depth artifacts
2. Adversarial detection      ← this methodology produces shadow-audit alerts
3. Coordinated disclosure     ← this methodology produces signed disclosure packages
4. Cross-protocol monitoring  ← this methodology produces propagation reports
5. Operational security
6. Incident response
7. Bug-bounty hardening
8. Foundation-grant verification
```

The first four map directly onto outputs of this methodology's four pillars. The last four are out of scope here — they're either operational (5, 6) or organizational (7, 8) and remain the protocol's responsibility.

---

## Tier eligibility

STRIDE has two TVL-driven tiers:

| Tier   | TVL threshold | Foundation funds |
|--------|---------------|------------------|
| Tier 1 | $10M+         | Ongoing opsec + active threat monitoring |
| Tier 2 | $100M+        | All Tier 1 + funded formal verification |

Tier-2 protocols qualifying for the formal-verification mandate may direct that funding toward this methodology's implementation as the artifact-producing layer. **No formal partnership today** — this is a stated Y1 OKR. Until then, customers fund directly.

---

## What this methodology produces for STRIDE assessors

| STRIDE pillar          | Methodology contribution                                              | Artifact format                                  |
|------------------------|-----------------------------------------------------------------------|--------------------------------------------------|
| Smart-contract integrity | Continuous hypothesis-driven verification with Kani proofs           | Per-cycle signed Merkle root + per-finding signed disclosure package |
| Adversarial detection  | Counterfactual mainnet sim flags state-divergent txs in real time (P1)| Shadow-audit alerts (Slack / email / webhook)    |
| Coordinated disclosure | Closed-loop fix bundle (P3) ships maintainer-ready PRs                | GitHub PR with patch + Kani proof + cargo test  |
| Cross-protocol monitoring | Bug-class propagation across the indexed corpus (P2)               | Cross-protocol propagation report                |

Each artifact is **cryptographically signed Ed25519** so STRIDE assessors can verify it independently without trusting the methodology operator. Public key at [`api.jelleo.com/keys/jelleo.ed25519.pub`](https://api.jelleo.com/keys/jelleo.ed25519.pub).

---

## Year-1 OKR (stated commitment, not yet a partnership)

Open an artifact-recognition channel with STRIDE assessors so methodology-signed attestations are accepted directly as evidence for the smart-contract integrity pillar — eliminating the duplicate-effort path where a Tier-2 protocol pays separately for an audit and for a methodology subscription.

Until that recognition channel exists, methodology output is provided as a **third-party artifact** that protocols can submit alongside their own STRIDE evidence package.

---

**Live reference:** [jelleo.com/methodology.html#stride](https://jelleo.com/methodology.html#stride)
