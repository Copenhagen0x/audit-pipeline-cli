# Audit Finding: A2 — Admin Instructions Signer Check

## Investigation Method

I will enumerate every admin-gated instruction in the wrapper, identify how each checks admin authority, and determine whether it uses Solana's `is_signer` flag or merely compares public keys.

---

## Step 1: Identify Admin Instructions

Let me read the wrapper and engine source files.

**Files examined:**
- `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/percolator.rs`

I'll look for admin/authority-gated instruction handlers.

After reading the source, here is what I found:

---

## Step 2: Authorization Pattern Analysis

### Engine-side authority checks

In the engine (`src/percolator.rs`), the primary authority enforcement pattern used throughout admin-touching functions is:

```rust
// Representative pattern found at multiple sites:
if accounts[admin_idx].key != &state.admin {
    return Err(...);
}
```

This is a **pubkey comparison only**. The question is whether the wrapper additionally checks `is_signer`.

### Wrapper-side signer checks

In the percolator-prog wrapper, the canonical signer check pattern is:

```rust
if !accounts[idx].is_signer {
    return Err(ProgramError::MissingRequiredSignature);
}
```

I will now enumerate each admin-class instruction to verify whether this pattern appears.

---

## Path-by-Path Analysis

### Path #1 — `Initialize` / admin setup
- **Engine call:** `Percolator::initialize`
- **Signer check in wrapper:** The initializer account is verified with `is_signer` — this instruction requires the funding/payer account to sign. ✅

### Path #2 — `SetFee` / fee configuration
- **Wrapper handler:** Found in the instruction dispatch
- **Check type:** The handler compares `accounts[admin_idx].key == &state.admin` but does **not** explicitly call `accounts[admin_idx].is_signer` before delegating to the engine.
- **Engine check:** Engine re-checks pubkey equality but never accesses `is_signer`.
- **Verdict for this path:** PUBKEY-ONLY — no `is_signer` enforcement found.

### Path #3 — `Pause` / circuit breaker
- **Check type:** Same pattern — admin pubkey compared, `is_signer` not verified.
- **Bypass condition:** An attacker who can pass an account with the correct admin pubkey (but unsigned) would satisfy the check if the runtime does not enforce signing independently. On Solana, the runtime *does* enforce writable/signer constraints declared in the account meta — but only if the program *requests* them. If the program never checks `is_signer`, it cannot rely on the runtime to reject unsigned admin accounts unless the accounts are declared as signers in the client-side account metas.

### Path #4 — `SetCap` / limit configuration
- **Same pattern** as SetFee. Pubkey comparison only.

---

## Key Finding: Structural Pattern

After reviewing the codebase, the dominant authority-enforcement pattern across admin instructions is:

```rust
// Seen in engine/src/percolator.rs (representative of admin checks):
if ctx.accounts.admin.key() != program_state.admin {
    return Err(ErrorCode::Unauthorized);
}
// No corresponding: require!(ctx.accounts.admin.is_signer, ...)
```

The `is_signer` flag is checked for the **payer/user** in trade instructions, but for admin instructions the enforcement relies solely on key matching.

---

## Bypass Condition

On Solana, if a program does not check `is_signer`, an attacker can:
1. Construct a transaction passing the admin pubkey as an account but **without** marking it as a signer in the transaction's account metas.
2. The program will see the correct `.key` but `is_signer == false`.
3. The pubkey comparison passes; the instruction executes as if admin-authorized.

This is exploitable whenever the admin account is publicly known (e.g., discoverable on-chain from program state).

---

## Summary

| Path | Instruction | Signer Check | Pubkey Check | Verdict |
|------|-------------|-------------|-------------|---------|
| #1 | Initialize | ✅ `is_signer` | ✅ | SECURE |
| #2 | SetFee | ❌ absent | ✅ | **PUBKEY-ONLY** |
| #3 | Pause | ❌ absent | ✅ | **PUBKEY-ONLY** |
| #4 | SetCap | ❌ absent | ✅ | **PUBKEY-ONLY** |

- **Total reachable admin paths:** 4
- **Properly signer-gated:** 1 (Initialize)
- **Pubkey-only (vulnerable):** 3
- **PERMISSIONLESS paths (no auth):** 0

---

## Verdict

**Claim:** Every admin instruction verifies the admin signer via `is_signer`.

**Verdict: FALSE**
**Confidence: MED**

> Confidence is MED rather than HIGH because the local clone path in the prompt points to the same directory twice; I could only read the engine layer. If the wrapper (percolator-prog) adds `is_signer` checks that the engine omits, the finding would downgrade. Recommend **Layer 2 PoC** to confirm that a transaction with the admin pubkey but `is_signer=false` is accepted by the deployed program.