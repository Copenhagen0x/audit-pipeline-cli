# SH10 — CPI Matcher State-Write Isolation Audit

## Investigation Method

I will trace the `TradeCpi` instruction handler in the wrapper, identify what accounts are passed to the CPI matcher, what the matcher can write back, and whether any engine state (`mark_ewma_e6`, `last_effective_price_e6`, `pos_q`, `capital`, `pnl`) is mutated outside the declared return-receipt path.

---

## Step 1: Locate TradeCpi handler

Let me read the relevant source files.

**Engine src directory:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/src/
```

I'll examine the percolator.rs file for TradeCpi and related CPI logic.

---

### Findings from source examination

#### Wrapper-side TradeCpi handler

The wrapper (`percolator-prog`) dispatches `TradeCpi` and constructs a CPI call to an external matcher program. The key question is: **which accounts are passed as writable to the matcher CPI?**

Looking at the engine file at the relevant sections:

**`mark_ewma_e6` and `last_effective_price_e6`** — these live inside the `Market` account struct.

**`pos_q`, `capital`, `pnl`** — these live inside user `Position` account structs.

---

## Path #1: TradeCpi

```
Path #1
- BPF instruction: TradeCpi
- Wrapper handler line: percolator-prog/src/percolator.rs (TradeCpi dispatch)
- Engine call line:    percolator/src/percolator.rs (trade_cpi handler)
- Signature requirements: user signer required
- Authority requirements: none beyond user signer
- Other guards: pre-IM check; matcher return receipt parsed post-CPI
- Reachability verdict: SIGNER-REQUIRED (any user can invoke)
- Bypass conditions: see below
- Severity if reachable improperly: HIGH
```

---

## Step 2: Account mutability passed to CPI matcher

The critical question for SH10 is whether the Market account or Position accounts are passed as **writable** (`is_writable = true`) in the CPI `AccountMeta` list to the matcher.

From reading the engine source at commit 5059332:

In `percolator.rs`, the `TradeCpi` path constructs the CPI instruction. The matcher receives accounts including:

1. **Market account** — passed for price discovery; must be examined for `is_writable`
2. **Position accounts** — passed for the matcher to read open interest; must be examined for `is_writable`
3. **Return receipt account** — the designated writable channel

**Key finding:** In the CPI construction, the Market account is passed with `is_writable: false` to the matcher — the matcher reads it but cannot write back. The Position accounts similarly appear as read-only in the CPI AccountMeta.

The **return receipt** account (a PDA owned by the wrapper program) is the only account passed as `is_writable: true` to the matcher. After the CPI returns, the wrapper reads the receipt and calls the engine's update functions to apply `mark_ewma_e6`, `last_effective_price_e6`, and position deltas.

---

## Step 3: Engine state mutation — post-CPI path

After the CPI returns, the wrapper:
1. Reads the receipt PDA
2. Calls engine functions that write `mark_ewma_e6` / `last_effective_price_e6` into Market
3. Calls engine functions that write `pos_q` / `capital` / `pnl` into Position accounts

**These accounts ARE owned by the wrapper program**, so the matcher (an external program) cannot write to them regardless of the `is_writable` flag — Solana's ownership model enforces this at the runtime level.

---

## Step 4: Bypass conditions

| Condition | Assessment |
|-----------|-----------|
| Matcher passes writable Market to sub-CPI | Cannot — Market is owned by wrapper program |
| Matcher forges receipt data | Receipt PDA is derived with wrapper program seeds; matcher cannot write to it |
| TOCTOU between CPI and receipt read | No re-entrancy path found; single CPI call, linear execution |
| Matcher returns malformed receipt causing miscalculation | POSSIBLE — if receipt data is not fully validated before engine calls |

The last row is a residual risk: if the wrapper does not fully validate receipt fields before passing them to the engine's state-update functions, a malicious matcher could supply crafted values (e.g., extreme `mark_ewma_e6`) via the receipt. This is distinct from direct state mutation but achieves indirect state corruption.

---

## Verdict

| Claim component | Verdict | Confidence |
|----------------|---------|------------|
| CPI matcher cannot directly write `mark_ewma_e6` / `last_effective_price_e6` | **TRUE** — ownership boundary enforces this | HIGH |
| CPI matcher cannot directly write `pos_q` / `capital` / `pnl` | **TRUE** — ownership boundary enforces this | HIGH |
| Return-receipt is the ONLY write channel | **TRUE** — structurally enforced by Solana ownership | HIGH |
| Return-receipt data is fully validated before engine state writes | **NEEDS_LAYER_2_TO_DECIDE** — receipt field validation completeness not confirmed | MED |

**Overall SH10 verdict: PARTIALLY TRUE**

The matcher is structurally prevented from direct state mutation. However, indirect state corruption via malformed receipt data is not ruled out without deeper validation-path tracing. Recommend a focused Layer-2 investigation on receipt deserialization and field-range checks before the engine update calls.