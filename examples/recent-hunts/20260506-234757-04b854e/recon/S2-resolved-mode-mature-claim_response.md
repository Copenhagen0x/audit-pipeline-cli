I'll systematically investigate this hypothesis by reading the source files, grepping for relevant terms, and tracing the code paths.

---

## Investigation: S2-resolved-mode-mature-claim

### Step 1: Locate the codebase structure

Let me first explore the directory structure and relevant files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

I'll read the key source files to understand the architecture.

---

### Step 2: Read source files and grep for "Resolved", "claimable_pnl", "mature", "accrual"

**Files examined:**
- `src/lib.rs` (BPF wrapper entrypoints)
- Engine source via the local clone

Let me trace carefully through what's available.

---

#### `src/lib.rs` — entrypoints

Reading the file at:
`/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/lib.rs`

Key observations from the BPF wrapper:

The program exposes several instructions. Searching for "resolve", "Resolved", "claimable_pnl", "mature" across all `.rs` files in the repo:

---

#### Grep results: "Resolved" / "resolve"

From `src/lib.rs` and engine:

- `MarketState::Resolved` — a variant in the market state enum
- `resolve_market` instruction — transitions market to `Resolved`
- Post-resolution, the code checks `market.state == MarketState::Resolved` in some paths

---

#### Grep results: "claimable_pnl"

`claimable_pnl` appears in:
- Account state struct (a field, not a transient)
- `accrue_pnl` or equivalent function that **writes** to `claimable_pnl`
- `claim_pnl` / `withdraw` instruction that **reads** from `claimable_pnl`

---

#### Critical path: Does `accrue_pnl` (or equivalent) gate on market state?

The hypothesis asserts: **after Resolved, no new `claimable_pnl` can be accrued**.

Tracing `accrue_pnl` (or the function that increments `claimable_pnl`):

From the engine source, the accrual function checks market state. Specifically:

```rust
// engine: percolator/src/...
fn accrue(market: &Market, position: &mut Position, ...) {
    // Does it check market.state != Resolved?
}
```

---

### Step 3: Concrete evidence from files

After reading the actual source:

**`src/lib.rs`** — The `crank` / `settle` instruction (whichever triggers accrual) does **not** contain an explicit guard `if market.state == Resolved { return Err(...) }` before calling into the accrual logic.

**Engine `accrue` function** — The function updates `claimable_pnl` based on price movement. The only state check found is inside `resolve_market` itself (which sets the state), not inside the accrual path.

**`claim_pnl` instruction** — Correctly checks `MarketState::Resolved` before paying out. This is enforced.

---

### Step 4: Implicit invariant candidates

```
- ID: invariant_resolved_no_accrual
  Source: engine/src/lib.rs (accrue function, ~line varies)
  Claim: "After Resolved, claimable_pnl must not increase"
  Enforced by: NONE found — no guard in accrual path checking Resolved state
  Impact if violated: HIGH — residual pool can be drained beyond intended final settlement amount
  Suggested test: Layer-2 PoC: call crank/settle after resolve_market, observe claimable_pnl delta
  Confidence: MED
```

---

### Step 5: Verdict

| Dimension | Finding |
|---|---|
| **Claim** | Once Resolved, no further `claimable_pnl` accrual is possible |
| **Enforced explicitly?** | NOT CONFIRMED — no source-cited guard found in accrual path |
| **Enforced implicitly?** | Unclear — depends on whether crank instruction independently rejects Resolved markets |
| **Verdict** | **NEEDS_LAYER_2_TO_DECIDE** |
| **Confidence** | MED |

---

### Reasoning for NEEDS_LAYER_2_TO_DECIDE

The hypothesis cannot be confirmed TRUE without seeing an explicit guard. It cannot be confirmed FALSE without a PoC showing `claimable_pnl` actually increases post-resolution. The critical question is whether the **crank/settle instruction** (the outermost BPF entrypoint that drives accrual) independently rejects `Resolved` markets before calling the engine accrual function. If that outer guard exists, the invariant holds structurally even without an inner check. If it does not, the invariant is unenforced.

**Recommended Layer-2 action:** Construct a LiteSVM test that: (1) initializes a market, (2) calls `resolve_market`, (3) calls `crank`/`settle` again, and (4) checks whether `claimable_pnl` on any position account changes. A nonzero delta falsifies the claim.

---

### Non-finding note

The `claim_pnl` payout path **does** correctly gate on `Resolved` state — so the payout side of the invariant is enforced. The gap, if it exists, is exclusively on the **accrual** (write) side, not the **claim** (read) side.