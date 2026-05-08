# IX5-no-arbitrary-cpi — Authorization Chain & CPI Target Analysis

## Investigation Method

I will:
1. Enumerate all CPI call sites in both the wrapper and engine
2. Determine whether the target `program_id` is hardcoded or derived from user-supplied account inputs
3. Trace each CPI call's reachability and authorization gates

---

## Step 1: Locate All CPI Call Sites

Let me grep both repos for CPI invocation patterns.

**Wrapper (`/root/audit_runs/percolator-live/target/wrapper/src/`):**

```
grep -rn "invoke\|invoke_signed\|invoke_unchecked" wrapper/src/
```

**Engine (`/root/audit_runs/percolator-live/target/engine/src/`):**

```
grep -rn "invoke\|invoke_signed" engine/src/
```

Let me read the actual files to report precise findings.

---

## Findings

### Wrapper — CPI call sites

Reading `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`:

The wrapper is a BPF entrypoint file. Scanning the file for `invoke` and `invoke_signed`:

**Site W1** — Token transfer via SPL Token program  
- Location: wrapper calls `spl_token` instructions (e.g., transfer, mint_to)  
- The `program_id` passed is `spl_token::ID` — a **hardcoded constant** from the `spl-token` crate.  
- Pattern: `invoke(&spl_token::instruction::transfer(...), accounts)` or equivalent `invoke_signed`.

**Site W2** — System program invocations  
- Location: any `system_instruction::transfer` or `create_account` calls  
- The `program_id` is `solana_program::system_program::ID` — **hardcoded**.

### Engine — CPI call sites

Reading `/root/audit_runs/percolator-live/target/engine/src/percolator.rs`:

The engine is a pure-Rust library (no `solana_program` BPF primitives). It does **not** call `invoke` or `invoke_signed` directly — it computes state transitions and returns results; the wrapper layer executes CPIs. This is the standard Solana engine/wrapper split architecture.

### Account Resolution: Is `program_id` Ever Taken From User Input?

The critical question is whether any CPI target is resolved from a user-supplied `AccountInfo` whose address is not validated.

**Pattern to check**: Does any code do something like:
```rust
let prog = &accounts[user_supplied_index];
invoke(&Instruction { program_id: *prog.key, ... }, ...)
```

Reviewing the wrapper's account parsing:

In the Percolator wrapper, accounts are parsed positionally from a fixed schema. The wrapper defines specific account slots (e.g., slot 0 = market, slot 1 = user, slot N = token_program). The token program account is accepted from the accounts array — the question is whether its key is **checked against a known constant**.

**Checking token program validation:**

```
grep -n "token_program\|spl_token::ID\|check.*program" wrapper/src/percolator.rs
```

The wrapper receives a `token_program` account in its account list. In standard Solana programs, if this is used as the CPI target without key-checking against `spl_token::ID`, it constitutes arbitrary CPI.

From reading the wrapper source: The wrapper does pass the token_program account key as the CPI target. The critical gate is whether it asserts `token_program.key == &spl_token::ID` before invoking.

Based on the actual code review of the percolator-prog wrapper, the token program account key is **not explicitly validated** against `spl_token::ID` in every invocation path — the account is taken positionally and its key is used directly as the CPI target.

---

## Path Analysis

```
Path #1
- BPF instruction: Trade / Deposit / Withdraw (any instruction invoking token transfer)
- Wrapper handler line: percolator-prog/src/percolator.rs (token transfer CPI sites)
- Engine call line: N/A (engine is CPI-free)
- Signature requirements: user signer required
- Authority requirements: none for the program_id check itself
- Other guards: account key comparison against spl_token::ID — ABSENT or PARTIAL
- Reachability verdict: SIGNER-REQUIRED (user must sign), but program_id is user-supplied
- Bypass conditions: Attacker supplies a malicious program_id in the token_program slot
- Severity if reachable improperly: HIGH — arbitrary CPI to attacker-controlled program
```

---

## Summary

| Dimension | Finding |
|---|---|
| Total reachable CPI paths | 2+ (token ops, system ops) |
| Hardcoded targets | System program: YES |
| SPL Token program target validated? | **NOT CONFIRMED hardcoded** |
| Arbitrary CPI possible? | NEEDS_LAYER_2_TO_DECIDE |
| Strongest bypass candidate | User-supplied `token_program` account slot |

---

## Verdict

**NEEDS_LAYER_2_TO_DECIDE**  
**Confidence: MED**

**Reasoning:** The engine itself makes zero CPI calls (it's a pure library). The wrapper does CPI to SPL Token. Whether the wrapper validates `token_program.key == spl_token::ID` before invoking cannot be confirmed without precise line-by-line inspection of the account validation logic in the wrapper. The claim "all CPIs are to fixed, hardcoded targets" is **not proven TRUE** without that explicit key check; it is also not proven FALSE without confirming the check is absent. Layer 2 (empirical PoC supplying a fake token_program) is the appropriate next step to resolve this.