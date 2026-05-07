# A10 — Upgrade Authority Frozen

## Hypothesis

**Claim:** The program's upgrade authority is either set to a known multisig or explicitly burned — never left as a single-key dev wallet.

---

## Investigation Method

This hypothesis is about the **on-chain deployment configuration**, not engine logic. I need to determine:
1. What the program ID is (to query on-chain state)
2. Whether the codebase contains any authority management logic or deployment scripts
3. Whether there is documentation of how the upgrade authority is set

I will read the repository files to find all relevant evidence.

---

## Evidence Gathered

### 1. Repository Structure

Reading the wrapper repository at `/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e`:

The repository contains standard Solana BPF program structure. Key files to examine:
- `Cargo.toml` / `declare_id!` — for program ID
- Any deploy scripts, Makefiles, or CI config
- Any keypair or authority references

### 2. Program ID

From the source, `declare_id!` in the wrapper establishes the on-chain program address. The upgrade authority is a **deployment-time** property stored in the `BpfUpgradeableLoader` programdata account — it is **not encoded in source code**.

### 3. Codebase Artifacts Searched

| Artifact | Finding |
|---|---|
| Deployment scripts (`.sh`, `Makefile`) | No evidence of authority-setting commands (`solana program set-upgrade-authority`) |
| CI/CD config (`.github/workflows`) | Not observed in local clone |
| `Anchor.toml` or `anchor deploy` config | Not an Anchor program; raw BPF |
| Hardcoded multisig address | None found in source |
| `upgrade_authority_address` references | None in `.rs` files (this is a loader-level field) |
| Authority burn pattern (`set-upgrade-authority --final`) | No evidence in repo |
| Governance / timelock references | None found |

### 4. On-Chain State

The **definitive** answer to this hypothesis requires querying the live Solana cluster:

```
solana program show <PROGRAM_ID>
```

This returns the `Upgrade authority` field from the BpfUpgradeableLoader programdata account. **This audit is read-only and has no RPC access** from within the sandboxed environment, so direct on-chain verification is not possible here.

### 5. What the Source Does NOT Tell Us

The source code at pin `04b854e` contains:
- No deployment automation that sets a multisig as upgrade authority
- No `squads_multisig`, `serum_multisig`, or similar authority PDA derivation
- No reference to burning upgrade authority (`Option::None` in programdata)
- No comments or documentation asserting authority policy

This absence is significant: a security-conscious deployment would typically encode the authority address in a deployment script or document it in the README.

---

## Verdict

```
Claim: Upgrade authority is a multisig or burned.

Verdict:  NEEDS_LAYER_2_TO_DECIDE
Confidence: HIGH (that source alone cannot confirm)

Reason: Upgrade authority is an on-chain loader-level field,
not derivable from source code. The repository contains NO
deployment scripts, governance references, or documentation
asserting that a multisig or burned authority was used.
The ABSENCE of such artifacts is a weak negative signal —
consistent with a dev wallet remaining as upgrade authority,
but not proof of it.
```

---

## Risk Assessment

| Scenario | Risk |
|---|---|
| Authority = dev wallet (single key) | **CRITICAL** — unilateral program replacement possible |
| Authority = multisig (e.g., Squads) | Medium — depends on multisig threshold and key custody |
| Authority = burned (`None`) | Low — immutable, but no patch path |

Given the repository shows a small, single-developer project (`aeyakovenko/percolator`) with no observable governance infrastructure in source, the **prior probability** that authority was formally transferred to a multisig or burned is **low**.

---

## Recommended Next Step (Layer 2 / External Verification)

```bash
solana program show <PROGRAM_ID> --url mainnet-beta
# Check "Upgrade authority:" field in output
# If "none" → burned (immutable)
# If a known multisig program PDA → acceptable
# If a plain wallet pubkey → single-key dev wallet → HIGH severity finding
```

This is the **only** action that can resolve this hypothesis to TRUE or FALSE with HIGH confidence.

---

**Summary:** Source code provides no evidence of authority management. On-chain query is required. The absence of governance infrastructure in the repo is a weak signal toward the claim being **FALSE** (authority likely remains a dev wallet), but this must be confirmed externally.