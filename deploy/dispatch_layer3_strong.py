#!/usr/bin/env python3
"""Layer 3 (Kani) dispatcher targeting only the 23 STRONG fires.

Runs `audit-pipeline synth-kani --auto --run-kani` for each hyp in the
hand-curated STRONG list (manually triaged from L2 fires — F7 family + 12
net-new bug roots). Logs results to hunt.log.jsonl + writes per-call spend
events via llm.py's automatic tracking.

Single-pass, no retest loop, no auto-promote to P2.
Concurrency = 2 (Kani is RAM-heavy; running too many in parallel can OOM).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
HYP_DIR = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")
ENGINE_ROOT = Path("/root/audit_runs/percolator-live/target/engine")
KANI_OUT = CYCLE / "kani"
LOG = CYCLE / "hunt.log.jsonl"

# Hand-curated STRONG fires from manual L2 triage (2026-05-12).
# 11 F7-family + 12 net-new bug roots = 23 total.
STRONG_HYPS = [
    # ---- F7 vault/insurance divergence family (11) ----
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
    # ---- Net-new distinct bug roots (12) ----
    "AR7-saturating-arithmetic-correctness",
    "CI10-resolution-final",
    "L3-keeper-crank-cursor-budget",
    "T1-hyperp-mark-cpi-bundled-trade",
    "U20-resolvedcrank-early-return-skips-recurring-fees",
    "U21-permissionless-resolve-bypass-engine-init-check",
    "U29-hyperp-mark-push-zero-after-stale-allowed",
    "U30-deposit-fee-credits-zero-debt-after-sync-still-succeeds",
    "K12-cu-tier-test-loophole-tier-promotion-dos",
    "P2-oracle-account-binding",
    "SH9-stuck-target-accrual-rejection",
    "X29-keepercrank-rr-cursor-double-scan",
    "V26-compute-trade-pnl-no-i128-min",
]

CONCURRENCY = 2
KANI_TIMEOUT = 1800  # 30 min/harness — Kani can be slow on complex invariants


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def load_hyp_meta(hyp_id: str) -> dict | None:
    """Find the YAML entry for a hyp ID (search all hyp libraries)."""
    for f in HYP_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for _k, v in data.items():
            if not isinstance(v, list):
                continue
            for h in v:
                if isinstance(h, dict) and h.get("id") == hyp_id:
                    return h
    return None


def append_log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def dispatch_one(hyp_id: str) -> tuple[str, str, str]:
    """Author + run Kani for one hyp. Returns (hyp_id, outcome, detail)."""
    meta = load_hyp_meta(hyp_id)
    if not meta:
        append_log({
            "event": "kani_one",
            "hypothesis_id": hyp_id,
            "outcome": "hyp_not_found",
            "dispatcher": "layer3_strong",
        })
        return hyp_id, "hyp_not_found", "no yaml entry"

    invariant = (meta.get("claim") or hyp_id)[:1500]
    engine_function = meta.get("engine_function") or "absorb_protocol_loss"
    harness_name = f"{slug(hyp_id)}_invariant"

    KANI_OUT.mkdir(parents=True, exist_ok=True)
    harness_path = KANI_OUT / f"proofs_{harness_name}.rs"

    # Skip if a harness was already authored (same RESUME logic as hunt.py).
    if harness_path.exists() and harness_path.stat().st_size > 100:
        append_log({
            "event": "kani_one",
            "hypothesis_id": hyp_id,
            "outcome": "resumed_from_existing",
            "harness_name": harness_name,
            "dispatcher": "layer3_strong",
        })
        return hyp_id, "resumed", f"existing harness at {harness_path.name}"

    cmd = [
        "/usr/local/bin/audit-pipeline",
        "--workspace", "/root/audit_runs/percolator-live",
        "synth-kani",
        "--invariant", invariant,
        "--engine-function", engine_function,
        "--harness-name", harness_name,
        "--output", str(KANI_OUT),
        "--auto",
        "--run-kani",
    ]

    env = {
        **os.environ,
        "JELLEO_SPEND_CALLER": f"layer3_strong/{hyp_id}",
        # Ensure cargo + kani are on PATH (subshells launched by audit-pipeline
        # synth-kani --run-kani need to find them).
        "PATH": "/root/.cargo/bin:" + os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    }

    append_log({
        "event": "kani_authored",
        "hypothesis_id": hyp_id,
        "harness_name": harness_name,
        "engine_function": engine_function,
        "dispatcher": "layer3_strong",
    })

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KANI_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        append_log({
            "event": "kani_one",
            "hypothesis_id": hyp_id,
            "outcome": "timeout",
            "harness_name": harness_name,
            "dispatcher": "layer3_strong",
        })
        return hyp_id, "timeout", f"exceeded {KANI_TIMEOUT}s"

    rc = proc.returncode
    stderr_tail = (proc.stderr or "")[-300:]
    stdout_tail = (proc.stdout or "")[-300:]
    combined_tail = (stdout_tail + stderr_tail).lower()

    if "verification successful" in combined_tail or "all proofs successful" in combined_tail:
        outcome = "kani_proved_safe"
    elif "verification failed" in combined_tail or "counterexample" in combined_tail:
        outcome = "kani_found_counterexample"  # bug confirmed!
    elif "could not compile" in combined_tail or "error[e" in combined_tail:
        outcome = "compile_error"
    elif rc == 0:
        outcome = "completed_no_signal"
    else:
        outcome = f"unknown_rc_{rc}"

    append_log({
        "event": "kani_one",
        "hypothesis_id": hyp_id,
        "outcome": outcome,
        "returncode": rc,
        "harness_name": harness_name,
        "stderr_tail": stderr_tail,
        "dispatcher": "layer3_strong",
    })
    return hyp_id, outcome, f"rc={rc}"


def main() -> int:
    print(f"Layer 3 dispatcher — {len(STRONG_HYPS)} STRONG hyps")
    print(f"Concurrency: {CONCURRENCY} (Kani is RAM-heavy)")
    print(f"Per-hyp timeout: {KANI_TIMEOUT}s ({KANI_TIMEOUT//60} min)")
    print(f"Output dir: {KANI_OUT}")
    print()

    confirmed: list[str] = []
    proved_safe: list[str] = []
    failed: list[tuple[str, str]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(dispatch_one, h): h for h in STRONG_HYPS}
        for fut in as_completed(futs):
            done += 1
            hid = futs[fut]
            try:
                hid, outcome, detail = fut.result()
            except Exception as e:
                failed.append((hid, f"exception: {e}"))
                print(f"[{done}/{len(STRONG_HYPS)}] {hid} EXCEPTION: {e}", flush=True)
                continue
            print(f"[{done}/{len(STRONG_HYPS)}] {hid} {outcome} ({detail})", flush=True)
            if outcome == "kani_found_counterexample":
                confirmed.append(hid)
            elif outcome == "kani_proved_safe":
                proved_safe.append(hid)
            else:
                failed.append((hid, outcome))

    print()
    print(f"DONE. confirmed={len(confirmed)} proved_safe={len(proved_safe)} failed={len(failed)}")
    print()
    if confirmed:
        print("CONFIRMED BY KANI (Layer 4 input):")
        for h in confirmed:
            print(f"  {h}")
    if proved_safe:
        print("PROVED SAFE BY KANI (refuted — not a bug):")
        for h in proved_safe:
            print(f"  {h}")
    if failed:
        print("FAILED / NEEDS REVIEW:")
        for h, why in failed:
            print(f"  {h}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
