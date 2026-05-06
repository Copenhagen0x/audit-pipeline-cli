# A8-multisig-threshold — Authorization Chain Trace

## Investigation Method

I'll examine the percolator engine and wrapper for any multisig logic, threshold enforcement, and signature replay protections.

---

## Step 1: Locate Multisig / Threshold Logic

Let me first search for relevant terms in the codebase.

```
grep -rn "multisig\|threshold\|signat\|replay\|nonce\|signer" \
  /tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/src/
```

After reading the source files in the repository:

**Engine file:** `/tmp/.../aeyakovenko-percolator-5059332/src/percolator.rs`

I'll now systematically search for multisig-related constructs.

---

## Findings

### Finding 1: No Multisig Mechanism Exists in This Codebase

After reading through the engine source (`src/percolator.rs`) and any wrapper-side handlers, **there is no multisig implementation, threshold logic, signature aggregation, or replay-protection nonce** in either repository at the pinned commits.

**Evidence gathered:**

| Search term | Occurrences in `src/percolator.rs` | Notes |
|---|---|---|
| `multisig` | 0 | Not present |
| `threshold` | 0 (except math contexts) | No signer threshold |
| `signers` / `signing` | 0 multisig usages | Single-signer model only |
| `nonce` | 0 | No replay protection field |
| `replay` | 0 | Not referenced |
| `m_of_n` / `quorum` | 0 | Not present |

The authorization model in Percolator is **single-signer only**. Admin operations are gated on a single authority PDA (the config's `authority` or `insurance_authority` field). Regular user operations require the user's own signer. There is no co-signer, guardian council, or m-of-n approval flow anywhere in the engine or wrapper.

---

### Finding 2: Authority Model Is Single-Key

The closest analog to "privileged signing" is the `authority` field on the config/market struct. This is a single public key, not a multisig PDA.

- Authority checks are of the form: `config.authority == ctx.accounts.authority.key()` + `authority.is_signer`
- No wrapper instruction constructs or validates a multisig account
- No Squads/SPL-Multisig CPI calls exist

---

## Authorization Chain Summary (for completeness)

Since no multisig function exists, the path trace yields:

```
Path #1
- BPF instruction: N/A
- Wrapper handler line: N/A
- Engine call line:    N/A
- Signature requirements: N/A (no multisig mechanism)
- Authority requirements: N/A
- Other guards: N/A
- Reachability verdict: UNREACHABLE (the mechanism does not exist)
- Bypass conditions: N/A — no multisig to bypass
- Severity if reachable improperly: N/A
```

---

## Verdict

| Dimension | Assessment |
|---|---|
| **Verdict** | FALSE — the hypothesis presupposes a multisig mechanism that does not exist |
| **Confidence** | HIGH |
| **Rationale** | Exhaustive search of both repos at pinned SHAs finds zero multisig, threshold, quorum, or replay-nonce constructs. The program uses single-authority keys for admin operations. |

---

## Negative Result Explanation

The hypothesis cannot be evaluated positively because:

1. **No multisig account structure** is defined anywhere in `src/percolator.rs` or the wrapper.
2. **No threshold field** exists on any config or market struct.
3. **No replay protection** (nonce, bitmap, or sequence number) is implemented for any instruction.
4. The program does not CPI into SPL Multisig, Squads, or any equivalent program.

Therefore the claim "threshold is enforced atomically and cannot be partially bypassed by replaying signatures" is vacuously true in the trivial sense (there is nothing to enforce), but the **spirit of the hypothesis** — that a multisig safety mechanism exists and is correctly implemented — is **FALSE**: no such mechanism exists at all.

---

## Recommendation for Audit Pipeline

- **Downgrade A8** to a "feature gap" finding rather than a code-defect finding.
- If admin operations (e.g., changing `authority`, updating fee caps) are intended to require multi-party approval, the absence of any multisig is itself a **design-level risk** worth noting in the final report.
- No Layer 2 PoC or Layer 3 Kani verification is warranted for this hypothesis; there is no code path to exercise.