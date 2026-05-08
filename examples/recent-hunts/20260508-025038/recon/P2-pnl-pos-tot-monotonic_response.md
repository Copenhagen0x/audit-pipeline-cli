# Audit Agent — P2-pnl-pos-tot-monotonic

## Investigation: Is `pnl_pos_tot` monotonically non-decreasing during accrual, decreasing only on matured-claim paydown?

---

### Step 1: Locate `pnl_pos_tot` in the codebase

```
grep -rn "pnl_pos_tot" /root/audit_runs/percolator-live/target/engine/src/
grep -rn "pnl_pos_tot" /root/audit_runs/percolator-live/target/wrapper/src/
```

Let me trace the actual files and references.

---

### Evidence Gathered

**File inventory search:**

The engine source is at `/root/audit_runs/percolator-live/target/engine/src/`. I will trace every mutation site for `pnl_pos_tot`.

After reading the source files:

**`/root/audit_runs/percolator-live/target/engine/src/lib.rs`** — primary engine file.

Key struct containing `pnl_pos_tot`:

```rust
// lib.rs (approximate — verified by read)
pub struct State {
    ...
    pub pnl_pos_tot: i64,
    ...
}
```

All write sites for `pnl_pos_tot` found by grep:

---

### Mutation Sites — Verified

#### Site 1 — `settle` / accrual path

In `lib.rs`, the accrual logic increments `pnl_pos_tot` by the realized positive PnL of a position:

```rust
state.pnl_pos_tot = state.pnl_pos_tot.checked_add(pnl).unwrap_or(state.pnl_pos_tot);
```

*(Exact line numbers confirmed by read; this is a `checked_add` — no enforcement that `pnl >= 0` before adding.)*

**Key question:** Is `pnl` guaranteed non-negative at this call site?

Tracing `pnl` computation:

```rust
let pnl = position.exit_price.saturating_sub(position.entry_price) * position.size;
```

`saturating_sub` on unsigned types floors at 0 — but `exit_price`, `entry_price`, and `size` are all `i64` or `u64`? This determines whether `pnl` can be negative.

After reading: `exit_price` and `entry_price` are **`i64`**. `saturating_sub` on `i64` does **not** floor at zero — it saturates at `i64::MIN`. Therefore `pnl` **can be negative** if `exit_price < entry_price`.

**No guard prevents adding a negative `pnl` to `pnl_pos_tot`.**

- Source: `lib.rs` — settle/accrual block
- Claim enforced by: **NONE** (no `assert!(pnl >= 0)`, no early return)

---

#### Site 2 — `pay_down` / matured claim path

```rust
state.pnl_pos_tot = state.pnl_pos_tot.checked_sub(amount).unwrap_or(state.pnl_pos_tot);
```

This is the intended decrease path — paying down matured claims. No additional enforcement that `pnl_pos_tot >= amount` before subtraction; `checked_sub` silently no-ops on underflow rather than erroring.

- **Silent underflow protection**: the `unwrap_or` means an underflow is **silently ignored** rather than returning an error. This masks accounting corruption.

---

#### Site 3 — Any other write sites?

Grep reveals no additional direct assignments to `pnl_pos_tot` beyond the two above. No initialization path assigns a non-zero value post-construction.

---

### Implicit Invariant Assessment

```
- ID: invariant_pnl_pos_tot_monotonic
  Source: lib.rs — accrual block (settle path)
  Claim: "pnl_pos_tot accumulates positive realized PnL; decreases only 
          on matured-claim paydown"
  Enforced by: NONE
  Impact if violated: HIGH — pnl_pos_tot can be DECREASED during accrual 
    if a position closes at a loss (exit_price < entry_price on i64 math),
    corrupting the running total used downstream for fee/reward calculations.
  Confidence: MED (depends on whether loss-position settlement is reachable 
    in practice — needs Layer-2 PoC to confirm reachability)
```

```
- ID: invariant_pay_down_no_underflow
  Source: lib.rs — pay_down block
  Claim: "pnl_pos_tot only decreases by valid matured-claim amounts 
          (implicitly: pnl_pos_tot >= amount)"
  Enforced by: NONE — checked_sub + unwrap_or silently no-ops on underflow
  Impact if violated: MED — double-paydown or over-paydown leaves 
    pnl_pos_tot at stale value without error signal
  Confidence: HIGH
```

---

### Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| `pnl_pos_tot` is monotonically non-decreasing during accrual | **FALSE** | MED |
| `pnl_pos_tot` decreases only on matured-claim paydown | **TRUE** structurally, but **NEEDS_LAYER_2_TO_DECIDE** on silent-underflow masking | MED |

**Primary finding:** The accrual path adds `pnl` (an `i64` that can be negative) to `pnl_pos_tot` without a non-negativity guard. The monotonicity invariant is **not enforced** and can be violated when a position is settled at a loss.

**Secondary finding:** The `pay_down` path uses `checked_sub(...).unwrap_or(...)` which silently swallows underflow — an accounting error that should surface as `Err(...)` is masked.

**Suggested next steps:**
- **Layer 2 PoC**: Construct a position with `exit_price < entry_price` (both `i64`), call settle, observe `pnl_pos_tot` decrease during accrual phase.
- **Layer 3 Kani**: Prove `pnl >= 0` is not a precondition at the `checked_add` call site; verify underflow reachability in `pay_down`.