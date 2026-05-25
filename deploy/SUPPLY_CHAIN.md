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

> **Round-2 note (informational)**: as of round-2 hardening, the `git config --global` lines above are **redundant for `jelleo-autoupdate.sh`** — that script pins `gpg.format=ssh` and `gpg.ssh.allowedSignersFile=...` inline via `-c` flags at every invocation, so the global config can't redirect the trust root. The global config is still useful for manual `git verify-commit` invocations the operator runs by hand. Leave it in place.

> **Round-2 note (BREAKING for GPG users)**: the inline pin sets `gpg.format=ssh` — this means `jelleo-autoupdate.sh` will REJECT commits signed with traditional GPG, even if they're otherwise valid. If your team uses GPG signing, every commit must be re-signed with SSH before this gate accepts it. The current `audit-pipeline-cli` repo has zero signed commits — **shipping this gate on a brand-new VPS without first signing at least one commit will leave the autoupdate timer rejecting every tick until you sign**.

### 4. Verify the setup

```bash
# On the VPS, manually verify:
# Round-5 fix (code-reviewer ROUND-4 LOW #2): pin gpg.format AND
# allowedSignersFile INLINE so this manual verify-commit works
# regardless of whether Step 3 has been completed yet. Without the
# inline pins, a missing global gitconfig silently passes verification
# (git falls back to OpenPGP and finds no keys), giving false confidence.
cd /root/audit-pipeline-cli
git -c gpg.format=ssh \
    -c gpg.ssh.allowedSignersFile=/root/.ssh/jelleo-allowed-signers \
    verify-commit HEAD
# Expected output: "Good "git" signature for kirill@jelleo.com..."
```

If `verify-commit` fails after deployment, `jelleo-autoupdate.sh` will:
1. Log `BLOCKED: HEAD signature verification failed`
2. Roll back to the prior HEAD via `git reset --hard`
3. Skip the install + restart entirely
4. Try again on the next 5-min tick

---

## Emergency bypass (use sparingly)

If you need to push an unsigned hotfix (e.g., signing key compromised, need to deploy a key-rotation), set the bypass env var.

**Round-2 hardening**: the bypass MUST be set via `systemctl edit` (a systemd-level Environment= directive), NOT via `/root/.audit-env`. The shipped `jelleo-autoupdate.service` hardcodes `Environment=JELLEO_ALLOW_UNSIGNED=0`, which SHADOWS any value injected via the EnvironmentFile. This prevents an attacker who achieves write access to `/root/.audit-env` from silently disabling the gate.

```bash
# On the VPS, edit the unit:
sudo systemctl edit jelleo-autoupdate.service
# In the drop-in editor, add:
#   [Service]
#   Environment=JELLEO_ALLOW_UNSIGNED=1
sudo systemctl daemon-reload
```

**WARN log behavior** (corrected in round-2): the `WARN: JELLEO_ALLOW_UNSIGNED=1 — supply-chain gate DISABLED for this tick` line is logged **only when a new commit is available for deployment** (i.e., when `LOCAL != REMOTE`). On a quiet repo with no pending commits, the bypass is silent — the script exits early before reaching the gate. To verify the bypass is active:

```bash
systemctl show -p Environment jelleo-autoupdate.service | grep ALLOW_UNSIGNED
```

Remove the override as soon as the hotfix lands:

```bash
sudo systemctl edit jelleo-autoupdate.service   # delete the bypass line
sudo systemctl daemon-reload
```

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
# Round-4 fix (code-reviewer ROUND-3 LOW #4): pin `gpg.format=ssh` AND
# `gpg.ssh.allowedSignersFile=/root/.ssh/jelleo-allowed-signers` INLINE
# on this verify-commit call. The autoupdate.sh path uses these inline;
# this manual operator example previously relied entirely on the operator's
# global git config (set up in Step 3 above). If the operator hasn't done
# Step 3 yet, the bare `git verify-commit HEAD` either silent-passes or
# emits a confusing error. Inline -c flags = same behavior regardless of
# operator's global config state.
gh repo clone Copenhagen0x/audit-pipeline-cli
cd audit-pipeline-cli
git -c gpg.format=ssh \
    -c gpg.ssh.allowedSignersFile=/root/.ssh/jelleo-allowed-signers \
    verify-commit HEAD
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
