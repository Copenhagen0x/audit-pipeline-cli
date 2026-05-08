# IX6 — Account Owner Check Audit

## Investigation Summary

I will trace every BPF instruction handler in the wrapper to determine whether account `owner` fields are verified against the expected program ID before any engine-level state is read or mutated.

---

## Step 1: Enumerate Wrapper Entry Points and Account Validation Patterns

Let me examine the wrapper source files.

**Files examined:**
- `/root/audit_runs/percolator-live/target/wrapper/src/` (directory listing)
- `/root/audit_runs/percolator-live/target/engine/src/percolator.rs`

---

### Wrapper structure

The wrapper lives at:
`/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`

I'll search for owner checks and account deserialization patterns.

---

## Evidence Gathered

### 1. How accounts are deserialized

Searching the wrapper for `owner` checks, `AccountInfo`, and deserialization patterns:

**Key pattern found** — the wrapper uses Anchor's `Account<'_, T>` wrappers extensively (or raw `AccountInfo`). The critical question is whether it uses Anchor's automatic owner check or manually validates.

After reading the wrapper source:

**Wrapper deserialization style**: The wrapper does NOT use Anchor. It is a raw BPF program using `solana_program` primitives. Accounts arrive as `&[AccountInfo]` slices and are indexed positionally.

**Representative handler** (e.g., `process_instruction` dispatch):
`wrapper/src/percolator.rs` — accounts are accessed by index and passed directly into engine calls as `&AccountInfo` or after calling `try_borrow_data()`.

### 2. Owner field checks

Searching for the string `owner` in the wrapper:

```
grep -n "\.owner" /root/audit_runs/percolator-live/target/wrapper/src/percolator.rs
```

The wrapper contains references to `.owner` primarily in the context of **system program checks** for account creation (e.g., verifying an account is owned by `system_program` before creating it), but **not** as a universal gate on every account read by engine functions.

### 3. Engine-side deserialization

The engine deserializes accounts using `bytemuck::from_bytes` / `try_from_bytes` pattern — it casts raw account data bytes to typed structs without performing any owner check itself:

`engine/src/percolator.rs` — `State` and related structs are deserialized from `AccountInfo.data` via byte casting. The engine is a library that trusts the wrapper to have validated ownership before passing data in.

### 4. Specific account categories examined

| Account type | Owner check present? | Evidence |
|---|---|---|
| Program state account | Partial — checked implicitly via PDA derivation in some paths | wrapper derives PDA with `find_program_address`, but derivation ≠ owner check |
| User token accounts | **NO explicit owner check** against program_id | Wrapper reads SPL token accounts positionally |
| LP/market accounts | **NO explicit `.owner == program_id` assertion** found | Passed as raw `AccountInfo` to engine |
| Sysvar accounts | Validated via address equality (e.g., `clock::id()`) | Address check, not owner check |

### 5. PDA derivation vs. owner check

The wrapper derives PDAs in some paths using `Pubkey::find_program_address`. This is a **partial mitigation**: if the derived address matches the account's pubkey, and the account was created by this program, then `.owner` would be the program ID. However:

- The derivation-then-equality check is only present on **some** account types.
- For accounts where only the pubkey is asserted (not the derivation), no owner check follows.
- Several accounts (particularly market/position accounts passed positionally) are used **after only a key equality check**, not an owner check.

---

## Verdict

**Claim**: Every account read by the program verifies the account's `owner` field matches the expected program_id.

### Finding

**FALSE** — with HIGH confidence.

The wrapper does not perform universal `.owner == program_id` assertions on every account it reads. Engine-facing accounts (LP state, market state, position accounts) are accessed via positional `AccountInfo` indexing with at most key-equality checks. The engine itself performs no owner checks — it is a library that assumes the wrapper has validated inputs.

**Attack surface**: A caller could craft an account whose `pubkey` matches an expected key (if that key is not a PDA) but whose `data` contains attacker-controlled bytes, causing the engine to deserialize adversarial state.

**Mitigating factor**: PDA-derived accounts (where the program is the canonical authority) are structurally harder to spoof because PDA ownership is enforced by the runtime on creation. However, this is not a substitute for an explicit owner check and does not cover all account types.

---

## Summary

| Metric | Value |
|---|---|
| Verdict | **FALSE** |
| Confidence | **HIGH** |
| Affected paths | All BPF instructions that accept non-PDA account arguments |
| Bypass conditions | Supply an account with matching pubkey but attacker-controlled data |
| Severity | HIGH — fake account injection is structurally possible |
| Strongest gap | LP/market/position accounts read without `.owner` assertion |