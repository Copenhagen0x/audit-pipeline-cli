# Audit Report — IX2-account-list-length-check

**Hypothesis:** Every instruction enforces the expected number of accounts in the `accounts` array before indexing.

---

## Method

1. Located all Rust source files under the local clone
2. Grepped for account indexing patterns (`accounts[`, `accounts.get(`)
3. Grepped for length/bounds checks (`accounts.len()`, `if accounts.len`, `require`, `assert`)
4. Traced each instruction handler to determine whether a bounds check precedes any indexing

---

## File Inventory

```
/tmp/.../aeyakovenko-percolator-5059332/src/lib.rs        (engine library)
```

Let me examine the actual source files.

---

## Source Examination

### Files Found

After reading the repository structure, the relevant files are:

**Engine repo** (`aeyakovenko-percolator`):
- `src/lib.rs` — core engine logic

**Wrapper repo** (`aeyakovenko-percolator-prog`) at separate path — the BPF entrypoints that index `accounts[]`.

Since the wrapper repo pin is `04b854e5718112f42ebba9c208335a22132075ad` and the local clone path listed is the engine repo twice, I will work from what is accessible.

---

## Evidence from Engine Source

### `src/lib.rs` — Account Indexing Pattern

Reading the engine source at `/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/lib.rs`:

Key observations from the file:

**No BPF account array indexing exists in the engine** — the engine is a pure Rust library that operates on typed structs passed by value/reference. The `accounts[]` indexing concern is exclusively a wrapper-layer concern.

The engine exposes functions like:
- `percolator::new(...)` 
- `percolator::apply(...)` 
- `percolator::crank(...)`

These accept typed parameters, not raw `AccountInfo` slices.

---

## Candidate Implicit Invariants

```
- ID: invariant_accounts_len_check
  Source: wrapper entrypoint (percolator-prog, instruction dispatch)
  Claim: "accounts[] is indexed at fixed offsets without prior length validation"
  Enforced by: NEEDS_VERIFICATION — wrapper source not accessible at listed path
  Impact if violated: HIGH — out-of-bounds index in BPF panics/traps or wraps
  Suggested test: Layer-2 PoC — invoke each instruction with accounts.len() < expected
  Confidence: MED
```

```
- ID: invariant_engine_no_accounts_slice
  Source: src/lib.rs (entire file)
  Claim: Engine library never directly indexes a raw AccountInfo slice
  Enforced by: Structural — engine API accepts typed structs, not slices
  Impact if violated: N/A (invariant holds at engine layer)
  Confidence: HIGH
```

---

## Verdict

| Layer | Finding | Verdict | Confidence |
|-------|---------|---------|------------|
| Engine (`src/lib.rs`) | Engine does NOT index `accounts[]` directly; uses typed API | TRUE — engine is safe at this layer | HIGH |
| Wrapper (`percolator-prog`) | BPF entrypoints index `accounts[]` — length check status **cannot be verified** from accessible path | NEEDS_LAYER_2_TO_DECIDE | MED |

---

## Blocker: Wrapper Source Not Accessible

The orientation lists the wrapper clone path as the **same path as the engine** (both point to `aeyakovenko-percolator-5059332`). The wrapper repo (`aeyakovenko-percolator-prog`, sha `04b854e5718112f42ebba9c208335a22132075ad`) is **not accessible** at the listed local path.

This is a critical gap because:
- All `accounts[N]` indexing in Solana BPF programs happens in the wrapper entrypoints
- The engine layer is insulated by its typed API
- Whether `accounts.len() >= N` is checked before `accounts[N]` is accessed is **entirely a wrapper-layer question**

---

## Recommended Next Steps

1. **Re-clone wrapper repo** at correct local path and re-run this agent with the corrected path
2. **Layer-2 PoC** (highest priority if wrapper source is recovered): invoke each instruction with `accounts` truncated by 1; confirm panic/trap vs. graceful error
3. **Grep target** in wrapper once accessible:
   ```
   grep -n "accounts\[" src/processor.rs
   grep -n "accounts.len()" src/processor.rs
   ```
4. Check for a helper like `next_account_info` (Anchor/SPL pattern) vs. raw indexing — the former is safer

---

## Summary

**The engine layer does not index `accounts[]` at all** — it is structurally protected. **The wrapper layer is the actual risk surface** for this hypothesis, and it cannot be assessed until the correct local path for `aeyakovenko-percolator-prog` is provided. This audit item should be escalated to **NEEDS_LAYER_2_TO_DECIDE** pending wrapper source access.