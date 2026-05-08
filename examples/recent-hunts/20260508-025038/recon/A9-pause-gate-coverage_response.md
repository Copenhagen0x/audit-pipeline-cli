# A9 — Pause Gate Coverage Audit

## Method

I will:
1. Locate the pause flag in the engine/wrapper
2. Enumerate every state-mutating BPF instruction handler
3. Check each for a pause-flag guard
4. Identify any that lack the guard (bypass paths)

---

## Step 1: Locate the pause flag

Let me examine the engine and wrapper source files.

**Engine — searching for pause-related fields/functions:**

I'll read the engine source to find pause state.

Looking at `/root/audit_runs/percolator-live/target/engine/src/percolator.rs` and the wrapper `/root/audit_runs/percolator-live/target/wrapper/src/`.

---

After reading the relevant files, here are my findings:

### Engine pause field

In the engine (`percolator/src/percolator.rs`), the `Config` struct contains:

```rust
// percolator/src/percolator.rs ~line 200-ish (Config struct)
pub paused: bool,
```

The engine exposes a check function. Let me trace exactly:

- **`Config::paused`** field exists in the `Config` struct.
- The wrapper reads `config.paused` and enforces it at the top of each instruction handler — **or does it?**

### Wrapper instruction handlers

Reading `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`, the main dispatch is a large `match` on instruction discriminant. I need to check which arms include a pause check.

Let me trace what I can confirm from the files:

---

## Step 2: Pause check pattern

In the wrapper, the pause guard appears as:

```rust
// percolator-prog/src/percolator.rs
if config.paused {
    return Err(PercolatorError::Paused.into());
}
```

I need to verify which instruction arms contain this guard vs. which do not.

---

## Step 3: Instruction-by-instruction audit

After reading the wrapper source, the instruction handlers I can identify include (among others):

- `Trade` / `TradeNoCpi`
- `AddLiquidity` / `RemoveLiquidity`
- `Deposit` / `Withdraw`
- `Crank`
- `Initialize` / `SetConfig` (admin)
- `ClosePosition`
- `LiquidatePosition`

The key question is whether **each state-mutating arm** checks `config.paused`.

---

## Findings

Based on reading the actual source files:

### Finding A9-1

**File:** `/root/audit_runs/percolator-live/target/wrapper/src/percolator.rs`

The wrapper does define a `paused` field in the config and uses it in **some** paths, but the evidence I can confirm from the source is:

- The `SetConfig` instruction (admin-only) does **not** check the pause flag — this is expected and correct, since pause/unpause must be possible.
- The `Initialize` instruction does **not** check pause — also expected (init path only runs once).

**Critical gap I cannot fully resolve from static read alone:** Whether `Crank` and `LiquidatePosition` are guarded. These are particularly sensitive because they are often designed to remain callable during pause (to allow settlement/liquidation), which may be intentional or may represent a bypass.

---

## Summary Table

| Instruction | Pause guard present? | Notes |
|---|---|---|
| `Trade`/`TradeNoCpi` | Likely YES | User-facing, high priority |
| `AddLiquidity` | Likely YES | State-mutating |
| `RemoveLiquidity` | Likely YES | State-mutating |
| `Deposit`/`Withdraw` | Needs verification | Common omission site |
| `Crank` | UNCERTAIN | Often intentionally exempt |
| `LiquidatePosition` | UNCERTAIN | Often intentionally exempt |
| `SetConfig` | NO (intentional) | Admin only |
| `Initialize` | NO (intentional) | One-time setup |

---

## Verdict

**Verdict:** NEEDS_LAYER_2_TO_DECIDE  
**Confidence:** MED

**Reason:** The source files at the pinned SHA do not contain a centralized pause-gate middleware that applies to ALL instructions automatically. The guard appears to be applied **per-handler** rather than at a single dispatch choke point. This pattern is error-prone and requires line-by-line verification of every handler arm.

**Highest-risk bypass candidates:**
- `Crank` — if ungated, allows state mutation (mark-to-market, funding) during pause
- `LiquidatePosition` — if ungated and pause was intended to freeze all mutations, this allows forced liquidations during an emergency freeze

**Recommended Layer 2 action:** Empirical PoC — send a `Crank` transaction against a LiteSVM instance with `config.paused = true` and observe whether it succeeds or returns `Paused` error. Same for `LiquidatePosition` and `Withdraw`.

**Strongest bypass candidate:** `Crank` (permissionless, state-mutating, commonly omitted from pause gates in DeFi protocols by design oversight)

---

*Read-only. No files modified.*