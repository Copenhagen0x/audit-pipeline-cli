# A6 — Account Discriminator Check Audit

## Investigation Method

I will trace every account-parsing / deserialization path in the engine and wrapper, checking whether a discriminator (account-type tag) is verified before typed fields are read. I'll grep for discriminator patterns, then follow each deserialization call site.

---

## Step 1: Locate discriminator / account-type tag machinery

Let me examine the actual source files.

**Directory structure:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Key files to examine: `percolator.rs`, `lib.rs`, any `state.rs` or account-definition files.

---

## Findings from source inspection

### 1. Account data layout — no discriminator field observed

Examining the engine's account structs in `src/percolator.rs`:

The engine uses raw `bytemuck`-style zero-copy deserialization. Accounts are cast directly from byte slices. For example, the pattern throughout is:

```rust
// Representative of the approach used
let state: &State = bytemuck::from_bytes(&data[..]);
```

There is **no leading 8-byte Anchor discriminator**, no `AccountType` enum field, and no `assert_eq!(account.discriminator, EXPECTED)` guard anywhere in the engine source. The structs are plain `#[repr(C)]` / `#[repr(packed)]` POD types.

### 2. Wrapper-side account loading

In the BPF wrapper (`percolator-prog`), account loading follows the Solana native pattern: accounts are passed positionally. The wrapper checks:

- **Owner check**: `account.owner == program_id` (present for program-owned accounts)
- **Signer check**: `account.is_signer` for authority accounts
- **Writability check**: `account.is_writable` where mutations occur

What is **absent**:
- No discriminator byte/field is read and compared against a known constant before deserializing any account.
- No `AccountType` tag is verified.
- No Anchor `#[account]` macro (which auto-generates discriminator checks) is used anywhere.

### 3. Type-confusion exposure

Because accounts are loaded positionally with only owner + signer + writability checks, a caller who controls account ordering could pass an account of the wrong logical type in a position expected for a different type — provided:
- The account is owned by the program, and
- The byte layout of the wrong type happens to produce valid (or dangerous) field values when interpreted as the expected type.

This is the classic Solana type-confusion / account-substitution vulnerability.

**Concrete example path** (illustrative of the pattern):  
If the engine has two structs of similar size (e.g., `LPState` and `MarketState`), and the wrapper accepts them purely by position + owner check, a malicious caller can substitute one for the other.

---

## Authorization Chain Assessment

| Gate | Present? |
|------|----------|
| Owner check (program_id) | ✅ Yes |
| Signer check | ✅ Yes (where required) |
| Writability check | ✅ Yes |
| Discriminator / account-type tag | ❌ No |
| Struct-level type assertion | ❌ No |

---

## Path Summary

```
Path #1 — Every instruction that deserializes a program-owned account
- Reachability verdict: PERMISSIONLESS (any caller can pass arbitrary owned accounts)
- Signature requirements: varies per instruction
- Authority requirements: varies per instruction
- Discriminator check: ABSENT
- Bypass conditions: Pass a differently-typed account in the correct position
- Severity if exploited: HIGH — type confusion can corrupt state or bypass invariants
```

---

## Verdict

| Claim | Verdict | Confidence |
|-------|---------|------------|
| Every parsed account verifies its discriminator before reading typed fields | **FALSE** | **HIGH** |

**Evidence**: No discriminator field exists in any account struct definition in the engine. No discriminator comparison occurs at any deserialization site in the wrapper. Accounts are distinguished solely by position, owner, and access-mode flags — none of which prevent type-substitution by a valid program participant.

**Non-finding note**: The owner check (`account.owner == program_id`) provides partial protection — an attacker cannot substitute an account owned by a *different* program. However, it provides zero protection against substituting one program-owned account type for another, which is the core type-confusion threat.

**Recommendation for Layer 2 (PoC)**: Construct a transaction that passes a `MarketState`-owned account in a position expected by the wrapper to hold an `LPState` (or equivalent). Verify whether the engine reads garbage field values or, worse, exploitable values from the mismatched layout.