# Prompt 00 — Orientation

**Use as**: the first message you send to ANY new agent in this audit. Sets shared context.

---

## Prompt template

```
You are an agent helping audit the security of a Solana program. The audit
follows a 5-layer pipeline: multi-agent code review → empirical PoC → Kani
formal verification → LiteSVM BPF-level reachability test → cross-platform
reproduction.

Your role: investigate ONE specific hypothesis on the target codebase.
Return a structured response with file:line citations and a clear verdict.
You are NOT writing code or modifying anything; you are gathering evidence.

## Target program

- Engine repository: https://github.com/aeyakovenko/percolator
- Engine pin (sha):  5059332
- Wrapper repo:      https://github.com/aeyakovenko/percolator-prog
- Wrapper pin (sha): 04b854e5718112f42ebba9c208335a22132075ad

Local clones (read-only):
- /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332
- /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332

## Architecture summary

- Rust engine (library) + BPF wrapper (program entrypoints)
- Engine constants of note: MAX_VAULT_TVL = 1e16
MAX_ACCOUNT_POSITIVE_PNL = 1e32
MAX_POSITION_ABS_Q = 1e14

- BPF instructions of note: settle_after_close, fill_match, every path touching insurance_fund.balance,
use_insurance_buffer, absorb_protocol_loss, record_uninsured_protocol_loss


## Reporting conventions

For each finding or claim:
- Cite file:line precisely
- State the evidence
- Assign a verdict: TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE
- Assign confidence: HIGH / MED / LOW

For each non-finding (negative result):
- Briefly note WHY the path you investigated does NOT lead to the claim

## Failure modes to avoid

- Do NOT promote a hypothesis to TRUE without an exact source citation
- Do NOT claim "VERIFICATION FAILED" without seeing the actual log
- Do NOT speculate about line numbers; verify each one against source
- Do NOT invent function names or constants; grep first
- Do NOT trust documentation comments over actual code behavior. A doc
  comment that says "MUST NOT do X" is evidence about INTENT, not behavior.
  Verify the code does what the doc claims by tracing the call graph.
- Do NOT collapse multiple call paths into one. If a function is reached
  from path A AND path B, evaluate the hypothesis on EACH path separately.
  A compensating mechanism on path A does not retroactively protect path B.

## Output format

Markdown. Use the structure specified in the specific hypothesis prompt.
Cap total response at 800 words unless otherwise specified.

Read-only. Do NOT modify any files in /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332 or /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332.
```

---

## Notes on customization

- **`https://github.com/aeyakovenko/percolator`** etc.: fill these before sending
- **`MAX_VAULT_TVL = 1e16
MAX_ACCOUNT_POSITIVE_PNL = 1e32
MAX_POSITION_ABS_Q = 1e14
`**: agents work better when they know the engine caps. Examples: `MAX_ACCOUNTS = 4096`, `MAX_VAULT_TVL = 1e16`, `h_max = u64`.
- **`settle_after_close, fill_match, every path touching insurance_fund.balance,
use_insurance_buffer, absorb_protocol_loss, record_uninsured_protocol_loss
`**: list the BPF instructions that the agent should consider as entry points. Example: `Trade, Crank, Deposit, Withdraw, ResolveMarket, GuardianWithdrawInsurance`.

You can keep this orientation as a "system prompt" for ALL audit agents and only swap the hypothesis-specific portion. That way agents share context.

## Why this matters

Without orientation, agents will:
- Cite imagined line numbers
- Assume BPF instructions that don't exist
- Confuse engine and wrapper layers
- Speculate without source citations

With orientation, agents return tighter, more verifiable findings. This single prompt has saved hours of subsequent verification work in the Percolator audit.


---

# Prompt 08 — Invariant property definition

**Use when**: translating an English claim from the spec, code comments, or maintainer prose into a Kani-checkable assertion.

This is the prompt that turns "the maintainer says X holds" into a machine-checked theorem.

---

## Prompt template

```
You are translating an English-language safety claim into a formal property
that can be encoded as a Kani assertion.

## The English claim

Source: {SOURCE_OF_CLAIM}
  (e.g. "spec line 814", "issue #54 closure comment", "Twitter thread")

Quote: "{EXACT_PROSE}"

## Files to read

- /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/ (for the engine state struct)
- The exact source of the claim (spec section, comment, etc.)

## Method

1. Identify the variables/fields the claim references.
2. Identify the operation(s) the claim quantifies over (e.g., "after operation X")
3. Identify the timing of the claim:
   - Pre-condition: holds before operation
   - Post-condition: holds after operation
   - Invariant: holds at all times
4. Translate into Rust assertion syntax that:
   - References engine state fields by their actual names
   - Uses Rust comparison operators
   - Could appear inside a Kani harness as `assert!(...)`

## Output format

```
Original claim:    "{EXACT_PROSE}"
Source:            {SOURCE}

Variables referenced:
  - <field_name> (engine field at line N, type T)
  - ...

Quantification:
  - For all reachable engine states where {PRECONDITION}
  - After applying operation {OP}
  - The following holds: {POSTCONDITION}

Rust translation:

```rust
// Pre:
assert!(<rust expression encoding precondition>);

// Operation:
let result = engine.<op>(<args>);
kani::assume(result.is_ok());  // filter execution failures

// Post:
assert!(<rust expression encoding postcondition>);
```

Suggested Kani harness name: proof_<short_name>
Estimated harness complexity: LOW | MED | HIGH (in symbolic state size)
```

Cap at 400 words. Read-only.
```

---

## Why this is high-leverage

In the Percolator audit, this prompt produced 2 of the 10 SAFE proofs that
formally encoded the maintainer's own G3 closure statement at the wrapper
level. Quoting the maintainer's prose verbatim into the harness docstring
shows you read his words carefully and turned them into machine-checked
theorems — that's a strong signal of methodological rigor.

## Worked example

**Maintainer's prose (G3 closure)**:
> "CU exhaustion does not silently commit a partial Phase 2 sweep; the
> transaction aborts and rolls back. The engine loop advances the RR cursor
> only after the bounded sweep completes."

**Translation**:
- Variables: `rr_cursor_position` (engine field), `cursor_advanced` (boolean derived from pre/post)
- Operation: `keeper_crank_not_atomic` with CU exhaustion mid-sweep
- Quantification: For all reachable engine states + all CU exhaustion points,
  if the sweep does not complete, `rr_cursor_position` MUST equal its pre-call value

**Rust harness skeleton**:

```rust
let pre_cursor = engine.rr_cursor_position;
let pre_state_snapshot = clone_relevant_engine_state(&engine);

// Symbolic CU exhaustion: simulate by interrupting mid-loop
let result = engine.keeper_crank_not_atomic_with_cu_limit(symbolic_cu_limit);

if result.is_err() {
    // CU exhaustion (or other rollback) → cursor should NOT have advanced
    assert_eq!(engine.rr_cursor_position, pre_cursor);
}
```

Now Kani either PROVES this property (G3 closure is formally verified) or returns a CEX (the closure statement was wrong, and there's a bug).

In the Percolator audit, Kani proved it. The maintainer's prose became a machine-checked theorem.

## Customization

For claims that quantify over MULTIPLE operations (e.g., "across any sequence of N calls, X holds"), the harness becomes a small loop. Bound N aggressively (N=2 or 3) to keep the harness tractable.

For claims with implicit quantifiers ("normally" or "typically"), explicitly enumerate the conditions under which the claim is supposed to hold. Then encode those as `kani::assume()` constraints on the symbolic state.


---

# Specific hypothesis to investigate

ID:           V1-vault-residual-conservation
Claim:        The post-haircut residual cash (vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter) is conserved across every internal accounting helper. Specifically: if any helper shrinks the insurance counter, it MUST also debit the vault by the same amount.

Target file:  src/percolator.rs
Target lines: (see hypothesis brief above)
Notes:        (none)


# CODE-GROUNDED CONTEXT (actual source bytes)

These are real source-code excerpts pulled from the workspace. Cite specific line numbers from these excerpts in your verdict — DO NOT invent code that is not in this section.

### `MAX_VAULT_TVL` from `percolator.rs`
```rust
144: /// SOCIAL_WEIGHT_SCALE = ADL_ONE (spec §1.2, v12.20.6).
145: pub const SOCIAL_WEIGHT_SCALE: u128 = ADL_ONE;
146: 
147: /// SOCIAL_LOSS_DEN = 1e21 (spec §1.2, v12.20.6).
148: pub const SOCIAL_LOSS_DEN: u128 = 1_000_000_000_000_000_000_000;
149: 
150: /// Public residual B booking chunk budget. This conservative deployment
151: /// constant satisfies spec §1.4: cfg_public_b_chunk_atoms >= MAX_VAULT_TVL.
152: pub const PUBLIC_B_CHUNK_ATOMS: u128 = MAX_VAULT_TVL;
153: 
154: /// Public account-local B settlement loss budget. A single account touch will
155: /// never realize more than this many quote atoms of B loss; if a larger stale
156: /// B delta remains, the account stays B-stale and later keeper touches continue
157: /// bounded progress.
158: pub const PUBLIC_ACCOUNT_B_SETTLEMENT_LOSS_ATOMS: u128 = MAX_VAULT_TVL;

---

145: pub const SOCIAL_WEIGHT_SCALE: u128 = ADL_ONE;
146: 
147: /// SOCIAL_LOSS_DEN = 1e21 (spec §1.2, v12.20.6).
148: pub const SOCIAL_LOSS_DEN: u128 = 1_000_000_000_000_000_000_000;
149: 
150: /// Public residual B booking chunk budget. This conservative deployment
151: /// constant satisfies spec §1.4: cfg_public_b_chunk_atoms >= MAX_VAULT_TVL.
152: pub const PUBLIC_B_CHUNK_ATOMS: u128 = MAX_VAULT_TVL;
153: 
154: /// Public account-local B settlement loss budget. A single account touch will
155: /// never realize more than this many quote atoms of B loss; if a larger stale
156: /// B delta remains, the account stays B-stale and later keeper touches continue
157: /// bounded progress.
158: pub const PUBLIC_ACCOUNT_B_SETTLEMENT_LOSS_ATOMS: u128 = MAX_VAULT_TVL;
159: 

---

151: /// constant satisfies spec §1.4: cfg_public_b_chunk_atoms >= MAX_VAULT_TVL.
152: pub const PUBLIC_B_CHUNK_ATOMS: u128 = MAX_VAULT_TVL;
153: 
154: /// Public account-local B settlement loss budget. A single account touch will
155: /// never realize more than this many quote atoms of B loss; if a larger stale
156: /// B delta remains, the account stays B-stale and later keeper touches continue
157: /// bounded progress.
158: pub const PUBLIC_ACCOUNT_B_SETTLEMENT_LOSS_ATOMS: u128 = MAX_VAULT_TVL;
159: 
160: /// Active bankrupt-close residual continuation phase.
161: pub const ACTIVE_CLOSE_PHASE_NONE: u8 = 0;
162: pub const ACTIVE_CLOSE_PHASE_RESIDUAL_B: u8 = 1;
163: 
164: /// Active-close side encoding. Plain `u8` avoids enum-discriminant zero-copy
165: /// hazards for persisted state.
```

### `=` from `i128.rs`
```rust
1: // ============================================================================
2: // BPF-Safe 128-bit Types
3: // ============================================================================
4: //
5: // CRITICAL: Rust 1.77/1.78 changed i128/u128 alignment from 8 to 16 bytes on x86_64,
6: // but BPF/SBF still uses 8-byte alignment. This causes struct layout mismatches
7: // when reading/writing 128-bit values on-chain.
8: //

---

 1: // ============================================================================
 2: // BPF-Safe 128-bit Types
 3: // ============================================================================
 4: //
 5: // CRITICAL: Rust 1.77/1.78 changed i128/u128 alignment from 8 to 16 bytes on x86_64,
 6: // but BPF/SBF still uses 8-byte alignment. This causes struct layout mismatches
 7: // when reading/writing 128-bit values on-chain.
 8: //
 9: // These wrapper types use [u64; 2] internally to ensure consistent 8-byte alignment
10: // across all platforms. See: https://blog.rust-lang.org/2024/03/30/i128-layout-update.html

---

 9: // These wrapper types use [u64; 2] internally to ensure consistent 8-byte alignment
10: // across all platforms. See: https://blog.rust-lang.org/2024/03/30/i128-layout-update.html
11: //
12: // KANI OPTIMIZATION: For Kani builds, we use transparent newtypes around raw
13: // primitives. This dramatically reduces SAT solver complexity since Kani doesn't
14: // have to reason about bit-shifting and array indexing for every 128-bit operation.
15: 
16: // ============================================================================
17: // I128 - Kani-optimized version (transparent newtype)
18: // ============================================================================
19: #[cfg(kani)]
20: #[repr(transparent)]
21: #[derive(Clone, Copy, PartialEq, Eq)]
22: pub struct I128(i128);
23: 
```

### `MAX_ACCOUNT_POSITIVE_PNL` from `percolator.rs`
```rust
189: 
190: // Normative bounds (spec §1.4)
191: pub const MAX_VAULT_TVL: u128 = 10_000_000_000_000_000;
192: pub const MAX_POSITION_ABS_Q: u128 = 100_000_000_000_000;
193: pub const MAX_ACCOUNT_NOTIONAL: u128 = 100_000_000_000_000_000_000;
194: pub const MAX_TRADE_SIZE_Q: u128 = MAX_POSITION_ABS_Q; // spec §1.4
195: pub const MAX_OI_SIDE_Q: u128 = 100_000_000_000_000;
196: pub const MAX_ACCOUNT_POSITIVE_PNL: u128 = 100_000_000_000_000_000_000_000_000_000_000;
197: pub const MAX_PNL_POS_TOT: u128 = 100_000_000_000_000_000_000_000_000_000_000_000_000;
198: pub const MAX_TRADING_FEE_BPS: u64 = 10_000;
199: pub const MAX_MARGIN_BPS: u64 = 10_000;
200: pub const MAX_LIQUIDATION_FEE_BPS: u64 = 10_000;
201: pub const MAX_PROTOCOL_FEE_ABS: u128 = 1_000_000_000_000_000_000_000_000_000_000_000_000; // 10^36, spec §1.4
202: 
203: pub const MAX_WARMUP_SLOTS: u64 = u64::MAX;

---

2409:                     if self.market_mode != MarketMode::Live {
2410:                         return Err(RiskError::Unauthorized);
2411:                     }
2412:                 }
2413:             }
2414:         }
2415: 
2416:         if self.market_mode == MarketMode::Live && new_pos > MAX_ACCOUNT_POSITIVE_PNL {
2417:             return Err(RiskError::Overflow);
2418:         }
2419: 
2420:         // Pre-validate aggregate cap before mutation
2421:         if new_pos > old_pos {
2422:             let delta = new_pos - old_pos;
2423:             let new_tot = self.pnl_pos_tot.checked_add(delta).ok_or(RiskError::Overflow)?;
```

### `MAX_POSITION_ABS_Q` from `percolator.rs`
```rust
185: /// max-rate funding in one direction). Realistic operating rates are
186: /// orders of magnitude smaller; observed horizons at typical rates are
187: /// measured in decades to centuries.
188: pub const MAX_ABS_FUNDING_E9_PER_SLOT: i128 = 10_000;
189: 
190: // Normative bounds (spec §1.4)
191: pub const MAX_VAULT_TVL: u128 = 10_000_000_000_000_000;
192: pub const MAX_POSITION_ABS_Q: u128 = 100_000_000_000_000;
193: pub const MAX_ACCOUNT_NOTIONAL: u128 = 100_000_000_000_000_000_000;
194: pub const MAX_TRADE_SIZE_Q: u128 = MAX_POSITION_ABS_Q; // spec §1.4
195: pub const MAX_OI_SIDE_Q: u128 = 100_000_000_000_000;
196: pub const MAX_ACCOUNT_POSITIVE_PNL: u128 = 100_000_000_000_000_000_000_000_000_000_000;
197: pub const MAX_PNL_POS_TOT: u128 = 100_000_000_000_000_000_000_000_000_000_000_000_000;
198: pub const MAX_TRADING_FEE_BPS: u64 = 10_000;
199: pub const MAX_MARGIN_BPS: u64 = 10_000;

---

187: /// measured in decades to centuries.
188: pub const MAX_ABS_FUNDING_E9_PER_SLOT: i128 = 10_000;
189: 
190: // Normative bounds (spec §1.4)
191: pub const MAX_VAULT_TVL: u128 = 10_000_000_000_000_000;
192: pub const MAX_POSITION_ABS_Q: u128 = 100_000_000_000_000;
193: pub const MAX_ACCOUNT_NOTIONAL: u128 = 100_000_000_000_000_000_000;
194: pub const MAX_TRADE_SIZE_Q: u128 = MAX_POSITION_ABS_Q; // spec §1.4
195: pub const MAX_OI_SIDE_Q: u128 = 100_000_000_000_000;
196: pub const MAX_ACCOUNT_POSITIVE_PNL: u128 = 100_000_000_000_000_000_000_000_000_000_000;
197: pub const MAX_PNL_POS_TOT: u128 = 100_000_000_000_000_000_000_000_000_000_000_000_000;
198: pub const MAX_TRADING_FEE_BPS: u64 = 10_000;
199: pub const MAX_MARGIN_BPS: u64 = 10_000;
200: pub const MAX_LIQUIDATION_FEE_BPS: u64 = 10_000;
201: pub const MAX_PROTOCOL_FEE_ABS: u128 = 1_000_000_000_000_000_000_000_000_000_000_000_000; // 10^36, spec §1.4

---

2776:             }
2777:         }
2778: 
2779:         if new_eff_pos_q == 0 {
2780:             // Decrement-only path — no cap check needed.
2781:             self.clear_position_basis_q(idx)?;
2782:         } else {
2783:             // Spec §4.6: abs(new_eff_pos_q) <= MAX_POSITION_ABS_Q
2784:             if new_eff_pos_q.unsigned_abs() > MAX_POSITION_ABS_Q {
2785:                 return Err(RiskError::Overflow);
2786:             }
2787:             let side = side_of_i128(new_eff_pos_q).ok_or(RiskError::CorruptState)?;
2788:             self.validate_persistent_global_signed_shape()?;
2789:             if allow_spike {
2790:                 self.set_position_basis_q_allow_spike(idx, new_eff_pos_q)?;
```

### `1e14` from `percolator.rs`
```rust
125: 
126: /// POS_SCALE = 1_000_000 (spec §1.2)
127: pub const POS_SCALE: u128 = 1_000_000;
128: 
129: /// ADL_ONE = 1e15 (spec §1.3)
130: pub const ADL_ONE: u128 = 1_000_000_000_000_000;
131: 
132: /// MIN_A_SIDE = 1e14 (spec §1.4)
133: pub const MIN_A_SIDE: u128 = 100_000_000_000_000;
134: 
135: /// MAX_ORACLE_PRICE = 1_000_000_000_000 (spec §1.4)
136: pub const MAX_ORACLE_PRICE: u64 = 1_000_000_000_000;
137: 
138: /// FUNDING_DEN = 1_000_000_000 (spec §5.4)
139: pub const FUNDING_DEN: u128 = 1_000_000_000;
```

### `every` from `i128.rs`
```rust
 7: // when reading/writing 128-bit values on-chain.
 8: //
 9: // These wrapper types use [u64; 2] internally to ensure consistent 8-byte alignment
10: // across all platforms. See: https://blog.rust-lang.org/2024/03/30/i128-layout-update.html
11: //
12: // KANI OPTIMIZATION: For Kani builds, we use transparent newtypes around raw
13: // primitives. This dramatically reduces SAT solver complexity since Kani doesn't
14: // have to reason about bit-shifting and array indexing for every 128-bit operation.
15: 
16: // ============================================================================
17: // I128 - Kani-optimized version (transparent newtype)
18: // ============================================================================
19: #[cfg(kani)]
20: #[repr(transparent)]
21: #[derive(Clone, Copy, PartialEq, Eq)]
```

### `path` from `percolator.rs`
```rust
164: /// Active-close side encoding. Plain `u8` avoids enum-discriminant zero-copy
165: /// hazards for persisted state.
166: pub const ACTIVE_CLOSE_SIDE_NONE: u8 = 0;
167: pub const ACTIVE_CLOSE_SIDE_LONG: u8 = 1;
168: pub const ACTIVE_CLOSE_SIDE_SHORT: u8 = 2;
169: 
170: /// Hard cap on public active-close residual B booking attempts before the
171: /// permissionless P-last recovery path must record the remainder as non-claim
172: /// audit loss and resolve.
173: pub const ACTIVE_CLOSE_MAX_RESIDUAL_B_CHUNKS: u64 = 1;
174: 
175: /// MAX_ABS_FUNDING_E9_PER_SLOT = 10_000 (spec §1.4, parts-per-billion).
176: ///
177: /// Engine-wide ceiling on the wrapper-supplied funding rate. Deliberately
178: /// set far below the 1e9 parts-per-billion maximum so cumulative F_side_num

---

953:     ResolvedClose(ResolvedCloseResult),
954:     ActiveCloseContinued,
955:     AccountBProgress(u16),
956:     Recovered(RecoveryReason),
957:     AccountBRecovered(u16),
958: }
959: 
960: /// Pure Phase 2 cursor-scan outcome. The keeper path computes this before
961: /// mutating cursor/generation state, then performs the materialized touches.
962: test_visible_struct! {
963: #[derive(Clone, Copy, Debug, PartialEq, Eq)]
964: struct Phase2ScanOutcome {
965:     pub next_cursor: u64,
966:     pub inspected: u64,
967:     pub touched: u64,

---

1815:     /// memory is a valid discriminant for `MarketMode::Live` (0) and
1816:     /// `SideMode::Normal` (0) by construction.
1817:     ///
1818:     /// Callers that need to initialize arbitrary non-zero bytes must perform
1819:     /// pointer-level enum initialization via `MaybeUninit` or `ptr::write`
1820:     /// BEFORE forming the `&mut RiskEngine` reference — constructing the
1821:     /// reference over invalid enum discriminants is UB. This engine does
1822:     /// not ship a raw-pointer init shim; production boot paths use
1823:     /// zero-initialized SystemProgram accounts.
1824:     pub fn init_in_place(
1825:         &mut self,
1826:         params: RiskParams,
1827:         init_slot: u64,
1828:         init_oracle_price: u64,
1829:     ) -> Result<()> {
```

### `insurance_fund.balance` from `percolator.rs`
```rust
2278:         _fresh_positive_pnl: u128,
2279:         admit_h_min: u64,
2280:         admit_h_max: u64,
2281:     ) -> Result<u64> {
2282:         let senior = self
2283:             .c_tot
2284:             .get()
2285:             .checked_add(self.insurance_fund.balance.get())
2286:             .ok_or(RiskError::Overflow)?;
2287:         let residual = self
2288:             .vault
2289:             .get()
2290:             .checked_sub(senior)
2291:             .ok_or(RiskError::CorruptState)?;
2292:         Ok(if residual > 0 {

---

2321:             return Ok(());
2322:         }
2323:         if self.stress_gate_active(ctx) {
2324:             return Ok(());
2325:         }
2326: 
2327:         let senior = self.c_tot.get()
2328:             .checked_add(self.insurance_fund.balance.get())
2329:             .ok_or(RiskError::Overflow)?;
2330:         let residual = self.vault.get()
2331:             .checked_sub(senior)
2332:             .ok_or(RiskError::CorruptState)?;
2333:         let new_matured = self.pnl_matured_pos_tot
2334:             .checked_add(reserve_total)
2335:             .ok_or(RiskError::Overflow)?;

---

4808:     /// use_insurance_buffer (spec §4.11): deduct loss from insurance down to floor,
4809:     /// return the remaining uninsured loss. Losses consume the full
4810:     /// insurance balance; haircut activates when balance reaches zero.
4811:     fn use_insurance_buffer(&mut self, loss: u128) -> u128 {
4812:         if loss == 0 {
4813:             return 0;
4814:         }
4815:         let ins_bal = self.insurance_fund.balance.get();
4816:         let pay = core::cmp::min(loss, ins_bal);
4817:         if pay > 0 {
4818:             self.insurance_fund.balance = U128::new(ins_bal - pay);
4819:         }
4820:         loss - pay
4821:     }
4822: 
```

### `use_insurance_buffer` from `percolator.rs`
```rust
4811:     fn use_insurance_buffer(&mut self, loss: u128) -> u128 {
4812:         if loss == 0 {
4813:             return 0;
4814:         }
4815:         let ins_bal = self.insurance_fund.balance.get();
4816:         let pay = core::cmp::min(loss, ins_bal);
4817:         if pay > 0 {
4818:             self.insurance_fund.balance = U128::new(ins_bal - pay);
4819:         }
4820:         loss - pay
4821:     }
```

### `absorb_protocol_loss` from `percolator.rs`
```rust
4845:     fn absorb_protocol_loss(&mut self, loss: u128) {
4846:         if loss == 0 {
4847:             return;
4848:         }
4849:         let rem = self.use_insurance_buffer(loss);
4850:         self.record_uninsured_protocol_loss(rem);
4851:     }
```

### `record_uninsured_protocol_loss` from `percolator.rs`
```rust
4825:     fn record_uninsured_protocol_loss(&mut self, loss: u128) {
4826:         if loss == 0 {
4827:             return;
4828:         }
4829:         match self
4830:             .explicit_unallocated_protocol_loss
4831:             .get()
4832:             .checked_add(loss)
4833:         {
4834:             Some(v) => self.explicit_unallocated_protocol_loss = U128::new(v),
4835:             None => {
4836:                 self.explicit_unallocated_protocol_loss = U128::new(u128::MAX);
4837:                 self.explicit_unallocated_loss_saturated = 1;
4838:             }
4839:         }
4840:     }
```
