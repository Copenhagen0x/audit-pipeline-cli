# S8 — Deposit/Withdraw Atomicity Audit

## Step 1: Repository Survey

Let me first locate the relevant files.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

I'll examine the directory structure and key files systematically.

---

## Files Examined

- `src/lib.rs` — main engine library
- `src/*.rs` — all source files in the engine

Let me trace deposit/withdraw paths carefully.

---

## Findings

### Finding 1: No Deposit/Withdraw in Engine

After grepping the engine source (`/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/`), the Percolator **engine library contains no `deposit` or `withdraw` function, instruction handler, or state field** by those names.

The engine (`aeyakovenko-percolator`) is a pure **market-making / volatility-absorption engine** — it tracks: `rr_cursor`, `sweep_generation`, `consumption`, volatility windows, and crank state. It has no vault, no token balance, no SPL-token transfer logic.

Deposit/withdraw instructions — if they exist — live exclusively in the **BPF wrapper** (`aeyakovenko-percolator-prog`, sha `04b854e5`), which was listed as the wrapper repo but whose local clone path was given as the same engine path. That repo is **not present** in the local snapshot provided.

---

### Finding 2: Engine State Fields — No Accounting Drift Risk from Engine Side

The engine state fields that could interact with a deposit/withdraw are:

| Field | File:approx-line | Notes |
|---|---|---|
| `consumption` | `src/lib.rs` | Tracks absorbed volatility units — not token amounts |
| `rr_cursor` | `src/lib.rs` | Round-robin cursor — not token amounts |
| `sweep_generation` | `src/lib.rs` | Generation counter — not token amounts |

None of these represent vault balances or user token accounts. There is no engine-level partial-commit risk for deposit/withdraw because the engine does not implement those operations.

---

### Finding 3: Wrapper Layer Not Available for Analysis

The wrapper repo (`percolator-prog`, sha `04b854e5`) — the only place deposit/withdraw BPF instructions would live — **is not present in the provided local snapshot**. The snapshot path resolves to the engine repo only:

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

Both listed paths point to the same engine clone. The wrapper is absent.

---

## Atomic Block Candidates (Engine Only)

No deposit/withdraw atomic blocks exist in the engine. The only multi-field atomic block relevant to this audit class is the **cursor-wrap block** (already identified as Bug #1 in prior layers):

```
- ID: state_transition_cursor_wrap (not deposit/withdraw)
  Block: src/lib.rs (cursor-wrap region)
  Fields written: rr_cursor=0, sweep_generation+=1, consumption=0
  Relevance to S8: NONE — this is not a vault/balance operation
```

---

## Verdict

| Sub-claim | Verdict | Confidence |
|---|---|---|
| Engine implements deposit/withdraw | **FALSE** | HIGH |
| Engine deposit/withdraw has partial-commit risk | **FALSE (vacuously)** | HIGH |
| Wrapper deposit/withdraw is atomic | **NEEDS_LAYER_2_TO_DECIDE** | HIGH |

---

## Summary

- **The engine contains no deposit or withdraw logic.** Hypothesis S8 is vacuously safe at the engine layer.
- **The wrapper layer (`percolator-prog`) is not available in the provided snapshot.** The BPF instruction handlers for deposit/withdraw — the only place this claim is testable — cannot be evaluated with the current evidence.
- **Recommended action**: Provide the wrapper repo local clone, then re-run this agent targeting the BPF entrypoint files. Specifically, look for:
  - SPL token `transfer` calls that are not bracketed by a single instruction context
  - Vault balance writes that occur before vs. after the token CPI succeeds
  - Any `try_borrow_mut_lamports` / `invoke` sequence where a partial return path skips the balance update

**No accounting drift can be confirmed or ruled out for the wrapper layer without that source.**

---

*Read-only. No files modified. All citations verified against actual source.*