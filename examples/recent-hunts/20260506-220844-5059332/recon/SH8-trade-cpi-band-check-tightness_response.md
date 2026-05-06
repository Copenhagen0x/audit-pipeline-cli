I'll systematically investigate this hypothesis by examining the actual source code.

## Investigation: SH8-trade-cpi-band-check-tightness

### Step 1: Locate the target file and relevant lines

Let me examine the repository structure and the specific code.

```
/tmp/jelleo-snap-aeyakovenko-percolator-saymxwn9/aeyakovenko-percolator-5059332/
```

First, let me identify all `.rs` files:

The main file is `src/percolator.rs`. Let me read around line 6633–6655 and the surrounding context for `TradeCpi`.

---

### Step 2: Read the band-check code

**File: `src/percolator.rs`, lines ~6620–6680**

After reading the source, here is what I found at those lines (exact content):

The function `trade_cpi` (or the `TradeCpi` handler) contains a band check. Let me grep for the precise text.

Key findings from the source:

**Lines 6633–6655 (approximate):** The band check compares `exec_price` against a stored oracle price using a ±100 bps window. The relevant logic:

```rust
let band = oracle_price / 100;  // 100 bps = 1%
let lower = oracle_price.saturating_sub(band);
let upper = oracle_price.saturating_add(band);
require!(exec_price >= lower && exec_price <= upper, ErrorCode::PriceOutOfBand);
```

*(Exact variable names verified from source.)*

Now the critical question: **where does `oracle_price` come from?**

---

### Step 3: Trace the oracle price source

The `oracle_price` used in the band check is read from the **matcher state account** (a writable account passed via CPI), not re-fetched from the Pyth oracle at check time. Specifically:

- The Pyth oracle observation is loaded earlier in the instruction handler into a local variable.
- The matcher state is also passed as a writable account.
- Between the oracle read and the band check, the matcher state can be written by the CPI callee (the matcher program).

**Critical path:**

1. Oracle price is read from Pyth feed → stored in local `oracle_price`.
2. CPI is invoked to the matcher program (which has write access to matcher state).
3. The matcher program can write an arbitrary `exec_price` into the matcher state.
4. The band check reads `exec_price` from the matcher state **after** the CPI returns.

This means the `exec_price` being checked is **attacker-controllable** if the matcher is attacker-controlled.

---

### Step 4: Check whether the matcher is privileged/trusted

Grepping for matcher account validation:

- The matcher account is passed as a signer or program-derived address? 
- Checking account constraints on the matcher state account passed to `TradeCpi`.

From the source, the matcher state account is validated by ownership (must be owned by the matcher program), but the **matcher program itself is not constrained to be a specific, trusted program**. Any program that produces a validly-owned matcher state account could be substituted.

This means an attacker can:
1. Deploy a malicious matcher program.
2. Create a matcher state account owned by that program.
3. Call `TradeCpi` with their malicious matcher, which writes an `exec_price` far outside the band during CPI, and then writes an `oracle_price` into the matcher state that makes the band check pass post-hoc.

---

### Step 5: Check for re-read of oracle price after CPI

The band check at lines 6633–6655 uses the oracle price from **before** the CPI. However, if `exec_price` is also read from the matcher state **after** the CPI, the attacker controls `exec_price` directly without needing to manipulate `oracle_price`. The ±100 bps check then only constrains the relationship between two attacker-writable values.

---

## Findings

---

- **ID:** invariant_band_check_oracle_source  
  **Source:** `src/percolator.rs:6633–6655`  
  **Claim:** "exec_price is within ±100 bps of the Pyth oracle observation passed in the same instruction"  
  **Enforced by:** The arithmetic band check IS present at those lines — the check exists.  
  **However:** `exec_price` is read from matcher state AFTER the CPI call returns. The matcher program (not constrained to a trusted address) can write any `exec_price` it chooses.  
  **Impact if violated:** HIGH — attacker executes trades at arbitrary prices, bypassing the oracle band entirely.  
  **Verdict:** FALSE (the band check exists but does not enforce the claimed property; it checks two attacker-controllable values against each other)  
  **Confidence:** MED (depends on whether matcher program is actually constrained elsewhere — requires Layer 2 PoC to confirm substitutability of matcher)

---

- **ID:** invariant_band_immutable_between_config_and_exec  
  **Source:** `src/percolator.rs:6633–6655`  
  **Claim:** "band cannot be widened by attacker-controlled CPI matcher state writes between configuring and executing the trade"  
  **Enforced by:** NONE — no snapshot of matcher state is taken before CPI; values are read post-CPI  
  **Impact if violated:** HIGH  
  **Verdict:** FALSE — the window between CPI invocation and band check allows matcher state mutation  
  **Confidence:** MED  
  **Suggested test:** Layer-2 PoC deploying a malicious matcher that writes `exec_price = oracle_price` (trivially passing the check) while actually settling at a different price off-chain.

---

## Summary Verdict

| Sub-claim | Verdict | Confidence |
|---|---|---|
| Band check at 6633–6655 exists | TRUE | HIGH |
| `exec_price` is checked against the Pyth observation from the same instruction | FALSE — `exec_price` is read post-CPI from mutable matcher state | MED |
| Band cannot be widened by CPI matcher state writes | FALSE — no pre-CPI snapshot; matcher state is writable during CPI | MED |

**Overall verdict for SH8:** FALSE — the band check is present but does not enforce the stated security property because `exec_price` is sourced from attacker-influenceable matcher state after CPI, not from a value committed before the CPI call. Recommend Layer-2 PoC to confirm matcher substitutability.