# Percolator Bounty 3 — Final Audit Conclusion
**Engine `5059332` · Wrapper `04b854e` · Program `2LfCFmDKwcnHunqdsCW9uV7KNgBgnFGASs8uM7MwHgHm`**
**Auditor: Jelleo · Date: 2026-05-06 · 24h continuous run**

## Verdict
**No new bounty-eligible win found.** Attack surface explored end-to-end across 12 parallel waves and 50+ executable LiteSVM tests. The five public bugs filed by `dcccrypto` and `vip-ultr` (#81–#85) sit in families the recon flagged (cascade-bypass, ADL-drift, oracle effective-price), but none of our independent constructions reproduce a public-call drain at this commit.

## Coverage
- **Cascade-bypass family** (#33/#60/#61/#65/#69/#81/#83): 4096-tier residual stress, free-list ABA, lock-generation persistence — all fixes verified active.
- **K/F & ADL accumulators**: same-slot atomic invariants (§10.24), envelope (§1.4), drift bound (§10.22) — held under crank-rate, queue-depth, and self-trade scenarios.
- **Insurance fund vault**: `use_insurance_buffer`, `absorb_protocol_loss`, `resolve_flat_negative` — F7-class vault-debit fix is live; counter and balance move together.
- **Fee-credits sign-flip (W6)**: 8 engine write sites, every one `checked_*` with pre-bounds; `validate_fee_credits_shape` rejects `fc > 0 || fc == i128::MIN`; `assert_public_postconditions` re-runs at every public ix exit. Tier-independent. **Refuted.**
- **VAA / oracle staleness**: identical line geometry to disclosed #84 — duplicate, not new.
- **Zombie funding (#85)**, **enqueue_adl ratchet**, **harvest math**: all our positive findings dissolved under verifier scrutiny (e.g., W8's "+233B" turned to −266B once 500B zombie funding cost was accounted; W10's 2.5M ratchet bound by K/PnL haircut at resolved-close).

## Disclosable (non-bounty) artifacts
1. **Wrapper regression d3f5a07** — drops `is_hyperp && h_min == 0` init guard previously labeled "print-money configuration" in the deleted code. Applies to hypothetical Hyperp deployments only; bounty 3 is Pyth, so not in scope, but worth a wrapper-side comment for downstream forks.
2. **Admin DoS, `new_account_fee = MAX_VAULT_TVL`** — admin-only, not a bounty path; flag for hardening.
3. **`FLAGS_OFF = 13`** — no compile-time tripwire if the constant drifts from its bit-mask intent.

## Methodology honesty
F7 was the only construction that converted from family-level pattern-match to a concrete exploit. The five new public issues confirm a real ceiling: independent rediscovery requires either deeper Solana/Rust internals than I bring solo, or longer iteration than 24h on a parallel agent fleet. Recon flagged the right neighborhoods; geometry construction is where the gap shows.

## What was actually shipped today
- Engine pin bumped `f6b13f5 → 5059332` and BPF rebuilt (SHA `6e2bb5a…b7002`) at 4096 tier.
- 50+ audit tests authored, dispatched, and verified across W1–W12.
- Website status line updated to reflect the truthful posture: 4 confirms (1 fired F7-class · 1 attestation · 2 cannot-test).
- No commits to `aeyakovenko/percolator-prog` from this run. PR #39 (F7 PoC) remains the only pushed artifact.

## Pre-OtterSec call posture (Thu 2026-05-07, 09:30 EST)
The pitch holds without a fresh win. F7 is the productizable proof point: family-level recon → concrete vault-debit exploit → upstream fix verified. New bounty 3 entries are out of reach in 24h, but they don't change Jelleo's value story — the pipeline that flagged the right neighborhoods is exactly what a paying customer rents.

**Status: clean stop. No commits pushed. Ready for the call.**
