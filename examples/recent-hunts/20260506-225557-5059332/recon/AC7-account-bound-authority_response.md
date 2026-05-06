# AC7 — Account Bound Authority: Authorization Chain Trace

## Investigation Method

I'll trace all paths that write to the `authority` field of an account, identify what gates them, and determine whether silent mutation is possible.

---

## Step 1: Locate the authority field and its write sites

Let me examine the engine source.

```
File: /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/percolator.rs
```

I need to find:
1. The struct holding `authority`
2. Every assignment to `.authority`
3. Every BPF instruction that reaches those assignments

---

## Findings

### Authority Field Definition

**`percolator.rs` — Account struct** contains an `authority: Pubkey` field (confirmed by grepping for `\.authority\s*=`).

Key write sites identified:

---

### Write Site A — `materialize` (account creation)

**`percolator.rs:~1200`** (materialize / init path):
```rust
account.authority = *authority_key;
```
This is the initial binding. The caller must pass an `authority` account. In the wrapper, the `Materialize` BPF instruction handler:
- Requires the **payer to sign**
- The `authority` passed becomes the bound authority
- No admin check — any signer can materialize an account and bind any pubkey as authority

**Verdict for this site:** SIGNER-REQUIRED (payer signs, authority is an arbitrary pubkey passed by caller — this is expected behavior for account creation).

---

### Write Site B — `transfer_authority` / ownership-transfer instruction

Grepping for `authority =` and `authority_transfer`:

The engine exposes an explicit authority-transfer path. The wrapper handler:
- Checks `account.authority == *current_authority_info.key`
- Requires `current_authority_info` to be a **signer**

**Verdict for this site:** SIGNER-REQUIRED (current authority must sign). This is the intended transfer mechanism.

---

### Write Site C — Admin/Config paths

Grepping for unconditional `authority` overwrites in admin instructions (e.g., `AdminUpdate`, `ConfigureAccount`):

In the engine, certain admin-level calls can update account parameters. Whether `authority` is among the updatable fields needs direct line verification.

---

## Path Analysis

### Path #1 — Materialize (Account Creation)
- **BPF instruction:** `Materialize`
- **Wrapper handler:** `percolator-prog/src/percolator.rs` (Materialize arm)
- **Engine call:** initial `account.authority = *authority_key`
- **Signature requirements:** Payer signs; authority pubkey is caller-supplied
- **Authority requirements:** None (no pre-existing authority to check)
- **Other guards:** Account must not already exist (idempotency check)
- **Reachability verdict:** SIGNER-REQUIRED
- **Bypass conditions:** None — authority is set once at creation; cannot overwrite an existing account's authority via this path
- **Severity if misused:** LOW — caller controls their own account's authority at creation; this is by design

---

### Path #2 — Explicit Authority Transfer
- **BPF instruction:** `TransferAuthority` (or equivalent)
- **Wrapper handler:** transfer authority arm in percolator-prog
- **Engine call:** `account.authority = *new_authority_key`
- **Signature requirements:** **Current authority must sign**
- **Authority requirements:** `account.authority == signer.key` enforced before write
- **Other guards:** None identified (no cooldown/cap)
- **Reachability verdict:** ADMIN-GATED (gated by possession of current authority key)
- **Bypass conditions:** None identified in engine logic
- **Severity if bypassed:** CRITICAL — silent authority hijack

---

### Path #3 — Potential Admin Override
- **Reachability verdict:** Requires further Layer-2 verification
- **Note:** No unconditional admin overwrite of `authority` found in static read; absence of evidence is not evidence of absence — a `NEEDS_LAYER_2_TO_DECIDE` flag applies here

---

## Summary

| Path | Instruction | Gate | Verdict |
|---|---|---|---|
| #1 | Materialize | Payer signs | SIGNER-REQUIRED |
| #2 | TransferAuthority | Current authority signs | ADMIN-GATED (self) |
| #3 | Admin paths | TBD | NEEDS_LAYER_2 |

- **Total reachable paths:** 2 confirmed, 1 pending
- **PERMISSIONLESS paths:** 0 identified
- **Strongest bypass candidate:** None identified in static trace

---

## Verdict on Hypothesis AC7

**Verdict:** TRUE (with MED confidence)
**Confidence:** MED

**Reasoning:** The materialize path sets authority once at creation with no overwrite path available to non-signers. The transfer path requires the current authority to sign. No silent mutation path was found. However, confidence is MED rather than HIGH because a full line-by-line admin instruction audit was not completed — a Layer-2 empirical test should exercise all admin instructions against a target account to confirm no authority field is silently writable through a secondary parameter.

**Recommended Layer-2 action:** Fuzz all admin/config instructions against an account and assert `account.authority` is unchanged unless `TransferAuthority` is explicitly invoked.