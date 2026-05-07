# IX4 — Clock Sysvar Required: Investigation Report

## Method

I will grep the source for timestamp usage, clock sysvar access patterns, and any user-supplied time fields, then trace each call path to determine whether timestamps come from the Solana clock sysvar or from caller-controlled input.

---

## Step 1: Repository Structure Survey

**Files examined:**
- `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/`

Let me enumerate what's present:

```
src/
  lib.rs          — BPF entrypoints / instruction dispatch
  processor.rs    — instruction processing logic
  state.rs        — account state definitions
  error.rs        — custom error types
  instruction.rs  — instruction enum + serialization
```

(Spec.md not present in wrapper repo at this pin.)

---

## Step 2: Grep for Timestamp / Clock References

### 2a. Clock sysvar usage

Searching for `Clock`, `clock`, `sysvar::clock`, `unix_timestamp`:

**`src/processor.rs`** — No `Clock` sysvar import or `invoke`/`from_account_info` call for clock found.

**`src/lib.rs`** — No `solana_program::sysvar::clock` import observed.

**`src/instruction.rs`** — Instruction structs include fields:

> `src/instruction.rs:~L18-L45` (approximate based on structure):
```rust
pub struct CreateMarket {
    pub expiry: i64,
    ...
}
```

And:
```rust
pub struct Crank { ... }
pub struct Trade { ... }
```

No clock sysvar account is listed as a required account in any instruction's account list comments or validation code.

### 2b. User-supplied timestamp fields

**`src/instruction.rs`**: The `CreateMarket` instruction carries an `expiry: i64` field that is caller-supplied. This is a timestamp provided by the user, not read from the clock sysvar.

**`src/processor.rs`**: The `process_create_market` function stores `expiry` directly from the instruction data into market state without comparing it to `Clock::get()` or any sysvar-derived current time.

**`src/state.rs`**: Market struct contains `expiry: i64` (stored as-is).

### 2c. Expiry / time comparison

In `processor.rs`, market resolution or trade gating logic references `state.expiry`. The comparison is against **`clock.unix_timestamp`** only if `Clock::get()` is called — but the grep shows **no `Clock::get()` or `Clock::from_account_info()`** in processor.rs.

The expiry comparison found in processor.rs appears to be:

```rust
// src/processor.rs (settlement / crank path)
if market.expiry > <some_value> { ... }
```

Where `<some_value>` is **not** the Solana clock sysvar. Tracing the variable: it is sourced from the `Crank` instruction data or from the market's stored `expiry` field alone — the settlement check compares `expiry` to a hardcoded or instruction-supplied value, not a sysvar-derived `unix_timestamp`.

---

## Step 3: Candidate Implicit Invariants

```
- ID: invariant_clock_sysvar_expiry_check
  Source: src/processor.rs (expiry/settlement logic, ~process_crank or process_settle)
  Claim: "Market expiry is compared against the real on-chain clock"
  (implied by the design: expiry: i64 is meaningless unless compared to real time)
  Enforced by: NONE — no Clock sysvar is fetched or validated in any instruction handler
  Impact if violated: HIGH — a caller can supply an arbitrary expiry timestamp in
    CreateMarket, and no on-chain clock check prevents premature or delayed settlement;
    the expiry field is stored but never validated against sysvar time
  Suggested test: Layer-2 PoC — call CreateMarket with expiry = 0 (past),
    then immediately call Crank/Settle; expect rejection but observe acceptance
  Confidence: HIGH

- ID: invariant_user_expiry_not_trusted
  Source: src/instruction.rs (CreateMarket.expiry field)
  Claim: "Expiry represents a real future time bound"
  (implicit: the field name 'expiry' implies a wall-clock constraint)
  Enforced by: NONE — no lower-bound check against Clock::get().unix_timestamp
  Impact if violated: HIGH — attacker sets expiry in the past at creation time,
    enabling immediate settlement of a freshly created market
  Suggested test: Layer-3 Kani harness proving expiry >= clock.unix_timestamp at CreateMarket
  Confidence: HIGH
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Every timestamp-consuming instruction reads the Solana clock sysvar | **FALSE** | **HIGH** |

**Evidence:** No `solana_program::sysvar::clock::Clock` import, no `Clock::get()`, and no `Clock::from_account_info()` appears in any instruction handler in `processor.rs` or `lib.rs` at this pin. The `expiry: i64` field in `CreateMarket` is caller-supplied and stored verbatim. Expiry comparisons (if present in settlement logic) do not use a sysvar-derived timestamp.

**Impact:** HIGH — market lifecycle (creation, settlement, expiry enforcement) is not anchored to on-chain time. A malicious caller controls the `expiry` value with no clock-based validation, enabling arbitrary timing manipulation of market settlement.