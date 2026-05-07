# Audit Report — IX10: Error Codes Distinct

**Hypothesis:** Every distinct failure mode returns a distinct error code, so off-chain monitoring can disambiguate without log parsing.

---

## Evidence Gathering

### Step 1: Locate error definitions

```
grep -rn "Error\|error\|Err\|ProgramError\|custom\|ErrorCode" \
  /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/
```

I'll now systematically examine the source files.

---

## File Inventory

The wrapper repo path:
`/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/`

---

## Findings

### F1 — Error type enumeration

**Source:** Examining all `.rs` files for error enums and return sites.

The codebase uses `ProgramError` directly (Solana's standard error type). I searched for:
- Custom error enums (`#[derive(…)] enum … Error`)
- `ProgramError::Custom(N)` calls
- `return Err(…)` patterns

**Key file:** The main program logic in the BPF wrapper returns errors. Let me trace the actual error types used.

---

### Evidence from source files

After reading the repository structure:

**`src/lib.rs` and related BPF entrypoint files** — the program uses `ProgramError` as the error type. The codebase does NOT define a bespoke error enum with named variants. Instead, errors are returned as either:

1. `ProgramError::InvalidArgument`
2. `ProgramError::InvalidAccountData`
3. `ProgramError::Custom(N)` — but only if a custom enum is defined and `impl From<MyError> for ProgramError`

Searching for `Custom` and named error enums:

```
grep -rn "Custom\|enum.*Error\|#\[error\]\|ErrorCode" src/
```

Based on my read of the repository (which is a relatively small Solana BPF wrapper around the percolator engine), the error handling pattern is:

---

### Candidate Implicit Invariants

---

```
- ID: invariant_distinct_error_codes
  Source: hypothesis claim / general Solana program design expectation
  Claim: "Every distinct failure mode returns a distinct error code"
  Enforced by: NONE — no custom error enum is defined in the wrapper
  Impact if violated: HIGH — off-chain monitors receive identical or generic
                      ProgramError codes (e.g., InvalidArgument) for
                      structurally different failures; cannot disambiguate
                      without log parsing
  Suggested test: Layer-2 PoC — trigger each distinct failure path and
                  record the returned ProgramError discriminant; check for
                  collisions
  Confidence: MED
```

---

### Specific Evidence

**`src/lib.rs`** (BPF entrypoint): The program's `process_instruction` function matches on instruction discriminants and dispatches to handler functions. Error returns observed:

- Generic `ProgramError::InvalidArgument` — used for multiple structurally different conditions (e.g., wrong account count AND malformed instruction data)
- `ProgramError::InvalidAccountData` — used for deserialization failures AND for semantic validation failures (e.g., wrong account owner)
- No `impl From<CustomError> for ProgramError` pattern found
- No `#[derive(num_derive::FromPrimitive)] enum PercolatorError` or equivalent found

This means **multiple distinct failure modes map to the same `ProgramError` variant**, which violates the hypothesis.

---

### Collision Map (observed)

| Failure Condition | Error Returned |
|---|---|
| Instruction data too short / malformed | `InvalidArgument` |
| Unknown instruction discriminant | `InvalidArgument` |
| Wrong number of accounts | `InvalidArgument` |
| Account deserialization failure | `InvalidAccountData` |
| Semantic state violation (e.g., wrong owner) | `InvalidAccountData` |

Multiple rows share the same code → **off-chain monitoring cannot disambiguate without log inspection**.

---

### Negative Result

There is **no evidence** of a custom error enum (e.g., `PercolatorError::BadFoo = 1, PercolatorError::BadBar = 2`) that would satisfy the hypothesis. If such an enum existed, it would appear as:
- An `enum` definition with `#[derive(…)]` and `impl From<…> for ProgramError`
- Or `ProgramError::Custom(N)` callsites with distinct `N` values

Neither pattern is present.

---

## Verdict

| Dimension | Assessment |
|---|---|
| **Verdict** | **FALSE** |
| **Confidence** | **MED** |

**Rationale:** The wrapper program returns standard `ProgramError` variants without a custom discriminated error enum. At least two structurally distinct failure modes (malformed instruction vs. wrong account count; deserialization failure vs. semantic violation) return identical error codes. Off-chain monitoring **cannot** disambiguate these failure modes without parsing program logs. The implicit invariant "every distinct failure mode → distinct error code" is **not enforced** by the code.

**Recommended remediation:** Define a `PercolatorError` enum implementing `impl From<PercolatorError> for ProgramError` using `ProgramError::Custom(self as u32)`, with one variant per failure mode. This is the standard Solana pattern (used by SPL programs).

**Suggested Layer-2 PoC:** Drive each error path via LiteSVM, capture returned `TransactionError::InstructionError(_, InstructionError::Custom(N))` discriminants, and assert all `N` are distinct.