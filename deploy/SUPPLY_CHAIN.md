# Supply-chain hardening

Operator setup for the supply-chain defenses added in `fix/audit-001-supply-chain-hardening` (audit 20260523-140726).

## What changed

Three CRITICAL and one HIGH supply-chain findings closed in one patch:

| Audit finding | Surface | Fix |
|---|---|---|
| `5c4e072e` / `500607134` | `jelleo-autoupdate.sh` runs `pip install` + `install_systemd.sh` as root every 5 min with zero integrity check | `git verify-commit HEAD` gate after pull; rollback on failure |
| `R2-3` chain | `pip install --user` can install path-hijack shims that survive git revert | `PIP_NO_USER=1` on install + `PYTHONNOUSERSITE=1` on the token-auth service |
| `R2-2` / `5819e8b5` | `bootstrap.sh` delivered via curl-pipe-bash with no integrity check | `JELLEO_BOOTSTRAP_VERIFIED` env-var gate — refuses to run without an explicit SHA-256 confirmation |
| `R2-2` corpus | `refresh_corpus.sh` runs `git submodule update --recursive` which executes attacker-controlled post-checkout hooks from compromised third-party repos | `-c core.hooksPath=/dev/null -c protocol.file.allow=never` on every git invocation |

---

## One-time setup (per operator + per VPS)

### 1. Set up SSH signing on each operator machine

```bash
# Generate a signing key (separate from any auth key)
ssh-keygen -t ed25519 -f ~/.ssh/jelleo-signing -C "ops-signing@jelleo.com"

# Configure git to sign with it
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/jelleo-signing.pub
git config --global commit.gpgsign true
```

### 2. Maintain the allowed-signers file

```bash
# Format: <email> <ssh-public-key-content>
cat > ~/.ssh/jelleo-allowed-signers <<EOF
kirill@jelleo.com $(cat ~/.ssh/jelleo-signing.pub | awk '{print $1, $2}')
EOF

# Tell git to use it for verification
git config --global gpg.ssh.allowedSignersFile ~/.ssh/jelleo-allowed-signers
```

To add a new authorized signer (e.g., a teammate), append their public key to `jelleo-allowed-signers`, commit it to a private repo + distribute to VPS.

### 3. Replicate on the VPS

```bash
# On the VPS (one-time):
mkdir -p /root/.ssh
# Copy jelleo-allowed-signers from operator machine to VPS:
scp ~/.ssh/jelleo-allowed-signers root@<vps>:/root/.ssh/
ssh root@<vps>
chmod 600 /root/.ssh/jelleo-allowed-signers
git config --global gpg.format ssh
git config --global gpg.ssh.allowedSignersFile /root/.ssh/jelleo-allowed-signers
```

### 4. Verify the setup

```bash
# On the VPS, manually verify:
cd /root/audit-pipeline-cli
git verify-commit HEAD
# Expected output: "Good "git" signature for kirill@jelleo.com..."
```

If `verify-commit` fails after deployment, `jelleo-autoupdate.sh` will:
1. Log `BLOCKED: HEAD signature verification failed`
2. Roll back to the prior HEAD via `git reset --hard`
3. Skip the install + restart entirely
4. Try again on the next 5-min tick

---

## Emergency bypass (use sparingly)

If you need to push an unsigned hotfix (e.g., signing key compromised, need to deploy a key-rotation), set the bypass env var:

```bash
# On the VPS, edit the unit:
sudo systemctl edit jelleo-autoupdate.service
# Add:
#   [Service]
#   Environment=JELLEO_ALLOW_UNSIGNED=1
sudo systemctl daemon-reload
```

This will log `WARN: JELLEO_ALLOW_UNSIGNED=1 — supply-chain gate DISABLED for this tick` every 5 minutes — visible in the `auto-update.log`. Remove the override as soon as the hotfix lands.

---

## Bootstrap usage (replaces the old curl-pipe-bash docs)

The OLD pattern was:
```bash
curl -sSL https://raw.githubusercontent.com/Copenhagen0x/audit-pipeline-cli/main/deploy/bootstrap.sh | bash
```
This is **blocked**. `bootstrap.sh` refuses to run without `JELLEO_BOOTSTRAP_VERIFIED` set.

The NEW pattern (use one):

```bash
# Option 1: scp + verify locally
scp deploy/bootstrap.sh user@vps:~
ssh user@vps
sha256sum bootstrap.sh   # compare against the published checksum
JELLEO_BOOTSTRAP_VERIFIED=<the-sha256-you-just-computed> bash bootstrap.sh

# Option 2: clone the verified-signed repo on the VPS
gh repo clone Copenhagen0x/audit-pipeline-cli
cd audit-pipeline-cli
git verify-commit HEAD
JELLEO_BOOTSTRAP_VERIFIED=$(sha256sum deploy/bootstrap.sh | cut -d' ' -f1) \
  bash deploy/bootstrap.sh

# Option 3 (DEVELOPMENT ONLY)
JELLEO_BOOTSTRAP_VERIFIED=skip bash bootstrap.sh
```

---

## Corpus refresh

No operator action needed — `refresh_corpus.sh` is now safe by default. Every `git` invocation against a corpus repo uses:
- `core.hooksPath=/dev/null` (disables post-checkout / post-merge hooks)
- `protocol.file.allow=never` (blocks file:// submodule SSRF)

Even if every corpus repo we clone (anchor, drift, mango, marginfi, phoenix, openbook, orca, meteora etc.) is compromised tomorrow, the hooks they ship cannot execute on the Jelleo VPS.

---

## Tests

`tests/test_supply_chain_hardening.py` enforces these defenses are present:
- `jelleo-autoupdate.sh` contains `git verify-commit HEAD`
- `jelleo-autoupdate.sh` contains the rollback (`git reset --hard "$LOCAL"`)
- `bootstrap.sh` contains the `JELLEO_BOOTSTRAP_VERIFIED` gate
- `refresh_corpus.sh` contains `core.hooksPath=/dev/null`
- `jelleo-token-auth.service` contains `PYTHONNOUSERSITE=1`

These are source-grep tests, not behavioral. They guard against accidental regression on the script defenses. A future audit should add a containerized integration test that fires an unsigned commit at a sandbox VPS and asserts the rollback happens.
