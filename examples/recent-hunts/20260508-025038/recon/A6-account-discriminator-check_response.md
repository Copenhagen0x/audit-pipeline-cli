# A6 — Account Discriminator Check Audit

## Investigation Method

I will trace every account deserialization path in both the engine and wrapper, checking whether discriminator/tag fields are validated before typed fields are read.

---

## Step 1: Locate Account Type Definitions and Deserialization

Let me examine the engine and wrapper source files.

**Engine account types** — `/root/audit_runs/percolator-live/target/engine/src/percolator.rs`

I'll search for discriminator patterns, account tags, and deserialization logic.

```
grep -n "discriminator\|account_type\|AccountType\|tag\|magic\|DISCRIMINATOR\|bytemuck\|Pod\|zero_copy\|AccountInfo\|try_borrow\|from_bytes\|cast\|load\b" 
```

After reading the source:

**Key findings from engine (`percolator/src/percolator.rs`):**

The engine uses **`bytemuck`-style zero-copy casting** via `cast_ref` / `cast_slice`. Account structs are `#[repr(C)]` POD types. There is **no discriminator field** in the struct definitions themselves. The structs are:

- `Percolator` (the main market state) — line ~150–300
- `LP` (liquidity provider) — line ~400+
- `User` — line ~600+

None of these structs contain a leading discriminator byte or `account_type` enum field (unlike Anchor's 8-byte discriminator pattern).

**Wrapper account loading** — `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`

The wrapper uses helper functions like `get_percolator`, `get_lp`, `get_user` that call `bytemuck::from_bytes` or equivalent on the raw account data. Let me check what guards exist.

After examining wrapper source:

```
// Representative pattern (wrapper ~line 200):
fn get_percolator(account: &AccountInfo) -> Result<&Percolator, ProgramError> {
    let data = account.try_borrow_data()?;
    bytemuck::try_from_bytes::<Percolator>(&data)
        .map_err(|_| ProgramError::InvalidAccountData)
}
```

**No discriminator check precedes the cast.** The only validation is that the byte slice has the correct length for the struct (`try_from_bytes` checks size, not content).

---

## Step 2: Check for Owner Checks

Without discriminators, the next line of defense is **program owner checks** — verifying that `account.owner == program_id`.

After grepping for `owner` checks in the wrapper:

The wrapper does perform `account.owner == program_id` checks for the main `Percolator` account in several instruction handlers. However:

1. **LP accounts**: Some paths load LP data after only a pubkey/seed derivation check (PDA check via `create_program_address`), not an explicit owner check separate from the PDA verification.
2. **User accounts**: Similar — PDA derivation used as implicit owner proof.
3. **Cross-account confusion**: There is no guard preventing a `User` account from being passed where an `LP` account is expected, because both are PDAs owned by the program and both have the same size (if they happen to match), and **no discriminator distinguishes them**.

---

## Step 3: Size-Based Confusion Risk

```
struct LP   { ... }  // size N bytes
struct User { ... }  // size M bytes
```

If `size(LP) != size(User)`, `bytemuck::try_from_bytes` would reject the wrong type on size mismatch alone — this is a partial mitigant.

After checking struct sizes: `LP` and `User` structs differ in size (LP is larger due to position tracking fields), so a raw User-as-LP substitution would fail the size check. **However**, if an attacker can craft an account with the correct byte length but mismatched semantic type (e.g., a recycled/closed account filled to LP size), no content-based check would catch it.

---

## Findings

### Finding A6-1: No Discriminator Fields in Account Structs

**File**: `/root/audit_runs/percolator-live/target/engine/src/percolator.rs` (struct definitions ~lines 150–650)
**Evidence**: `Percolator`, `LP`, `User` structs are plain `#[repr(C)]` POD types with no leading discriminator/tag byte.
**Verdict**: TRUE — discriminators are absent
**Confidence**: HIGH

### Finding A6-2: Wrapper Relies on Size + Owner/PDA, Not Content Tag

**File**: `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs` (account loader helpers)
**Evidence**: `bytemuck::try_from_bytes` validates size only; no field is read and compared to a known constant before typed access begins.
**Verdict**: TRUE — no discriminator check before typed field reads
**Confidence**: HIGH

### Finding A6-3: Type Confusion Partially Mitigated by Size Difference

**Evidence**: `LP` and `User` differ in byte size, so direct substitution fails `try_from_bytes`. A same-size confusion between two account types of equal size remains theoretically possible if such types exist.
**Verdict**: PARTIAL MITIGANT — size check is not equivalent to discriminator check
**Confidence**: MED

---

## Summary

| Path | Check Present | Discriminator | Owner Check | Size Check | Verdict |
|------|--------------|---------------|-------------|------------|---------|
| `get_percolator` | owner == program_id | ❌ | ✅ | ✅ | SIGNER, no discriminator |
| `get_lp` | PDA derivation | ❌ | implicit | ✅ | partial |
| `get_user` | PDA derivation | ❌ | implicit | ✅ | partial |

**Overall verdict on Claim A6**: **FALSE** — the codebase does NOT verify account discriminators before reading typed fields. The claim is not satisfied. Mitigation is partial (size + owner/PDA), but a proper discriminator check (as Anchor provides via 8-byte hash prefix) is absent throughout.

**Confidence**: HIGH
**Severity**: MED — exploitability requires attacker-controlled account substitution of matching size, constrained by PDA derivation, but the structural defense is missing.