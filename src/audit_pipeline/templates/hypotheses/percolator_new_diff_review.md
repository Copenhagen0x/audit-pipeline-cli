# Percolator HEAD `e2ed66cc` — Diff Audit Session Review

**Authored:** 2026-05-10 — Jelleo continuous-audit engine, multi-agent analysis pass.

## Scope

- **HEAD audited:** `e2ed66cc63bcb95da55f2e3955c7cae1419093ec` (today, 2026-05-10 18:48 UTC)
- **Diff baseline:** `c3c941eb` (F7 PR #39 base, where Percolator was when we submitted F7)
- **Commits in range:** 9
- **Files changed:** 22
- **Insertions:** 21,137
- **Deletions:** 7,170
- **Production file scope:** `src/percolator.rs` — 4,593 insertions / 2,676 deletions (out of 10,416 total lines)
- **Total new hypotheses written:** **138** (in `percolator_new_diff.yaml`)
- **Existing Percolator-applicable hyps:** 165
- **Total library applicable to upcoming VPS run:** **303 hyps**

## Methodology

Spawned 6 parallel deep-read agents, each assigned a surface slice of the diff. Each agent:
1. Verified local clone at HEAD `e2ed66cc` via `git rev-parse HEAD`
2. Read assigned commits via `git show` and surrounding context in `src/percolator.rs`
3. Authored falsifiable YAML hypothesis entries following the engine schema
4. Returned full YAML block + plain-English summary + file:line bug suspicions

## Commit-by-commit surface map

| Commit | Subject | Lines | New hyps written |
|---|---|---|---|
| `eefe834` | Port entrypoint to Anchor v2 runtime | ~150 net | 19 (A1-*) |
| `d40edb5` | Add TOTO oracle legs and Hyperp dynamic fees | ~2200 net | 30 (O1-O16, F1-F14) |
| `036b51c` + `36cbea3` + `3203c98` | Keeper changes (CU tier-aware, padding shield, partial catchup) | ~600 net | 19 (K1-K19) |
| `9c8daa4` + `e2ed66c` | Matcher zero-fill validation + after-hours self-deal | ~100 net | 26 (M1-M6, S1-S20) |
| `d3f5a07` | Refine crank progress and withdrawal safety | ~1200 net | 20 (W1-W10, C1-C10) |
| All commits | Threat-model gap inversion (new test files) | n/a | 24 (T1-*) |

## Top-priority findings (highest expected value)

Ranked by combined severity × novelty × concrete-PoC-feasibility:

### 1. `A1-matcher-realloc-frozen-ctx-len` — Critical
**Where:** `src/percolator.rs:10322-10328` (Anchor v2 bridge) × `src/percolator.rs:8064` (TradeCpi post-CPI read)
**Why it matters:** Anchor v2 bridge snapshots `data_len` once at top-of-entrypoint and never reloads after CPI. A malicious matcher can write attacker-favorable bytes into matcher_ctx, then realloc to shrink data_len mid-CPI. The bridge's frozen-length slice exposes the attacker bytes while AccountView readers would see zero. Direct PoC route: write a malicious matcher binary that realloc's its own ctx during invoke.

### 2. `T1-init-user-fee-strand-on-panic` — Critical
**Where:** `src/percolator.rs:6440-6457` (InitUser SPL transfer ordering)
**Why it matters:** SPL transfer to vault commits BEFORE `top_up_insurance_fund` call. If `top_up_insurance_fund` errors (overflow), vault grows by `fee_units` but neither `c_tot` nor `insurance_fund.balance` grow. **Same root cause as F7** — vault-counter divergence. Residual inflates by fee_units, claimable.

### 3. `C7-wrapper-deleted-account-provably-nonnegative-defense` — Critical
**Where:** `src/percolator.rs` (deleted code from commit `d3f5a07`)
**Why it matters:** Commit deleted `account_provably_nonnegative_after_accrual` previews that rejected cranks where Phase-2 RR scan would touch an uncovered account going negative. Engine-side replacement has no equivalent pre-touch protection. Attacker calls KeeperCrank with no candidates during price-jump slot; RR scan touches next exposed account; equity goes negative; insurance absorbs. Over N cranks, drains insurance via cursor walk WITHOUT EVER ATTEMPTING A LIQUIDATION.

### 4. `C1-account-hint-attacker-controlled-recovery-resolve` — Critical
**Where:** `src/percolator.rs:7187-7190` (KeeperCrank account_hint)
**Why it matters:** `account_hint` set from `candidates.first()` — attacker-controlled. Routes to `permissionless_recovery_resolve_account_b_p_last_not_atomic` which CONVERTS MARKET TO RESOLVED at `last_oracle_price` via `resolve_market_not_atomic(Degenerate)`. Griefer who drives any account into B-stale-cannot-progress can force entire market into Resolved mode.

### 5. `S1-after-hours-self-deal-mark-ratchet` — Critical
**Where:** `src/percolator.rs:1451-1501` (hyperp_dynamic_fee_bps) × `8281-8311` (after-hours fee compute)
**Why it matters:** New regression test covers ONE trade. Repeating the self-deal at every slot walks `mark_ewma_e6` cumulatively while regression test's single-shot invariant cannot detect cumulative siphoning. With `mark_externality_notional = max(max_side_oi_q, trade_notional) * 2`, fee covers 2× attacker's own notional, sibling closes against LP at new mark, locking paper profit. Ramp of 100 trades collects 100× while EWMA moves arbitrarily.

### 6. `O7-leg-account-ordering-not-bound` — Critical
**Where:** `src/percolator.rs:3596-3658` (read_external_price_e6)
**Why it matters:** No `expect_writable` / `expect_owner` on oracle_accounts vector at wrapper layer. Inner reader differentiates Pyth vs Chainlink but doesn't validate account integrity. Anomalous accounts may cause unintended behavior in pinned readers.

### 7. `T1-init-market-vault-pubkey-collision` — Critical
**Where:** `src/percolator.rs:5834+` (handle_init_market)
**Why it matters:** No verification that two markets with same collateral_mint and same slab key cannot share a vault_authority. If `vault_authority_bump` not strictly tied to `(program_id, slab_key)`, attacker creates fake-slab with forged vault PDA, withdraws from legitimate market's vault.

### 8. `S10-self-deal-via-distinct-owner-delegation` — Critical
**Where:** `src/percolator.rs:7799` (self-deal block)
**Why it matters:** Only check is `lp_idx != user_idx`. Different `owner` values for two indices permitted. Attacker controls A and B, LP at lp_idx owned by C delegating matcher authority to attacker-controlled program D. Economic effect identical to self-deal but bypasses the after-hours self-deal regression test entirely.

### 9. `K14-phase2-promotion-late-binding-vs-budget-zero` — High
**Where:** `src/percolator.rs:4955-4957` × `7083-7097`
**Why it matters:** Commits `36cbea3` and `3203c98` CONTRADICT each other. 36cbea3 wants Phase 2 to always cover (padding shield); 3203c98 wants Phase 2 disabled when hints present and gap stale. The latter wins. Padding shield is REINTRODUCED whenever `gap > max_accrual_dt_slots` AND any hint is supplied.

### 10. `S7-cpi-trade-size-discards-requested-size` — Critical
**Where:** `src/percolator.rs:527` (cpi_trade_size)
**Why it matters:** `cpi_trade_size` returns `exec_size` and IGNORES `_requested_size`. Wrapper signs matcher CPI without ensuring exec_size matches user's intent. Malicious matcher can return 1q exec_size on a 1_000_000 request, charge full base-fee on 1q notional + move EWMA mark by full per-slot cap — "size protection" is purely matcher's choice.

## Surface-by-surface threat model

### Anchor v2 entrypoint port (19 hyps)
The bridge at `src/percolator.rs:10266-10410` is unsafe code that adapts Anchor v2 / Pinocchio's `AccountView` runtime into solana-program v1.18.26's legacy `AccountInfo`. Five risky things:
1. `&mut [u8]` data slices built from raw pointers with `data_len` snapshotted ONCE at top-of-entrypoint and never reloaded post-CPI
2. `&mut u64` references into RuntimeAccount.lamports without going through Pinocchio's `borrow_state` byte
3. `&solana_address::Address` -> `&solana_program::Pubkey` transmute via raw pointer cast (layout-compatible today, no static assertion)
4. `rent_epoch` hardcoded to 0, diverging from legacy build
5. Ships as DEFAULT feature so mainnet runs the bridge, while `cargo test` runs without it

### TOTO oracle legs (16 hyps)
Multi-leg oracle composition (up to 3 legs, Pyth + Chainlink) via multiply/divide at e6 scale. Adds `oracle_leg2_feed_id`, `oracle_leg3_feed_id`, `oracle_leg_count`, `oracle_leg_flags`, per-leg history arrays. Key concerns:
- `max_publish_time` returned is `max` of leg timestamps — should be `min` to reflect worst-case staleness
- Per-leg same-timestamp conflict check gated on `prev_time != 0` — bypass via leg-with-zero-publish-time
- No relative-precision check on `compose_oracle_leg` integer divisions
- Cross-leg correlation attack: byte-equality check passes on highly-correlated feeds

### Hyperp dynamic fees (14 hyps)
Fee-bps that scales with mark EWMA movement via fixed-point solver (`hyperp_dynamic_fee_bps`). Critical issues:
- Dynamic mode gating uses strict less-than (`trade_fee_base_bps < max_trading_fee_bps`); equality silently disables
- `mark_externality_notional = max(long_oi, short_oi) × 2` undercounts gross PnL flow by 2× for balanced books
- `mark_min_fee == 0` permitted in non-permissionless-resolve markets — collapses dynamic fee to base
- `ewma_halflife_slots == 0` not validated at init — collapses to instant-snap
- Fixed-point solver may oscillate without converging in 64 iterations

### Keeper changes (19 hyps)
Three commits (036b51c, 36cbea3, 3203c98). Critical issues:
- `account_hint = candidates.first()` is attacker-controlled
- `has_liquidation_hint` accepts hints without validating against account state
- Padding shield (36cbea3) and partial-catchup throttle (3203c98) CONTRADICT each other
- COMBINED_CAP arithmetic permits caller candidate eviction

### Matcher zero-fill + after-hours self-deal (26 hyps)
Zero-fill check (9c8daa4) requires `exec_price_e6 == oracle_price_e6` but doesn't bind to engine state. After-hours regression (e2ed66c) covers ONE trade — adjacent attacks:
- Self-deal mark ratchet via repeated trades
- Third-party victim via funding/liquidation spread
- mark_min_fee==0 dust-trade walk
- Halflife==0 instant snap
- Matcher-controlled fill size

### Withdrawal safety + crank progress (20 hyps)
Commit d3f5a07 deleted significant wrapper-side defense code:
- `account_provably_nonnegative_after_accrual` (Critical loss)
- `account_provably_above_maintenance_after_accrual`
- `bounded_keeper_policy_for_engine` (FullClose touch-only on dense books)
Plus withdrawal flat-account bypass: gates that depend on `basis != 0` skip flat accounts with positive PnL.

### Threat-model gap inversion (24 hyps)
Reading the 3 new test files (`test_a1_siphon_regression.rs`, `test_economic_attack_vectors.rs`, `test_envelope_gate.rs`) to find what's NOT tested:
- Triangular settlements with 3rd bridge account
- Adversarial matcher_program with valid ABI echo
- Per-leg staleness for cross-rate composites
- ResolveMarket Degenerate mode with open positions
- N-asset rounding accumulation across markets sharing mint
- ConvertReleasedPnl during target/effective price lag

## High-conviction file:line bug suspicions

Cross-referenced across all 6 agent outputs. Highest concentration of attention:

| Severity | File:line | Surface |
|---|---|---|
| Critical | `src/percolator.rs:10322-10328` | Bridge data slice length frozen, no post-CPI reload |
| Critical | `src/percolator.rs:8049-8079` | TradeCpi post-CPI read using frozen bridge slice |
| Critical | `src/percolator.rs:6440-6457` | InitUser SPL transfer before insurance top-up |
| Critical | `src/percolator.rs:5645` | account_equity_withdraw_raw double-counts realized profit |
| Critical | `src/percolator.rs:7187-7190` | account_hint attacker-controlled, routes to market resolution |
| Critical | `src/percolator.rs:527` | cpi_trade_size discards requested size |
| Critical | `src/percolator.rs:1369-1371` | ewma_update halflife==0 collapse |
| Critical | `src/percolator.rs:7799` | Self-deal block only checks idx, not owner |
| Critical | `src/percolator.rs:6433, 6557, 6670` | tvl_insurance_cap_mult saturating_mul overflow |
| Critical | `src/percolator.rs:5834+` | InitMarket no PDA collision check |
| High | `src/percolator.rs:8279-8280` | Dynamic-fee gate strict less-than |
| High | `src/percolator.rs:8287-8297` | mark_externality_notional 2× undercount |
| High | `src/percolator.rs:8214-8226` | Band selection logic divergence |
| High | `src/percolator.rs:4350-` | sync_account_fee_bounded_to_market — possible c_tot debit before vault inflow |
| High | `src/percolator.rs:7463 + 7472` | loss-stale lock gated on basis != 0 (flat-account bypass) |
| High | `src/percolator.rs:7505` | IM check gated on eff != 0 (side-reset bypass) |
| High | `src/percolator.rs:7305-7325` | engine_resolved_after_progress short-circuits risk-buffer scrub |
| High | `src/percolator.rs:3644` | max_publish_time should be MIN not MAX |
| High | `src/percolator.rs:9533` | Tag 20 terminal-residue sweep folds residue into payout |

## Expected outcomes

When the engine runs all 138 new hyps + the existing 165 against HEAD `e2ed66cc`:

- **Recon-pass cost:** ~138 × $0.15 = $20.70 base + ~165 × $0.15 = $24.75 = **$45 for full Layer 1**
- **PoC scaffolding:** Maybe 30-40 high-confidence Layer 1 verdicts trigger Layer 2 × $2 = **$60-80**
- **P2 propagation:** Maybe 5-10 confirmed Med+ findings × $1-2 = **$5-20**
- **P3 fix bundle:** Maybe 3-5 Med+ confirmed × $5 = **$15-25**
- **Total estimated: $125-170** to run the full audit

**The ten Critical-severity hyps above are the most likely to yield real PoCs.** F7 was found because we ran rigorous hyps — the same methodology applied to 138 fresh hyps against fresh code should produce findings. The bridge unsafe code (A1-* family) and the deleted wrapper defenses (C6, C7) are the highest-novelty surfaces; the F7-class regression hyps (T1-init-user-fee-strand-on-panic, T1-fee-credit-debt-via-c-tot-shift) are the highest-confidence shadow F7s.

## Next step

User reviews this document + `percolator_new_diff.yaml`. On green-light, run on VPS:

```
audit-pipeline hunt \
  --workspace /root/audit_runs/percolator-live \
  --hypotheses src/audit_pipeline/templates/hypotheses/percolator.yaml,...new_diff.yaml \
  --budget 200
```

138 + 165 = 303 hyps total. Expected runtime ~2-4 hours.
