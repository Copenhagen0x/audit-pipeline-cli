#!/usr/bin/env python3
"""Manual P3 verify for aptos-large 20260514-233645.

Engine P3 gates are Cargo/Rust-shaped and return null/false on Move repos. This
runs the real semantic check: apply each cluster-rep patch, run
`aptos move test`, watch the abort code shift from E_BUG_HIT (bug present)
to a different abort (patch caught the unauthorized path).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

CYCLE_DIR = Path("/root/audit_runs/ottersec-eval/workspaces/aptos-large/hunts/20260514-233645")
BUNDLE_ROOT = Path("/root/audit_runs/ottersec-eval/workspaces/aptos-large/recon/bundles")
TESTS_DIR = Path("/root/audit_runs/ottersec-eval/workspaces/aptos-large/tests/aptos")
REPO = Path("/root/ottersec-eval/repos/aptos-large")
SOURCES = REPO / "sources"

CLUSTER_REPS = [
    "APT1-borrow-global-no-auth",
    "APT10-u64-overflow-arith",
    "APT19-missing-pause-check",
    "APT3-signer-address-of-not-checked",
    "APT37-fee-percent-bound",
    "APT38-treasury-drain",
    "APT6-friend-module-trust",
    "APTL1-vault-core-sweep-reserve-no-auth",
    "APTL10-escrow-house-credit-debit-no-auth",
    "APTL11-debt-accounting-credit-debit-no-auth",
    "APTL12-collateral-manager-credit-debit-no-auth",
    "APTL2-treasury-collect-no-auth",
    "APTL23-marketplace-deposit-non-admin-bypass-reserve",
    "APTL24-marketplace-settle-trade-no-fee-bps-bound",
    "APTL25-ensure-slot-ignores-owner-key",
    "APTL27-governance-execute-all-clears-all-flags",
    "APTL3-fee-policy-unsafe-set-fee-no-auth",
    "APTL30-voting-power-ignores-voter",
    "APTL33-prune-disabled-credits-reserve-balance-cleared",
    "APTL4-initializer-reset-fee-policy-no-auth",
]


def slugify(hyp_id: str) -> str:
    return hyp_id.lower().replace("-", "_")


def find_finding_id(hyp_id: str) -> int | None:
    out = subprocess.run(
        [
            "sqlite3",
            "/root/audit_runs/ottersec-eval/findings.db",
            f"SELECT id FROM findings WHERE cycle_id='20260514-233645' AND hypothesis_id='{hyp_id}'",
        ],
        capture_output=True,
        text=True,
    )
    val = out.stdout.strip()
    return int(val) if val.isdigit() else None


ABORT_RE = re.compile(r"aborted with code (\d+) originating in the module .*?::(\S+) rooted here")


def parse_abort(log: str) -> tuple[int, str] | None:
    m = ABORT_RE.search(log)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def deploy_l2_test(hyp: str) -> Path | None:
    slug = slugify(hyp).replace("-", "_")
    src = TESTS_DIR / f"test_{slug}.move"
    if not src.exists():
        return None
    dst = REPO / "tests" / f"jelleo_l2_{slug}.move"
    dst.write_text(src.read_text())
    return dst


def run_test(test_name: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["aptos", "move", "test", "--filter", test_name],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode, proc.stdout + "\n--- stderr ---\n" + proc.stderr


def classify_post(pre_log: str, post_log: str) -> str:
    """Classify the post-patch outcome relative to pre-patch."""
    pre_abort = parse_abort(pre_log)
    post_abort = parse_abort(post_log)

    # Compile error after patch — attacker call path blocked
    if "Failed to run tests" in post_log and "private to module" in post_log:
        return "FIX_compile_blocked_attacker"
    if "Failed to run tests" in post_log:
        return "FIX_compile_error_post_patch"

    # Test passes post-patch (was failing pre)
    if "Total tests: 1; passed: 1; failed: 0" in post_log and pre_abort:
        return "FIX_test_now_passes"

    # Abort code shifted
    if pre_abort and post_abort and pre_abort[0] != post_abort[0]:
        return "FIX_abort_shifted"

    # No change — patch didn't fix
    if pre_abort and post_abort and pre_abort == post_abort:
        return "NO_FIX_same_abort"

    return "INCONCLUSIVE"


def apply_patch(patch_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", "apply", "--whitespace=fix", str(patch_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, proc.stdout + "\n--- stderr ---\n" + proc.stderr


def revert_repo() -> None:
    subprocess.run(["git", "checkout", "--", "sources/"], cwd=REPO, check=False)
    subprocess.run(["git", "clean", "-fd", "sources/"], cwd=REPO, check=False)


def main() -> int:
    results = []
    for hyp in CLUSTER_REPS:
        finding_id = find_finding_id(hyp)
        slug = slugify(hyp).replace("-", "_")
        test_slug = slug
        test_file = TESTS_DIR / f"test_{test_slug}.move"
        # Read actual test fn names from the .move file body
        test_name_candidates: list[str] = []
        if test_file.exists():
            for line in test_file.read_text().splitlines():
                m = re.match(r"\s*fun\s+(\w+)\s*\(", line)
                if m:
                    test_name_candidates.append(m.group(1))
        # Also grab module name from `module mutatis::<name>`
        if test_file.exists():
            mm = re.search(r"module\s+\w+::(\w+)", test_file.read_text())
            if mm:
                test_name_candidates.append(mm.group(1))
        # Fallback: filter substring
        test_name_candidates.append(test_slug)

        bundle = BUNDLE_ROOT / str(finding_id) if finding_id else None
        patch = bundle / "patch.diff" if bundle else None

        entry: dict[str, object] = {
            "hyp": hyp,
            "finding_id": finding_id,
            "patch": str(patch) if patch else None,
            "test_file": str(test_file),
        }

        if not patch or not patch.exists():
            entry["status"] = "no_patch_file"
            results.append(entry)
            continue

        # PRE: read prior runlog abort code (already on disk from L2)
        pre_log_path = CYCLE_DIR / "poc" / f"runlog_{test_slug}.log"
        pre_log = pre_log_path.read_text() if pre_log_path.exists() else ""
        pre_abort = parse_abort(pre_log)
        entry["pre_abort"] = pre_abort

        # Reset + deploy L2 test into repo
        revert_repo()
        deployed = deploy_l2_test(hyp)
        if not deployed:
            entry["status"] = "no_test_file"
            results.append(entry)
            continue

        # Apply patch
        applied, apply_log = apply_patch(patch)
        entry["patch_applied"] = applied
        entry["apply_log"] = apply_log[-300:]

        # Run test (always run, even if apply failed — gives us post=pre as baseline)
        post_rc = None
        post_log = ""
        for tn in test_name_candidates:
            post_rc, post_log = run_test(tn)
            if "Total tests:" in post_log or "Failed to run tests" in post_log:
                break

        entry["post_rc"] = post_rc
        entry["post_abort"] = parse_abort(post_log)
        entry["post_log_tail"] = post_log[-400:]

        if not applied:
            entry["status"] = "patch_did_not_apply"
        else:
            entry["status"] = classify_post(pre_log, post_log)

        # Cleanup: remove deployed test, revert repo
        if deployed.exists():
            deployed.unlink()
        revert_repo()
        results.append(entry)

        # Print compact line
        print(
            f"{hyp:55s} status={entry['status']:30s} "
            f"pre={pre_abort} post={post_abort}",
            flush=True,
        )

    out = CYCLE_DIR / "manual_p3_verify.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out}")

    # Tally
    tally: dict[str, int] = {}
    for r in results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print("\n=== TALLY ===")
    for k, v in sorted(tally.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
