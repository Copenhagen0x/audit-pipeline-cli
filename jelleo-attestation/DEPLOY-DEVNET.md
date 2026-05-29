# P4.4 — Devnet deploy + first attestation (operator runbook)

Run these on **Windows** (the Solana CLI + the built `.so` + `solders` all live here;
the VPS has no Solana toolchain). Everything is **devnet only** — mainnet is blocked
in the tooling and is gated behind the external audit. Preview every step first
(omit `--send`), then re-run with `--send`.

## Constants
- Program id (fixed by `declare_id!`): `72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk`
- Program **authority** (signs attestations): `CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL`
  - secret: `C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-attestation-authority.json`
- `.so`: `C:\Users\btrco\OneDrive\Desktop\Jelleo\code\percolator-deliverable\audit-pipeline-cli\jelleo-attestation\target\deploy\jelleo_attestation.so`
- CLI: run from `…\percolator-deliverable\audit-pipeline-cli` as `python -m audit_pipeline.cli …`

## 0. Pre-flight (DO NOT SKIP)
```powershell
# a) Force devnet — your default config points at MAINNET; this is the #1 foot-gun.
solana config set --url https://api.devnet.solana.com
solana config get        # confirm RPC URL = devnet

# b) Confirm the authority key is the one the program expects.
solana-keygen pubkey "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-attestation-authority.json"
#   -> must print  CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL

# c) Confirm the program-id keypair matches declare_id! (else the deploy address
#    won't match and PDAs break). This keypair is the program's identity — back it up.
solana-keygen pubkey "C:\Users\btrco\OneDrive\Desktop\Jelleo\code\percolator-deliverable\audit-pipeline-cli\jelleo-attestation\target\deploy\jelleo_attestation-keypair.json"
#   -> must print  72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk
```
**Also recommended:** re-run the 7 litesvm tests on the Linux VPS against the rebuilt
`.so` (the authority-key swap changed the binary). They validate program behaviour
(front-run resistance, auth-gating, rotation). Confirmatory, not strictly blocking.

## 1. Make a SEPARATE deployer / upgrade-authority key
The deployer pays for the deploy **and becomes the BPF upgrade authority** (can replace
the on-chain binary). Keep it **distinct from the attestation authority (`CLvf…`)** so a
compromise of one key does not grant both "write attestations" and "replace the program".
```powershell
solana-keygen new --no-bip39-passphrase --outfile "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-deployer.json"
solana-keygen pubkey "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-deployer.json"   # note this pubkey
```

## 2. Fund both keys (free devnet SOL)
```powershell
solana airdrop 2 <DEPLOYER_PUBKEY> --url devnet
solana airdrop 2 CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL --url devnet
# (faucet may rate-limit; retry, or use https://faucet.solana.com)
```

## 3. Deploy the program (IRREVERSIBLE on devnet, but devnet is disposable)
```powershell
solana program deploy `
  "C:\Users\btrco\OneDrive\Desktop\Jelleo\code\percolator-deliverable\audit-pipeline-cli\jelleo-attestation\target\deploy\jelleo_attestation.so" `
  --program-id "C:\Users\btrco\OneDrive\Desktop\Jelleo\code\percolator-deliverable\audit-pipeline-cli\jelleo-attestation\target\deploy\jelleo_attestation-keypair.json" `
  --upgrade-authority "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-deployer.json" `
  --keypair "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-deployer.json" `
  --url devnet
# Verify it's live:
solana program show 72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk --url devnet
```

## 4. Initialize the Config (one-time) — preview, then send
```powershell
cd "C:\Users\btrco\OneDrive\Desktop\Jelleo\code\percolator-deliverable\audit-pipeline-cli"
python -m audit_pipeline.cli merkle attest-init   # PREVIEW
python -m audit_pipeline.cli merkle attest-init --send `
  --keypair "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-attestation-authority.json" --cluster devnet
```

## 5. Register the audited protocol (one-time per protocol)
`<PROTOCOL>` = the on-chain program id of the audited project.
```powershell
python -m audit_pipeline.cli merkle attest-register <PROTOCOL>   # PREVIEW
python -m audit_pipeline.cli merkle attest-register <PROTOCOL> --send `
  --keypair "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-attestation-authority.json" --cluster devnet
```

## 6. Publish a cycle's attestation
First compute + sign the cycle's merkle.json **with the protocol bound in** (so the
publisher reads the protocol from the signed sidecar), then publish.
```powershell
# from the workspace that holds hunts/<cycle>/  (use --workspace <path> if not cwd)
python -m audit_pipeline.cli merkle compute <CYCLE_ID> --protocol <PROTOCOL>
python -m audit_pipeline.cli merkle publish-onchain <CYCLE_ID>   # PREVIEW (verify-gated)
python -m audit_pipeline.cli merkle publish-onchain <CYCLE_ID> --send `
  --keypair "C:\Users\btrco\OneDrive\Desktop\Jelleo\.audit-keys\jelleo-attestation-authority.json" --cluster devnet
```
The command prints the tx signature + an `explorer.solana.com/...?cluster=devnet` link.

## Safety rails (the tooling enforces these; don't fight them)
- **Mainnet is blocked** two ways: the cluster-name/URL string check, AND a live
  genesis-hash check that refuses the mainnet genesis. Use `--cluster devnet`.
- `publish-onchain` is **verify-gated**: it refuses any unsigned/tampered/wrong-domain
  /protocol-less merkle.json before building anything.
- `--keypair` for `publish`/`attest-register` **must be the authority** (`CLvf…`) or the
  CLI refuses (the on-chain program would reject it anyway).
- Never put the `.audit-keys\*.json` secrets or `secret.txt` into a repo (a `.gitignore`
  guards `keys/`, but `.audit-keys` is outside the repo on purpose).
- **Devnet is periodically reset** by Solana — this is a rehearsal. Mainnet only after
  the $80K external audit.
