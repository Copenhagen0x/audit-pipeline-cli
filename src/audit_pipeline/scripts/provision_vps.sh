#!/usr/bin/env bash
# provision_vps.sh — set up a fresh Ubuntu 22.04 VPS with the audit toolchain.
#
# Usage:
#   bash scripts/provision_vps.sh <vps-host> <ssh-key-path>
#
# Example:
#   bash scripts/provision_vps.sh root@1.2.3.4 ~/.ssh/audit_vps
#
# Prerequisites on YOUR machine:
#   - SSH key already authorized on the VPS (use ssh-copy-id first)
#   - VPS reachable on port 22
#
# What this installs on the VPS:
#   - Rust 1.95 + cargo-build-sbf
#   - Solana CLI 3.1.14
#   - Kani 0.67.0 + nightly-2025-11-21 toolchain
#   - tmux (for long-running session persistence)
#   - gh CLI (for issue/PR work)
#   - build-essential, git, curl, jq, gzip
#
# Total install time: ~15-20 min on a 6-core VPS.

set -euo pipefail

VPS_HOST="${1:?Usage: provision_vps.sh <vps-host> <ssh-key>}"
SSH_KEY="${2:?Usage: provision_vps.sh <vps-host> <ssh-key>}"

echo "==> Provisioning audit toolchain on $VPS_HOST"
echo "    Using SSH key: $SSH_KEY"
echo

# Sanity check: can we reach the VPS?
if ! ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VPS_HOST" "echo connected"; then
    echo "ERROR: cannot SSH to $VPS_HOST. Verify the key and host." >&2
    exit 1
fi

# === System packages ===
echo "==> Installing system packages (apt)"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq \
    build-essential \
    git \
    curl \
    jq \
    gzip \
    tmux \
    pkg-config \
    libssl-dev \
    libudev-dev \
    libsystemd-dev \
    cmake \
    clang \
    llvm \
    python3 \
    python3-pip

echo "  apt packages installed"
EOF

# === Rust toolchain ===
echo "==> Installing Rust 1.95"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e

if command -v rustc &>/dev/null && [[ "$(rustc --version)" == *"1.95"* ]]; then
    echo "  Rust 1.95 already present"
else
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.95
    source "$HOME/.cargo/env"
    echo "  Rust installed: $(rustc --version)"
fi

# Add nightly for Kani (specific version)
rustup toolchain install nightly-2025-11-21 --profile minimal --no-self-update
echo "  Nightly-2025-11-21 installed"
EOF

# === Solana CLI ===
echo "==> Installing Solana CLI 3.1.14"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
source "$HOME/.cargo/env"

if command -v solana &>/dev/null && [[ "$(solana --version)" == *"3.1"* ]]; then
    echo "  Solana 3.1.x already present"
else
    sh -c "$(curl -sSfL https://release.anza.xyz/v3.1.14/install)"
    export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
    echo "  Solana installed: $(solana --version)"
fi

# Add to .bashrc for future SSH sessions
if ! grep -q "solana/install/active_release/bin" ~/.bashrc; then
    echo 'export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"' >> ~/.bashrc
fi

# Verify cargo-build-sbf
which cargo-build-sbf && echo "  cargo-build-sbf available"
EOF

# === Kani ===
echo "==> Installing Kani 0.67.0"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
source "$HOME/.cargo/env"

if command -v cargo-kani &>/dev/null; then
    echo "  Kani already present: $(cargo kani --version 2>&1 | head -1)"
else
    cargo install --locked --version 0.67.0 kani-verifier
    cargo kani setup
    echo "  Kani installed: $(cargo kani --version)"
fi
EOF

# === gh CLI ===
echo "==> Installing gh CLI"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e

if command -v gh &>/dev/null; then
    echo "  gh CLI already present: $(gh --version | head -1)"
else
    type -p curl >/dev/null || apt install curl -y
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt update -qq
    apt install gh -y
    echo "  gh CLI installed: $(gh --version | head -1)"
fi
EOF

# === Working directory + scratch space ===
echo "==> Creating audit working directories"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
mkdir -p /tmp/audit/{engine,wrapper,results,scripts}
chmod 755 /tmp/audit
echo "  /tmp/audit/{engine,wrapper,results,scripts} created"
EOF

# === Final verification ===
echo "==> Verifying toolchain"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -lc' <<'EOF' || true
echo
echo "Rust:        $(rustc --version)"
echo "Cargo:       $(cargo --version)"
echo "Solana:      $(solana --version 2>&1 || echo MISSING)"
echo "build-sbf:   $(which cargo-build-sbf 2>&1)"
echo "Kani:        $(cargo kani --version 2>&1 | head -1)"
echo "gh CLI:      $(gh --version 2>&1 | head -1)"
echo "tmux:        $(tmux -V)"
echo
echo "Provision complete."
EOF

echo
echo "==> Done. VPS is ready for audit work."
echo "    Working dir: /tmp/audit/"
echo "    Next step:   bash scripts/dispatch_kani.sh $VPS_HOST <harness>"
