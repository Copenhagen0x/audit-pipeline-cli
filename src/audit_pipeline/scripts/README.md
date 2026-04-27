# Scripts

Bash scripts for the operational layers of the audit pipeline (provisioning, dispatch, fetch, cross-platform compare).

## Quick reference

| Script | What it does | When to run |
|---|---|---|
| [`provision_vps.sh`](./provision_vps.sh) | Install Rust + Solana + Kani + tmux + gh on a fresh Ubuntu 22.04 VPS | Once per audit project (fresh VPS) |
| [`helpers/generate_audit_keypair.sh`](./helpers/generate_audit_keypair.sh) | Create a dedicated Ed25519 SSH keypair | Once per audit project (before provisioning) |
| [`helpers/sync_target_repos.sh`](./helpers/sync_target_repos.sh) | Clone target engine + wrapper at pinned SHAs onto VPS | Once per audit, then re-run if you re-anchor SHAs |
| [`dispatch_kani.sh`](./dispatch_kani.sh) | Push a Kani harness to VPS, run it in tmux | Per Layer-3 harness, OR `--baseline` for the full suite |
| [`check_kani_status.sh`](./check_kani_status.sh) | Pull current state of a running Kani session | Periodically while Kani runs |
| [`fetch_kani_artifacts.sh`](./fetch_kani_artifacts.sh) | Pull Kani run log + summary + timings TSV from VPS | After a Kani run completes |
| [`dispatch_litesvm.sh`](./dispatch_litesvm.sh) | Run a LiteSVM test on the VPS | Per Layer-4 cross-platform run |
| [`cross_platform_compare.sh`](./cross_platform_compare.sh) | Diff a test's output between local and VPS | Per Layer-5 reproduction check |

## End-to-end flow

```bash
# 0. One-time setup
bash scripts/helpers/generate_audit_keypair.sh ~/.ssh/audit_vps
ssh-copy-id -i ~/.ssh/audit_vps.pub root@<vps-host>
bash scripts/provision_vps.sh root@<vps-host> ~/.ssh/audit_vps

# 1. Sync target code
bash scripts/helpers/sync_target_repos.sh root@<vps-host> ~/.ssh/audit_vps \
    https://github.com/<org>/<engine-repo> <engine-sha> \
    https://github.com/<org>/<wrapper-repo> <wrapper-sha>

# 2. Develop Kani harness locally; copy to VPS
scp -i ~/.ssh/audit_vps tests/proofs_my_finding.rs \
    root@<vps-host>:/tmp/audit/engine/tests/

# 3. Dispatch Kani run
bash scripts/dispatch_kani.sh root@<vps-host> ~/.ssh/audit_vps \
    proof_my_finding_does_not_panic

# 4. Periodically check progress
bash scripts/check_kani_status.sh root@<vps-host> ~/.ssh/audit_vps \
    proof_my_finding_does_not_panic

# 5. When complete, fetch artifacts
bash scripts/fetch_kani_artifacts.sh root@<vps-host> ~/.ssh/audit_vps \
    proof_my_finding_does_not_panic

# 6. Same flow for LiteSVM (Layer 4)
bash scripts/dispatch_litesvm.sh root@<vps-host> ~/.ssh/audit_vps \
    test_my_finding_litesvm

# 7. Cross-platform compare (Layer 5)
bash scripts/cross_platform_compare.sh root@<vps-host> ~/.ssh/audit_vps \
    /local/path/to/wrapper test_my_finding_litesvm

# 8. Re-run maintainer's existing baseline (Layer 5 capstone)
bash scripts/dispatch_kani.sh root@<vps-host> ~/.ssh/audit_vps --baseline
# wait for it to finish (hours), then:
bash scripts/fetch_kani_artifacts.sh root@<vps-host> ~/.ssh/audit_vps --baseline
```

## Common gotchas

### "ssh: connect to host: Connection refused"

The VPS is down OR your firewall is blocking port 22. Check VPS status with your provider's dashboard.

### "Permission denied (publickey)" on first run

The public key isn't on the VPS yet. Run `ssh-copy-id` first OR manually paste the `.pub` content into `~/.ssh/authorized_keys` on the VPS.

### "tmux session already exists" when dispatching

Either you already started a run with the same name, OR a prior run died but left the session. Kill it: `ssh <host> "tmux kill-session -t <name>"`.

### Kani harness doesn't compile on VPS

Cargo compiles all tests when ANY test is requested. A broken file in `tests/` blocks everything. SSH in, run `cargo check --tests --features test`, fix the compile error.

### LiteSVM `init_market` fails with `Custom(4)` error

The BPF artifact (`target/deploy/*.so`) was built with different feature flags than the test. Rebuild with matching flags: `cargo build-sbf --features small` and run tests with `--features small`.

### Disk fills up on VPS

Kani's `target/kani/` accumulates GBs. Clean periodically: `ssh <host> "cd /tmp/audit/engine && cargo clean -p <crate>"`.

## Customization

These scripts are written for the specific shape of audit work that produced the Percolator audit. To adapt:

- For a non-Solana target, edit `provision_vps.sh` to skip Solana/cargo-build-sbf installation
- For a private VPS network (VPN, SSH bastion), wrap the `ssh` invocations with your jumphost config
- For multi-VPS parallel runs, copy the dispatch scripts and parameterize the session-name prefix

The scripts assume the standard `/tmp/audit/{engine,wrapper,results}` layout. If you want a different layout, update `WORKDIR` constants at the top of each script.

## See also

- [`../docs/layer5-cross-platform-reproduction.md`](../docs/layer5-cross-platform-reproduction.md) for the conceptual layer that these scripts implement
- [`../docs/lessons-learned.md`](../docs/lessons-learned.md) for operational gotchas accumulated from Percolator
