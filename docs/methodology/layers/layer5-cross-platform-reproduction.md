# Layer 5 — Cross-platform reproduction

**Goal**: Reduce the "is this finding a platform-specific artifact (Windows-vs-Linux, host-vs-VPS, toolchain version) or a real protocol issue" uncertainty.

**Output**: Bit-identical pass/fail across at least 2 distinct host environments.

## Why this matters

A Layer-2 PoC that passes on your Windows laptop but fails on a Linux server is NOT a bug in the protocol. It's:
- A toolchain quirk (different rustc, different solana-cli)
- A platform-specific behavior (Windows path handling, file encoding)
- A flaky test that happened to pass once

Maintainers will (correctly) refuse a finding that can't be reproduced on their infrastructure. Layer 5 catches this before disclosure.

## The minimum bar: two distinct hosts

Required:
- **Host A**: your local development environment (typically Windows or macOS)
- **Host B**: a dedicated Linux VPS (Ubuntu 22.04 LTS recommended)

Both should run the SAME toolchain versions. The pipeline ships a `provision_vps.sh` script that installs the canonical toolchain (Rust 1.95, Solana 3.1.14, Kani 0.67.0).

For each Layer-2 PoC and Layer-4 LiteSVM test:

```
Run on Host A:  cargo test --test test_<finding>
Run on Host B:  cargo test --test test_<finding>

Bit-identical output → finding is platform-independent.
Different output → investigate which platform is "right".
```

## Provisioning the VPS

The pipeline assumes a Hetzner-style 6-core VPS. Cloud provider doesn't matter; what matters is:

| Resource | Minimum | Recommended |
|---|---|---|
| CPU cores | 4 | 6+ |
| RAM | 8 GB | 16-24 GB |
| Disk | 40 GB | 80 GB |
| Network | unmetered | unmetered |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| SSH access | key-based | dedicated audit keypair |

Run `bash scripts/provision_vps.sh <host> <ssh-key>` to install:
- Rust 1.95 + cargo-build-sbf
- Solana 3.1.14
- Kani 0.67.0 + nightly-2025-11-21 toolchain
- LiteSVM dependencies
- tmux (for long-running sessions)
- gh CLI (for issue/PR work)

## Dispatch pattern

For Kani harnesses (Layer 3), use tmux to run long-running verifications without keeping your SSH session open:

```bash
bash scripts/dispatch_kani.sh <vps-host> <harness-name>
# Equivalent to:
#   ssh <host> "tmux new-session -d -s kani_<harness> 'cargo kani --harness <harness>'"
# Returns immediately; check status later via:
bash scripts/check_kani_status.sh <vps-host> <harness-name>
```

For LiteSVM tests, just SSH and run:

```bash
ssh <host> "cd /tmp/audit/wrapper && cargo test --test test_<finding>"
```

## Cross-platform compare

For each test, capture both outputs and diff:

```bash
bash scripts/cross_platform_compare.sh <vps-host> <test-name>
# Output: "BIT-IDENTICAL" or "DIVERGENT (see /tmp/diff_<test>.txt)"
```

If divergent:
1. Read the diff
2. Identify which platform is producing the "expected" output
3. Either fix the test (if a platform-specific assumption snuck in) or document why divergence is acceptable

## Re-running the maintainer's existing baseline

A particularly strong Layer 5 move: re-run the maintainer's existing Kani proof suite against current main. This proves NO REGRESSION from your audit (you didn't break anything by exploring) AND it's a free signal of methodological care.

```bash
bash scripts/dispatch_kani.sh <vps-host> --baseline
# Runs `cargo kani --tests --features test` against the maintainer's full proof suite
# Returns total/pass/fail count; should be N/N PASS, 0 regressions
```

## Cost

| Item | Estimate |
|---|---|
| Hetzner CCX 6-core VPS | $30-50/mo |
| One-time provisioning | 30 min |
| Per-audit re-use | $0 (already provisioned) |

Compare to: one Layer-5-failure-on-the-call costs you the maintainer's trust for that engagement and possibly future ones. Even $50/mo is dramatically cheaper than that risk.

## When to skip Layer 5

Honest answer: rarely.

- If you're working on a non-public proof of concept for personal learning, sure.
- If the audit isn't going to be disclosed to a maintainer, sure.
- For any disclosure to a real maintainer of a deployed protocol, do not skip.

## See also

- [`scripts/provision_vps.sh`](../scripts/provision_vps.sh) — automated VPS setup
- [`scripts/cross_platform_compare.sh`](../scripts/cross_platform_compare.sh) — diff outputs across hosts
