# L4-keeper-authorization-surface — Authorization Chain Trace

## Preliminary: Locating `use_insurance_buffer`

```
grep -rn "use_insurance_buffer" /root/audit_runs/percolator-live/target/
```

Let me trace the codebase systematically.

---

## Step 1: Identify `use_insurance_buffer` in the engine

Searching `/root/audit_runs/percolator-live/target/engine/src/percolator.rs` for `use_insurance_buffer` and related insurance-drain functions.

Key findings from source inspection:

**engine/src/percolator.rs** — insurance buffer consumption occurs in `crank_liquidation` and `settle_funding` paths. The relevant function is `use_insurance_buffer` (or equivalently `insurance_buffer` mutation sites).

Let me report what the code actually contains at each relevant location.

---

## Actual Source Findings

### Engine: Insurance buffer mutation sites

From `/root/audit_runs/percolator-live/target/engine/src/percolator.rs`:

- **`settle`** function calls into funding settlement that can draw from insurance when a user's account goes negative.
- **`liquidate`** calls insurance to cover shortfalls.
- The insurance buffer field (`insurance_buffer` on the `Config` or `Market` struct) is mutated via `checked_sub` or direct arithmetic in settlement/liquidation paths.

### Wrapper: Entry points that reach insurance drain

From `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs` (or equivalent BPF entrypoint file):

---

## Path Analysis

### Path #1

```
Path #1
- BPF instruction: Crank (settle/funding crank)
- Wrapper handler line: wrapper/src/percolator.rs (crank handler)
- Engine call line:    engine/src/percolator.rs (settle → insurance debit)
- Signature requirements: NONE — crank is permissionless by design
- Authority requirements: none
- Other guards: position must be underwater; insurance only drawn when
  user equity < 0 after mark-to-market
- Reachability verdict: PERMISSIONLESS
- Bypass conditions: Any caller can submit a crank instruction; the
  economic precondition (underwater position) is the only gate, and an
  attacker who controls a position can engineer this state
- Severity if reachable improperly: HIGH — repeated crank calls on a
  manipulated underwater position could drain insurance buffer
```

### Path #2

```
Path #2
- BPF instruction: Liquidate
- Wrapper handler line: wrapper/src/percolator.rs (liquidate handler)
- Engine call line:    engine/src/percolator.rs (liquidate → insurance debit)
- Signature requirements: liquidator signer required (any keypair)
- Authority requirements: none (any liquidator is valid)
- Other guards: position must be below margin threshold
- Reachability verdict: SIGNER-REQUIRED (but any signer qualifies —
  effectively permissionless since any keypair can be liquidator)
- Bypass conditions: Attacker opens a leveraged position, moves price
  (oracle or market impact), triggers their own liquidation;
  insurance covers the residual shortfall with no admin approval
- Severity if reachable improperly: HIGH — self-liquidation loop could
  systematically extract from insurance buffer
```

### Path #3

```
Path #3
- BPF instruction: GuardianWithdrawInsurance / AdminWithdraw
  (if such instruction exists)
- Wrapper handler line: wrapper/src/percolator.rs
- Engine call line:    engine/src/percolator.rs
- Signature requirements: insurance_authority or admin key must sign
- Authority requirements: ADMIN-GATED
- Other guards: config.insurance_authority check enforced
- Reachability verdict: ADMIN (not a risk path for permissionless drain)
- Bypass conditions: none identified on this path
- Severity if reachable improperly: N/A — properly gated
```

---

## Critical Negative Result: No Floor Enforcement Found

Searching for a floor / minimum balance guard on insurance draws:

```
grep -n "insurance_floor\|min_insurance\|insurance_min\|floor" \
  /root/audit_runs/percolator-live/target/engine/src/percolator.rs
```

**No floor constant or floor-check was found in the engine source.** Insurance draws in settlement and liquidation paths use arithmetic that stops at zero (via `min()` or `saturating_sub`) but do **not** enforce a configurable floor above zero.

This means the claim in the hypothesis — *"cannot drain below its initial floor"* — has **no code-level enforcement** on the permissionless paths.

---

## Summary

| Metric | Value |
|---|---|
| Total reachable paths | 3 |
| PERMISSIONLESS paths | 1 (Crank) |
| SIGNER-but-open paths | 1 (Liquidate — any signer qualifies) |
| ADMIN-only paths | 1 |
| Floor enforcement found | **NONE** |

**Strongest bypass candidate:** Self-liquidation loop via Path #2. Attacker opens position, manipulates to underwater state, liquidates repeatedly; each cycle draws insurance with no floor stopping premature exhaustion.

---

## Verdict

**Claim is FALSE.**

- Confidence: **MED** (the codebase grep confirms no floor guard; the exact line numbers for insurance arithmetic require Layer-2 PoC confirmation of the loop economics)
- Paths #1 and #2 are reachable without admin authority
- No insurance floor is enforced at the code level on these paths
- Finding should be promoted to **Layer-2 empirical PoC** to demonstrate economic feasibility of insurance exhaustion