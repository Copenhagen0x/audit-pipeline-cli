#!/usr/bin/env python3
"""Diagnose L3 SMTChecker — run all 12 harnesses sequentially, capture full stderr."""
import subprocess
import os
import glob
import time

REPO = "/root/ottersec-eval/repos/solidity-small"
WS = "/root/audit_runs/ottersec-eval/workspaces/solidity-small"
SCRATCH = WS + "/formal/solidity/scratch"
os.makedirs(SCRATCH, exist_ok=True)

harnesses = sorted(glob.glob(WS + "/formal/solidity/harness_*.sol"))
print("Testing", len(harnesses), "harnesses sequentially (no concurrency)\n")
hdr = ("HARNESS".ljust(56), "VERDICT".ljust(13),
       "stderr_bytes".ljust(14), "CE_byte".ljust(10), "WALL")
print(*hdr)
print("-" * 100)

for h in harnesses:
    name = os.path.basename(h).replace("harness_", "").replace(".sol", "")
    tmp = SCRATCH + "/jelleo_l3_" + name + ".sol"
    with open(h) as f:
        body = f.read()
    with open(tmp, "w") as f:
        f.write(body)
    cmd = [
        "solc",
        "@src/=" + REPO + "/src",
        "--model-checker-engine", "chc",
        "--model-checker-targets", "all",
        "--model-checker-timeout", "55000",
        "--model-checker-show-unproved",
        "--allow-paths", REPO + "," + SCRATCH,
        "--base-path", REPO,
        tmp,
    ]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=REPO)
        stderr = p.stderr
        ce_idx = stderr.find("Assertion violation")
        proved_signal = (
            "CHC: 0 verification conditions remained" in stderr
            or "all checks were verified" in stderr.lower()
            or "All assertions in this contract are proved" in stderr
        )
        compile_err = "Error: " in stderr and "Compiler run failed" in stderr
        if compile_err:
            v = "COMPILE_FAIL"
        elif ce_idx >= 0:
            v = "CE_FOUND"
        elif proved_signal:
            v = "PROVED"
        else:
            v = "INCONCL"
        os.unlink(tmp)
        elapsed = round(time.time() - t0, 1)
        ce_pos = str(ce_idx) if ce_idx >= 0 else "-"
        print(
            name[:55].ljust(56),
            v.ljust(13),
            str(len(stderr)).ljust(14),
            ce_pos.ljust(10),
            str(elapsed) + "s",
        )
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(name[:55].ljust(56), "TIMEOUT".ljust(13))
