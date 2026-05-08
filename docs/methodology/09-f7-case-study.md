# §09 · F7 — worked example

The first Critical disclosure produced by the methodology. A structural insurance-residual drain on Anatoly Yakovenko's Percolator perpetual DEX, disclosed in [PR #39](https://github.com/aeyakovenko/percolator-prog/pull/39).

This section walks the entire loop from cycle dispatch to upstream PR. It's the canonical demonstration of how the four pillars compose in practice.

---

## Summary

**Severity:** Critical · **Status:** Disclosed · **Cracked:** 2026-04-22 · **Disclosed:** 2026-04-30

A structural property of the haircut-residual formula combined with `use_insurance_buffer`'s vault-preserving design lets a single attacker drain the insurance fund via a self-trade. The bug is not detectable by a single static-analysis pass — every individual function holds its documented invariant. It's detectable by simulating the **interaction** of three protections under a self-trade primitive — which is exactly what Layer-2 hypothesis generation plus Layer-4 PoC dispatch was designed to do.

**One-line root cause:** `use_insurance_buffer` mutates the insurance counter only, never the underlying vault. The haircut residual `vault − c_tot − insurance` therefore *grows* by the absorbed amount, and the K/F-winning side of a self-trade claims that residual as matured PnL.

---

## How the loop found it

Cycle dispatched 2026-04-22. F7 was hypothesis number seven in the Percolator library.

| Layer | What it produced |
|-------|-----------------|
| L1 (static)             | Flagged `use_insurance_buffer` for unilateral counter mutation — decrements `insurance_fund.balance` without a paired vault adjustment. |
| L2 (LLM hypothesis gen) | Against the bug-class library "insurance-residual coupling," generated F7's hypothesis: *if vault stays constant when insurance shrinks, any formula computing a residual from `vault − ··· − insurance` grows by the absorbed amount*. |
| L3 (Anchor-aware)       | Traced the residual into `finalize_touched_accounts_post_live` at `percolator/src/percolator.rs:3567-3576`. Confirmed the formula structure. |
| L4 (LiteSVM PoC)        | Built a self-trade fork harness exercising `InitUser` + `InitLP` + `TradeNoCpi` + `LiquidateAtOracle`. Observed the predicted `A.matured = ΔK·N`. PoC committed at `43cdcd8`. |
| L5 → manual triage      | Verdict forwarded to disclosure. Posted as PR #39 against `aeyakovenko/percolator-prog`. |

Total automated wall-clock: ~18 minutes from hypothesis generation to LiteSVM-confirmed PoC. Total Anthropic API spend on the F7-producing cycle: $7.43.

---

## Root cause — three lines, three files

### 1. The asymmetric mutation — `percolator.rs:2291-2301`

```rust
fn use_insurance_buffer(&mut self, pay: i128) -> i128 {
    let take = pay.min(self.insurance_fund.balance);
    self.insurance_fund.balance -= take;   // only this counter moves
    // vault is intentionally NOT decremented:
    // insurance is accounted as held "outside" the vault.
    take
}
```

### 2. The dev-documented intent — `percolator.rs:2303-2321`

An adjacent comment block documents the residual-grows-on-absorption behavior as *intentional*. From the dev's perspective: when insurance covers a deficit, the vault stays consistent because the K/F-winning counterparty is independent and will eventually claim the matching surplus. **This is true when bankrupt and winning sides are independent. F7's contribution is showing the comment is vacuous when the attacker is both sides.**

### 3. The residual formula — `percolator.rs:3567-3576`

```rust
let senior_sum = c_tot + insurance;
let residual   = vault.saturating_sub(senior_sum);
let h_num      = residual.min(matured_pos_tot);
let is_whole   = h_num >= matured_pos_tot;
if is_whole {
    // matured converts whole to capital — claimed at withdraw
}
```

When `insurance` shrinks by `pay` and `vault` is unchanged, `residual` grows by `pay`. The K/F-winning side, whose `matured` equals `ΔK·N`, finds `h_num = matured` exactly — and converts the entire matured amount to claimable capital.

---

## Why Toly's listed protections didn't catch it

| Protection (documented invariant set)                                    | Status     | Mechanism |
|--------------------------------------------------------------------------|-----------|-----------|
| Loss settlement local-before-insurance                                   | ✅ Holds   | `settle_losses` charges loser's capital first. |
| K/F PnL conservation (A loss = B gain)                                  | ✅ Holds   | Side accumulators preserve this exactly across all paths. |
| Haircut residual cap `R = V − (C_tot + I)`                              | ❌ Vacuous | Residual *grows* by the insurance-absorbed amount, so the "cap" never bites — it caps growth, not the amount claimed. |

The third row is F7. It's not a missing check — the cap is enforced exactly as documented. The structural property is that the cap doesn't *oppose* the attacker's profit; the attacker's profit shows up *as* residual growth, which the cap unconditionally permits.

---

## End-to-end balance proof

**Setup:** Attacker opens A (long N) and B (short N) via `TradeNoCpi` self-trade with `matcher_program = [0;32]`. Each posts `0.2N` capital (IM = 20%). Insurance = 5 SOL (the actual mainnet balance at disclosure time). Vault = `5 + 0.4N`.

**Step 1 — adverse swing past IM.** Wait for any oracle move `ΔK > 0.2`. Pyth SOL/USD provides this routinely.

**Step 2 — liquidate B.** Attacker calls `LiquidateAtOracle(B)` (permissionless, verified on-chain: `liquidationAuthority = [0;32]`):

- `touch B` → `B.pnl = -ΔK·N` (settled from side accumulator)
- `settle_losses(B)` → charges `B.capital = 0.2N` → `B.pnl = -(ΔK-0.2)·N`
- `attach_effective_position(B, 0)` → `B.basis = 0`; the side K-accumulator is **not** decremented
- `enqueue_adl` → `use_insurance_buffer((ΔK-0.2)·N)` → insurance shrinks; **vault unchanged**

**State:** `vault = 5 + 0.4N`, `insurance = 5 − (ΔK − 0.2)·N`, `c_tot = 0.2N` (A only).

**Step 3 — touch A.** Anything that calls `accrue_market_to + touch_account_live_local` for A (Trade, Withdraw, KeeperCrank). Reads current K_long (still at `+ΔK`; never decremented in Step 2): `A.pnl = +ΔK·N`, routed through warmup → matured.

**Step 4 — conversion at `finalize_touched_accounts_post_live`:**

```
senior_sum = c_tot + insurance       = 0.2N + (5 − (ΔK − 0.2)·N)
residual   = vault − senior_sum      = (5 + 0.4N) − (0.2N + 5 − (ΔK − 0.2)·N) = ΔK·N
h_num      = min(residual, matured)  = min(ΔK·N, ΔK·N) = ΔK·N

is_whole = true → A's matured converts whole to capital.
```

**Step 5 — withdraw.** A withdraws `0.2N + ΔK·N`. Net P&L:

```
A_in   = 0.2N
B_in   = 0.2N
A_out  = 0.2N + ΔK·N
B_out  = 0
─────────────
Net    = (0.2N + ΔK·N) − (0.2N + 0.2N) = (ΔK − 0.2)·N
```

Insurance loss = `(ΔK − 0.2)·N`. **Exact match.**

---

## Sizing

At `N = 25 SOL` and `ΔK = 0.4` (one ~40% adverse swing), the attacker captures `(0.4 − 0.2) · 25 = 5 SOL` — the entire insurance fund balance at the time of disclosure. `IM = 0.2` is the break-even. Any `ΔK > 0.2` is profitable.

Permissionless instructions used (all verified on-chain `[0;32]`):

```
InitUser           InitLP
TradeNoCpi         LiquidateAtOracle
KeeperCrank        Withdraw
```

---

## Recommended fixes

Three independent options — any one suffices:

1. **Make insurance-buffer use also debit the vault.** *This is the patch packaged in PR #39.*
2. **Track consumed insurance in the residual formula.** `residual = vault − c_tot − I − I_consumed_this_epoch`.
3. **Reject the bilateral-signer self-trade primitive** (`matcher_program = [0;32]` with both sides controlled by the same operator).

PR #39 was opened with option 1 + a LiteSVM regression test (commit `43cdcd8`). Local verification: zero regressions across 277 existing tests; F7's PoC test fails as expected with the patch reverted, passes with the patch applied.

---

## Disclosure timeline

| Date       | Event |
|------------|-------|
| 2026-04-22 | Cycle dispatched. F7 hypothesis generated. L3 trace lands on `finalize_touched_accounts_post_live`; L4 PoC fires positive on first run. |
| 2026-04-22 | Manual triage. F7's vector (residual formula plus self-trade) survives cross-check. |
| 2026-04-23 → 04-29 | Patch construction. Vault-debit fix verified against existing test suite. LiteSVM regression test (`43cdcd8`) exercises the full self-trade primitive end-to-end. |
| 2026-04-30 | PR #39 opened against `aeyakovenko/percolator-prog`. Includes root-cause writeup, end-to-end balance proof, the patch, and the LiteSVM PoC test. |
| 2026-05-04 | Validation comment posted with worked balance proof and patched-vs-unpatched test diff. Vault-debit fix verified locally — zero regressions across 277 tests. |
| Ongoing    | Sibling-class hypotheses ("insurance-residual coupling") queued for cross-protocol propagation against any other Solana protocol that maintains a vault counter and an out-of-vault insurance counter. |

---

## What this proves about the methodology

F7 is the canonical test case for why the methodology is structured the way it is. The bug is not detectable by:

- A single static-analysis pass (every individual function holds its documented invariant).
- Reading the dev's intent (the design is intentional, and the comment block at `percolator.rs:2303-2321` explicitly endorses it).

It is detectable by **simulating the interaction of three protections under a self-trade primitive** — which is exactly what L2 hypothesis generation plus L4 PoC dispatch was designed to do.

The four pillars that produced F7 are the same four pillars proposed for every protocol on the funded plan:

- **P1 — detection** (Layers 1-6): the loop that landed F7 in 18 minutes of automated wall-clock.
- **P2 — propagation**: F7's bug class is now a sibling-hypothesis seed for every protocol with a vault/insurance accumulator pair.
- **P3 — fix bundle**: PR #39 ships the patch, the regression test, and the worked balance proof — not just a finding.
- **P4 — attestation**: every cycle that touched F7 was signed Ed25519. The public key lives at [`api.jelleo.com/keys/jelleo.ed25519.pub`](https://api.jelleo.com/keys/jelleo.ed25519.pub); the cycle-receipt archive at [`api.jelleo.com/cycles/`](https://api.jelleo.com/cycles/).

F7 is one disclosure. The thesis is that the same loop, scaled across the cluster, produces an F7-class disclosure on every protocol that runs it long enough — and that the cost of running the loop is structurally lower than the cost of the attacks it prevents.

---

**Live reference:** [jelleo.com/case-studies/f7-percolator/](https://jelleo.com/case-studies/f7-percolator/)
**Disclosure PR:** [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39)
