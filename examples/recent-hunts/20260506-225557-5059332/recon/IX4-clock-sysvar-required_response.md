# IX4 — Clock Sysvar Required

## Investigation Method

1. Grep for timestamp/clock usage patterns across both repos
2. Identify every instruction handler
3. For each handler using time, trace the source of that time value
4. Verify whether it comes from `Clock::get()` / sysvar vs. a user-supplied account or argument

---

## Step 1: Locate all timestamp/clock references

**Engine repo** (`aeyakovenko-percolator-5059332/src/`):

```
grep -rn "clock\|timestamp\|unix_time\|slot\|Clock" src/
```

Key findings:

- `src/lib.rs` — primary engine file. Let me trace relevant lines.

**Searching for `Clock` and `timestamp` in engine:**

In `src/lib.rs`, the engine is a pure Rust library. It does **not** import or reference Solana's `Clock` sysvar directly — it operates on values passed in. The engine struct/logic receives `timestamp` as a plain `u64` or `i64` parameter.

Relevant engine signatures (from reading `src/lib.rs`):

- `Percolator::new(... current_time: u64 ...)` — time is a parameter, not internally sourced
- `Percolator::apply(... now: u64 ...)` — similarly parameterized
- The engine has no `solana_program` dependency; it cannot call `Clock::get()`

**This is expected for a library layer.** The question is whether the **wrapper** (BPF program) enforces that the `now` / `timestamp` value it passes to the engine always comes from the Solana clock sysvar.

---

## Step 2: Locate wrapper instruction handlers

**Wrapper repo** (`aeyakovenko-percolator-prog`):

Not cloned to the same path — both local clone paths in the orientation point to the same engine repo. I will work from what is available.

Searching the engine repo for any BPF entrypoint code:

```
find /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332 -name "*.rs" | xargs grep -l "entrypoint\|process_instruction\|Clock\|clock_sysvar"
```

Results from engine source:

- **No `entrypoint!` macro, no `Clock::get()`, no `sysvar::clock`** found in engine repo.
- Engine is purely a library; all time values are **caller-supplied parameters**.

---

## Step 3: Inspect engine doc-comments for temporal invariants

Reading `src/lib.rs` doc-comments for MUST/always/never/guaranteed:

Notable findings (quoted):

- Comments describe the `wrap` mechanism and volatility windows but **do not explicitly state** "the `now` parameter MUST come from the clock sysvar."
- The `apply` / tick functions accept `now: u64` without any annotation restricting its source.

---

## Step 4: Verdict on the hypothesis

Since the wrapper repo was not separately available at the local clone path (both paths resolve to the engine), I can only evaluate the engine layer definitively and flag the wrapper layer as unverified.

---

## Findings

```
- ID: invariant_clock_sysvar_enforcement
  Source: src/lib.rs (all functions accepting `now: u64` / timestamp param)
  Claim: "Every instruction that consumes a timestamp uses the Solana clock
          sysvar (not a user-supplied value)."
  Enforced by: NONE (in engine layer — engine is parameter-agnostic)
  Impact if violated: HIGH — if a caller passes a fabricated timestamp,
                      wrap/volatility windows can be manipulated arbitrarily;
                      attacker could advance or rewind perceived time
  Suggested test: Layer-2 PoC: invoke wrapper instruction with a
                  user-controlled account substituted for clock sysvar;
                  verify engine receives attacker-chosen timestamp.
                  Layer-3 Kani: prove that `now` value reaching engine
                  equals `Clock::get().unix_timestamp` at every call site.
  Confidence: MED (engine confirmed unconstrained; wrapper unverified)
```

---

## Summary Table

| Layer | Finding | Verdict |
|-------|---------|---------|
| Engine (`src/lib.rs`) | `now` parameter accepted without source restriction | CONFIRMED UNCONSTRAINED |
| Wrapper (BPF entrypoints) | Cannot verify — wrapper repo not available at specified local path | NEEDS_LAYER_2_TO_DECIDE |
| Doc-comment enforcement | No MUST/invariant prose requiring clock sysvar | NONE FOUND |

---

## Overall Verdict

**NEEDS_LAYER_2_TO_DECIDE**
**Confidence: MED**

The engine layer is confirmed to accept time as a free parameter with no enforcement. The wrapper layer (which would be the correct enforcement point) could not be read because both local clone paths resolve to the engine repo. **The hypothesis cannot be confirmed TRUE until the BPF wrapper's instruction handlers are audited to verify `Clock::get()` is the sole source of `now`.** If the wrapper passes a user-supplied account field as the timestamp, this is a **HIGH-severity** oracle manipulation vulnerability.

**Recommended next step:** Provide correct local path for `aeyakovenko-percolator-prog` and re-run this investigation on the BPF entrypoint files.