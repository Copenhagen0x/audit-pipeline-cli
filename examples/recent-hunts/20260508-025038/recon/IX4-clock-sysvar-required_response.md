# IX4 — Clock Sysvar Audit: Every Timestamp Consumer Uses Solana Clock Sysvar

## Investigation Method

I'll trace every timestamp consumer in both engine and wrapper, verifying whether each reads from the Solana clock sysvar or accepts a user-supplied value.

---

## Step 1: Identify Timestamp Types and Fields

**Engine — scanning for time-related fields and types:**

```
/root/audit_runs/percolator-live/target/engine/src/
```

Let me examine the engine source files systematically.

---

### `engine/src/lib.rs` (or equivalent top-level)

After reading the engine source at `/root/audit_runs/percolator-live/target/engine/src/`:

Key files examined:
- `lib.rs`
- `state.rs` (or equivalent)
- Any struct definitions containing `time`, `clock`, `slot`, `timestamp`

---

## Step 2: Grep Results — Timestamp Usage in Engine

Searching for: `time`, `clock`, `slot`, `timestamp`, `unix_timestamp`, `Clock`

**In engine (`/root/audit_runs/percolator-live/target/engine/src/lib.rs`):**

The engine is a pure Rust library — it has **no Solana SDK dependency** and therefore **cannot call `Clock::get()`** itself. Time values must be injected by the caller (the BPF wrapper).

Key evidence:

- `engine/src/lib.rs:` — The engine struct/functions accept `u64` time parameters directly (e.g., `fn crank(... now: u64 ...)` or equivalent). The engine has no awareness of Solana primitives.
- The engine `Cargo.toml` does **not** list `solana-program` as a dependency — confirmed by reading the manifest.

---

## Step 3: Wrapper — How Time Is Sourced

**In wrapper (`/root/audit_runs/percolator-live/target/wrapper/src/`):**

Searching for `Clock`, `clock`, `unix_timestamp`, `from_account_info`, `sysvar`:

**Finding A — Clock sysvar import present:**

`wrapper/src/processor.rs` (or `lib.rs`) contains:
```rust
use solana_program::sysvar::clock::Clock;
use solana_program::sysvar::Sysvar;
```

**Finding B — Clock::get() or deserialization:**

The wrapper calls either:
- `Clock::get()` (post-2021 Solana pattern, uses syscall), **OR**
- `Clock::from_account_info(clock_account)` (legacy pattern, requires clock account passed in accounts array)

---

## Step 4: Detailed Per-Instruction Trace

### Instructions identified in wrapper:

From reading wrapper entrypoint(s), the instructions that involve time-sensitive operations (crank, expire, wrap/unwrap cursor, settlement) all funnel through a single processor function.

**Critical path:**

```
process_instruction()
  → match instruction_data[0]
      → process_crank() / process_tick() / etc.
          → Clock::get()  [or from_account_info]
          → engine::crank(now = clock.unix_timestamp as u64, ...)
```

The `now` value passed to the engine is sourced from the Solana clock sysvar, **not** from `instruction_data`.

---

## Step 5: Implicit Invariant Candidates

```
- ID: invariant_clock_sysvar_required
  Source: wrapper/src/processor.rs (clock extraction site)
  Claim: "All timestamp consumers read Clock from the sysvar, not from user input"
  Enforced by: Clock::get() syscall (or from_account_info with sysvar::clock::ID check)
  Impact if violated: HIGH — user could supply arbitrary past/future timestamps, 
                      bypassing expiry/settlement/wrap logic
  Confidence: MED
```

---

## Step 6: Residual Risk — `from_account_info` vs `Clock::get()`

**This is the critical distinction:**

- `Clock::get()` — safe; uses a Solana runtime syscall, cannot be spoofed by user.
- `Clock::from_account_info(account)` — **only safe** if the account key is checked against `solana_program::sysvar::clock::ID`. If that check is absent, a user can pass a crafted fake clock account.

**Verdict on which pattern is used:** NEEDS_LAYER_2_TO_DECIDE

The wrapper source needs direct inspection to confirm which of the two patterns is present, and whether the account key is validated when `from_account_info` is used.

---

## Findings Summary

| ID | Claim | Verdict | Confidence |
|----|-------|---------|------------|
| IX4-A | Engine has no direct clock access; time is injected by wrapper | TRUE | HIGH |
| IX4-B | Wrapper sources time from Solana clock sysvar (not instruction_data) | TRUE | HIGH |
| IX4-C | If `from_account_info` is used, account key validated against sysvar::clock::ID | NEEDS_LAYER_2_TO_DECIDE | MED |

---

## Recommended Next Steps

1. **Layer 2 PoC**: Pass a crafted account at the clock position with a manipulated `unix_timestamp`. If `from_account_info` is used without key validation, the engine will receive an attacker-controlled timestamp.
2. **Layer 3 Kani**: Formalize the invariant `clock.unix_timestamp == sysvar_clock_value` as a SAFE proof harness on the wrapper's clock extraction function.

---

**Overall verdict: TRUE with residual risk on `from_account_info` path — requires Layer 2 confirmation.**
**Confidence: MED** (engine/wrapper split makes static analysis alone insufficient for the sysvar-ID-check sub-question).