#!/usr/bin/env bash
# Jelleo pipeline VPS bootstrap. Idempotent — safe to re-run.
# Run AS THE `audit` USER on the VPS after the toolchain is installed.
#
# Usage:
#   ssh -i ~/.ssh/percolator_vps <user>@<host>
#   curl -sSL https://raw.githubusercontent.com/Copenhagen0x/audit-pipeline-cli/main/deploy/bootstrap.sh | bash
# OR after scp'ing this file:
#   bash bootstrap.sh

set -euo pipefail

# ============================================================================
# Config (edit if needed before running)
# ============================================================================
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
echo "[2/8] Cloning CLI + methodology repos..."
if [[ ! -d "$HOME/audit-pipeline-cli" ]]; then
    git clone "$CLI_REPO" "$HOME/audit-pipeline-cli"
else
    cd "$HOME/audit-pipeline-cli" && git pull --ff-only && cd "$HOME"
fi
if [[ ! -d "$HOME/solana-audit-pipeline" ]]; then
    git clone "$METHODOLOGY_REPO" "$HOME/solana-audit-pipeline"
else
    cd "$HOME/solana-audit-pipeline" && git pull --ff-only && cd "$HOME"
fi

# 3. pip install the CLI in user mode
echo "[3/8] Installing audit-pipeline CLI..."
cd "$HOME/audit-pipeline-cli"
python3 -m pip install --user -e .
cd "$HOME"

# Make sure ~/.local/bin is on PATH for this session
export PATH="$HOME/.local/bin:$PATH"
which audit-pipeline || { echo "audit-pipeline not on PATH"; exit 1; }
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
echo "[5/8] Cloning target repos into workspace..."
mkdir -p "$WORKSPACE/target"
if [[ ! -d "$WORKSPACE/target/engine/.git" ]]; then
    rm -rf "$WORKSPACE/target/engine"
    git clone "$ENGINE_REPO" "$WORKSPACE/target/engine"
fi
if [[ ! -d "$WORKSPACE/target/wrapper/.git" ]]; then
    rm -rf "$WORKSPACE/target/wrapper"
    git clone "$WRAPPER_REPO" "$WORKSPACE/target/wrapper"
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
