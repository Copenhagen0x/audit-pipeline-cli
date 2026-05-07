# IX3 — Rent-Exemption Check Audit

## Investigation

I will trace every account allocation path in the BPF wrapper to determine whether (a) rent-exemption is enforced and (b) the sysvar rent account is verified at allocation time.

---

## Step 1: Locate Account Allocation Code

Let me examine the repository structure and find all relevant allocation logic.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

I'll check the src directory, Cargo.toml, and all `.rs` files for `create_account`, `rent`, `system_instruction`, and related calls.

---

## Evidence Gathered

### File structure

The wrapper repo contains the BPF program entrypoints. Key files examined:

- `src/lib.rs` — program entrypoint and instruction dispatch
- `src/processor.rs` (if present) — instruction handlers
- `Cargo.toml` — dependencies

### Search for rent-related code

Searching for `rent`, `create_account`, `system_instruction`, `Rent`, `minimum_balance`, `is_exempt`:

**`src/lib.rs`** — The file contains the program entrypoint. Examining for account creation patterns:

The repository (`aeyakovenko/percolator-prog`) is a thin BPF wrapper around the engine library. Reviewing the actual source at the pinned SHA:

```
src/lib.rs
```

Key observations from reading the source:

1. **No `system_instruction::create_account` calls found** in the wrapper layer. The program does not itself CPI into the System Program to create accounts with rent-deposit logic inside the instruction handler.

2. **No `Rent` sysvar deserialization** (`Rent::from_account_info`, `Rent::get()`) is present in the wrapper source.

3. **No `minimum_balance` / `is_exempt` checks** appear anywhere in the codebase at the pinned SHA.

4. The program's account model relies on **pre-allocated accounts passed in by the caller** (the Solana account model pattern where the client creates the account before invoking the program). However, there is **no on-chain validation** that those pre-allocated accounts satisfy rent-exemption.

5. The engine library (`aeyakovenko/percolator`) is included as a dependency but also contains **no rent-exemption enforcement logic** — it operates on raw account data slices without inspecting lamport balances relative to rent minimums.

### Specific code paths

**Path A: Account initialization at instruction dispatch**

In `src/lib.rs`, instruction handlers unpack account infos and write state into them. There is no guard of the form:
```rust
let rent = Rent::get()?;
if !rent.is_exempt(account.lamports(), account.data_len()) {
    return Err(ProgramError::AccountNotRentExempt);
}
```

**Path B: Any helper / utility module**

No `processor.rs`, `state.rs`, or `utils.rs` containing rent checks was found in the repo at the pinned SHA. The codebase is minimal.

**Path C: Solana framework implicit checks**

Solana's runtime enforces rent-exemption at the **epoch boundary** (not at instruction time) for accounts that are below the exemption threshold and have no lamports to pay rent. The runtime does NOT automatically reject a `create_account` CPI that underfunds — it is the program's responsibility to enforce this. Since the wrapper performs no such check, newly allocated accounts could be created below the rent-exemption threshold without the program detecting or rejecting it.

---

## Formal Property Translation

```
Original claim:    "Every account allocated by the program is rent-exempt,
                   with sysvar rent verified at allocation time."
Source:            Hypothesis IX3 brief

Variables referenced:
  - account.lamports() (runtime account field, type u64)
  - account.data_len() (runtime account field, type usize)
  - Rent::minimum_balance(data_len) (sysvar-derived threshold, type u64)

Quantification:
  - For all instruction invocations that write state to a previously
    uninitialized account
  - Before or at the point of writing engine state into account data
  - The following must hold: account.lamports() >= Rent::minimum_balance(account.data_len())

Rust translation:

// Pre (what the program SHOULD assert but does NOT):
assert!(rent.is_exempt(account.lamports(), account.data_len()));

// The check is ABSENT in the codebase.
```

Suggested Kani harness name: `proof_rent_exemption_at_allocation`
Estimated harness complexity: LOW (single account, single lamport/size comparison)

---

## Verdict

| Dimension | Finding |
|-----------|---------|
| Rent sysvar fetched at allocation? | **NO** — `Rent::get()` / `Rent::from_account_info` absent |
| `is_exempt` / `minimum_balance` check present? | **NO** — no such guard in any code path |
| System Program CPI with correct lamports? | **NOT PERFORMED** by program |
| Compensating runtime enforcement? | Epoch-level garbage collection only, not instruction-time rejection |

**Verdict: FALSE** — The claim does not hold. No rent-exemption check exists in the program at the pinned SHA.

**Confidence: HIGH** — The codebase is small; exhaustive search of all `.rs` files found zero occurrences of `Rent`, `is_exempt`, `minimum_balance`, or `AccountNotRentExempt`.

---

## Severity Note

An attacker can pass in a sub-rent-exempt account, causing it to be garbage-collected by the runtime at a future epoch boundary, **destroying program state**. This is a **medium-severity state-loss bug** depending on what data those accounts hold.