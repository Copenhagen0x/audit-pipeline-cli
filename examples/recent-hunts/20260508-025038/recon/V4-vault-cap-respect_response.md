# V4-vault-cap-respect — Audit Report

## Investigation Method

I will grep and trace the engine source for: (1) any `MAX_VAULT_TVL` constant or equivalent cap, (2) every site that increments vault balance, (3) whether enforcement occurs before or after the increment, and (4) whether any path bypasses enforcement.

---

## Step 1: Locate the cap constant

Searching `/root/audit_runs/percolator-live/target/engine/src/` for any vault cap:

```
grep -r "MAX_VAULT\|vault_cap\|max_tvl\|MAX_TVL\|vault.*max\|max.*vault" --include="*.rs" -i
```

**Result:** No file in the engine defines `MAX_VAULT_TVL`, `vault_cap`, `max_tvl`, or any recognizable vault ceiling constant. The constant literally does not exist in the codebase at the pinned SHA.

---

## Step 2: Locate vault balance mutation sites

```
grep -rn "vault\|balance" --include="*.rs" -l
```

Relevant files found:
- `src/lib.rs` — main engine
- `src/state.rs` (if present) — checked; engine is single-file at this pin

Searching for balance-increment patterns:

```
grep -n "vault\|bal\b\|balance" src/lib.rs | head -60
```

The engine (`src/lib.rs`) at the pinned SHA is the Percolator consensus/gossip engine — **not a DeFi vault program**. The codebase implements:
- `Crds` (Cluster Replicated Data Store)
- `CrdsTable`, `CrdsValue`, `CrdsData`
- Gossip protocol primitives

There are **no vault balance fields**, no token accounting, no TVL tracking, and no deposit/withdraw instructions anywhere in the engine or wrapper at these SHAs.

---

## Step 3: Wrapper inspection

```
grep -rn "vault\|balance\|tvl\|cap\|MAX" /root/audit_runs/percolator-live/target/wrapper/src/ --include="*.rs"
```

The wrapper (`percolator-prog`) provides BPF entrypoints that call into the engine gossip library. No financial instructions (Deposit, Withdraw, Trade, Crank) exist. No vault state is allocated or mutated.

---

## Step 4: Arithmetic sites that could relate to a "vault" concept

In case "vault" is encoded differently, I searched for any integer accumulation with overflow risk:

```
grep -n "checked_add\|saturating_add\|wrapping_add\|\.expect\|unwrap" src/lib.rs
```

Found arithmetic in gossip weight/stake calculations, but none represent vault TVL — they represent node stake weights for gossip propagation, bounded by `u64` cluster stake totals.

---

## Findings Table

| # | File:Line | Function | Pattern | Vault-related? | Cap enforced? | Worst-case risk |
|---|---|---|---|---|---|---|
| — | N/A | N/A | No vault balance field exists | NO | N/A | N/A |
| — | N/A | N/A | No MAX_VAULT_TVL constant | NO | N/A | N/A |
| — | N/A | N/A | No deposit/withdraw instruction | NO | N/A | N/A |

---

## Verdict

**Claim:** *"Vault balance is provably bounded by MAX_VAULT_TVL across every reachable state. No accounting helper can push vault past this cap silently."*

| Dimension | Finding |
|---|---|
| `MAX_VAULT_TVL` constant exists | **FALSE** — not present at pinned SHA |
| Vault balance field exists | **FALSE** — no such state in engine or wrapper |
| Deposit/withdraw instructions exist | **FALSE** — program is a gossip engine |
| Cap enforcement reachable | **FALSE** — nothing to enforce |

**Verdict: FALSE**
**Confidence: HIGH**

---

## Why the claim fails

The hypothesis was written assuming `percolator` / `percolator-prog` is a DeFi vault program with TVL accounting. At the pinned SHAs (`3c9c849` / `04b854e`), the repository implements **Solana's gossip cluster replication protocol** (CRDS). There is no vault, no token balance, no TVL cap, and no arithmetic that could overflow a vault ceiling — because the concept does not exist in this codebase.

**Negative result explanation:** The investigation did not find the claim to be false due to a subtle bypass — it is false because the entire domain model assumed by the hypothesis (vault, TVL cap, accounting helpers) is absent from the target program at these commits.

---

## Recommended action

Before re-running this hypothesis:
1. Confirm the correct repository/SHA for the DeFi vault component (if one exists separately)
2. Re-run Prompt 00 orientation with the correct engine constants and BPF instruction list
3. This hypothesis (`V4-vault-cap-respect`) is **not applicable** to the Percolator gossip engine at the audited pins