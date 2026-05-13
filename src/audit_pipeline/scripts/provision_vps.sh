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
#   PHASE 1h additions for OtterSec multi-language pipeline:
#   - CBMC (Bounded Model Checker for C — L3 formal)
#   - AFL++ (American Fuzzy Lop, fuzzer for C — L4 runtime)
#   - solc (Solidity compiler with SMTChecker — L3 + L2 PoC)
#   - Foundry (forge/cast/anvil — L2 PoC + L4 runtime for Solidity)
#   - Aptos CLI (move test + move prove — L2 + L3 + L4 for Aptos)
#   - clang (already in toolchain — handles C L2 PoC + sanitizers)
#
# Total install time: ~30-45 min on a 6-core VPS (was ~15-20 pre-Phase-1h).

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

# ============================================================
# PHASE 1h — multi-language toolchain (OtterSec eval prerequisites)
# ============================================================

# === CBMC (C bounded model checker — L3 formal for C) ===
echo "==> Installing CBMC"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
if command -v cbmc &>/dev/null; then
    echo "  CBMC already present: $(cbmc --version | head -1)"
else
    apt-get install -y -qq cbmc
    echo "  CBMC installed: $(cbmc --version | head -1)"
fi
EOF

# === AFL++ (coverage-guided fuzzer for C — L4 runtime for C) ===
echo "==> Installing AFL++"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
if command -v afl-fuzz &>/dev/null; then
    echo "  AFL++ already present: $(afl-fuzz --version 2>&1 | head -1)"
else
    # Ubuntu 22.04 ships AFL++ in the universe repo as afl++
    apt-get install -y -qq afl++ || apt-get install -y -qq afl
    echo "  AFL++ installed: $(afl-fuzz --version 2>&1 | head -1)"
fi
EOF

# === solc (Solidity compiler — L2 + L3 SMTChecker for Solidity) ===
echo "==> Installing solc (Solidity compiler)"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
if command -v solc &>/dev/null; then
    echo "  solc already present: $(solc --version | tail -1)"
else
    # Use the official solc PPA for reliable releases
    add-apt-repository -y ppa:ethereum/ethereum 2>/dev/null || true
    apt-get update -qq
    apt-get install -y -qq solc || {
        # Fallback: download the static binary from the official releases
        curl -fsSL https://github.com/ethereum/solidity/releases/download/v0.8.26/solc-static-linux \
             -o /usr/local/bin/solc
        chmod +x /usr/local/bin/solc
    }
    echo "  solc installed: $(solc --version | tail -1)"
fi
EOF

# === Foundry (forge / cast / anvil — L2 PoC + L4 fuzz for Solidity) ===
echo "==> Installing Foundry"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
if command -v forge &>/dev/null; then
    echo "  Foundry already present: $(forge --version | head -1)"
else
    # Install via foundryup (the canonical installer)
    curl -L https://foundry.paradigm.xyz | bash
    # foundryup writes to ~/.foundry/bin/ — source the env
    if [[ -d "$HOME/.foundry/bin" ]]; then
        export PATH="$HOME/.foundry/bin:$PATH"
        # Persist in shell rc for future sessions
        grep -q '.foundry/bin' "$HOME/.bashrc" || \
            echo 'export PATH="$HOME/.foundry/bin:$PATH"' >> "$HOME/.bashrc"
    fi
    foundryup
    echo "  Foundry installed: $(forge --version | head -1)"
fi
EOF

# === Aptos CLI (move test + move prove — L2 + L3 + L4 for Aptos) ===
echo "==> Installing Aptos CLI"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$VPS_HOST" 'bash -s' <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
if command -v aptos &>/dev/null; then
    echo "  Aptos CLI already present: $(aptos --version 2>&1 | head -1)"
else
    # Install via the official aptos installer script
    apt-get install -y -qq libssl-dev pkg-config
    curl -fsSL https://aptos.dev/scripts/install_cli.py | python3 || {
        # Fallback: direct binary download from latest release
        APTOS_VERSION="$(curl -s https://api.github.com/repos/aptos-labs/aptos-core/releases/latest \
                        | grep -oE 'aptos-cli-[0-9]+\.[0-9]+\.[0-9]+' | head -1 | sed 's/aptos-cli-//')"
        APTOS_VERSION="${APTOS_VERSION:-3.5.0}"
        curl -fsSL "https://github.com/aptos-labs/aptos-core/releases/download/aptos-cli-v${APTOS_VERSION}/aptos-cli-${APTOS_VERSION}-Linux-x86_64.zip" \
             -o /tmp/aptos.zip
        cd /tmp && unzip -o aptos.zip
        mv aptos /usr/local/bin/aptos
        chmod +x /usr/local/bin/aptos
    }
    # Move Prover backend (boogie + z3) — pulled in via `aptos move prove`
    # on first run; explicit install for predictability
    aptos update prover-dependencies 2>/dev/null || \
        echo "  (prover backend will install on first 'aptos move prove' run)"
    echo "  Aptos CLI installed: $(aptos --version 2>&1 | head -1)"
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
echo "=== Solana / Rust pipeline ==="
echo "Rust:        $(rustc --version)"
echo "Cargo:       $(cargo --version)"
echo "Solana:      $(solana --version 2>&1 || echo MISSING)"
echo "build-sbf:   $(which cargo-build-sbf 2>&1)"
echo "Kani:        $(cargo kani --version 2>&1 | head -1)"
echo
echo "=== C pipeline ==="
echo "clang:       $(clang --version | head -1)"
echo "CBMC:        $(cbmc --version 2>&1 | head -1)"
echo "AFL++:       $(afl-fuzz --version 2>&1 | head -1)"
echo "afl-clang:   $(afl-clang-fast --version 2>&1 | head -1 || echo MISSING)"
echo
echo "=== Solidity pipeline ==="
echo "solc:        $(solc --version 2>&1 | tail -1)"
echo "forge:       $(forge --version 2>&1 | head -1)"
echo "cast:        $(cast --version 2>&1 | head -1)"
echo "anvil:       $(anvil --version 2>&1 | head -1)"
echo
echo "=== Aptos / Move pipeline ==="
echo "aptos:       $(aptos --version 2>&1 | head -1)"
echo
echo "=== Operator tools ==="
echo "gh CLI:      $(gh --version 2>&1 | head -1)"
echo "tmux:        $(tmux -V)"
echo
echo "Provision complete. All 4 language pipelines installed."
EOF

echo
echo "==> Done. VPS is ready for audit work."
echo "    Working dir: /tmp/audit/"
echo "    Next step:   bash scripts/dispatch_kani.sh $VPS_HOST <harness>"
