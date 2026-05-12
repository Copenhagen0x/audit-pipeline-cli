#!/usr/bin/env python3
"""P3 (fix-bundle) dispatcher for Kani-confirmed bugs.

For each confirmed hyp:
  1. Call `audit-pipeline bundle draft <finding_id>` — LLM authors patch +
     writeup, persisted to workspace/findings/bundles/<id>/
  2. Call `audit-pipeline bundle verify <finding_id>` — runs the 4-5 machine
     verification gates: PoC reproduces pre-patch / patch applies cleanly /
     PoC passes post-patch / no regressions / signature.
  3. Log result. NO upstream PR — that's a manual gate per HARD RULE memory.

Concurrency = 2.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
WORKSPACE = Path("/root/audit_runs/percolator-live")
ENGINE_REPO = WORKSPACE / "target" / "engine"
POC_DIR = CYCLE / "poc"
LOG = CYCLE / "hunt.log.jsonl"

CONCURRENCY = 1  # serialize: per-hyp file management on engine_repo/tests/
DRAFT_TIMEOUT = 1200
VERIFY_TIMEOUT = 1800
ENGINE_TESTS = Path("/root/audit_runs/percolator-live/target/engine/tests")
L2_BACKUP = Path("/tmp/l2_poc_backup")

# Kani-confirmed bugs (21). Run P3 on these — they have strongest evidence.
KANI_CONFIRMED = [
    "H1-residual-conservation",
    "H5-permissionless-trigger-surface",
    "PD4-residual-conservation",
    "PD9-haircut-direction-monotonic",
    "S3-settle-after-close",
    "V1-residual-conservation-strict",
    "V1-vault-residual-conservation",
    "V2-vault-balance-equation",
    "V5-haircut-direction",
    "V7-insurance-counter-vault-coupling",
    "SH6-resolve-flat-negative-gate",
    "AR7-saturating-arithmetic-correctness",
    "CI10-resolution-final",
    "T1-hyperp-mark-cpi-bundled-trade",
    "U20-resolvedcrank-early-return-skips-recurring-fees",
    "U21-permissionless-resolve-bypass-engine-init-check",
    "U29-hyperp-mark-push-zero-after-stale-allowed",
    "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
    "P2-oracle-account-binding",
    "V26-compute-trade-pnl-no-i128-min",
]


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def append_log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def hyp_to_finding_id(hyp_id: str, cycle_id: str = "20260511-183154") -> int | None:
    """Map hypothesis_id → integer finding id for this cycle."""
    import sqlite3
    db = sqlite3.connect("/root/audit_runs/percolator-live/findings.db")
    row = db.execute(
        "SELECT id FROM findings WHERE hypothesis_id=? AND cycle_id=?",
        (hyp_id, cycle_id),
    ).fetchone()
    db.close()
    return row[0] if row else None


def draft_one(hyp_id: str) -> dict:
    """Run audit-pipeline bundle draft + verify."""
    poc_test_name = f"test_{slug(hyp_id)}"
    poc_path = POC_DIR / f"{poc_test_name}.rs"
    if not poc_path.is_file():
        return {"hyp_id": hyp_id, "outcome": "no_poc", "detail": str(poc_path)}

    finding_id = hyp_to_finding_id(hyp_id)
    if finding_id is None:
        return {"hyp_id": hyp_id, "outcome": "no_finding_row"}

    # Per-hyp file mgmt: only the relevant L2 PoC test must live in tests/
    # during cargo test runs, otherwise the 437 broken sibling PoCs from
    # earlier dispatcher runs break the full-suite gate. Stage just this
    # hyp's PoC into tests/ and remove after.
    L2_BACKUP.mkdir(parents=True, exist_ok=True)
    test_in_engine = ENGINE_TESTS / f"{poc_test_name}.rs"
    test_in_backup = L2_BACKUP / f"{poc_test_name}.rs"
    staged = False
    if test_in_backup.is_file() and not test_in_engine.is_file():
        import shutil
        shutil.copy(str(test_in_backup), str(test_in_engine))
        staged = True
    elif not test_in_engine.is_file() and poc_path.is_file():
        # Fallback: copy directly from cycle/poc/
        import shutil
        shutil.copy(str(poc_path), str(test_in_engine))
        staged = True

    env = {
        **os.environ,
        "JELLEO_SPEND_CALLER": f"p3_v1/{hyp_id}/draft",
    }

    # 1. draft (LLM authors patch)
    append_log({
        "event": "bundle_draft", "hypothesis_id": hyp_id,
        "phase": "draft_start", "dispatcher": "p3_v1",
    })
    draft_cmd = [
        "/usr/local/bin/audit-pipeline",
        "--workspace", str(WORKSPACE),
        "bundle", "draft", str(finding_id),
        "--engine-repo", str(ENGINE_REPO),
        "--target-file", "src/percolator.rs",
        "--poc-source-file", str(poc_path),
        "--poc-test-name", poc_test_name,
    ]
    try:
        proc = subprocess.run(
            draft_cmd, capture_output=True, text=True,
            timeout=DRAFT_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        append_log({"event": "bundle_one", "hypothesis_id": hyp_id,
                    "outcome": "draft_timeout", "dispatcher": "p3_v1"})
        return {"hyp_id": hyp_id, "outcome": "draft_timeout"}
    draft_rc = proc.returncode
    draft_tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-500:]
    if draft_rc != 0:
        append_log({"event": "bundle_one", "hypothesis_id": hyp_id,
                    "outcome": "draft_failed", "rc": draft_rc,
                    "stderr_tail": draft_tail, "dispatcher": "p3_v1"})
        return {"hyp_id": hyp_id, "outcome": "draft_failed", "rc": draft_rc,
                "tail": draft_tail}

    # 2. verify (apply patch + run gates)
    env["JELLEO_SPEND_CALLER"] = f"p3_v1/{hyp_id}/verify"
    verify_cmd = [
        "/usr/local/bin/audit-pipeline",
        "--workspace", str(WORKSPACE),
        "bundle", "verify", str(finding_id),
        "--engine-repo", str(ENGINE_REPO),
        "--poc-test-name", poc_test_name,
    ]
    try:
        proc = subprocess.run(
            verify_cmd, capture_output=True, text=True,
            timeout=VERIFY_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        append_log({"event": "bundle_one", "hypothesis_id": hyp_id,
                    "outcome": "verify_timeout", "dispatcher": "p3_v1"})
        return {"hyp_id": hyp_id, "outcome": "verify_timeout"}
    verify_rc = proc.returncode
    verify_tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-800:]

    # Read verification.json from bundle dir
    bundle_dir = WORKSPACE / "recon" / "bundles" / str(finding_id)
    verification_path = bundle_dir / "verification.json"
    verification = {}
    if verification_path.is_file():
        try:
            verification = json.loads(verification_path.read_text())
        except Exception:
            pass

    gates = verification.get("gates", {})
    pass_count = sum(1 for g in gates.values() if g.get("passed") is True)
    skip_count = sum(1 for g in gates.values() if g.get("passed") is None)
    fail_count = sum(1 for g in gates.values() if g.get("passed") is False)

    if fail_count > 0:
        outcome = "verify_some_gates_failed"
    elif pass_count == 0:
        outcome = "verify_no_gates_passed"
    else:
        outcome = "bundle_verified" if fail_count == 0 else "bundle_partial"

    append_log({
        "event": "bundle_one", "hypothesis_id": hyp_id,
        "outcome": outcome, "draft_rc": draft_rc, "verify_rc": verify_rc,
        "gates_pass": pass_count, "gates_skip": skip_count,
        "gates_fail": fail_count, "dispatcher": "p3_v1",
    })
    # Cleanup: remove staged test if we put it there
    if staged and test_in_engine.is_file():
        try:
            test_in_engine.unlink()
        except OSError:
            pass

    return {
        "hyp_id": hyp_id, "outcome": outcome,
        "draft_rc": draft_rc, "verify_rc": verify_rc,
        "gates_pass": pass_count, "gates_skip": skip_count,
        "gates_fail": fail_count, "verify_tail": verify_tail,
    }


def main() -> int:
    print(f"P3 v1 fix-bundle dispatcher")
    print(f"Hyps: {len(KANI_CONFIRMED)} Kani-confirmed bugs")
    print(f"Concurrency: {CONCURRENCY}")
    print()
    verified = []
    failed = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(draft_one, h): h for h in KANI_CONFIRMED}
        for fut in as_completed(futs):
            done += 1
            hid = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                failed.append((hid, f"exception: {e}"))
                print(f"[{done}/{len(KANI_CONFIRMED)}] {hid} EXCEPTION: {e}",
                      flush=True)
                continue
            print(f"[{done}/{len(KANI_CONFIRMED)}] {hid} {r.get('outcome')} "
                  f"(gates: pass={r.get('gates_pass')} skip={r.get('gates_skip')} "
                  f"fail={r.get('gates_fail')})", flush=True)
            if r.get("outcome") == "bundle_verified":
                verified.append(hid)
            else:
                failed.append((hid, r.get("outcome")))
    print()
    print(f"DONE. verified={len(verified)} failed={len(failed)}")
    if verified:
        print("VERIFIED FIX BUNDLES:")
        for h in verified:
            print(f"  {h}")
    if failed:
        print("FAILED / NEEDS REVIEW:")
        for h, why in failed:
            print(f"  {h}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
