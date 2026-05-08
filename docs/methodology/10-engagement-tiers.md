# §10 · Engagement tiers

Three depth tiers, three customer profiles. Pricing is **2.4× raw API cost** (58% gross margin). Customers move between tiers month over month; engagement length is open-ended.

---

## Tiers

| Tier              | Hyps / day | Mix                                                                               | Engagement / mo | Engagement / yr |
|-------------------|------------|-----------------------------------------------------------------------------------|-----------------|-----------------|
| **Foundation**    | ~300       | Sonnet-only · daily runs · monitoring depth                                       | $30K            | $360K           |
| **Production ⭐** | ~500       | Sonnet + tool-using Opus + weekly Kani · default for $1B+ TVL DeFi               | $60K            | $720K           |
| **Ceiling**       | ~1,500+    | Multi-round refinement · attack-chain enumeration · Kani synthesis on every Critical/High | $120K        | $1.44M          |

⭐ = recommended default. Tier movement is open — customers ramp up before launches, ramp down between releases.

---

## Common surface (every tier)

All tiers ship the same contractual surface — what differs is depth, not breadth:

- Signed cycle receipts (§07)
- 24h / weekly / monthly reports (§08)
- Live customer dashboard
- Immediate notification on Critical/High (§08)
- GitHub-issue auto-file on confirmed findings
- Access to the cross-protocol propagation channel (§04)

---

## What higher tiers add

**Production** adds:

- Kani proofs on every Critical/High finding (formal verification, not just empirical PoC)
- P3 fix-bundle PRs (maintainer-ready PR with patch + Kani proof + cargo test)
- On-call SLA on disclosure response

**Ceiling** adds (on top of Production):

- Multi-round adversarial refinement in Layer 1 (debate-then-redebate-then-confirm)
- Attack-chain enumeration depth (BFS over instruction sequences instead of single-tx PoCs)
- Kani synthesis on **every** Critical/High (not just confirmed ones)
- Foundation-grant reimbursement path (STRIDE T2)
- Direct line to founder + on-call team

---

## STRIDE Tier-2 reimbursement path

STRIDE Tier-2 ($100M+ TVL) protocols qualifying for Solana Foundation formal-verification mandate funding may direct that funding toward this methodology's implementation as the artifact-producing layer. **No formal partnership today** — this is a stated Y1 OKR. Until the recognition channel exists, methodology output is a third-party artifact protocols submit alongside their own STRIDE evidence package.

---

## Pricing model

Pricing is `2.4× raw API cost`. The 2.4× multiplier covers:

- 1.0× — direct LLM API spend (Anthropic)
- 0.4× — operations: VPS hosting, RPC bandwidth, CI, storage, backups
- 1.0× — engineering + on-call SLA + customer-portal infra + signing key custody

Net 58% gross margin. Independently-verifiable: each cycle's signed receipt includes the LLM cost line item; customers can aggregate cycle costs over a billing period and confirm the multiplier.

There is **no per-finding fee** and no bug-bounty share. The contract is for continuous coverage. A quiet month (no Criticals) is a successful month — the absence of disclosures is the security artifact.

---

## Onboarding

```
Week 1   : protocol scoping (which class library applies, custom hyps)
Week 2-3 : initial hunt cycle, triage backlog, calibrate severity floor
Week 4   : first weekly digest mailed; first signed receipts published
Month 2+ : steady state — daily 24h reports, weekly digest, immediate Crit/High alerts
```

Deliverable contract: by end of week 3, the customer should see the first non-trivial confirmed finding (or a high-confidence "no Criticals found in the initial hyp library" attestation, signed). Onboarding fee: zero — pay-as-you-go from month 1.

---

## Engagement-length minimums

There is **no minimum engagement length**. Customers can pause month-to-month. The continuous nature of the methodology means a paused customer is *uncovered* during the pause window — explicitly acknowledged in the contract — but pausing is not penalized.

Practical observation: the value compounds with engagement length. Bug-class catalog growth, propagation hits across the customer's protocol cluster, and Kani-proof-set accumulation are all functions of cumulative engagement duration. Customers who run for 6+ months see disproportionate value vs. customers who run for 1-2 months.

---

**Live reference:** [jelleo.com/methodology.html#tiers](https://jelleo.com/methodology.html#tiers)
**Pricing page:** [jelleo.com/integrate/](https://jelleo.com/integrate/) (request integration form)
