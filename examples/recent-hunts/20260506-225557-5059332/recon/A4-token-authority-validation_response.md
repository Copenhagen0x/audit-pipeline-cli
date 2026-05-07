# A4 — Token Authority Validation Audit

## Investigation: Token transfers always validate source token account owner

---

## Step 1: Locate token transfer sites in the codebase

Let me systematically search the source files.

```
Repository root:
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

I'll examine the source files to find all token transfer invocations and their authority checks.

---

## File inventory

The engine repo (`aeyakovenko-percolator-5059332`) contains:
- `src/percolator.rs` — main engine

The wrapper repo is listed as the same path (both point to the same clone). I will look at what's present.

---

## Searching for token transfer patterns

Key patterns to find:
- `transfer` (SPL token CPI)
- `invoke` / `invoke_signed`
- `token_account`, `token_program`
- Authority checks: `.owner`, `is_signer`, `authority`

---

## Findings from source examination

### Engine: `src/percolator.rs`

After reading the percolator engine, this is a **Rust library** (not a BPF program itself). It contains pure math/state logic. It does **not** issue SPL token CPIs directly — it manipulates internal accounting state (`Balance`, vault balances, etc.).

The BPF wrapper (percolator-prog) is what actually calls `invoke`/`invoke_signed` for token transfers. However, both local clone paths point to the same engine repository. The wrapper source (`percolator-prog`) does not appear to be present at the specified local path.

---

## What IS present: internal transfer accounting

Within `src/percolator.rs`, token-like transfers are handled as **internal balance mutations**, not on-chain CPI calls at this layer. Key observations:

### `deposit` / `withdraw` style balance mutations
The engine tracks balances internally. Authority validation for who can mutate whose balance is the relevant check.

### Searching for explicit authority validation patterns

In the engine's internal accounting:
- Balance updates are gated by index/slot logic, not by checking a token account owner field
- The engine trusts that the **wrapper layer** has already validated authority before calling engine functions

This is the critical architectural split: **the engine assumes pre-validation by the wrapper**.

---

## Authorization chain for token-adjacent operations

Since the wrapper source is not accessible in the local clone, I can only assess the engine side:

```
Path #1 (Engine-internal balance mutation)
- BPF instruction: Unknown (wrapper not in local clone)
- Wrapper handler line: NOT ACCESSIBLE
- Engine call line: percolator.rs — balance mutation functions
- Signature requirements: Delegated to wrapper layer (engine does not check)
- Authority requirements: Engine does NOT enforce — relies on wrapper pre-check
- Other guards: None at engine layer
- Reachability verdict: NEEDS_LAYER_2_TO_DECIDE
- Bypass conditions: If wrapper fails to validate authority before calling engine, engine will execute the transfer accounting unconditionally
- Severity if reachable improperly: HIGH (unauthorized balance manipulation)
```

---

## Verdict on the Hypothesis

| Sub-claim | Evidence | Verdict | Confidence |
|---|---|---|---|
| Engine validates source token account owner | Engine does **not** contain SPL token CPI calls or `.owner` checks against signers | **FALSE at engine layer** | HIGH |
| Wrapper validates source token account owner | Wrapper source not accessible in local clone | NEEDS_LAYER_2_TO_DECIDE | LOW |
| End-to-end: transfers always validate authority | Cannot confirm without wrapper source | NEEDS_LAYER_2_TO_DECIDE | LOW |

---

## Key Finding

**The engine (`percolator.rs`) contains no token account owner validation.** It performs internal accounting mutations and explicitly delegates authority validation responsibility to the BPF wrapper layer. This is an architectural pattern where:

1. The engine is a pure logic library — it trusts its caller
2. The wrapper must enforce all signer/authority checks before invoking engine functions

**The hypothesis "token transfers always validate source token account owner" CANNOT be confirmed TRUE without auditing the wrapper (`percolator-prog`) source, which was not accessible at the specified local path.**

---

## Recommended Next Steps

- **Layer 2**: Obtain and audit `percolator-prog` wrapper source (the correct local clone path appears to be missing or mis-specified)
- Specifically search wrapper for: `invoke_signed` calls, `authority` account validation against `is_signer`, and any `token::transfer` CPI constructors
- Verify whether each BPF instruction handler checks `ctx.accounts.source.owner == expected_authority` before calling into the engine

**Overall verdict: NEEDS_LAYER_2_TO_DECIDE** — the engine layer definitively does not validate token account ownership; whether the wrapper compensates is unverifiable from the provided file paths.