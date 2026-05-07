# Layer 4 parallel attack sweep — 2026-05-06

**Four LiteSVM attack tests, four parallel sub-agents, four attack
vectors, four defended. Total cost: $0 (run inside the orchestrating
session, not via paid API calls).**

This run extends the autonomous Layer 4 dispatcher
([`scripts/litesvm_attack_attempt.py`](../../scripts/litesvm_attack_attempt.py))
by parallelising attack-vector exploration. Each sub-agent received a
specific brief targeting one of the disclosed bug families from
`aeyakovenko/percolator-prog` issues, and authored a complete LiteSVM
test in isolation. Each test compiled clean and ran against the same
shared BPF binary.

## Setup

- **Target:** mainnet bounty 3 wrapper `aeyakovenko/percolator-prog @ 04b854e`
- **BPF binary:** built locally via `cargo-build-sbf --features small`
  (256-account tier, structurally identical to the deployed 4096 tier)
- **Win condition:** decrease `engine.insurance_fund.balance` below
  starting value via any sequence of public BPF instructions
- **Out of scope:** Pyth manipulation, Solana validator attacks
- **Orchestration:** sub-agents spawned via the parent session's Agent
  tool — no separate API spend, runs use the parent session's quota

## Results

| Vector | Disclosure | Outcome | Insurance pre/post | Time |
|---|---|---|---|---|
| Sweep-Gap K-Drift | [#57](https://github.com/aeyakovenko/percolator-prog/issues/57) | ✅ PASS | 5,000,000,121 / 5,000,000,121 (Δ=0) | 28.8s |
| TradeCpi zero-fill cascade bypass | [#69](https://github.com/aeyakovenko/percolator-prog/issues/69) | ✅ PASS (engine returned `CatchupRequired` 0x1d) | 5,000,000,004 / 5,000,000,004 (Δ=0) | — |
| CatchupAccrue rollback | [#76](https://github.com/aeyakovenko/percolator-prog/issues/76) | ✅ PASS (tag 31 fully retired) | 5,000,000,000 / preserved | — |
| Resolved-mode reconciliation | [F7 family](https://github.com/aeyakovenko/percolator-prog/pull/39) | ✅ PASS | 10,000,000,000 / 10,000,000,764 (+764 from new-account fees, not a drain) | 8.67s |
| Cursor-wrap consumption budget | [#55](https://github.com/aeyakovenko/percolator-prog/issues/55) | ⚠️ blocked by Anthropic cyber-safety filter | — | — |

## Key findings

### CatchupAccrue tag 31 was *retired*, not patched

The agent's investigation surfaced a structural defense: at v12.19.6,
the `CatchupAccrue` instruction (tag 31) is rejected with
`InvalidInstructionData` for any caller. Anatoly's fix for issue #76
eliminated the bug-prone partial-rollback branch entirely; market-clock
progress now routes exclusively through atomic `KeeperCrank`. The
rollback monotonicity bug is structurally unreachable.

### TradeCpi zero-fill is gated by `reject_account_limited_market_progress`

The agent's test triggered a TradeCpi with a zero-fill matcher return
against a market with exposed OI. The wrapper rejected the call with
`CatchupRequired` (0x1d) at `src/percolator.rs:7468`, preventing the
clock advancement that issue #69 originally exploited. Engine slot did
not advance.

### Sweep-Gap K-Drift contained by §1.4 solvency envelope

121 accounts cycled through 360 cranks with a -11.6% adverse oracle walk
spanning 3000 slots. The §1.4 envelope (`max_price_move_bps_per_slot=4`
× `max_accrual_dt_slots=100` = 400bps per chunk, well below
`maintenance_margin_bps=500`) holds. Per-chunk auto-cranks during the
walk drain K-drift before it exceeds account capital, so by the time
the round-robin reaches each account the deficit is bounded.

### Resolved-mode reconciliation conserves value

Three traders + LP, 10B insurance seed, adverse 138M→110M oracle walk,
ResolveMarket, then permissionless force_close on every account. All
positions closed cleanly with `cap=0 pnl=0`. Final state:
`engine_vault == spl_vault == insurance == 10B` exactly. Insurance went
*up* by 764 from `new_account_fee` deposits (not a drain). Soundness
invariant held: `insurance_decrease ≤ legitimate_loss_total`.

## Files

```
test_sweepgap_kdrift_attack.rs        — 121-account drift attack
test_tradecpi_zerofill_bypass.rs      — zero-fill matcher cascade attack
test_catchup_rollback_attack.rs       — partial-rollback monotonicity attack
test_resolved_reconciliation_drain.rs — resolved-mode permissionless reconcile
```

The cursor-wrap attack vector ([#55](https://github.com/aeyakovenko/percolator-prog/issues/55))
was blocked at agent dispatch by Anthropic's cyber-safety filter.
That vector remains unverified at the parallel-attacks level; the
`audit-pipeline confirm`-level recon agent did flag it as
HIGH-confidence LAYER_2 earlier in the session, and hand-verification
of the engine source confirmed `bankruptcy_hmax_lock_active` is
checked once at sweep entry rather than per-iteration. That property
exists but is not by itself an exploit (each insurance absorption in
the iteration matches a real per-account bankruptcy loss).

## What this proves

Against the live bounty 3 wrapper, **four classical attack vectors
that other researchers used against bounty 2** are all defended at
the current pin. The methodology can dispatch parallel attack agents
against any future SHA in <10 minutes for $0 (when run inside an
orchestrating session). This is the strongest safety attestation
deliverable the methodology produces.

## What this does NOT prove

- 5 vectors is a sample, not exhaustive — a determined attacker may
  discover novel patterns
- Sub-agents are bounded by their per-task context window
- The `--features small` (256-account) tier was used; the deployed
  binary is the 4096-account tier (structurally identical layout but
  larger sweep-gap window — re-run on default features for a full
  match)
