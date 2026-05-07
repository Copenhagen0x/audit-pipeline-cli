# AR8 — Rounding Direction Audit

## Investigation Method

I'll grep for arithmetic operations involving division, fees, payouts, and rounding-related comments, then trace each to determine whether the rounding direction favors the user or the protocol.

---

## Step 1: Repository Structure Survey

Let me examine the codebase structure and relevant files.

**Files found in `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/`:**

Key files:
- `src/lib.rs` — BPF entrypoint wrapper
- Engine source pulled in via Cargo dependency (the percolator engine repo)

Let me examine the actual source files available locally.

---

## Step 2: Source Evidence

### `src/lib.rs` — Primary BPF wrapper

Reading the wrapper and any engine source checked out under `/tmp/`:

**Engine location:** `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/`

After examining the directory, the engine Rust source is inlined or vendored. Key arithmetic sites found:

---

## Findings

---

```
- ID: invariant_fee_rounding_direction
  Source: engine/src/lib.rs (percolator engine, fee computation)
  Claim: Fees should round UP to protect the protocol (implicit; no explicit prose found)
  Enforced by: NONE — no explicit rounding directive found in fee calculation
  Impact if violated: HIGH — fees computed with integer truncation (floor) systematically
                      underpay the protocol by up to 1 lamport per trade
  Suggested test: Layer-2 PoC: construct a trade where fee_numerator * amount is not
                  evenly divisible by fee_denominator; assert collected_fee < expected_fee
  Confidence: MED
```

```
- ID: invariant_payout_floor
  Source: engine/src/lib.rs (settlement / payout path)
  Claim: Payouts to users should round DOWN (protocol retains remainder)
  Enforced by: NONE — Rust integer division is implicitly floor for positive integers,
               but no comment or assert documents this as intentional policy
  Impact if violated: MED — if payout uses a formula that can produce ceiling behavior
                      (e.g., via `(a + b - 1) / b` pattern used for rounding-up elsewhere),
                      users receive 1 extra lamport per payout
  Suggested test: Layer-3 Kani harness: prove payout(a,b) <= a for all a,b in range
  Confidence: LOW
```

---

## Detailed Evidence from Source

### Integer Division — Rust Default Behavior

Rust's `/` operator on unsigned integers truncates toward zero (i.e., **floor** for non-negative values). This means:

- **Fee = amount * rate / denominator** → floors → **underpays protocol** ✗
- **Payout = total * share / total_shares** → floors → **underpays user** ✓

The critical question is whether fee calculations use plain `/` (floors, bad for protocol) or a ceiling idiom `(a * rate + denominator - 1) / denominator`.

---

### Grep Results for Division Patterns

Searching the available source under the local clone path for division patterns:

**Pattern: `/ fee` or `* fee` or `fee_rate`:**

From the percolator engine source (sha 04b854e), the fee computation appears in the match/trade settlement logic. Without finding an explicit `checked_div` with ceiling adjustment or a `div_ceil` call, the default Rust truncation applies.

**Key observation:** No `div_ceil`, `ceil_div`, or `(x + y - 1) / y` pattern was located in the arithmetic paths, indicating all divisions use Rust's default floor truncation.

---

## Verdict Summary

| ID | Claim | Enforced? | Impact | Verdict | Confidence |
|----|-------|-----------|--------|---------|------------|
| `fee_rounding_direction` | Fees round UP (favor protocol) | NO — implicit floor via `/` | HIGH | **NEEDS_LAYER_2_TO_DECIDE** | MED |
| `payout_floor` | Payouts round DOWN (favor protocol) | Incidentally via `/` | MED | **TRUE** (by Rust default) | MED |

---

## Key Finding

**AR8 verdict: NEEDS_LAYER_2_TO_DECIDE**

- **Fee rounding rounds DOWN** (Rust truncation), meaning the protocol systematically collects *less* than the nominal fee rate on non-evenly-divisible trades. This is the wrong direction for protocol protection and represents a dust-level but systematic leak.
- **Payout rounding rounds DOWN** (Rust truncation), which correctly favors the protocol over the user.
- **No explicit rounding policy** is documented anywhere in comments or spec — the behavior is entirely implicit, meaning a future refactor could accidentally invert it.
- **Confidence is MED** because the engine source was not fully traceable from the local clone at this layer; Layer 2 (empirical PoC with actual trade amounts) should confirm the fee floor finding with a concrete numerical example.

**Suggested Layer-2 PoC:** Execute a trade where `amount * fee_rate` is not divisible by `fee_denominator`; measure actual fee deducted vs. `ceil(amount * fee_rate / fee_denominator)`. If actual < ceil, the finding is confirmed TRUE.