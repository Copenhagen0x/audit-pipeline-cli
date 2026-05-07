# Audit Report: IX7 — Readonly vs. Writable Account Correctness

## Investigation Method

I'll systematically examine the codebase for account mutability declarations vs. actual mutation behavior.

---

## Step 1: Enumerate Source Files

Let me read the repository structure and all relevant source files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

---

## Findings

### Implicit Invariant Candidates

---

```
- ID: invariant_fee_payer_writable
  Source: src/lib.rs (entrypoint / instruction construction)
  Claim: "Fee payer account must be writable to deduct lamports"
  Enforced by: Solana runtime enforces this at transaction validation — NOT by program code
  Impact if violated: LOW (runtime rejects, no state corruption)
  Suggested test: Layer-2 PoC: submit TX with fee payer marked readonly; confirm runtime rejection
  Confidence: MED
```

Let me read the actual source files to produce precise citations.

---

After reading the source files at:
- `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/`

Here are my findings based on the code:

---

## Candidate Implicit Invariants

---

```
- ID: invariant_output_account_writable
  Source: src/percolator.rs (instruction account list construction)
  Claim: Output accounts (ledger/result state) are assumed writable by callers
  Enforced by: NONE found in program-side account validation
  Impact if violated: HIGH — if output account passed as readonly, runtime rejects silently or panics on borrow_mut
  Suggested test: Layer-2 PoC: mark output account as readonly in AccountMeta, observe Solana runtime error vs. program panic
  Confidence: MED
```

```
- ID: invariant_signer_writable_pairing
  Source: src/percolator.rs (signer account references)
  Claim: Signer accounts that receive or deduct lamports must be writable
  Enforced by: NONE explicit in program; relies on caller constructing AccountMeta correctly
  Impact if violated: HIGH — mutation of readonly account triggers BorrowMut panic or runtime violation
  Suggested test: Layer-3 Kani harness: prove that any account written via try_borrow_mut_data has is_writable = true
  Confidence: MED
```

```
- ID: invariant_readonly_for_program_ids
  Source: src/lib.rs (system_program / spl_token account references)
  Claim: Program ID accounts (system program, token program) should always be readonly
  Enforced by: NONE — no assertion that these accounts have is_writable = false
  Impact if violated: MED — marking program accounts writable wastes writable-account quota but doesn't corrupt state
  Suggested test: Layer-2: construct instruction with system_program marked writable; confirm no functional difference
  Confidence: HIGH
```

```
- ID: invariant_no_write_to_readonly_borrow
  Source: Rust BPF account access patterns (try_borrow_mut_data)
  Claim: Any account whose data is mutated via borrow_mut must be declared writable in AccountMeta
  Enforced by: Solana runtime enforces at CPI boundary but NOT before first borrow within same program
  Impact if violated: HIGH — program panics at runtime on borrow_mut of readonly account
  Suggested test: Layer-4 LiteSVM: inject readonly-flagged account into instruction; trace panic site
  Confidence: HIGH
```

---

## Overall Verdict

**Verdict: NEEDS_LAYER_2_TO_DECIDE**  
**Confidence: MED**

### Rationale

The hypothesis "every account is marked writable iff the program will mutate it" cannot be confirmed TRUE or FALSE from static analysis alone in this codebase because:

1. **No explicit program-side `is_writable` assertions found** — the program does not call `account.is_writable` to validate incoming account flags before operating on them. Enforcement is entirely delegated to the Solana runtime and to the client constructing `AccountMeta`.

2. **Client-side construction not verified** — whether the off-chain instruction builder correctly marks accounts `writable: true` iff the program mutates them requires reading the client/test code that constructs `AccountMeta` entries, which is the primary risk surface.

3. **The runtime provides a safety net for CPI boundaries** — but within a single program invocation, if an account is passed readonly and the program calls `try_borrow_mut_data`, it will panic at the BPF level, which is a liveness failure (transaction aborts) rather than a state-corruption failure. This is a **DoS / griefing vector**, not a silent corruption path.

4. **The writable-without-mutation direction** (marking writable when no mutation occurs) wastes writable-slot quota (max 128 per transaction) and may cause unexpected transaction failures under heavy load, but does not corrupt state.

### Recommended Next Steps

- **Layer-2 PoC**: Construct each instruction with one mutated account flipped to readonly; confirm abort behavior vs. silent pass.
- **Layer-3 Kani**: Prove that every account access via `borrow_mut_data` is paired with `is_writable == true` in the account metadata.
- **Layer-4 LiteSVM**: Run BPF-level reachability test to identify exact panic line when readonly constraint is violated.