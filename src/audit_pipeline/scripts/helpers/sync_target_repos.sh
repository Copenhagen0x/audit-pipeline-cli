#!/usr/bin/env bash
# sync_target_repos.sh — clone target engine + wrapper repos to the VPS at pinned SHAs.
#
# Usage:
#   bash scripts/helpers/sync_target_repos.sh <vps-host> <ssh-key> \
#       <engine-repo-url> <engine-sha> \
#       <wrapper-repo-url> <wrapper-sha>
#
# Example:
#   bash scripts/helpers/sync_target_repos.sh root@1.2.3.4 ~/.ssh/audit_vps \
#       https://github.com/aeyakovenko/percolator 5940285 \
#       https://github.com/aeyakovenko/percolator-prog c447686

set -euo pipefail

VPS_HOST="${1:?Usage: sync_target_repos.sh <vps> <key> <engine-url> <engine-sha> <wrapper-url> <wrapper-sha>}"
SSH_KEY="${2:?}"
ENGINE_URL="${3:?}"
ENGINE_SHA="${4:?}"
WRAPPER_URL="${5:?}"
WRAPPER_SHA="${6:?}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no"

echo "==> Syncing target repos to $VPS_HOST"
echo "    Engine:  $ENGINE_URL @ $ENGINE_SHA"
echo "    Wrapper: $WRAPPER_URL @ $WRAPPER_SHA"

$SSH "$VPS_HOST" "bash -s" <<EOF
set -e
mkdir -p /tmp/audit
cd /tmp/audit

# Engine
if [[ -d engine ]]; then
    cd engine
    git fetch origin
    git checkout $ENGINE_SHA
    cd ..
else
    git clone $ENGINE_URL engine
    cd engine
    git checkout $ENGINE_SHA
    cd ..
fi
echo "  Engine at: \$(cd engine && git rev-parse HEAD)"

# Wrapper
if [[ -d wrapper ]]; then
    cd wrapper
    git fetch origin
    git checkout $WRAPPER_SHA
    cd ..
else
    git clone $WRAPPER_URL wrapper
    cd wrapper
    git checkout $WRAPPER_SHA
    cd ..
fi
echo "  Wrapper at: \$(cd wrapper && git rev-parse HEAD)"

# Pin engine dependency in wrapper if it's a path-dep
if [[ -f wrapper/Cargo.toml ]] && grep -q 'path = "../percolator"' wrapper/Cargo.toml; then
    echo "  Wrapper Cargo.toml expects ../percolator — symlinking"
    ln -sfn /tmp/audit/engine /tmp/audit/percolator
fi

echo "  Sync complete."
EOF
