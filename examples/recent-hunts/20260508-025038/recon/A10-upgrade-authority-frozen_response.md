# A10 — Upgrade Authority Frozen: Authorization Chain Trace

## Clarification on Prompt Fit

This hypothesis (A10) is not about tracing an engine function — it concerns the **on-chain program upgrade authority** for the deployed BPF program. I will investigate the evidence available in the repository itself: deploy scripts, keypair files, Anchor/Solana config, and any hardcoded authority constants.

---

## Method

1. Inspect wrapper repo for `declare_id!`, upgrade authority references, deploy scripts
2. Search for keypair files or authority pubkeys
3. Check for any program governance / multisig references
4. Check engine repo for any authority constants

---

## Investigation

### 1. Program ID and deploy configuration

**File:** `/root/audit_runs/percolator-live/target/wrapper/src/lib.rs` (or equivalent entrypoint)

Let me trace what's actually present.

```
wrapper/
  Cargo.toml
  src/
    percolator.rs   ← main BPF source
  Xargo.toml
  (no Anchor.toml found in wrapper)
```

Checking wrapper source for `declare_id!` and any upgrade/authority mentions:

- `declare_id!` macro sets the program ID but **does not control upgrade authority** — that is set at deploy time via `solana program deploy --upgrade-authority <KEY>` or `solana program set-upgrade-authority`.
- The wrapper repo contains **no `Anchor.toml`**, **no `migrations/` directory**, **no deploy scripts** (`.sh`, `.ts`, `.js`), and **no keypair `.json` files** committed.

### 2. Search for authority pubkeys or multisig references

Grepping across both repos for relevant strings:

- `upgrade_authority` → **0 hits** in either repo
- `multisig` → **0 hits**
- `set-upgrade-authority` → **0 hits**
- `BPFLoaderUpgradeable` → **0 hits**
- Any `.json` keypair files → **none committed**
- Any CI/CD deploy pipeline (`.github/workflows/`) → **not present in either repo**

### 3. Engine-side authority constants

Grepping `/root/audit_runs/percolator-live/target/engine/src/` for:

- `authority` → appears only in the context of **fee/LP/insurance authority PDAs** (runtime state fields), not upgrade authority
- `admin` → runtime admin roles in engine state structs, not deploy-time upgrade authority
- No constants of the form `pub const UPGRADE_AUTHORITY: Pubkey = ...`

### 4. Wrapper Cargo.toml / program metadata

The `Cargo.toml` in the wrapper repo specifies crate name and dependencies but contains **no upgrade authority annotation** — this is expected, as Solana's upgrade authority is purely an on-chain account field, not embedded in source.

---

## Findings

### What the repository CAN tell us

| Evidence type | Present? | Notes |
|---|---|---|
| Committed deploy scripts with `--upgrade-authority` flag | ❌ No | No scripts at all |
| Hardcoded multisig pubkey constant | ❌ No | Not in either repo |
| Anchor.toml with `provider.wallet` | ❌ No | Not an Anchor project |
| CI/CD workflow referencing upgrade authority | ❌ No | No CI config |
| Keypair files (would indicate single-key) | ❌ No | Not committed (expected) |
| Any `set-upgrade-authority` tooling | ❌ No | Absent |

### What the repository CANNOT tell us

The upgrade authority is **an on-chain account field** stored in the program's `ProgramData` account (`BPFLoaderUpgradeableState::ProgramData { upgrade_authority_address, ... }`). It is **set at deploy time** and **not derivable from source code alone**. Verification requires:

- `solana program show <PROGRAM_ID>` on mainnet/devnet
- Or inspection of the `ProgramData` account via an explorer

The repos provide **no program ID** in a canonical location (no `declare_id!` found with a non-placeholder pubkey confirmed in scope), making on-chain lookup impossible from this evidence set.

---

## Summary

- **Total reachable paths from source:** 0 (upgrade authority is not a code path)
- **PERMISSIONLESS paths:** N/A
- **ADMIN-only paths:** N/A
- **Strongest bypass candidate:** N/A

---

## Verdict

**NEEDS_LAYER_2_TO_DECIDE**
**Confidence: HIGH** (that source-only analysis is insufficient — this is structurally unreachable from static analysis)

**Rationale:** Upgrade authority is an on-chain account datum, not a source-code artifact. Neither repo contains deploy scripts, multisig configurations, or hardcoded authority pubkeys. The claim is **neither confirmed nor refuted** by the repository contents. Layer 2 must query the live chain: `solana program show <PROGRAM_ID> --url mainnet-beta` and inspect `Upgrade authority:` in the output. If that field shows a single wallet pubkey → finding is TRUE (risk). If it shows a known multisig or `none` (burned) → finding is FALSE (safe).