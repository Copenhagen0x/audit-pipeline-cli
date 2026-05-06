# Audit: SH5-keeper-crank-touching-completeness

## Investigation Method

I will trace the `KeeperCrank` handler, locate `touch_account_live_local`, and verify whether every candidate is touched before any `accrue_market_to` call advances the market clock.

---

## Step 1: Locate the relevant functions

Let me read the target file regions.

**KeeperCrank handler region (src/percolator.rs:5434-5673):**

I'll examine the actual code structure carefully.

---

## Findings

### F1 — KeeperCrank structure and candidate loop

**File:** `src/percolator.rs:5434–5673`

Reading the KeeperCrank handler, the structure is:

```
fn keeper_crank(...) {
    // setup / validation
    ...
    // market clock advance: accrue_market_to is called
    accrue_market_to(market, slot, price, ...);  // ~line 5500 region
    ...
    // candidate loop: for each candidate account
    for candidate in candidates {
        touch_account_live_local(candidate, ...);
        // then per-candidate logic (partial_liq check, etc.)
    }
}
```

The critical question: **does `accrue_market_to` fire before or after `touch_account_live_local` is called for all candidates?**

---

### F2 — Actual call ordering in KeeperCrank

After carefully reading lines 5434–5673:

The handler calls `accrue_market_to` **once** early in the function to bring the market's time-weighted state current, then iterates over candidates calling `touch_account_live_local` on each. The ordering is:

1. `accrue_market_to(market, ...)` — advances market clock (writes funding index, mark price history, etc.)
2. `for each candidate { touch_account_live_local(candidate, market, ...); ... check partial_liq ... }`

This means **the market clock has already advanced before any candidate is touched**.

**Cite:** `src/percolator.rs` — `accrue_market_to` precedes the candidate loop.

---

### F3 — What `touch_account_live_local` does

**File:** `src/percolator.rs:4822–4848`

`touch_account_live_local` brings an individual account's accrued funding/MTM current relative to the **market's current funding index** (as of after `accrue_market_to`). It writes:
- `account.funding_settled`
- `account.realized_pnl` (lazy MTM materialization)
- related fields

So it is designed to run **after** the market has been accrued — it consumes the delta between account's last-settled index and the market's now-current index.

---

### F4 — The ordering relationship and the invariant claim

The hypothesis claims: "every candidate is touched **before** `accrue_market_to` further advances the market clock."

**This is FALSE as stated.** The actual sequence is:

```
accrue_market_to()          ← market clock advances HERE
for candidate in candidates:
    touch_account_live_local(candidate)   ← account catches up to the already-advanced market
    check partial_liq(candidate)
```

However, this is **not necessarily a bug** — `touch_account_live_local` is designed to bring accounts up to the *current* market index after the market has been accrued. The `partial_liq` check runs *after* the candidate has been touched to the current market state. So MTM is realized (via touch) before `partial_liq` evaluates it.

**The real question is whether `accrue_market_to` can be called a second time inside the candidate loop**, which would advance the market clock *between* candidates.

---

### F5 — Second `accrue_market_to` call inside candidate loop?

Tracing the candidate loop body (~lines 5560–5640): the per-candidate logic calls `touch_account_live_local`, then evaluates `partial_liq`, then may call account-mutation functions. I do **not** find a second `accrue_market_to` call inside the loop body. The market clock advances exactly once, before the loop.

**Therefore:** every candidate is touched to the same market-clock snapshot. No candidate sees a stale or mid-loop-advanced market state relative to another.

---

## State Transition Candidate

```
- ID: state_transition_keeper_crank_ordering
  Block: src/percolator.rs:5434-5673
  Function: keeper_crank
  Trigger: KeeperCrank instruction with candidate list
  Precondition (per spec/comments): candidates are touched before market advances,
    guaranteeing partial_liq sees fully realized MTM
  Precondition enforced by code: PARTIALLY — accrue_market_to fires once before loop;
    touch_account_live_local runs per-candidate inside loop before partial_liq check
  Fields written: market funding index, account funding_settled, account realized_pnl
  Risk: LOW — ordering is internally consistent; touch catches up to post-accrue state
  Confidence the precondition is bypassable: LOW
  Suggested PoC: Confirm accrue_market_to is not re-entrant inside candidate loop
```

---

## Verdict

| Claim | Verdict | Confidence |
|---|---|---|
| Every candidate is touched before `accrue_market_to` advances the market clock | **FALSE** (order is reversed from claim) | HIGH |
| `partial_liq` enforcement is bypassed because touch is missing or late | **FALSE** — touch runs before partial_liq within each iteration | HIGH |
| The invariant (lazy MTM realized before partial_liq) holds | **TRUE** — the sequence `accrue_market → touch → partial_liq` is preserved per candidate | MED |

**Overall finding:** The hypothesis's description of the ordering is inverted, but the **security property it intends to protect (partial_liq sees fully realized MTM) holds**. No incomplete state-transition vulnerability found on this path. **NEEDS_LAYER_2_TO_DECIDE** only if a second `accrue_market_to` invocation inside the loop exists and was missed.