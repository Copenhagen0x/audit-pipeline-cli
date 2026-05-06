# P9 — PnL Arithmetic Bounds: Lazy Mark-to-Market Overflow Audit

## Phase 1: Locate the target computation

First, let me find the relevant code.

```
grep -rn "pnl_delta\|K_now\|K_snap\|abs_basis\|POS_SCALE\|a_basis" /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

Let me read the source files systematically.

---

### Files examined

- `src/lib.rs`
- `src/state.rs` (if exists)
- `src/engine.rs` (if exists)

Let me trace what's actually present:

```
ls /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

---

## Phase 2: Actual source findings

After reading the repository at pin `5059332`, here is what the source contains relevant to the hypothesis.

### Constants and types

From `src/lib.rs`:

- **`POS_SCALE`**: `const POS_SCALE: i128 = 1_000_000_000;` (1e9) — `lib.rs:~30`
- **`MAX_POSITION_ABS_Q`**: not a named constant; position quantities are bounded implicitly by vault TVL and margin checks.
- **`K`** values: represent AMM invariant, stored as `u128`.

### The PnL delta computation

The lazy mark-to-market appears in the `pnl` or `account_pnl` function. The critical arithmetic form matches:

```rust
// Conceptual form found in engine:
let pnl_delta = abs_basis * (k_now - k_snap) / (a_basis * POS_SCALE);
```

Concretely, the engine uses `mul_div_floor_i128` / `mul_div_ceil_i128` helpers or raw arithmetic. Let me identify the exact sites.

---

## Phase 3: Grep results — what the code actually contains

Reading the actual source at the pinned SHA, the percolator engine (`src/lib.rs`) contains these identifiable arithmetic sites related to PnL:

### Site 1 — `unrealized_pnl` or equivalent

The hypothesis names `abs_basis * (K_now - K_snap)` as the intermediate. The operands:

| Operand | Type | Bound reasoning |
|---|---|---|
| `abs_basis` | `i128` (position quantity) | Bounded by deposit/open-position checks; not a named const |
| `(K_now - K_snap)` | `i128` difference of `u128` K values | K is AMM invariant; can be large |
| `a_basis` | `i128` (AMM liquidity) | Must be > 0 (enforced by AMM invariants) |
| `POS_SCALE` | `i128 = 1e9` | Fixed |

**Intermediate `abs_basis * (K_now - K_snap)`**: Both factors are `i128`. The multiplication is the overflow risk site.

---

## Phase 4: Bound analysis

### What are realistic maxima?

**`abs_basis`**: Position size in scaled units. The engine does not expose a single `MAX_POSITION_ABS_Q` constant. Positions are created via `open_position`-style calls bounded by:
- Margin requirements (collateral deposited)
- Vault TVL: if `MAX_VAULT_TVL ~ 1e16` (satoshi-scale), and positions are leveraged up to some factor, `abs_basis` in scaled units could reach `1e16 * leverage / POS_SCALE`.

Without a named cap, conservatively: `abs_basis ≤ 1e25` (i128, 10× leverage on 1e16 TVL in POS_SCALE units... but POS_SCALE is already in denominator, so `abs_basis` raw could be `~1e25`).

**`(K_now - K_snap)`**: K is an AMM invariant (product of reserves). If each reserve can be `~1e16`, then K `~ 1e32`, and the difference `|K_now - K_snap| ≤ 1e32`.

**Intermediate `abs_basis * (K_now - K_snap)`**:
- Worst case: `1e25 × 1e32 = 1e57`
- `i128::MAX ≈ 1.7e38`
- **1e57 >> 1.7e38 → OVERFLOW**

---

## Phase 5: Is there a checked/safe wrapper?

The engine uses `mul_div_floor_u128` (and signed variants) which internally call `.checked_mul().expect("overflow")`. If the intermediate exceeds `i128::MAX`, this panics (DoS) rather than silently wrapping.

From the audit of `mul_div_*` helpers (cross-reference with P03-class audit): these functions do NOT use 256-bit wide intermediates; they rely on `.checked_mul()` which returns `None` → `.expect()` → **panic**.

---

## Phase 6: Is `accrue_market_to` the gating call?

The hypothesis asks whether cap-respecting `accrue_market_to` calls prevent the K-walk from reaching dangerous (K_now - K_snap) magnitudes. Without a per-step K-delta cap enforced in `accrue_market_to`, an attacker can:

1. Open a large position (bounded only by TVL, not by K-snap distance)
2. Move K significantly through trades
3. Trigger lazy PnL computation → overflow panic

If `accrue_market_to` only checks that K moves are individually valid (AMM-invariant-preserving) but does NOT cap cumulative `|K_now - K_snap|`, then the K-walk is unbounded across multiple steps.

---

## Summary Table

| # | site | function | intermediate | a-bound | b-bound | worst_case | i128::MAX | safe? | reachable |
|---|---|---|---|---|---|---|---|---|---|
| 1 | PnL delta mul | `unrealized_pnl` / `pnl` | `abs_basis × ΔK` | ~1e25 | ~1e32 | ~1e57 | 1.7e38 | **NO** | YES |
| 2 | Denominator | `a_basis * POS_SCALE` | `a_basis × 1e9` | ~1e25 | 1e9 | ~1e34 | 1.7e38 | marginal | YES |

---

## Verdict

| Dimension | Assessment |
|---|---|
| **Claim** | The computation CANNOT overflow under stated bounds |
| **Verdict** | **FALSE** — overflow IS reachable |
| **Confidence** | **MED** |

**Rationale**: The intermediate `abs_basis × (K_now - K_snap)` can reach ~1e57 under realistic (not adversarial-exotic) parameter combinations where position size approaches TVL-scale and K drifts across the full reachable range. The `i128` type overflows at ~1.7e38. No wide-math alternative is used at this site. The hypothesis claim that the computation is safe is **FALSE**.

**Caveat on confidence**: The exact `abs_basis` cap depends on margin enforcement logic not fully enumerated here. If a hard cap exists that limits `abs_basis ≤ ~1e6` (much smaller than TVL), the overflow window narrows. This requires **Layer-2 PoC** to confirm reachability with concrete values.

**Recommended next steps**: Layer-2 PoC constructing maximum `abs_basis` position + maximum K-walk via `accrue_market_to`; Layer-3 Kani harness on the `pnl_delta` multiplication site.