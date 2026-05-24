#!/usr/bin/env bash
# Jelleo pipeline VPS bootstrap. Idempotent — safe to re-run.
# Run AS ROOT on the VPS after the toolchain is installed.
#
# Round-4 fix (devils-advocate ROUND-3 MED #6): header previously said "as
# the `audit` user" but PIP_NO_USER=1 + system pip install requires root
# write to /usr/local/lib/python*/site-packages. Running as non-root with
# PIP_NO_USER=1 fails with Permission denied at step 3 and the operator
# falls back to JELLEO_BOOTSTRAP_VERIFIED=skip. Fix: run as root from the
# start. Audit-fix: also removed the `export PATH=$HOME/.local/bin:$PATH`
# line that previously injected /root/.local/bin into PATH at HEAD — the
# exact binary-hijack vector the .service hardening just closed.
#
# ── INTEGRITY-GATED USAGE (audit finding R2-2 5819e8b5) ─────────────────
# The PREVIOUS instructions used curl-pipe-bash with NO integrity check —
# a single MITM of raw.githubusercontent.com or repo compromise would land
# arbitrary root code on the VPS. That pattern is now BLOCKED by the
# JELLEO_BOOTSTRAP_VERIFIED gate below.
#
# Correct usage (one of three options, in order of preference):
#
#   1. scp + verify locally, then run:
#        scp deploy/bootstrap.sh user@vps:~
#        ssh user@vps
#        sha256sum bootstrap.sh                # <-- compare with published checksum
#        JELLEO_BOOTSTRAP_VERIFIED=<sha256> bash bootstrap.sh
#
#   2. Download + verify via gh (uses GitHub authenticated HTTPS):
#        gh repo clone Copenhagen0x/audit-pipeline-cli
#        cd audit-pipeline-cli
#        # Round-5 fix: pin gpg.format AND allowedSignersFile inline so the
#        # verify-commit call works regardless of operator's global gitconfig
#        # state. Match SUPPLY_CHAIN.md Option 2 exactly.
#        git -c gpg.format=ssh \
#            -c gpg.ssh.allowedSignersFile=/root/.ssh/jelleo-allowed-signers \
#            verify-commit HEAD
#        JELLEO_BOOTSTRAP_VERIFIED=$(sha256sum deploy/bootstrap.sh | cut -d' ' -f1) \
#          bash deploy/bootstrap.sh
#
#   3. (TEMPORARY only) Bypass during development:
#        JELLEO_BOOTSTRAP_VERIFIED=skip bash bootstrap.sh
#      Logs a loud WARN.
#
# Do NOT use:
#   curl -sSL https://raw.githubusercontent.com/.../bootstrap.sh | bash
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Integrity gate ──
if [[ -z "${JELLEO_BOOTSTRAP_VERIFIED:-}" ]]; then
    cat >&2 <<'EOF'
ERROR: bootstrap.sh requires JELLEO_BOOTSTRAP_VERIFIED to be set.

This protects against curl-pipe-bash supply chain attacks.

Compute the SHA-256 of this script locally first:
    sha256sum bootstrap.sh
Then re-run with:
    JELLEO_BOOTSTRAP_VERIFIED=<the-sha256-you-just-computed> bash bootstrap.sh

Or, to bypass (DEVELOPMENT ONLY, NOT FOR PRODUCTION):
    JELLEO_BOOTSTRAP_VERIFIED=skip bash bootstrap.sh

EOF
    exit 2
fi

if [[ "$JELLEO_BOOTSTRAP_VERIFIED" == "skip" ]]; then
    echo "WARN: JELLEO_BOOTSTRAP_VERIFIED=skip — supply chain check BYPASSED" >&2
elif [[ ${#JELLEO_BOOTSTRAP_VERIFIED} -ne 64 ]]; then
    echo "ERROR: JELLEO_BOOTSTRAP_VERIFIED must be the 64-char SHA-256 hex, or 'skip'" >&2
    exit 2
else
    # Actually cross-check the supplied hash against the running script's SHA-256.
    # The previous version only validated the FORMAT of the supplied hash — it
    # never compared it to the script itself, so a MITM that swapped both the
    # script AND its published hash would bypass the gate. This block closes
    # that hole. (Code-reviewer flagged this on initial draft.)
    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "ERROR: sha256sum not available — install coreutils or set JELLEO_BOOTSTRAP_VERIFIED=skip" >&2
        exit 2
    fi
    # Round-2 fix (devils-advocate HIGH #4): detect non-file invocation forms
    # (`bash -s < script.sh`, `bash <(curl ...)`, etc.) BEFORE attempting to
    # hash $0. In those forms $0 is `bash` or `/dev/fd/<n>`, NOT the script
    # file. sha256sum would succeed but hash the wrong thing — leading the
    # operator to a confusing "mismatch" error and a likely fallback to
    # JELLEO_BOOTSTRAP_VERIFIED=skip. Refuse upfront with a precise message.
    case "$0" in
        bash|/bin/bash|/usr/bin/bash|*/bash|-*|sh|/bin/sh|*/sh)
            echo "ERROR: \$0='$0' — bootstrap.sh was invoked via \`bash -s\`," >&2
            echo "  process-substitution \`bash <(curl ...)\`, or stdin redirect." >&2
            echo "  The SHA-256 self-check cannot work in these forms because" >&2
            echo "  \$0 resolves to the shell binary, not the script file." >&2
            echo "  Fix: download the script to a file first, then run:" >&2
            echo "    curl -fsSL https://... -o bootstrap.sh" >&2
            echo "    JELLEO_BOOTSTRAP_VERIFIED=<sha> bash bootstrap.sh" >&2
            exit 2
            ;;
        /dev/fd/*|/proc/self/fd/*)
            echo "ERROR: \$0='$0' — bootstrap.sh was invoked via process substitution." >&2
            echo "  Download to a real file first (see ERROR message in case \`bash\`)." >&2
            exit 2
            ;;
    esac
    if [[ ! -f "$0" ]]; then
        echo "ERROR: \$0='$0' is not a regular file — refusing to hash." >&2
        echo "  Download the script to a regular file before invoking with verification." >&2
        exit 2
    fi
    ACTUAL_SHA=$(sha256sum "$0" 2>/dev/null | awk '{print $1}')
    if [[ -z "$ACTUAL_SHA" ]]; then
        echo "ERROR: could not compute SHA-256 of $0" >&2
        exit 2
    fi
    if [[ "$JELLEO_BOOTSTRAP_VERIFIED" != "$ACTUAL_SHA" ]]; then
        echo "ERROR: SHA-256 mismatch" >&2
        echo "  supplied: $JELLEO_BOOTSTRAP_VERIFIED" >&2
        echo "  actual:   $ACTUAL_SHA" >&2
        echo "Refusing to run a modified script." >&2
        exit 2
    fi
    echo "Bootstrap SHA-256 verified: $ACTUAL_SHA"
fi

# ============================================================================
# Config (edit if needed before running)
# ============================================================================
# Round-4 fix (devils-advocate ROUND-3 HIGH #2): GIT_SAFE applied to ALL
# git clone/pull operations below. Without core.hooksPath=/dev/null +
# protocol.file.allow=never, a compromised upstream that ships a malicious
# .git/hooks/post-checkout would execute as root during the initial
# `git clone` (before any commit verification could gate it). bootstrap.sh
# has the strongest integrity guarantee of any script (SHA-256 self-check)
# but that guarantee only covers THIS script, not the repos it then clones.
# R5b-3 (2026-05-24): protocol.ext.allow=never closes ext:: RCE
# (a `.gitmodules` URL `ext::malicious_cmd` would execute shell
# during submodule init without this).
GIT_SAFE=(-c core.hooksPath=/dev/null -c protocol.file.allow=never -c protocol.ext.allow=never)

WORKSPACE="${WORKSPACE:-$HOME/audit_runs/percolator-live}"
ENGINE_REPO="https://github.com/aeyakovenko/percolator"
WRAPPER_REPO="https://github.com/aeyakovenko/percolator-prog"

CLI_REPO="https://github.com/Copenhagen0x/audit-pipeline-cli"
METHODOLOGY_REPO="https://github.com/Copenhagen0x/solana-audit-pipeline"

PROGRAM="6qWZvUtfyShbxTQkwjCayk3LuGqTGJwBo2QfkePK5jdJ"
SLAB_ACCOUNT="CJKBStEn5VXEF9VNTChKKb5YW84MV7LycqMMziVuxJSc"

# ============================================================================

echo "=== Jelleo bootstrap ==="
echo "Workspace: $WORKSPACE"
echo

# 1. Verify python3 + pip
echo "[1/8] Verifying Python..."
python3 --version
python3 -m pip --version

# 2. Clone the CLI + methodology repos to ~
# Round-4 fix: GIT_SAFE applied to clone+pull. See declaration near top.
echo "[2/8] Cloning CLI + methodology repos..."
if [[ ! -d "$HOME/audit-pipeline-cli" ]]; then
    git "${GIT_SAFE[@]}" clone "$CLI_REPO" "$HOME/audit-pipeline-cli"
else
    cd "$HOME/audit-pipeline-cli" && git "${GIT_SAFE[@]}" pull --ff-only && cd "$HOME"
fi
if [[ ! -d "$HOME/solana-audit-pipeline" ]]; then
    git "${GIT_SAFE[@]}" clone "$METHODOLOGY_REPO" "$HOME/solana-audit-pipeline"
else
    cd "$HOME/solana-audit-pipeline" && git "${GIT_SAFE[@]}" pull --ff-only && cd "$HOME"
fi

# 3. pip install the CLI (system-site, NOT --user)
# PIP_NO_USER=1 closes the path-hijack vector (audit finding R2-3): installing
# to /root/.local/lib/.../site-packages would let a future supply-chain shim
# silently override security-critical modules (cryptography, audit_pipeline.bundle).
echo "[3/8] Installing audit-pipeline CLI..."
cd "$HOME/audit-pipeline-cli"
PIP_NO_USER=1 python3 -m pip install -e .
cd "$HOME"

# Round-4 fix (devils-advocate ROUND-3 MED #6): REMOVED the previous
# `export PATH="$HOME/.local/bin:$PATH"` line. PIP_NO_USER=1 install puts
# the audit-pipeline entry-point in /usr/local/bin (already on default PATH).
# Adding $HOME/.local/bin to PATH HEAD was the exact binary-hijack vector
# that the .service hardening just removed — keeping it here in bootstrap
# would re-introduce the vulnerability the moment the operator runs
# anything afterward in the same shell.
which audit-pipeline || { echo "audit-pipeline not on PATH (expected /usr/local/bin)"; exit 1; }
audit-pipeline --version

# 4. Init the audit workspace
echo "[4/8] Initialising audit workspace..."
mkdir -p "$(dirname "$WORKSPACE")"
if [[ -d "$WORKSPACE" ]] && [[ -f "$WORKSPACE/workspace.json" ]]; then
    echo "  Workspace already exists at $WORKSPACE — skipping init."
else
    # Use latest known SHAs (will be updated by `freshness --update` next)
    audit-pipeline --workspace "$WORKSPACE" init \
        --engine-repo  "$ENGINE_REPO" \
        --engine-sha   a946e55 \
        --wrapper-repo "$WRAPPER_REPO" \
        --wrapper-sha  17f70b0 \
        --output       "$WORKSPACE" \
        --target-name  percolator-live \
        --no-clone
fi

# 5. Clone target repos INTO the workspace
# Round-4 fix: GIT_SAFE applied. The target repos (percolator engine + wrapper)
# are THIRD-PARTY upstreams we don't control — exactly the surface where
# malicious post-checkout hooks could land before any audit gate ever runs.
echo "[5/8] Cloning target repos into workspace..."
mkdir -p "$WORKSPACE/target"
if [[ ! -d "$WORKSPACE/target/engine/.git" ]]; then
    rm -rf "$WORKSPACE/target/engine"
    git "${GIT_SAFE[@]}" clone "$ENGINE_REPO" "$WORKSPACE/target/engine"
fi
if [[ ! -d "$WORKSPACE/target/wrapper/.git" ]]; then
    rm -rf "$WORKSPACE/target/wrapper"
    git "${GIT_SAFE[@]}" clone "$WRAPPER_REPO" "$WORKSPACE/target/wrapper"
fi

# 6. Bring everything to current upstream HEAD
echo "[6/8] Pulling target repos to current HEAD..."
audit-pipeline --workspace "$WORKSPACE" freshness --update || \
    echo "  (freshness --update non-zero; non-fatal, continuing)"

# 7. Pre-create the shadow + watch output dirs
echo "[7/8] Pre-creating output directories..."
mkdir -p "$WORKSPACE/shadow" "$WORKSPACE/watch" "$WORKSPACE/recon" "$WORKSPACE/findings"

# 8. Smoke-test: one-shot shadow poll, one-shot watch poll
echo "[8/8] Smoke-testing one-shot shadow + watch..."
echo "  shadow start --once..."
audit-pipeline --workspace "$WORKSPACE" shadow start \
    --program "$PROGRAM" \
    --watch-account "$SLAB_ACCOUNT" \
    --once --limit 5 || echo "  (shadow smoke non-zero; check log)"
echo "  watch --once..."
audit-pipeline --workspace "$WORKSPACE" watch --once || \
    echo "  (watch smoke non-zero; check log)"

echo
echo "=== Bootstrap complete ==="
echo
echo "Next steps:"
echo "  1. Copy the systemd units:"
echo "       sudo cp $HOME/audit-pipeline-cli/deploy/jelleo-shadow.service /etc/systemd/system/"
echo "       sudo cp $HOME/audit-pipeline-cli/deploy/jelleo-watch.service  /etc/systemd/system/"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl enable --now jelleo-shadow.service"
echo "       sudo systemctl enable --now jelleo-watch.service"
echo
echo "  2. Verify both running:"
echo "       systemctl status jelleo-shadow jelleo-watch"
echo
echo "  3. Check live alerts:"
echo "       audit-pipeline --workspace $WORKSPACE shadow tail"
echo
echo "  4. Set up Gist sync (one-time):"
echo "       gh gist create $WORKSPACE/shadow/state.json --public --filename STATUS.md"
echo "       export GIST_ID=<id-from-above>"
echo "       echo 'GIST_ID=$GIST_ID' >> ~/.bashrc"
echo "       Add to crontab:  0 * * * * GIST_ID=$GIST_ID $HOME/audit-pipeline-cli/deploy/sync_to_gist.sh"
