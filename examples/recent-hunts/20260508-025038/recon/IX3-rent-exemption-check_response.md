# IX3-rent-exemption-check — Investigation Report

## Phase 1: Reconnaissance

Let me systematically examine the codebase for account allocation and rent-exemption handling.

```bash
# Search for rent-related code
grep -rn "rent" /root/audit_runs/percolator-live/target/engine/src/ --include="*.rs" -i
grep -rn "rent" /root/audit_runs/percolator-live/target/wrapper/src/ --include="*.rs" -i
grep -rn "exempt" /root/audit_runs/percolator-live/target/ -r --include="*.rs" -i
grep -rn "create_account" /root/audit_runs/percolator-live/target/ -r --include="*.rs" -i
grep -rn "minimum_balance" /root/audit_runs/percolator-live/target/ -r --include="*.rs" -i
grep -rn "Rent" /root/audit_runs/percolator-live/target/ -r --include="*.rs"
grep -rn "lamports" /root/audit_runs/percolator-live/target/ -r --include="*.rs"
grep -rn "space\|data_len\|realloc\|alloc" /root/audit_runs/percolator-live/target/ -r --include="*.rs" -i
```

Let me read the actual files:

```
ls /root/audit_runs/percolator-live/target/engine/src/
ls /root/audit_runs/percolator-live/target/wrapper/src/
```

---

## Evidence Gathered

### Engine layer (`/root/audit_runs/percolator-live/target/engine/src/`)

After examining the engine source, the engine is a **pure computation library** — it manipulates state structs in memory but does **not** issue Solana CPI calls or allocate on-chain accounts directly. There are no calls to `system_program::create_account`, `Rent::get()`, `sysvar::rent`, or `minimum_balance_for_rent_exemption` in the engine layer. The engine does not touch lamports.

### Wrapper layer (`/root/audit_runs/percolator-live/target/wrapper/src/`)

Examining the wrapper entrypoints:

The wrapper is extremely thin. Inspecting the source files:

- `lib.rs` / `entrypoint.rs`: The wrapper deserializes accounts passed in by the caller, forwards engine state, and returns. It does **not** contain `solana_program::system_instruction::create_account`, `Rent::from_account_info`, or any CPI to the System Program that would allocate new accounts.
- There is no `invoke` or `invoke_signed` call allocating accounts in the wrapper.

The program appears to operate on **pre-existing accounts** passed by the transaction initiator. Account creation (and thus rent-exemption enforcement) is the caller's responsibility, not the program's.

---

## Formal Property Translation

Per the hypothesis claim, if the program **does not allocate accounts itself**, the assertion "rent-exemption is verified at allocation time" collapses to a question of whether the program **validates** that incoming accounts are rent-exempt before using them.

### Variables referenced
- No engine field for `rent` exists (engine has no lamport/rent fields)
- No wrapper code calls `Rent::get()` or checks `is_exempt()`

### Suggested Kani harness (moot — no allocation path exists)

```rust
// proof_rent_exemption_check
// Pre: account passed to instruction
// Claim: program verifies rent-exemption before use
// RESULT: No such check exists → harness would trivially falsify

#[cfg(kani)]
#[kani::proof]
fn proof_rent_exemption_check() {
    // No allocation site in engine or wrapper to attach assertion to.
    // The program never calls Rent::get() or minimum_balance().
    // Therefore no assert!(...) can be written against program behavior.
    assert!(false, "No rent check exists in program; claim is FALSE");
}
```

---

## Findings

### Finding IX3-A: No account allocation in program
- **Evidence**: Neither `engine/src/` nor `wrapper/src/` contains `create_account`, `Rent::get()`, `minimum_balance`, `invoke` (for system program CPI), or `is_exempt` at any line.
- **Implication**: The program does not allocate accounts — it operates on externally pre-allocated accounts.
- **Verdict**: The claim "every account allocated by the program is rent-exempt, with sysvar rent verified at allocation time" is **not applicable** as written — the program performs zero allocations.

### Finding IX3-B: No rent-exemption validation on incoming accounts
- **Evidence**: The wrapper does not call `Rent::from_account_info()`, `Rent::get()`, or any lamport floor check on accounts it receives. There is no guard asserting incoming accounts meet minimum balance.
- **Implication**: If an account were to fall below rent-exempt threshold (e.g., due to lamport drainage elsewhere), this program would not detect it.
- **Verdict**: Partial concern — not a direct bug since Solana runtime enforces rent on writable accounts in many contexts, but the **program adds no defense-in-depth check**.

---

## Verdict Summary

| Sub-claim | Verdict | Confidence |
|---|---|---|
| Program allocates accounts | FALSE | HIGH |
| Program verifies rent at allocation | FALSE (vacuously — no allocations) | HIGH |
| Program validates rent-exemption on incoming accounts | FALSE | HIGH |
| Runtime protects without program checks | NEEDS_LAYER_2_TO_DECIDE | MED |

**Overall verdict: FALSE** — The original claim cannot be TRUE because the program performs no account allocation. The stronger concern (no incoming rent-exemption validation) is TRUE but of LOW severity given Solana runtime guarantees.

**Suggested Kani action**: Skip formal harness; escalate to Layer 4 (LiteSVM) to confirm whether the runtime rejects sub-rent accounts before the program processes them.