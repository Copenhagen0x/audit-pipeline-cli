# Authorization Chain Trace: O3-position-authority-binding

## Setup: Identifying the Mutation Targets

First, let me locate all sites where `position_q` and `claimable_pnl` are mutated.

```
grep -n "position_q\|claimable_pnl" /root/audit_runs/percolator-live/target/engine/src/percolator.rs
grep -rn "position_q\|claimable_pnl" /root/audit_runs/percolator-live/target/wrapper/src/
```

---

## Engine-Side Mutation Sites

After reading the engine source, the key mutation sites are:

**`position_q` mutations** (engine/src/percolator.rs):
- `trade()` — line ~3900–3950: adjusts `position_q` for both maker and taker
- `liquidate()` — line ~4800+: zeroes/reduces `position_q`
- `settle_pnl()` / `settle_funding()` — adjusts realized side effects on position

**`claimable_pnl` mutations**:
- `settle_pnl()` — credits/debits `claimable_pnl`
- `liquidate()` — may credit insurance/liquidator `claimable_pnl`
- `claim_pnl()` — decrements `claimable_pnl` (redemption path)

---

## Wrapper Instruction Enumeration

Reading `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`:

---

### Path #1 — `trade` via `TradeNoCpi` / `Trade`

```
Path #1
- BPF instruction: TradeNoCpi (and Trade)
- Wrapper handler line: wrapper/src/percolator.rs ~5800–5870
- Engine call line:    engine/src/percolator.rs ~3900 (trade())
- Signature requirements: user account signer + LP account signer
- Authority requirements: none beyond user+LP both signing
- Other guards: pre-IM (initial margin) check on both sides after trade
- Reachability verdict: SIGNER-REQUIRED (both parties must sign)
- Bypass conditions: The LP account's signer is the LP authority, not the
  position holder's authority. A willing LP CAN alter a user's position_q
  if the user also signs — but neither side can unilaterally move the other.
  No config-conditional bypass found.
- Severity if reachable improperly: HIGH — bilateral signing means both
  consenting parties are required; no unilateral mutation path here.
```

**Assessment**: `position_q` only changes when *both* user AND LP sign. This IS authority-binding from the user's perspective (user must sign). However, the LP authority is a separate role; the LP's countersigning authority is checked by the LP account's `authority` field at wrapper line ~5820.

---

### Path #2 — `liquidate`

```
Path #2
- BPF instruction: Liquidate
- Wrapper handler line: wrapper/src/percolator.rs ~6100–6180
- Engine call line:    engine/src/percolator.rs ~4800 (liquidate())
- Signature requirements: liquidator signer only (NOT the position holder)
- Authority requirements: none — any account may act as liquidator
- Other guards: engine enforces that position is *below* maintenance margin
  before proceeding (engine ~4810); position_q zeroed, claimable_pnl
  credited to liquidator and insurance fund
- Reachability verdict: PERMISSIONLESS (liquidator does not need position
  holder's signature; position holder does NOT sign)
- Bypass conditions: The health check (maintenance margin < 0) is the sole
  gate. If that check passes, any caller can mutate the victim's position_q
  and claimable_pnl without victim consent.
- Severity if reachable improperly: HIGH — this is an intentional design
  choice (liquidations must be permissionless), but it IS a path where
  position_q and claimable_pnl are mutated WITHOUT the bound authority
  signing.
```

---

### Path #3 — `settle_pnl` / `settle_funding`

```
Path #3
- BPF instruction: SettlePnl / SettleFunding (wrapper ~6400)
- Wrapper handler line: wrapper/src/percolator.rs ~6390–6450
- Engine call line:    engine/src/percolator.rs (settle_pnl, settle_funding)
- Signature requirements: NONE found — no signer check in wrapper handler
- Authority requirements: none identified
- Other guards: only that the account exists and oracle price is valid
- Reachability verdict: PERMISSIONLESS
- Bypass conditions: No authority check. Any caller can invoke SettlePnl on
  any account and mutate claimable_pnl.
- Severity if reachable improperly: MED–HIGH — if settlement math is
  correct, mutations are net-neutral or net-positive for the account holder;
  but if there is any rounding/precision error, a hostile caller can invoke
  this repeatedly to erode claimable_pnl via accumulated rounding.
```

---

### Path #4 — `claim_pnl`

```
Path #4
- BPF instruction: ClaimPnl (wrapper ~6500)
- Wrapper handler line: wrapper/src/percolator.rs ~6500–6540
- Engine call line:    engine/src/percolator.rs (claim_pnl)
- Signature requirements: account authority must sign
- Authority requirements: account.authority == signer enforced at wrapper
- Other guards: claimable_pnl >= amount
- Reachability verdict: ADMIN-GATED (account's own authority)
- Bypass conditions: none identified
- Severity if reachable improperly: N/A — properly gated
```

---

## Summary

| Path | Instruction | Verdict | Position Holder Signs? |
|------|-------------|---------|----------------------|
| 1 | Trade/TradeNoCpi | SIGNER (both parties) | YES |
| 2 | Liquidate | PERMISSIONLESS | NO |
| 3 | SettlePnl/SettleFunding | PERMISSIONLESS | NO |
| 4 | ClaimPnl | ADMIN-GATED (own authority) | YES |

- **Total reachable paths**: 4
- **PERMISSIONLESS paths**: 2 (Liquidate, SettlePnl) — these violate the claim's strict reading
- **SIGNER paths**: 1 (Trade — bilateral, not unilateral)
- **ADMIN-only paths**: 1 (ClaimPnl)

---

## Verdict on Hypothesis O3-position-authority-binding

**CLAIM**: `position_q` and `claimable_pnl` can only be mutated when the account's bound authority signs OR via permissionless instructions that explicitly preserve net-positive equity.

**Verdict**: **NEEDS_LAYER_2_TO_DECIDE** | Confidence: **MED**

**Rationale**:

1. **Liquidate (Path #2)** is intentionally permissionless and explicitly NOT net-positive for the victim (equity is consumed). This *violates* the claim's literal text — but may be an *intended* protocol exception. Whether the claim is meant to carve out liquidations requires checking the specification intent.

2. **SettlePnl (Path #3)** is permissionless with no signer requirement. If settlement math is perfectly neutral, this is benign. However, the "net-positive equity preservation" part of the claim requires empirical verification (Layer 2/3) of the settlement arithmetic for rounding behavior under adversarial invocation.

3. **Strongest bypass candidate**: Path #3 (SettlePnl with repeated invocation + rounding erosion of `claimable_pnl`). This should be the primary target for Layer 2 PoC and Layer 3 Kani verification.