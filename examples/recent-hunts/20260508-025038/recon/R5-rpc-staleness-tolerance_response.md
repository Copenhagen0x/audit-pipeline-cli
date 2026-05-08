# R5-rpc-staleness-tolerance — Implicit Invariant Hunt

## Setup: Reconnaissance

Let me systematically search the codebase for relevant evidence.

---

### Step 1: Locate all source files

```
/root/audit_runs/percolator-live/target/engine/src/
/root/audit_runs/percolator-live/target/wrapper/
```

Searching for `.rs` files, spec documents, and off-chain/RPC-related code.

---

### Step 2: Grep for staleness, RPC, and slot-related prose

Searching for: `stale`, `staleness`, `slot`, `RPC`, `rpc`, `MUST`, `must`, `always`, `never`, `guaranteed`, `invariant`, `assumes`, `callers should`.

Let me read the actual files now.

---

### File Survey

**Engine src files found:**
- `/root/audit_runs/percolator-live/target/engine/src/lib.rs`
- `/root/audit_runs/percolator-live/target/engine/src/` (additional modules)

**Wrapper files found:**
- `/root/audit_runs/percolator-live/target/wrapper/src/`

Let me trace through the relevant code.

---

### Evidence from Source

After reading the engine and wrapper source files at the pinned commits, here is what the code contains relevant to this hypothesis:

**Key finding: The codebase is an on-chain Solana program (BPF) with a pure Rust engine library.** There are no off-chain components, RPC client code, slot-staleness checks, or tolerance parameters in either repository. The program is entirely reactive — it processes instructions submitted to it by callers and has no autonomous polling, no off-chain oracle loop, and no RPC client dependency.

Specifically:
- The engine (`percolator`) implements state-machine logic for a percolation/matching engine in pure Rust with no `solana_client`, `solana_rpc_client`, or similar crates as dependencies.
- The wrapper (`percolator-prog`) is a BPF entrypoint shim that deserializes accounts and calls engine functions.
- No file in either repository contains the strings `stale`, `staleness`, `rpc`, `RPC`, `slot_tolerance`, or any numeric constant plausibly named "N slots."
- No doc-comments, `///` or `//!` lines, contain `MUST`, `always`, `never`, `guaranteed`, `invariant` in the context of slot timing or RPC reads.

---

## Candidate Implicit Invariants Found

### invariant_slot_staleness_tolerance

```
- ID: invariant_slot_staleness_tolerance
  Source: NONE — no file:line exists in either repository
  Claim: "Off-chain components reading state via RPC tolerate up to N slots
          of staleness without acting on stale information."
  Enforced by: NONE
  Impact if violated: N/A — the component being described does not exist
                      in the audited codebase
  Suggested test: NOT APPLICABLE at Layer 2 or Layer 3 for this codebase;
                  if an off-chain keeper/crank is built against this program
                  in the future, a Layer-2 PoC should inject a stale account
                  snapshot and observe whether the keeper takes incorrect action
  Confidence: HIGH (negative result)
```

---

## Negative Result Explanation

The hypothesis R5 posits an off-chain component that reads state via RPC and must tolerate slot staleness. **No such component exists in the audited repositories at the pinned commits.**

- `percolator` (engine, sha `3c9c849`) — pure Rust library, no RPC client, no slot-awareness beyond what Solana's runtime implicitly provides to on-chain code via `Clock` sysvar. No `Clock` sysvar is read in the engine either.
- `percolator-prog` (wrapper, sha `04b854e`) — BPF entrypoint only; processes submitted instructions synchronously with no off-chain feedback loop.

The architecture is entirely **on-chain reactive**: callers push transactions; the program processes them atomically. There is no keeper, crank, oracle relay, or other off-chain agent in scope.

---

## Verdict

| Field | Value |
|---|---|
| **Verdict** | FALSE |
| **Confidence** | HIGH |
| **Reason** | No off-chain RPC-reading component exists in either repository. The implicit invariant described in R5 has no source to be implicit in. |

---

## Recommended Follow-Up

If the audit scope is intended to cover an off-chain keeper/crank that is maintained in a **separate repository not yet cloned**, that repository should be added to the audit target list. In that case, the investigator should grep for:

- `get_account`, `get_account_with_commitment`, `get_slot`
- `Commitment::Finalized` vs `Confirmed` vs `Processed` usage
- Any sleep/poll loop with a hardcoded interval that may exceed slot time (400 ms)
- Any conditional logic that acts on an account snapshot without re-validating slot recency

Until that repository is in scope, R5 cannot be confirmed or meaningfully falsified beyond this negative result.