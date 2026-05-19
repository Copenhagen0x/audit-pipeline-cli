"""L3 formal-verification adapter for C — CBMC.

CBMC is a Bounded Model Checker for C/C++. Given a harness with
``__CPROVER_assume()`` (preconditions) and ``__CPROVER_assert()``
(invariants), CBMC explores all reachable program states up to a
bounded loop-unwinding depth and either:

  * Proves the invariant holds for ALL inputs (verification successful), OR
  * Returns a CONCRETE COUNTEREXAMPLE — a specific input that violates
    the assertion (constructive proof of the bug).

Built-in checks we always enable:
  * --bounds-check          (array bounds)
  * --pointer-check         (null deref, dangling)
  * --signed-overflow-check (signed integer overflow)
  * --unsigned-overflow-check
  * --conversion-check      (cast truncation)
  * --div-by-zero-check

Harness pattern the LLM writes:

    #include "src/program_a.h"

    int main(void) {
        // Symbolic input (CBMC explores ALL admitted values)
        size_t len;
        __CPROVER_assume(len <= 4096);  // bound the search

        unsigned char buf[64];
        // Fill buf symbolically...
        for (size_t i = 0; i < 64; i++) buf[i] = nondet_uchar();

        Frame f;
        int rc = parse_frame(buf, len, &f);

        // Invariant: parse must reject inputs that would overflow
        __CPROVER_assert(rc != 0 || f.length < sizeof(f.payload),
                          "parse accepted out-of-bounds length");
        return 0;
    }
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


class CFormalAdapter(LanguageFormalAdapter):
    """C formal-verification adapter (CBMC)."""

    language = "c"
    harness_file_extension = ".c"
    verifier = "cbmc"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")

        return f"""You are authoring a CBMC formal-verification harness for the Jelleo audit engine.

CBMC explores ALL admitted inputs under bounded loop unwinding and
either PROVES the assertion holds or returns a concrete COUNTEREXAMPLE.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Run command

  cbmc \\
    --unwind 16 \\
    --bounds-check --pointer-check \\
    --signed-overflow-check --unsigned-overflow-check \\
    --conversion-check --div-by-zero-check \\
    -I {target_repo_root}/src \\
    harness_<name>.c <required src/*.c files>

CBMC FIRES (= bug constructively proven) when it finds a counterexample:
  VERIFICATION FAILED

CBMC SUCCEEDS (= invariant holds within unwind bound) when it proves:
  VERIFICATION SUCCESSFUL

# Your task

Write a single self-contained C file `harness_<finding_name>.c` that:

1. `#include`s the headers needed to call `{engine_function}`.

   STATIC FUNCTIONS: if `{engine_function}` is declared `static` inside
   a program_*.c file (no header export), you MUST #include that .c
   file directly to reach it. The engine build path skips standalone
   program_*.c files (each has its own main() that would collide).
   Use this prelude BEFORE your own `main()`:

       #define main __unused_main_program_X
       #include "program_X.c"   /* relative to src/ via -I flag */
       #undef main

2. In `main()`, declares symbolic inputs (just plain locals — CBMC
   treats uninitialized values as symbolic by default for the
   --no-assertions path; for explicit nondeterminism use the helpers
   `nondet_uint()`, `nondet_size_t()`, etc).
3. Uses `__CPROVER_assume(...)` to bound the search to REALISTIC
   inputs only — without bounds, CBMC explores the entire u64 range
   and runs forever. Pick bounds that match how the function is
   called in practice AND pin any symbolic input the hypothesis says
   the bug needs (e.g. for an off-by-one bug at `len == sizeof(buf)`,
   pin `__CPROVER_assume(len == sizeof(buf))`).
4. Calls `{engine_function}` with the symbolic inputs.
5. Chooses the right ASSERTION STRATEGY for the bug class:

   * **MEMORY-SAFETY BUGS** (out-of-bounds write/read, use-after-free,
     double-free, null-deref, integer-overflow-then-allocate). DO NOT
     write your own `__CPROVER_assert`. CBMC's built-in flags catch
     these AUTOMATICALLY:
       - `--bounds-check`   → flags `buf[i] = …` when `i >= sizeof(buf)`
       - `--pointer-check`  → flags use-after-free, double-free, null-deref
       - `--signed-overflow-check` / `--unsigned-overflow-check` →
         flags integer overflow in size computations
     Just SET UP the precondition that exercises the bug and CALL the
     function. CBMC reports `VERIFICATION FAILED` with the exact line
     of the OOB write / UAF / overflow inside the engine source.
     Example (off-by-one):
         uint16_t payload_len = nondet_uint16();
         __CPROVER_assume(payload_len == sizeof(out.payload));  /* pin to the boundary case */
         (void)parse_frame(raw, raw_len, &out);  /* CBMC's --bounds-check fires inside */

   * **LOGIC / AUTHORIZATION / OUTPUT bugs** (missing privilege check,
     save/load divergence, predictable token, format-string injection,
     race condition). DO write a single `__CPROVER_assert(...)` that
     encodes the security contract the CALLER relies on. The assertion
     must be observable from the function's RETURN value or the
     function's documented side-effects — NOT a random struct field.
     Example (missing role gate — `session_check` doesn't take a role):
         int ok = session_check(&attacker_sess, attacker_sess.token, now);
         /* The caller wrongly assumes ok==1 means admin. session_check
          * doesn't check role at all → every valid non-admin session
          * returns ok==1 → assertion fails. */
         __CPROVER_assert(!ok || attacker_sess.is_admin,
                          "session_check passed for a non-admin caller");

   Choose the right strategy based on the hypothesis's `bug_class`.
   If unsure, prefer the MEMORY-SAFETY strategy — it's harder to get
   wrong because CBMC does the bug-detection.

# Important

* Loop unwinding bound is 16 by default — write the harness to
  fit within that bound or use `__CPROVER_assume(i < 16)` on loop
  counters.
* If a struct has many fields, you don't need to initialize all of
  them — CBMC treats uninitialized values as symbolic.
* Use `__CPROVER_assume(...)` on POINTER VALIDITY before
  dereferencing pointers in the harness body.
* When the hypothesis claim names a SPECIFIC boundary input that
  triggers the bug (`payload_len == 64`, `attempts >= 2`, value of
  exactly `sizeof(buf)`), PIN that value via `__CPROVER_assume(...)`.
  Otherwise CBMC may spend its unwind budget on non-boundary inputs
  and exit without exercising the bug.
* DO NOT write `__CPROVER_assert(field < SIZE, ...)` for memory-
  safety bugs — that asserts the wrong thing and CBMC will return
  "VERIFICATION SUCCESSFUL" while the OOB write still happens. The
  bug is the OOB write itself; let `--bounds-check` find it.

# Output format

Output ONLY a single ```c ... ``` fenced code block. If you can't
write a real harness, output:

  /* CANNOT_VERIFY: <one-line reason> */
  int main(void) {{ return 0; }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:c|C)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "int main" in body or "__CPROVER" in body:
                return body + "\n"
        stripped = llm_response.strip()
        if "int main" in stripped or "__CPROVER" in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a CBMC C harness from the LLM response."
        )

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "formal" / "c"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"harness_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    # Per-language unwind bound. 16 is generous for most boundary
    # checks but small for any harness that scans buffers — callers
    # can override via env CBMC_UNWIND.
    DEFAULT_UNWIND = 32

    def run_verifier(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        timeout_s: int = 600,
    ) -> FormalOutcome:
        harness_path = (
            workspace / "formal" / "c"
            / f"harness_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(
                f"CBMC harness not found at {harness_path}. Did write_harness_file run?"
            )

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_VERIFY" in body:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="CANNOT_VERIFY marker present — harness was stubbed",
                returncode=-1,
                duration_s=0.0,
                verifier=self.verifier,
                reason="harness stub (CANNOT_VERIFY)",
            )

        include_dir = target_repo_root / "src"
        # Skip .c files with their own top-level int main() — otherwise
        # linking multi-program OSec eval targets explodes with "multiple
        # definition of main". The harness owns main(); the LLM must
        # #include the program file directly to call its static funcs.
        _MAIN_RE_C = re.compile(r"^\s*(?:int|void|static\s+int)\s+main\s*\(", re.M)
        src_files = []
        if include_dir.is_dir():
            for p in include_dir.rglob("*.c"):
                if "vendor" in p.parts or "tests" in p.parts:
                    continue
                try:
                    if _MAIN_RE_C.search(p.read_text(encoding="utf-8", errors="replace")):
                        continue
                except OSError:
                    pass
                src_files.append(str(p))

        # Configurable unwind via CBMC_UNWIND env (default 32). Add
        # --unwinding-assertions so unwind-bound exceeded becomes a
        # counterexample (not silent unsoundness). Without this flag
        # CBMC silently truncates loops it can't unroll and reports
        # VERIFICATION SUCCESSFUL even when assertions WOULD fire
        # past the bound.
        try:
            unwind = int(os.environ.get("CBMC_UNWIND", str(self.DEFAULT_UNWIND)))
        except ValueError:
            unwind = self.DEFAULT_UNWIND
        cmd = [
            "cbmc",
            "--unwind", str(unwind),
            "--unwinding-assertions",
            "--bounds-check", "--pointer-check",
            "--signed-overflow-check", "--unsigned-overflow-check",
            "--conversion-check", "--div-by-zero-check",
            f"-I{include_dir}",
            str(harness_path),
            *src_files,
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except FileNotFoundError:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="cbmc not installed — `apt install cbmc` or build from source",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: cbmc",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="cbmc timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="cbmc timeout (increase --unwind or simplify harness)",
            )

        duration = time.time() - t0
        # 2026-05-18: parse the FULL stdout for the terminal verdict
        # line before truncating. CBMC's "VERIFICATION FAILED" /
        # "VERIFICATION SUCCESSFUL" is the LAST line; truncating to
        # the first 8000 chars buries the verdict for any non-trivial
        # harness (typical c-small CSMALL01 output is ~40k chars
        # because every memory-safety check on every dereference site
        # gets its own SUCCESS/FAILURE line). Previously every C cycle
        # silently came back "neither proved nor produced counter-
        # example" — the engine never saw the verdict line.
        full_stdout = proc.stdout
        stdout = full_stdout[:8000]
        # Append the tail of full_stdout if the verdict line lives past
        # the 8000-char window, so the verdict + the last FAILURE
        # report survive into the stored stdout for downstream readers.
        if ("VERIFICATION FAILED" in full_stdout
                or "VERIFICATION SUCCESSFUL" in full_stdout) \
                and "VERIFICATION" not in stdout:
            stdout = stdout + "\n...[truncated]...\n" + full_stdout[-4000:]
        stderr = proc.stderr[:4000]

        # CBMC reports outcomes via these terminal lines (read full output)
        if "VERIFICATION FAILED" in full_stdout:
            # Counterexample found — bug constructively proven
            assertion_line = "(no specific assertion line found)"
            m = re.search(r"\[.+?\]\s+(.+?):\s+FAILURE", full_stdout)
            if m:
                assertion_line = m.group(0)[:200]
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"CBMC found counterexample: {assertion_line}",
                metadata={"assertion_failed": assertion_line},
            )
        if "VERIFICATION SUCCESSFUL" in full_stdout:
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="CBMC proved invariant holds under unwind bound",
            )

        return FormalOutcome(
            proved=False,
            counterexample=False,
            harness_path=harness_path,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            duration_s=duration,
            verifier=self.verifier,
            reason="CBMC neither proved nor produced counterexample (likely compile error or unwind-bound exceeded)",
        )
