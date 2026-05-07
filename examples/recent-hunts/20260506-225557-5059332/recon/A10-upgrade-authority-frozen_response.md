# A10 — Upgrade Authority Audit

## Hypothesis
The Solana BPF program's upgrade authority is either set to a known multisig or explicitly burned (set to `None`) — never left as a single-key dev wallet.

---

## Method

This hypothesis is **deployment-state**, not source-code-state. The upgrade authority is set at deploy time via `solana program deploy` / `solana program set-upgrade-authority`. I will:

1. Search the wrapper repo for any `declare_id!`, keypair references, or deploy scripts that reveal the intended authority.
2. Search both repos for any programmatic `upgrade_authority` handling (e.g., `BpfUpgradeableLoader` CPI).
3. Check `Cargo.toml`, `Xargo.toml`, and any `deploy/` or `scripts/` artifacts for authority configuration.
4. Assess what the source alone can tell us vs. what requires on-chain inspection.

---

## Evidence Gathering

### 1. Program ID / declare_id!

Searching the wrapper repo for `declare_id!`:

```
/tmp/.../aeyakovenko-percolator-5059332/src/percolator.rs
```

The single local clone present is the **engine** repo (library crate). The wrapper repo (`percolator-prog` @ `04b854e`) was listed but the local path given for both entries points to the same engine clone. The wrapper source is **not present** in the provided local paths.

This limits what can be verified from source alone.

### 2. Engine repo — any upgrade authority references?

Searching the engine source for upgrade-authority-related terms:

- `upgrade_authority` — **not found** in any `.rs` file in `src/`
- `BpfUpgradeableLoader` — **not found**
- `set_upgrade_authority` — **not found**
- `multisig` — **not found**
- `burn` (authority context) — **not found**

The engine is a pure library crate (`lib.rs` entry). It contains no BPF entrypoint, no `declare_id!`, and no loader CPI. This is expected — authority management lives in the wrapper.

### 3. Deploy scripts / CI artifacts

Searching the engine repo root for deploy tooling:

- `Makefile`, `deploy.sh`, `scripts/` — **not found**
- `.github/workflows/` — **not present** in the cloned snapshot
- `Anchor.toml` — **not found** (not an Anchor project)
- `Cargo.toml` at root: confirms `[lib]` crate type, no `[[bin]]` entrypoints, no deploy hooks.

### 4. What the wrapper would contain (absent from local clone)

The wrapper repo (`percolator-prog` @ `04b854e`) would be the authoritative location for:

- `declare_id!("...")` — the on-chain program address
- Any `set_upgrade_authority` CPI or initialization logic
- Any `upgrade_authority` account constraint (e.g., Anchor `#[account(constraint = ...)]`)

Since the wrapper is **not present in the provided local path**, these cannot be verified from source.

---

## On-Chain Verification Requirement

The ground truth for upgrade authority is the **on-chain program account**, inspectable via:

```bash
solana program show <PROGRAM_ID>
```

The output field `Upgrade authority` will be one of:
- A specific pubkey (single-key — HIGH risk)
- A multisig address (requires cross-referencing with e.g. Squads)
- `(none)` / `None` — authority burned (immutable program)

This is a **deployment artifact**, not a source artifact. Source code alone cannot prove the hypothesis either TRUE or FALSE.

---

## Findings

| # | Item | Evidence | Verdict |
|---|------|----------|---------|
| 1 | Engine source contains upgrade-authority logic | No such code found | FALSE (expected for lib crate) |
| 2 | Wrapper source available for inspection | Wrapper clone not present at given path | CANNOT VERIFY |
| 3 | Deploy scripts with authority configuration | Not found in engine repo | CANNOT VERIFY |
| 4 | On-chain authority state | Requires `solana program show` | CANNOT VERIFY FROM SOURCE |

---

## Summary

**Verdict: NEEDS_LAYER_2_TO_DECIDE**
**Confidence: HIGH** (that source alone is insufficient — this is inherently a deployment-state question)

### Why this cannot be resolved from source

The upgrade authority is set at deployment, not encoded in the program logic. No compensating source-level control (e.g., a CPI that revokes authority post-deploy) exists in the engine repo. The wrapper repo — which would contain `declare_id!` and any deployment scripts — is absent from the provided local paths.

### Recommended next step (Layer 2 / empirical)

```bash
# 1. Obtain program ID from wrapper repo declare_id!
# 2. Query on-chain state:
solana program show <PROGRAM_ID> --url mainnet-beta
# 3. If authority is a pubkey, cross-check against known multisigs (e.g., Squads v4)
# 4. If authority is None → burned (safe)
```

Until on-chain state is inspected, **the claim is neither confirmed nor refuted**. A single-key dev wallet upgrade authority would constitute a **HIGH severity centralization risk** (silent rug / malicious upgrade vector).