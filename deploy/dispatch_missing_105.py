#!/usr/bin/env python3
"""One-shot dispatcher for the 105 hyps the hunt loop never picked up.

Reads recon_summary.json, finds the L2 queue (TRUE + NEEDS_LAYER_2_TO_DECIDE),
subtracts what's already on disk (cargo_*.log present), looks up each missing
hyp in the templates/hypotheses/*.yaml files, and dispatches each via the
existing 'audit-pipeline poc-llm' subcommand (same code path the hunt uses).

Concurrency = 8 (matches hunt's --max-concurrent).
Logs poc_llm_authored / poc_test_run events to hunt.log.jsonl in the same
format so the dashboard / triage scripts keep working.

NO retest loop. Each missing hyp is dispatched exactly once.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
HYP_DIR = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")
ENGINE_ROOT = Path("/root/audit_runs/percolator-live/target/engine")
ENGINE_TESTS = ENGINE_ROOT / "tests"
POC_OUT = CYCLE / "poc"
LOG = CYCLE / "hunt.log.jsonl"
RECON_SUMMARY = CYCLE / "recon" / "recon_summary.json"

CONCURRENCY = 8
CARGO_TIMEOUT = 600


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def load_all_hyps() -> dict[str, tuple[str, dict]]:
    """Returns {hyp_id: (source_yaml_basename, hyp_dict)}."""
    out: dict[str, tuple[str, dict]] = {}
    for f in HYP_DIR.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"WARN: failed to parse {f.name}: {e}", file=sys.stderr)
            continue
        for _k, v in data.items():
            if not isinstance(v, list):
                continue
            for h in v:
                if isinstance(h, dict) and h.get("id"):
                    out[h["id"]] = (f.name, h)
    return out


def l2_queue_from_recon() -> set[str]:
    rs = json.loads(RECON_SUMMARY.read_text(encoding="utf-8"))
    queue: set[str] = set()
    for v in rs.get("verdicts", []):
        vd = (v.get("verdict") or "").upper()
        if vd in ("TRUE", "NEEDS_LAYER_2_TO_DECIDE"):
            hid = v.get("hypothesis_id")
            if hid:
                queue.add(hid)
    return queue


def already_on_disk() -> set[str]:
    """Slugs of hyps that already have a cargo_*.log file."""
    seen: set[str] = set()
    for f in POC_OUT.glob("cargo_*.log"):
        seen.add(f.stem[len("cargo_"):])
    return seen


def append_log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    line = json.dumps(event)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def dispatch_one(hyp_id: str, source_yaml_basename: str, hyp: dict) -> tuple[str, bool, str]:
    """Author + run a single hyp's PoC. Returns (hyp_id, fired, status_str)."""
    payload = {"hypotheses": [hyp]}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.safe_dump(payload, tmp)
        tmp_path = tmp.name

    try:
        cmd = [
            "/usr/local/bin/audit-pipeline",
            "--workspace", "/root/audit_runs/percolator-live",
            "poc-llm",
            "--hypothesis-id", hyp_id,
            "--hypotheses", tmp_path,
            "--engine-root", str(ENGINE_ROOT),
            "--output", str(POC_OUT),
        ]
        # Tag the spend log so we can attribute these calls to the dispatcher
        # vs the main hunt loop. llm.py reads JELLEO_SPEND_CALLER and writes
        # it into each api_calls.jsonl event.
        env = {
            **__import__("os").environ,
            "JELLEO_SPEND_CALLER": f"dispatch_missing/{hyp_id}",
        }
        append_log({
            "event": "poc_llm_authored",
            "hypothesis_id": hyp_id,
            "source": source_yaml_basename,
            "dispatcher": "missing_105",
        })
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if proc.returncode != 0:
            append_log({
                "event": "poc_test_run",
                "hypothesis_id": hyp_id,
                "fired": False,
                "status": "author_failed",
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-300:],
            })
            return hyp_id, False, f"author_failed rc={proc.returncode}"

        # Step 2: copy authored test from poc/ into engine_dir/tests/ so
        # cargo can find it. This mirrors hunt.py's logic.
        hyp_slug = slug(hyp_id)
        scaffold_path = POC_OUT / f"test_{hyp_slug}.rs"
        if not scaffold_path.is_file():
            append_log({
                "event": "poc_test_run",
                "hypothesis_id": hyp_id,
                "fired": False,
                "status": "scaffold_missing",
            })
            return hyp_id, False, "scaffold_missing"
        test_dest = ENGINE_TESTS / f"test_{hyp_slug}.rs"
        try:
            test_dest.write_text(scaffold_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:
            append_log({
                "event": "poc_test_run",
                "hypothesis_id": hyp_id,
                "fired": False,
                "status": "copy_failed",
                "error": str(e),
            })
            return hyp_id, False, f"copy_failed: {e}"

        # Step 3: run cargo test --features test --test test_<slug>
        log_path = POC_OUT / f"cargo_{hyp_slug}.log"
        try:
            cargo_proc = subprocess.run(
                ["cargo", "test", "--features", "test", "--test", f"test_{hyp_slug}"],
                cwd=str(ENGINE_ROOT),
                capture_output=True,
                text=True,
                timeout=CARGO_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            append_log({
                "event": "poc_test_run",
                "hypothesis_id": hyp_id,
                "fired": False,
                "status": "cargo_timeout",
            })
            return hyp_id, False, "cargo_timeout"

        combined = (cargo_proc.stdout or "") + "\n" + (cargo_proc.stderr or "")
        log_path.write_text(combined, encoding="utf-8")

        # Step 4: parse cargo outcome (mirrors hunt.py logic)
        looks_compile_failed = (
            "could not compile" in combined
            or "error[E" in combined.split("test result:")[0]
            or "did not match any tests" in combined
            or "no test target" in combined.lower()
        )
        looks_test_fired = (
            "test result: FAILED" in combined
            or "panicked at" in combined
            or "assertion failed" in combined
        )
        if looks_compile_failed:
            outcome = "compile_error"
            fired = False
        elif looks_test_fired:
            outcome = "test_failed_bug_reproduced"
            fired = True
        elif cargo_proc.returncode == 0:
            outcome = "test_passed_no_bug"
            fired = False
        else:
            outcome = f"unknown_rc_{cargo_proc.returncode}"
            fired = False

        append_log({
            "event": "poc_test_run",
            "hypothesis_id": hyp_id,
            "cargo_rc": cargo_proc.returncode,
            "outcome": outcome,
            "fired": fired,
            "status": "ok",
        })
        return hyp_id, fired, "FIRED" if fired else outcome
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


def main() -> int:
    all_hyps = load_all_hyps()
    queue = l2_queue_from_recon()
    on_disk_slugs = already_on_disk()
    missing: list[tuple[str, str, dict]] = []
    for hid in sorted(queue):
        if slug(hid) in on_disk_slugs:
            continue
        if hid not in all_hyps:
            print(f"WARN: {hid} in recon queue but not in any yaml — skipping", file=sys.stderr)
            continue
        src, hyp = all_hyps[hid]
        missing.append((hid, src, hyp))
    print(f"L2 queue: {len(queue)} hyps")
    print(f"On disk: {len(on_disk_slugs)} hyps")
    print(f"To dispatch: {len(missing)} hyps")

    fires: list[str] = []
    failures: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(dispatch_one, hid, src, hyp): hid for hid, src, hyp in missing}
        for fut in as_completed(futs):
            hid = futs[fut]
            done += 1
            try:
                hid, fired, status = fut.result()
            except Exception as e:
                failures.append(hid)
                print(f"[{done}/{len(missing)}] {hid} EXCEPTION: {e}", flush=True)
                continue
            tag = "FIRE" if fired else status
            print(f"[{done}/{len(missing)}] {hid} {tag}", flush=True)
            if fired:
                fires.append(hid)
    print(f"\nDONE. fires={len(fires)} failures={len(failures)}")
    print("NEW FIRES:")
    for h in fires:
        print(f"  {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
