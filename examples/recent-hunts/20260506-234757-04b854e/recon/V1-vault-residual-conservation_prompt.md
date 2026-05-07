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
- Engine pin (sha):  04b854e
- Wrapper repo:      https://github.com/aeyakovenko/percolator-prog
- Wrapper pin (sha): 04b854e5718112f42ebba9c208335a22132075ad

Local clones (read-only):
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e

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

Read-only. Do NOT modify any files in /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e or /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e.
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

- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/ (for the engine state struct)
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
5333:                         return Err(PercolatorError::OracleInvalid.into());
5334:                     }
5335:                     p
5336:                 } else {
5337:                     initial_mark_price_e6
5338:                 };
5339: 
5340:                 // Validate new_account_fee: 0 ≤ fee ≤ MAX_VAULT_TVL.
5341:                 if new_account_fee > percolator::MAX_VAULT_TVL {
5342:                     return Err(ProgramError::InvalidInstructionData);
5343:                 }
5344:                 // Scale alignment: `new_account_fee` is paid in BASE units
5345:                 // and split out of `fee_payment` at InitUser/InitLP. The
5346:                 // wrapper rejects misaligned `fee_payment` (the outer
5347:                 // dust check), but the SPLIT into (fee, capital) can

---

5334:                     }
5335:                     p
5336:                 } else {
5337:                     initial_mark_price_e6
5338:                 };
5339: 
5340:                 // Validate new_account_fee: 0 ≤ fee ≤ MAX_VAULT_TVL.
5341:                 if new_account_fee > percolator::MAX_VAULT_TVL {
5342:                     return Err(ProgramError::InvalidInstructionData);
5343:                 }
5344:                 // Scale alignment: `new_account_fee` is paid in BASE units
5345:                 // and split out of `fee_payment` at InitUser/InitLP. The
5346:                 // wrapper rejects misaligned `fee_payment` (the outer
5347:                 // dust check), but the SPLIT into (fee, capital) can
5348:                 // still produce dust on each side if `new_account_fee`

---

5443:                 // mark_min_fee upper bound: prevent setting so high that
5444:                 // EWMA never updates. Compare in u128 — MAX_PROTOCOL_FEE
5445:                 // _ABS is 10^36, casting to u64 wraps modulo 2^64 and
5446:                 // yields a meaningless threshold. mark_min_fee is u64
5447:                 // so under the 10^36 ceiling this check is effectively
5448:                 // "always passes", which matches the spec (no tighter
5449:                 // per-market cap today). If a tighter cap is desired in
5450:                 // the future (e.g., MAX_VAULT_TVL-scaled), replace the
5451:                 // ceiling here.
5452:                 if (mark_min_fee as u128) > percolator::MAX_PROTOCOL_FEE_ABS {
5453:                     return Err(PercolatorError::InvalidConfigParam.into());
5454:                 }
5455: 
5456:                 // F2: Hyperp liveness spoof defense. When a Hyperp
5457:                 // market enables permissionless resolution, the ONLY
```

### `=` from `percolator.rs`
```rust
11: 
12: // 1. mod constants
13: pub mod constants {
14:     use crate::state::{MarketConfig, SlabHeader};
15:     use core::mem::{align_of, size_of};
16:     use percolator::RiskEngine;
17: 
18:     pub const MAGIC: u64 = 0x504552434f4c4154; // "PERCOLAT"
19: 
20:     pub const HEADER_LEN: usize = size_of::<SlabHeader>();
21:     pub const CONFIG_LEN: usize = size_of::<MarketConfig>();
22:     pub const ENGINE_ALIGN: usize = align_of::<RiskEngine>();
23: 
24:     pub const fn align_up(x: usize, a: usize) -> usize {
25:         (x + (a - 1)) & !(a - 1)

---

13: pub mod constants {
14:     use crate::state::{MarketConfig, SlabHeader};
15:     use core::mem::{align_of, size_of};
16:     use percolator::RiskEngine;
17: 
18:     pub const MAGIC: u64 = 0x504552434f4c4154; // "PERCOLAT"
19: 
20:     pub const HEADER_LEN: usize = size_of::<SlabHeader>();
21:     pub const CONFIG_LEN: usize = size_of::<MarketConfig>();
22:     pub const ENGINE_ALIGN: usize = align_of::<RiskEngine>();
23: 
24:     pub const fn align_up(x: usize, a: usize) -> usize {
25:         (x + (a - 1)) & !(a - 1)
26:     }
27: 

---

14:     use crate::state::{MarketConfig, SlabHeader};
15:     use core::mem::{align_of, size_of};
16:     use percolator::RiskEngine;
17: 
18:     pub const MAGIC: u64 = 0x504552434f4c4154; // "PERCOLAT"
19: 
20:     pub const HEADER_LEN: usize = size_of::<SlabHeader>();
21:     pub const CONFIG_LEN: usize = size_of::<MarketConfig>();
22:     pub const ENGINE_ALIGN: usize = align_of::<RiskEngine>();
23: 
24:     pub const fn align_up(x: usize, a: usize) -> usize {
25:         (x + (a - 1)) & !(a - 1)
26:     }
27: 
28:     pub const ENGINE_OFF: usize = align_up(HEADER_LEN + CONFIG_LEN, ENGINE_ALIGN);
```

### `every` from `percolator.rs`
```rust
111:         "KeeperCrank candidate Phase 1 + Phase 2 must fit engine touched-account capacity"
112:     );
113: 
114:     // ── Engine envelope constants (wrapper-owned, immutable per deployment) ──
115:     //
116:     // These values populate the engine's per-market RiskParams envelope at
117:     // InitMarket. They are NOT decoded from instruction data and NOT admin-
118:     // configurable — every deployment uses these exact values. The envelope
119:     // invariant
120:     //   ADL_ONE * MAX_ORACLE_PRICE * MAX_ABS_FUNDING_E9_PER_SLOT *
121:     //     MAX_ACCRUAL_DT_SLOTS <= i128::MAX
122:     // must hold: 1e15 * 1e12 * 1e4 * 100 = 1e33 < i128::MAX (≈1.7e38). ✓
123:     //
124:     // Tightened from the prior stress-test values (1e6 rate / 1e5 dt) to the
125:     // production-aligned trade-off (low rate cap, long accrual window) in

---

175:     ///     MAX_ABS_FUNDING_E9_PER_SLOT = 10^4
176:     /// the lifetime ceiling is
177:     ///     i128::MAX / (10^15 · 10^12 · 10^4)  ≈ 1.7 × 10^7 slots
178:     ///
179:     /// ═════════════════════════════════════════════════════════════════
180:     /// OPERATIONAL ASSUMPTION — accepted finite market lifetime
181:     /// ═════════════════════════════════════════════════════════════════
182:     /// The engine does not expose an F-index rebase, so every deployed
183:     /// market has a finite cumulative-funding lifetime bounded by the
184:     /// envelope above. At 400 ms/slot (~7.89 × 10^7 slots/year), the
185:     /// worst-case lifetimes at sustained max-rate funding are:
186:     ///
187:     /// ```text
188:     /// rate <= 10_000 (global max)  ⇒ ~1.7e7 slots  ≈ 2.6 months
189:     /// rate <=  1_000               ⇒ ~1.7e8 slots  ≈ 2.15 years

---

1211:             return None;
1212:         }
1213:         Some(inverted as u64)
1214:     }
1215: 
1216:     /// Convert a raw oracle price to engine-space: invert then scale.
1217:     /// All Hyperp internal prices (hyperp_mark_e6, last_effective_price_e6)
1218:     /// must be in engine-space. Apply this at every ingress point:
1219:     /// InitMarket, PushHyperpMark, TradeCpi mark-update.
1220:     #[inline]
1221:     pub fn to_engine_price(raw: u64, invert: u8, unit_scale: u32) -> Option<u64> {
1222:         let after_invert = invert_price_e6(raw, invert)?;
1223:         scale_price_e6(after_invert, unit_scale)
1224:     }
1225: 
```

### `path` from `percolator.rs`
```rust
 87:     /// Engine Phase 2 inspected-slot cap. This is intentionally explicit even
 88:     /// when the deployment wants full-slab greedy progress: avoid delegating
 89:     /// unbounded `u64::MAX` scan policy to the engine API.
 90:     pub const RR_SCAN_LIMIT_PER_CRANK: u64 = percolator::MAX_ACCOUNTS as u64;
 91: 
 92:     // Compile-time invariant: the crank's total fee-sync budget
 93:     // (FEE_SWEEP_BUDGET) must accommodate the wrapper's per-crank
 94:     // liquidation/candidate-sync allowance. The candidate-sync path
 95:     // caps itself at min(LIQ_BUDGET_PER_CRANK, FEE_SWEEP_BUDGET); if
 96:     // this constant were raised above FEE_SWEEP_BUDGET the belt-and-
 97:     // braces min() would silently under-apply the budget. Assert so
 98:     // a mismatch is a build error.
 99:     const _: () = assert!(
100:         (LIQ_BUDGET_PER_CRANK as usize) <= FEE_SWEEP_BUDGET,
101:         "LIQ_BUDGET_PER_CRANK must not exceed FEE_SWEEP_BUDGET"

---

206:     ///   (c) Engine-side F-index rebase (out of wrapper scope).
207:     ///
208:     /// Admin-free deployments that intend to run indefinitely should
209:     /// treat the theoretical floor as a LIVENESS BUDGET: once the
210:     /// cumulative funding envelope is exhausted, future accrue_market
211:     /// _to calls saturate and the market effectively freezes. At that
212:     /// point `permissionless_resolve_stale_slots` is the fallback exit
213:     /// path for users. Operators MUST set that field > 0 on admin-
214:     /// free markets (a zero value combined with envelope exhaustion
215:     /// would trap capital).
216:     pub const MIN_FUNDING_LIFETIME_SLOTS: u64 = 10_000_000;
217:     pub const MATCHER_ABI_VERSION: u32 = 2;
218:     pub const MATCHER_CONTEXT_LEN: usize = 320;
219:     /// Maximum variadic accounts forwarded by TradeCpi to the matcher.
220:     /// Transaction account limits are an outer bound; this protocol cap keeps

---

273:     /// h_max is independent from `max_accrual_dt_slots` and oracle staleness.
274:     /// 6_480_000 slots is ~30 days at 400 ms/slot.
275:     pub const MAX_PROFIT_MATURITY_SLOTS: u64 = 6_480_000;
276:     /// Maximum hard oracle-staleness horizon for permissionless market
277:     /// resolution.
278:     ///
279:     /// This is a product liveness bound, NOT an accrual envelope and NOT
280:     /// coupled to `MAX_ACCRUAL_DT_SLOTS`. The two paths are deliberately
281:     /// separate:
282:     ///
283:     /// - live market progress uses KeeperCrank and is chunked by
284:     ///   `MAX_ACCRUAL_DT_SLOTS`;
285:     /// - dead-oracle exit uses ResolvePermissionless, which calls the
286:     ///   engine's Degenerate resolve arm at P_last with rate=0 and does not
287:     ///   call `accrue_market_to`.
```

### `touching` from `percolator.rs`
```rust
907:         /// Reject the trade - nonce unchanged, no engine call
908:         Reject,
909:         /// Accept the trade - nonce incremented, engine called with chosen_size
910:         Accept { new_nonce: u64, chosen_size: i128 },
911:     }
912: 
913:     /// Pure decision function for TradeCpi instruction.
914:     /// Models the wrapper's full policy without touching the risk engine.
915:     ///
916:     /// # Arguments
917:     /// * `old_nonce` - Current nonce before this trade
918:     /// * `shape` - Matcher account shape validation inputs
919:     /// * `identity_ok` - Whether matcher identity matches LP registration
920:     /// * `pda_ok` - Whether LP PDA matches expected derivation
921:     /// * `abi_ok` - Whether matcher return passes ABI validation

---

1687:         EngineAccountKindMismatch,
1688:         InvalidTokenAccount,
1689:         InvalidTokenProgram,
1690:         InvalidConfigParam,
1691:         HyperpTradeNoCpiDisabled,
1692:         EngineCorruptState,
1693:         /// Wrapper-level: the requested operation cannot safely advance market
1694:         /// time. Caller must run KeeperCrank to commit account-touching market
1695:         /// progress, then retry the original operation.
1696:         CatchupRequired,
1697:         /// Deposit rejected: post-deposit `c_tot` would exceed
1698:         /// `tvl_insurance_cap_mult * insurance_fund.balance`.
1699:         /// Only triggered when the admin has enabled the cap via UpdateConfig.
1700:         DepositCapExceeded,
1701:         /// `WithdrawInsuranceLimited` called within the configured

---

2234:                 29 => Ok(Instruction::ResolvePermissionless),
2235:                 30 => {
2236:                     let user_idx = read_u16(&mut rest)?;
2237:                     Ok(Instruction::ForceCloseResolved { user_idx })
2238:                 }
2239:                 // Tag 31 (CatchupAccrue) retired. Public market-clock progress
2240:                 // is routed through KeeperCrank so exposed markets get an
2241:                 // account-touching revalidation/liquidation turn.
2242:                 31 => return Err(ProgramError::InvalidInstructionData),
2243:                 32 => {
2244:                     // UpdateAuthority { kind: u8, new_pubkey: [u8; 32] }
2245:                     let kind = read_u8(&mut rest)?;
2246:                     let new_pubkey = read_pubkey(&mut rest)?;
2247:                     Ok(Instruction::UpdateAuthority { kind, new_pubkey })
2248:                 }
```

### `insurance_fund.balance` from `percolator.rs`
```rust
1691:         HyperpTradeNoCpiDisabled,
1692:         EngineCorruptState,
1693:         /// Wrapper-level: the requested operation cannot safely advance market
1694:         /// time. Caller must run KeeperCrank to commit account-touching market
1695:         /// progress, then retry the original operation.
1696:         CatchupRequired,
1697:         /// Deposit rejected: post-deposit `c_tot` would exceed
1698:         /// `tvl_insurance_cap_mult * insurance_fund.balance`.
1699:         /// Only triggered when the admin has enabled the cap via UpdateConfig.
1700:         DepositCapExceeded,
1701:         /// `WithdrawInsuranceLimited` called within the configured
1702:         /// `insurance_withdraw_cooldown_slots` window.
1703:         InsuranceWithdrawCooldown,
1704:         /// `WithdrawInsuranceLimited` amount exceeds
1705:         /// `insurance_withdraw_max_bps * insurance_fund.balance / 10_000`

---

1698:         /// `tvl_insurance_cap_mult * insurance_fund.balance`.
1699:         /// Only triggered when the admin has enabled the cap via UpdateConfig.
1700:         DepositCapExceeded,
1701:         /// `WithdrawInsuranceLimited` called within the configured
1702:         /// `insurance_withdraw_cooldown_slots` window.
1703:         InsuranceWithdrawCooldown,
1704:         /// `WithdrawInsuranceLimited` amount exceeds
1705:         /// `insurance_withdraw_max_bps * insurance_fund.balance / 10_000`
1706:         /// (with a minimum floor of 10 units to avoid Zeno's-paradox lockout
1707:         /// at small bps × small insurance).
1708:         InsuranceWithdrawCapExceeded,
1709:         /// Engine reported that bounded public progress cannot continue
1710:         /// without an explicit terminal-recovery path.
1711:         EngineRecoveryRequired,
1712:     }

---

1858:         AdminForceCloseAccount {
1859:             user_idx: u16,
1860:         },
1861:         /// BOUNDED live insurance withdrawal. Gated by
1862:         /// `header.insurance_operator` (distinct from `insurance_authority`
1863:         /// — the split is what makes the bounds meaningful). Per-call
1864:         /// amount capped at `config.insurance_withdraw_max_bps *
1865:         /// insurance_fund.balance / 10_000` with a floor of 10 units
1866:         /// (anti-Zeno). Calls must be at least
1867:         /// `config.insurance_withdraw_cooldown_slots` apart. Works on
1868:         /// LIVE markets only; resolved markets use the unbounded tag 20.
1869:         WithdrawInsuranceLimited {
1870:             amount: u64,
1871:         },
1872:         /// Direct fee-debt repayment (§10.3.1). Owner only.
```
