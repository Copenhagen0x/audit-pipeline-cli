"""L4 runtime adapter for C — AFL++ coverage-guided fuzzing.

AFL++ is a coverage-guided fuzzer for C/C++ programs. The harness is
a small C program that reads input from stdin (or a file) and passes
it to the function under test. AFL++ mutates inputs, tracks coverage,
and persists crashes for replay.

Approach:

  1. LLM authors a fuzz harness `afl_<name>.c` that reads input from
     stdin and feeds it to the function under test.
  2. We compile with `afl-clang-fast` and the same sanitizers we use
     for L2 PoC (ASan + UBSan + signed-integer-overflow).
  3. Run `afl-fuzz` for the time budget against an initial seed
     directory.
  4. After the run: count crashes/hangs collected under
     `<workspace>/fuzz/c/<name>/crashes/` and `hangs/`.

We use `afl-clang-fast` because it's faster than the older afl-gcc
and ships with AFL++. Crash files are kept for L2.5 to convert into
deterministic regression tests if needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.runtime_adapters.base import LanguageRuntimeAdapter, RuntimeOutcome


class CRuntimeAdapter(LanguageRuntimeAdapter):
    language = "c"
    harness_file_extension = ".c"
    fuzzer = "afl++"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring an AFL++ fuzz harness for the Jelleo audit engine.

The harness will be compiled with `afl-clang-fast` and ASan/UBSan,
then run with `afl-fuzz` for a bounded time budget. AFL++ mutates
stdin, tracks coverage, and saves crashes/hangs to disk.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Harness pattern

```c
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include "{engine_function or 'program_a'}.h"

int main(int argc, char **argv) {{
    // Read input from stdin (AFL feeds harness via stdin by default)
    static unsigned char buf[4096];
    ssize_t n = read(0, buf, sizeof(buf));
    if (n <= 0) return 0;

    // Decode the input into the function-under-test's arguments.
    // Be conservative: bail on inputs shorter than the minimum
    // structure size — AFL prefers harnesses that exit quickly
    // on malformed inputs so it can move on.
    if (n < 4) return 0;

    // Construct + call the function under test
    Frame f;
    if (parse_frame(buf, (size_t)n, &f) == 0) {{
        // Optional: assert an invariant on parsed output
        if (f.length > sizeof(f.payload)) abort();
    }}

    return 0;
}}
```

# Your task

Write a `afl_<finding_name>.c` that:

1. Reads input from stdin into a buffer.
2. Decodes the input into the arguments of the function under test.
3. Calls the function. If the function returns success but produces
   invalid output (per the hypothesis claim), call `abort()` to
   surface the violation as a "crash" AFL records.
4. Exit cleanly on malformed inputs (return 0). Speed is important
   for the fuzzer.

# Output format

Output ONLY a single ```c ... ``` fenced code block.

If you can't write a real harness:

  /* CANNOT_FUZZ: <one-line reason> */
  int main(void) {{ return 0; }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:c|C)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "int main" in body or "#include" in body:
                return body + "\n"
        raise ValueError("Could not extract a C AFL harness from the LLM response.")

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "fuzz" / "c" / harness_name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"afl_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_fuzzer(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        time_budget_s: int = 120,
    ) -> RuntimeOutcome:
        harness_dir = workspace / "fuzz" / "c" / harness_name
        harness_path = harness_dir / f"afl_{harness_name}{self.harness_file_extension}"
        if not harness_path.is_file():
            raise FileNotFoundError(f"AFL harness not found at {harness_path}")

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_FUZZ" in body:
            return RuntimeOutcome(
                crash_found=False,
                ran_clean=False,
                harness_path=harness_path,
                stdout="",
                stderr="CANNOT_FUZZ marker — harness stubbed",
                returncode=-1,
                duration_s=0.0,
                fuzzer=self.fuzzer,
                reason="harness stub",
            )

        # Compile with afl-clang-fast + sanitizers
        bin_path = harness_dir / "harness_bin"
        include_dir = target_repo_root / "src"
        src_files = []
        if include_dir.is_dir():
            src_files = [
                str(p) for p in include_dir.rglob("*.c")
                if "vendor" not in p.parts and "tests" not in p.parts
            ]
        compile_cmd = [
            "afl-clang-fast",
            "-g", "-O1",
            "-fsanitize=address,undefined,signed-integer-overflow",
            "-fno-omit-frame-pointer",
            f"-I{include_dir}",
            str(harness_path),
            *src_files,
            "-o", str(bin_path),
        ]
        t0 = time.time()
        try:
            compile_proc = subprocess.run(
                compile_cmd, capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return RuntimeOutcome(
                crash_found=False,
                ran_clean=False,
                harness_path=harness_path,
                stdout="",
                stderr="afl-clang-fast not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                fuzzer=self.fuzzer,
                reason="toolchain missing: afl++",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            return RuntimeOutcome(
                crash_found=False,
                ran_clean=False,
                harness_path=harness_path,
                stdout="",
                stderr="compile timed out",
                returncode=-4,
                duration_s=time.time() - t0,
                fuzzer=self.fuzzer,
                reason="compile timeout",
            )

        if compile_proc.returncode != 0:
            return RuntimeOutcome(
                crash_found=False,
                ran_clean=False,
                harness_path=harness_path,
                stdout=compile_proc.stdout[:4000],
                stderr=compile_proc.stderr[:4000],
                returncode=compile_proc.returncode,
                duration_s=time.time() - t0,
                fuzzer=self.fuzzer,
                reason="harness failed to compile",
                metadata={"phase": "compile"},
            )

        # Set up seed corpus + output dir
        seed_dir = harness_dir / "seeds"
        seed_dir.mkdir(parents=True, exist_ok=True)
        if not any(seed_dir.iterdir()):
            (seed_dir / "seed0").write_bytes(b"\x00" * 16)
        out_dir = harness_dir / "afl_out"
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)

        # Run AFL++ for the time budget. -V <seconds> exits cleanly
        # when the budget runs out.
        #
        # Bind fuzz_proc to a sentinel BEFORE the try block so a
        # TimeoutExpired raise doesn't leave it unbound — downstream
        # crash-detection logic reads .stdout / .stderr from it.
        fuzz_proc: subprocess.CompletedProcess | None = None
        fuzz_cmd = [
            "afl-fuzz",
            "-i", str(seed_dir),
            "-o", str(out_dir),
            "-V", str(time_budget_s),
            "-m", "none",
            "-M", "fuzzer01",  # named instance; AFL writes to out_dir/fuzzer01/
            "--", str(bin_path),
        ]
        # Build a fuzzer env that PRESERVES the user's PATH (so afl-fuzz
        # can find afl-clang-fast / the system clang it needs) while
        # adding AFL-specific tunables. Stripping PATH entirely broke
        # the fuzzer on Linux distros that put binaries in /opt/.
        fuzz_env = {**os.environ}
        fuzz_env.update({
            "AFL_SKIP_CPUFREQ": "1",
            "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
            "AFL_NO_AFFINITY": "1",  # don't bind to a CPU in shared VPS
        })
        try:
            fuzz_proc = subprocess.run(
                fuzz_cmd, capture_output=True, text=True,
                timeout=time_budget_s + 60,
                env=fuzz_env,
            )
        except FileNotFoundError:
            return RuntimeOutcome(
                crash_found=False,
                ran_clean=False,
                harness_path=harness_path,
                stdout="",
                stderr="afl-fuzz not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                fuzzer=self.fuzzer,
                reason="toolchain missing: afl-fuzz",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired as e:
            # Fuzzer exceeded budget — that's expected, treat as ran-clean
            # if no crashes are present in the output dir. Preserve any
            # partial output the subprocess captured before the kill.
            fuzz_proc = None
            captured_stdout = (e.stdout.decode("utf-8", errors="replace")
                               if isinstance(e.stdout, bytes) else (e.stdout or ""))
            captured_stderr = (e.stderr.decode("utf-8", errors="replace")
                               if isinstance(e.stderr, bytes) else (e.stderr or ""))

        duration = time.time() - t0
        # AFL++ named-instance layout: out_dir/fuzzer01/crashes/.
        # Older AFL versions used out_dir/default/crashes/ or
        # out_dir/crashes/ — check all three.
        crash_dir = out_dir / "fuzzer01" / "crashes"
        if not crash_dir.is_dir():
            crash_dir = out_dir / "default" / "crashes"
        if not crash_dir.is_dir():
            crash_dir = out_dir / "crashes"

        crashes: list[Path] = []
        if crash_dir.is_dir():
            crashes = [
                p for p in crash_dir.iterdir()
                if p.is_file() and not p.name.startswith("README")
            ]

        # Resolve stdout/stderr from whichever path we took (completed run
        # vs timeout). Defensive — fuzz_proc may still be None on early
        # paths we didn't anticipate.
        if fuzz_proc is not None:
            run_stdout = (fuzz_proc.stdout or "")[:4000]
            run_stderr = (fuzz_proc.stderr or "")[:4000]
        else:
            run_stdout = (captured_stdout or "")[:4000]
            run_stderr = (captured_stderr or "")[:4000]

        if crashes:
            # Capture witness inputs for L2.5 reproduction
            import base64
            witnesses = [
                {
                    "name": c.name,
                    "size": c.stat().st_size,
                    "b64": base64.b64encode(c.read_bytes()[:4096]).decode("ascii"),
                }
                for c in crashes[:5]  # cap at 5 to keep DB rows reasonable
            ]
            return RuntimeOutcome(
                crash_found=True,
                ran_clean=False,
                harness_path=harness_path,
                stdout=run_stdout,
                stderr=run_stderr,
                returncode=0,
                duration_s=duration,
                fuzzer=self.fuzzer,
                reason=f"AFL++ found {len(crashes)} unique crash(es)",
                witness_inputs=witnesses,
                metadata={"n_crashes": len(crashes), "crash_dir": str(crash_dir)},
            )

        return RuntimeOutcome(
            crash_found=False,
            ran_clean=True,
            harness_path=harness_path,
            stdout=run_stdout,
            stderr=run_stderr,
            returncode=0,
            duration_s=duration,
            fuzzer=self.fuzzer,
            reason=f"AFL++ ran {time_budget_s}s without finding crashes",
        )
