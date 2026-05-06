# IX3-rent-exemption-check — Formal Property Definition

---

## Step 1: Locate the source code evidence

Let me trace the actual code before translating any claim.

### Searching for rent-related code in the wrapper

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

I'll examine the directory structure and relevant files.

---

## Findings from Code Inspection

### Wrapper entrypoints

The wrapper repo (`percolator-prog`) at pin `04b854e5` is referenced but the local clone path points to the engine repo. I will work with what is available at the local path.

**Key files examined:**
- `src/lib.rs` — engine state and operations
- Any `processor.rs`, `instruction.rs`, or `entrypoint.rs` in the wrapper

From the engine source at the local clone, the relevant structures involve account allocation. The English claim to formalize is:

> **"Every account allocated by the program is rent-exempt, with sysvar rent verified at allocation time."**

**Source:** Hypothesis IX3 (audit brief)

---

## Step 2: Translation

```
Original claim:    "Every account allocated by the program is rent-exempt,
                   with sysvar rent verified at allocation time."
Source:            Audit hypothesis IX3-rent-exemption-check

Variables referenced:
  - account.lamports         (the lamports held by the newly allocated account)
  - account.data.len()       (size of the account's data, determines exempt minimum)
  - rent.minimum_balance(n)  (sysvar Rent method: minimum lamports for n bytes)
  - rent_sysvar_account      (the Rent sysvar AccountInfo passed into the instruction)

Quantification:
  - For all instruction handlers that allocate (create) a new on-chain account
  - At the point where the account is created / space is initialized
  - The following holds: account.lamports >= rent.minimum_balance(account.data.len())
  AND the Rent sysvar was explicitly loaded and verified (not defaulted/hardcoded)

Rust translation:
```

```rust
// Pre: symbolic engine/account state before allocation
let data_len: usize = kani::any();
kani::assume(data_len <= 10_240); // Solana max account size

let lamports_provided: u64 = kani::any();
let rent_epoch: u64 = kani::any();

// Symbolic Rent sysvar (as the program would receive it)
let rent = Rent {
    lamports_per_byte_year: kani::any(),
    exemption_threshold: kani::any::<f64>(),
    burn_percent: kani::any(),
};
// Bound to realistic Solana mainnet values so symbolic state is tractable
kani::assume(rent.lamports_per_byte_year >= 1 && rent.lamports_per_byte_year <= 10_000);
kani::assume(rent.exemption_threshold >= 1.0 && rent.exemption_threshold <= 3.0);

let minimum = rent.minimum_balance(data_len);

// Operation: simulate the allocation check the program must perform
// The program MUST verify: lamports_provided >= minimum
// If it does NOT, the following assert encodes the broken invariant:
assert!(
    lamports_provided >= minimum,
    "Account allocated with insufficient lamports for rent exemption"
);

// Secondary: the sysvar must have been loaded from the accounts array,
// not hardcoded — this is a structural check (harness calls the actual
// instruction handler with a symbolic rent sysvar account)
```

```
Suggested Kani harness name:  proof_rent_exempt_on_allocation
Estimated harness complexity: MED
  (symbolic Rent sysvar fields + symbolic data_len + one allocation path;
   complexity rises to HIGH if multiple instruction handlers allocate accounts,
   requiring one harness sub-case per handler)
```

---

## Caveats for the Harness Author

| Concern | Note |
|---|---|
| `exemption_threshold` is `f64` | Kani has limited float support; consider fixing to `2.0` with `kani::assume` |
| Multiple allocating instructions | Each entrypoint that calls `system_instruction::create_account` needs a separate sub-harness |
| Rent sysvar identity | The structural check (was the correct sysvar key passed?) is better tested in Layer 4 (LiteSVM) than Kani |
| Engine vs. wrapper | If rent checking lives only in the BPF wrapper, the engine library has no `assert!`-able hook; the harness must call the wrapper's `process_instruction` entry |

---

**Verdict on formalizability:** TRUE — the claim is directly encodable as a Kani `assert!` comparing `lamports >= rent.minimum_balance(data_len)` on each allocation path, contingent on identifying the exact allocating instruction handlers in the wrapper source.

**Confidence:** MED (harness is well-defined, but the wrapper source lines need confirmation before the harness can be written without invented function names).