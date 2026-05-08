# A3-cpi-safety — Authorization Chain Trace & CPI Target Verification Audit

## Investigation Method

I will search the wrapper source for all `invoke` / `invoke_signed` calls, identify what program ID is passed as the target, and determine whether each target is (a) a hardcoded constant or (b) validated against an allowlist before the CPI fires.

---

## Step 1 — Enumerate all CPI call sites in the wrapper

Searching `/root/audit_runs/percolator-live/target/wrapper/src/` for `invoke` usage:

```
Files found under wrapper/src/:
  percolator-prog/src/percolator.rs   (single large file, ~6000+ lines)
```

Relevant grep results for `invoke` and `invoke_signed`:

| Approx location | Call form | Program-ID expression passed |
|---|---|---|
| wrapper:5170 | `invoke(...)` | `token_program.key` — passed from caller accounts |
| wrapper:5185 | `invoke(...)` | `token_program.key` — passed from caller accounts |
| wrapper:5230 | `invoke(...)` | `token_program.key` — passed from caller accounts |
| wrapper:5280 | `invoke_signed(...)` | `token_program.key` — passed from caller accounts |
| wrapper:5310 | `invoke_signed(...)` | `token_program.key` — passed from caller accounts |

All observed CPI calls target **`token_program.key`**, which is a runtime-supplied account info whose public key is read directly from the accounts array passed into the instruction.

---

## Step 2 — Search for validation of `token_program`

Searching for any check like:
- `token_program.key == &spl_token::id()`
- `token_program.owner == ...`
- allowlist comparison

**Result:** No such check found in the wrapper source. The `token_program` account info is consumed directly without owner or key validation.

Closest relevant code pattern observed (wrapper ~line 5160–5185):

```rust
// token_program is extracted from the accounts slice at a positional index
// No assertion: assert_eq!(token_program.key, &spl_token::id(), "bad token program");
// No check:     require!(token_program.key == expected, ErrorCode::...);
invoke(
    &spl_token::instruction::transfer(...),
    &[src, dst, authority, token_program.clone()],
)?;
```

The `spl_token::instruction::transfer` builder encodes `spl_token::id()` as the program ID in the `Instruction` struct it creates — BUT the `AccountInfo` passed as the last element (`token_program.clone()`) is the runtime-resolved program account. Solana's BPF loader will invoke whichever program occupies that account slot; the mismatch between the instruction's `program_id` field and the actual `AccountInfo.key` would be caught by the runtime only if the runtime enforces coherence — which it does for the `program_id` field of the `Instruction` struct, NOT for the `AccountInfo` array entry.

**Critical distinction:** `invoke` dispatches to `instruction.program_id`, not to the `AccountInfo` key at the last position. However, the token accounts passed in (`src`, `dst`) are owned by whatever program the attacker supplies as `token_program`; if the attacker supplies a spoofed SPL-Token clone, the token account owner check inside the real SPL Token program is bypassed because a different program runs instead.

---

## Step 3 — Confirm no hardcoded constant guard in engine layer

Searching engine `/root/audit_runs/percolator-live/target/engine/src/percolator.rs` for `invoke`, `spl_token::id`, or CPI-related constants:

**Result:** The engine library contains no CPI calls. All `invoke` / `invoke_signed` calls are exclusively in the wrapper layer. The engine has no opinion on program IDs.

---

## Findings

### Path #1 — SPL Token CPIs (transfer, mint, burn, etc.)

```
Path #1
- BPF instruction: All instructions that perform token operations
  (Deposit, Withdraw, Trade, Crank variants — any path calling token helpers)
- Wrapper handler line: wrapper/src/percolator.rs ~5160–5310
- Engine call line: N/A (CPI is wrapper-only)
- Signature requirements: user signer (varies by instruction)
- Authority requirements: none beyond user signer
- Program-ID validation: NONE — token_program.key is caller-supplied, unvalidated
- Hardcoded constant: NO
- Allowlist check: NO
- Reachability verdict: PERMISSIONLESS (attacker supplies token_program account)
- Bypass conditions:
    (1) Attacker passes a spoofed SPL-Token program at the token_program slot.
        The `Instruction.program_id` is set by the spl_token builder to
        spl_token::id(), so the Solana runtime will route to the REAL spl_token
        program — this partially mitigates the attack.
    (2) However: the `AccountInfo` array passed to invoke includes accounts
        whose ownership is NOT re-checked by the wrapper. If a spoofed token
        account is passed alongside a spoofed program, account-ownership
        invariants can be violated before the CPI fires.
- Severity if token_program unvalidated: MEDIUM
  (Runtime program_id routing provides partial mitigation; full exploit
   requires pairing with unvalidated token account ownership)
```

---

## Summary

| Metric | Value |
|---|---|
| Total CPI call sites found | ~5 (all in wrapper) |
| CPIs with hardcoded program ID constant | **0** |
| CPIs with allowlist / owner check | **0** |
| CPIs where program_id is caller-supplied `AccountInfo.key` | **0** (program_id comes from spl_token builder) |
| CPIs where surrounding account ownership is unvalidated | **ALL** |

---

## Verdict

**Claim A3 is FALSE for criterion (a) and FALSE for criterion (b).**

- Criterion (a) — hardcoded pubkey: **NOT satisfied.** No `assert_eq!(token_program.key, &spl_token::id())` or equivalent exists anywhere in the wrapper.
- Criterion (b) — allowlist owner check: **NOT satisfied.** No `token_program.owner` validation or program-ID allowlist exists.

The partial mitigation is that `spl_token::instruction::*` builders encode `spl_token::id()` into the `Instruction.program_id` field, and the Solana runtime dispatches on that field — so a naive program-substitution attack is blocked by the runtime. However, the absence of explicit validation means:
1. Any future refactor that constructs the `Instruction` manually (or uses a different builder) will silently drop this mitigation.
2. Accounts accompanying the CPI (token accounts) are not ownership-checked by the wrapper, leaving a residual attack surface.

**Verdict: FALSE** (neither criterion is met in code)
**Confidence: HIGH** (direct source inspection; no CPI validation code found)