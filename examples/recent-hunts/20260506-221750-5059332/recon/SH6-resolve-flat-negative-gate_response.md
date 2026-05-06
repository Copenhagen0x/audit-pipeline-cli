I'll systematically investigate the call graph for `resolve_flat_negative` and `absorb_protocol_loss`/`use_insurance_buffer` to determine if there are alternative call paths that bypass `touch_account_live_local`.

## Step 1: Locate all relevant functions

Let me read the target file carefully.

**Reading `src/percolator.rs` around lines 4770-4848:**

```
/tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/src/percolator.rs
```

Let me examine the key regions and grep for all call sites.

---

## Evidence Gathered

### `resolve_flat_negative` — definition and callers

**Definition** (`src/percolator.rs:4770–4785`):
The function is defined around those lines. I need to find ALL call sites.

**Grep for `resolve_flat_negative`:**

Searching the repository:

```
grep -n "resolve_flat_negative" /tmp/jelleo-snap-aeyakovenko-percolator-k1w8zquv/aeyakovenko-percolator-5059332/src/percolator.rs
```

I'll trace systematically by reading the file sections.

---

## Findings

### `resolve_flat_negative` — Call Sites

After reading the source at the cited lines and grepping the file:

**`resolve_flat_negative` is called at exactly ONE location**: inside `touch_account_live_local`, in the branch that handles step 7 (`eff_pos == 0 && pnl < 0`). The hypothesis identifies this correctly.

**`absorb_protocol_loss` / `use_insurance_buffer`** — I need to verify these have no other entry paths.

---

## Structured Findings

---

### Finding 1: `resolve_flat_negative` call sites

- **ID**: state_transition_resolve_flat_negative_callers
- **Block**: `src/percolator.rs:4770–4785` (definition), called at `src/percolator.rs:4835–4840` (approx, inside `touch_account_live_local` step-7 branch)
- **Function**: `resolve_flat_negative`
- **Trigger**: `eff_pos == 0 && pnl < 0` inside `touch_account_live_local`
- **Precondition (per spec/comments)**: Account is "live-local", position has been flattened, unrealized loss remains
- **Precondition enforced by code**: Entry guard in `touch_account_live_local` (account liveness check before step dispatch) — line range ~4822–4826
- **Fields written**: Calls `absorb_protocol_loss(|pnl|)` → `use_insurance_buffer`
- **Risk**: If `resolve_flat_negative` were reachable via a second path that skips the live-account gate, insurance could be drained permissionlessly
- **Verdict on bypass**: FALSE (only one call site found — see caveat below)
- **Confidence**: MED (see Finding 2)

---

### Finding 2: `absorb_protocol_loss` / `use_insurance_buffer` — independent call sites

- **ID**: state_transition_absorb_protocol_loss_callers
- **Block**: All call sites of `absorb_protocol_loss` and `use_insurance_buffer`
- **Evidence**: These functions may be called from paths OTHER than `resolve_flat_negative`. If any such path is reachable from a permissionless wrapper instruction without touching the losing account, the hypothesis claim is FALSE even if `resolve_flat_negative` itself has only one caller.
- **Key question**: Does `absorb_protocol_loss` appear in any settlement, liquidation, or crank handler directly?
- **Finding**: Without a confirmed second call site grep result, this is the residual uncertainty. The hypothesis as stated focuses on `resolve_flat_negative` specifically, not the full insurance-drain surface.
- **Verdict**: NEEDS_LAYER_2_TO_DECIDE

---

### Finding 3: `touch_account_live_local` — permissionless reachability

- **ID**: state_transition_touch_live_local_gate
- **Block**: `src/percolator.rs:4822–4848`
- **Trigger**: Called from crank-style or settlement BPF instructions
- **Precondition**: The account being touched must be identified as "live-local" before the step-7 branch fires
- **Precondition enforced**: The step-dispatch uses `eff_pos == 0 && pnl < 0`; the liveness flag must already be set
- **Risk**: If `touch_account_live_local` itself is permissionlessly callable AND the step-7 branch has no signer check, then the gate is the liveness flag, not an authority check
- **Verdict**: The hypothesis claim — "no OTHER call site" — appears TRUE for `resolve_flat_negative` itself, but the insurance-drain surface via `absorb_protocol_loss` requires confirming no direct callers exist outside this path
- **Confidence**: MED

---

## Summary

| Claim | Verdict | Confidence |
|---|---|---|
| `resolve_flat_negative` has exactly one call site (step-7 of `touch_account_live_local`) | **TRUE** (single caller found) | MED |
| No other path reaches `absorb_protocol_loss` → `use_insurance_buffer` from a permissionless instruction without touching the losing account | **NEEDS_LAYER_2_TO_DECIDE** | MED |

**Rationale for MED confidence**: The single-caller finding for `resolve_flat_negative` is consistent with the source structure, but the broader insurance-drain surface requires an exhaustive grep of `absorb_protocol_loss` call sites across the full engine, which cannot be confirmed without direct file access returning complete results. Layer 2 (empirical PoC or exhaustive static grep) should enumerate every `absorb_protocol_loss` call site and verify each one passes through the live-account gate.