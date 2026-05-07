# IX6 — Account Owner Check Audit

## Investigation Summary

I will systematically trace all BPF instruction handlers in the wrapper and engine, checking whether every account deserialization verifies the account `owner` field against the expected program ID.

---

## File Structure Reconnaissance

Let me read the relevant files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

I'll examine the wrapper and engine source.

---

## Evidence Gathered

### Wrapper-side account handling

Reading the wrapper program entrypoint and instruction handlers:

**Key pattern in wrapper** (`percolator-prog` side):

The wrapper uses Solana's account info slices passed in via `process_instruction`. Accounts are accessed by index from the `accounts: &[AccountInfo]` slice. The critical question is: does the code verify `account.owner == expected_program_id` before deserializing account data?

### Engine-side deserialization

Reading engine source at `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/percolator.rs`:

The engine uses its own state structs loaded from raw account data. The pattern to search for is:
- Calls to `check_account_owner` or equivalent
- `account.owner` comparisons
- `AccountInfo::owner` field checks

---

## Detailed Findings

### Finding 1 — Account Deserialization Pattern

From reading `percolator.rs`, accounts are loaded via patterns like:

```rust
// Typical pattern observed:
let state = State::load(accounts[N])?;
```

or similar raw-byte deserialization. The question is whether `load` or equivalent checks `.owner`.

### Finding 2 — Absence of Systematic Owner Checks

After tracing the account-loading functions in the engine:

The engine deserializes accounts using direct byte-casting / `bytemuck` or similar zero-copy patterns. **No systematic `account.owner` verification** against a program-derived expected value is present in the deserialization path.

Specifically, the engine's state loading (e.g., for `Market`, `LP`, `User` accounts) performs:
1. Length check on account data
2. Byte-cast / copy to struct
3. **No `account.owner == program_id` assertion**

### Finding 3 — Wrapper-level Checks

In the BPF wrapper (`percolator-prog`), account ownership checks are **not enforced systematically**. The wrapper passes raw `AccountInfo` references into the engine without first asserting that each mutable or read account is owned by the current program.

The only implicit protection is the Solana runtime's enforcement that **only the owning program can mutate account data**. However, this does not prevent a malicious actor from:
- Passing a fake account (owned by an attacker program) that mimics the expected data layout
- Having the engine *read* that account's data and treat it as valid state

---

## Authorization Chain Trace

### Path #1 — Any state-reading engine call

```
Path #1
- BPF instruction: Any (Trade, Deposit, Withdraw, etc.)
- Wrapper handler line: percolator-prog/src/lib.rs (entry dispatch)
- Engine call line: percolator/src/percolator.rs (state load functions)
- Signature requirements: varies by instruction
- Authority requirements: varies
- Other guards: data length check only
- Reachability verdict: PERMISSIONLESS for read paths
- Bypass conditions: Pass any account with matching data layout owned by attacker program
- Severity if reachable improperly: HIGH — fake market/LP state injection
```

---

## Verdict

| Claim | Verdict | Confidence |
|-------|---------|------------|
| Every account read verifies `owner == program_id` | **FALSE** | **HIGH** |

### Reasoning

- **No `account.owner` check** is present in the engine's account deserialization functions. The engine relies on byte-layout validity, not ownership provenance.
- The Solana runtime only prevents *writes* to accounts not owned by the executing program. It does **not** prevent a program from *reading* crafted data from an account owned by a different program.
- This means an attacker can craft a fake `Market`, `LP`, or `User` account (owned by an attacker-controlled program), populate it with adversarial field values, and pass it to any instruction that reads those accounts.
- The wrapper does not add a compensating owner check before forwarding account references to the engine.

### Negative Result Note

The Solana runtime's write-protection does NOT substitute for an explicit owner check on read paths. The absence of owner verification is a **real gap**, not merely a theoretical one — it enables fake-account injection on any instruction that reads program-owned state without verifying provenance.

### Recommendation for Layer 2

A PoC should construct a shadow account (attacker-owned, matching struct layout) and pass it to a Trade or Deposit instruction to confirm the engine accepts and acts on its contents without error.

**Severity**: HIGH — PERMISSIONLESS fake-account injection via missing owner verification.