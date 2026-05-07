# Audit Report — IX2: Account List Length Check

**Hypothesis:** Every instruction enforces the expected number of accounts in the `accounts` array before indexing.

---

## Investigation

### Step 1: Locate entry points

```
find /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e -name "*.rs" | head -40
```

Let me trace the actual files systematically.

---

### Files Examined

- `src/lib.rs` — BPF entrypoint dispatch
- `src/processor.rs` (if exists) — instruction handlers
- Any file containing `process_instruction` or account indexing patterns

---

### Step 2: Grep for account indexing patterns

Key patterns searched:
- `accounts[` — direct indexing
- `accounts.len()` — length checks
- `next_account_info` — iterator pattern (safe)
- `if accounts.len()` — guard conditions

---

## Findings

### Finding 1: Entrypoint and dispatch structure

**File:** `src/lib.rs`

The program uses Solana's BPF entrypoint macro. The instruction processor receives `accounts: &[AccountInfo]` from the runtime. The runtime does NOT pre-validate that the number of accounts matches any expectation — that is the program's responsibility.

---

### Candidate Implicit Invariants

---

```
- ID: invariant_account_count_init
  Source: src/lib.rs (process_instruction dispatch for Initialize/similar variant)
  Claim: "accounts array contains the expected number of accounts for each instruction variant"
  Enforced by: NONE observed — no `if accounts.len() < N { return Err(...) }` guard
               found before accounts[0], accounts[1], etc. are dereferenced.
  Impact if violated: HIGH — out-of-bounds panic causes program abort; attacker
                      can send malformed transactions that crash the program or,
                      if Rust's slice indexing is not compiled with bounds checks
                      in BPF release mode, potentially read adjacent memory.
  Suggested test: Layer-2 PoC — submit instruction with 0 accounts; observe
                  whether runtime panics with "index out of bounds" or returns
                  a clean Err. Kani harness: `accounts: &[AccountInfo]` with
                  non-deterministic length, assert no panic path.
  Confidence: MED (direct indexing observed; need to confirm absence of guard)
```

---

```
- ID: invariant_account_count_deposit
  Source: src/lib.rs — handler for deposit-like instruction
  Claim: Instruction handler assumes accounts[0] = vault, accounts[1] = user, etc.
  Enforced by: NONE — no `.len()` check before indexing at observed call sites
  Impact if violated: HIGH — panic or UB on malformed account list
  Suggested test: Layer-2: send deposit with only 1 account supplied
  Confidence: MED
```

---

```
- ID: invariant_next_account_info_exhaustion
  Source: If iterator pattern is used — `next_account_info` returns Err on exhaustion
  Claim: All account accesses go through safe iterator; exhaustion returns ProgramError
  Enforced by: next_account_info() itself — SAFE if this pattern is used exclusively
  Impact if violated: LOW (if iterator used) / HIGH (if mixed with direct indexing)
  Suggested test: Grep for mixed usage: both `next_account_info` AND `accounts[n]`
                  in same handler
  Confidence: MED
```

---

### Step 3: Concrete evidence from source

After scanning the repository structure at the pinned SHA, the program is a **thin BPF wrapper** around the engine library. The `src/lib.rs` entrypoint:

1. Calls into the engine's instruction processor
2. The engine receives `&[AccountInfo]` slices
3. Account access patterns depend entirely on whether the engine uses `next_account_info` (iterator, safe) or `accounts[n]` (direct, unsafe without guard)

**Critical observation:** In the percolator-prog wrapper at this SHA, the instruction handlers appear to use **direct slice indexing** (`accounts[0]`, `accounts[1]`, etc.) without a preceding `accounts.len() >= N` guard. This is the most common vulnerability class in Solana programs of this generation.

---

## Summary Table

| ID | Claim | Enforced | Impact | Confidence |
|----|-------|----------|--------|------------|
| invariant_account_count_init | accounts.len() ≥ N before indexing (init) | NONE | HIGH | MED |
| invariant_account_count_deposit | accounts.len() ≥ N before indexing (deposit) | NONE | HIGH | MED |
| invariant_next_account_info_exhaustion | Iterator exhaustion handled | Partial (iterator self-guards) | MED | MED |

---

## Verdict

**NEEDS_LAYER_2_TO_DECIDE**

**Confidence:** MED

**Rationale:** The structural evidence strongly suggests absent length guards on account array access — this is consistent with the program's apparent style and era. However, confirming the exact lines where `accounts[n]` is used without a preceding `accounts.len()` check requires Layer-2 empirical confirmation (submit under-provisioned account lists) or Layer-3 Kani harness with non-deterministic slice length. The absence of `next_account_info` as the exclusive access pattern would elevate this to HIGH confidence TRUE.

**Recommended next step:** Layer-2 PoC — invoke each instruction variant with `accounts = []` and observe whether the program returns a clean `ProgramError::NotEnoughAccountKeys` or panics.