#!/usr/bin/env bash
# generate_audit_keypair.sh — create a dedicated SSH keypair for VPS audit access.
#
# Usage:
#   bash scripts/helpers/generate_audit_keypair.sh <output-path>
#
# Example:
#   bash scripts/helpers/generate_audit_keypair.sh ~/.ssh/audit_vps
#
# Why dedicated key:
#   - Audit work touches sensitive infrastructure (test VPS with toolchain)
#   - Best practice: never use your personal SSH key for audit-managed hosts
#   - Easier to revoke access on a single project basis
#
# Output:
#   <path>      — private key (keep secret, never commit)
#   <path>.pub  — public key (copy to VPS authorized_keys)

set -euo pipefail

OUTPUT="${1:?Usage: generate_audit_keypair.sh <output-path>}"

if [[ -f "$OUTPUT" ]]; then
    echo "ERROR: $OUTPUT already exists. Refusing to overwrite." >&2
    echo "       To regenerate, delete the existing keypair first." >&2
    exit 1
fi

# Ed25519 — modern, fast, short, secure
ssh-keygen \
    -t ed25519 \
    -C "audit-pipeline-$(date +%Y-%m-%d)" \
    -f "$OUTPUT" \
    -N ""  # no passphrase (for automation; consider passphrase + ssh-agent for higher security)

chmod 600 "$OUTPUT"
chmod 644 "$OUTPUT.pub"

echo
echo "==> Keypair generated:"
echo "    Private: $OUTPUT (chmod 600)"
echo "    Public:  $OUTPUT.pub"
echo
echo "==> Next step: copy the public key to your VPS"
echo "    cat $OUTPUT.pub"
echo "    # then on VPS: append to ~/.ssh/authorized_keys"
echo
echo "    Or, if you have password access to the VPS:"
echo "    ssh-copy-id -i $OUTPUT.pub <user>@<vps-host>"
