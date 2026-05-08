# §07 · Cryptographic attestation

Every cycle ships with an Ed25519 signed receipt. Every finding ships with a per-finding signed disclosure package. The platform's public key is itself published — anyone can verify a receipt without trusting the operator.

This is what makes the methodology composable: insurers, partner protocols, and STRIDE assessors can require — programmatically — that a counterparty's last attestation is fresh, signed by the right key, and covers the expected invariant set.

---

## What gets signed

| Artifact                              | Format                            | Where it lives |
|---------------------------------------|-----------------------------------|----------------|
| Per-cycle HTML report                 | HTML + base64 Ed25519 sig        | `api.jelleo.com/cycles/<id>/cycle.html` + `.sig` |
| Per-cycle PDF report                  | PDF + base64 Ed25519 sig          | `api.jelleo.com/cycles/<id>/cycle.pdf` + `.sig` |
| Weekly digest                         | HTML + sig                        | mailed to customer + archived |
| Monthly digest                        | HTML + sig                        | mailed to customer + archived |
| Per-finding disclosure package        | tarball + sig                     | attached to the GitHub issue at `disclosed` time |

The signature covers the **canonical bytes** of the artifact. Re-rendering with different formatting produces a different hash and breaks signature verification — so the signed bytes are the source of truth, not a re-rendered visual.

---

## Key management

The platform key is a single Ed25519 keypair generated once on the host and stored at `/etc/audit-pipeline/jelleo.ed25519` (private) and `/var/www/jelleo.com/keys/jelleo.ed25519.pub` (public, world-readable).

Generation:

```bash
audit-pipeline sign keygen
# writes private key to <workspace>/keys/jelleo.ed25519
# writes public key  to <workspace>/keys/jelleo.ed25519.pub
```

The public key is also mirrored at:

- `https://jelleo.com/keys/jelleo.ed25519.pub` (Netlify static)
- `https://api.jelleo.com/keys/jelleo.ed25519.pub` (VPS nginx, with CORS)
- Per-customer manifest (alongside their cycle data)

The private key never leaves the host. There is no key-escrow service. If the host is compromised, the platform must rotate to a new keypair and publish the new public key with a deprecation notice for the old one.

**Per-customer keys** — Y1 funded-state delta. Each customer gets a derived sub-key so per-customer attestation is independently verifiable. Today: single platform key.

---

## Verification

Any third party can verify an artifact:

```bash
# 1. Fetch the artifact + signature
curl -O https://api.jelleo.com/cycles/<id>/cycle.html
curl -O https://api.jelleo.com/cycles/<id>/cycle.html.sig

# 2. Fetch the platform public key
curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub

# 3. Verify with audit-pipeline (the implementation has the helper)
audit-pipeline sign verify --artifact cycle.html --sig cycle.html.sig --pubkey jelleo.ed25519.pub
# → "✓ signature valid, signed by <fingerprint>"
```

Or with a generic Ed25519 verifier (Python `cryptography` library, openssl, etc.) — the format is standard PKCS#8 base64.

---

## On-chain registry (Y1 funded delta)

Today: signed receipts are off-chain (HTTP-published). The funded-state plan adds an Anchor program that records each cycle's Merkle root on-chain, indexed by the protocol's program ID:

```
Program: jelleo-attestation
Account: cycle_attestation_<protocol_pubkey>_<cycle_id>
Data:
  protocol         : Pubkey
  cycle_id         : str
  engine_sha       : str
  invariant_count  : u32
  merkle_root      : [u8; 32]    // Merkle root over per-finding receipts
  signer           : Pubkey       // platform key (or per-customer key)
  ts               : i64
```

Insurers and partner protocols can then require an on-chain CPI to check that `cycle_attestation_<protocol>_latest` exists, is signed by the expected key, and is fresh (within N slots). The audit becomes a composable on-chain primitive.

**Why this matters:** It shifts security from "did you have a paywalled PDF?" to "does the chain show a fresh, signed attestation from the right key?" — a primitive any program can compose.

---

## Receipt fingerprint format

For UI display we render a short fingerprint (first 8 bytes of the signature, hex, colon-separated):

```
3a:c1:8e:42:7f:11:b9:dd…
```

Same convention as SSH host fingerprints. It's not a complete signature — it's a visual identifier. Full verification still requires the full sig + pubkey + bytes.

---

**Live reference:** [jelleo.com/methodology.html#attestation](https://jelleo.com/methodology.html#attestation)
**Public key:** [api.jelleo.com/keys/jelleo.ed25519.pub](https://api.jelleo.com/keys/jelleo.ed25519.pub)
**Implementation:** [`audit_pipeline.commands.sign`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/commands/sign.py)
