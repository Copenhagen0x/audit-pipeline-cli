"""Layer-2 PoC adapter for C source repos.

Test framework: standalone C test program compiled with clang +
AddressSanitizer + UndefinedBehaviorSanitizer. The "fired" signal is:

  * Non-zero exit code with a sanitizer report in stderr (ASan,
    UBSan), OR
  * Explicit ``assert`` failure inside the test, OR
  * Segfault / abort / signal-induced exit

ASan catches buffer overflows, UAF, double-free, stack-buffer-overflow,
heap-use-after-free, etc. UBSan catches signed integer overflow,
null-pointer deref, divide-by-zero, alignment violations. Combined,
they cover the bulk of the C bug catalogue in our hypothesis library.

The LLM author is asked to:
  1. Include the headers from src/ that the bug requires
  2. Set up the witness state (the inputs that trigger the bug)
  3. Call the function under test
  4. Either:
       (a) explicitly ``assert`` an invariant that should hold but won't, OR
       (b) let the sanitizer detect the violation automatically

If the program runs to ``return 0`` cleanly with no sanitizer hits and
no assertion failure, the bug isn't reachable from the witness state =
PocOutcome.fired = False.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.poc_adapters.base import LanguagePocAdapter, PocOutcome

# Sanitizer kinds we look for in stderr — used to populate
# PocOutcome.metadata['sanitizer'] so L2.5 auto-judge knows what kind
# of fault was caught.
_SANITIZER_PATTERNS = (
    (r"AddressSanitizer:\s*([a-z\-]+)",                "asan"),
    (r"UndefinedBehaviorSanitizer:\s*([a-z\-]+)",      "ubsan"),
    (r"runtime error:\s*(.+)",                          "ubsan"),
    (r"==\d+==ERROR:\s+LeakSanitizer",                 "lsan"),
    (r"Segmentation fault",                            "segfault"),
    (r"\babort\b",                                     "abort"),
)


# Markers that mean the test didn't actually exercise the bug — same
# pseudo-pass list the Solana post-cycle gate already uses. We block
# these at PoC write time so a "I can't test this" stub never counts
# as a passed/fired result.
#
# NOTE: we intentionally DO NOT match bare "TODO" or "FIXME" — many
# real C tests have a `// TODO: replace with stronger oracle once X`
# comment for follow-up work and are still exercising the bug. We
# only block markers that signal "the author gave up authoring this
# test." See Phase 1d audit finding C-2.
_PSEUDO_PASS_MARKERS = (
    "CANNOT_TEST",
    "/* placeholder",
    "// placeholder",
)


class CAdapter(LanguagePocAdapter):
    """C PoC adapter (clang + ASan/UBSan)."""

    language = "c"
    test_file_extension = ".c"
    framework = "clang+sanitizers"

    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        return f"""You are authoring a Layer-2 Proof-of-Concept C test for the Jelleo audit engine.

Your test will be compiled with:

  clang -g -O0 \\
        -fsanitize=address,undefined,signed-integer-overflow \\
        -fno-omit-frame-pointer \\
        -I {target_repo_root}/src \\
        -o /tmp/poc test_<name>.c <required .c files from src/>
  /tmp/poc

The test FIRES (= bug confirmed) when ANY of these happen:
  * AddressSanitizer reports buffer-overflow / UAF / double-free / etc
  * UndefinedBehaviorSanitizer reports signed-integer-overflow / null
    pointer deref / divide-by-zero / shift-out-of-bounds / etc
  * An explicit `assert(...)` fails
  * The program crashes (segfault / abort / SIGBUS)

The test PASSES (= bug NOT reachable from your witness state) when the
program runs to a clean `return 0` with no sanitizer report.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Target file: {target_file}
Engine function: {engine_function}
Relevant instructions: {relevant}

# Grounded source

{source_context}

# Your task

Write a single self-contained C file `test_<finding_name>.c` that:

1. `#include`s the headers needed to call `{engine_function}` and
   construct the witness state. Use the actual paths from src/
   (e.g. `#include "auth/session.h"`).
2. In `main()`, sets up the EXACT witness state from the hypothesis
   (attacker-controlled length, off-by-one boundary, malformed frame,
   etc). Use realistic values that drive the bug — not random ones.
3. Calls `{engine_function}` with the witness inputs.
4. Either:
     (a) Explicitly asserts an invariant that SHOULD hold but won't
         given the bug (e.g. `assert(parsed.length <= sizeof(buffer));`).
     (b) Lets the sanitizer detect the violation automatically (preferred
         for memory-safety bugs — write the boundary case and let ASan
         catch the OOB).
5. Returns 0 cleanly if the bug is NOT reachable. NEVER use a sanitizer
   suppression file, NEVER comment out the bug-triggering line.

# Output format

Output ONLY a single ```c ... ``` fenced code block containing the
complete test. Do not output any prose, explanation, or markdown
outside the fenced block.

The file MUST be syntactically valid C99/C11 and MUST compile cleanly
with the command line above. If you can't write a real PoC (e.g. the
hypothesis is wrong, the bug isn't reachable, or you don't have enough
information), output a single C file containing:

  /* CANNOT_TEST: <one-line reason> */
  int main(void) {{ return 0; }}

The `CANNOT_TEST:` marker is recognized by the post-cycle gate as a
non-fire — it doesn't count as a passed test. Don't use it lightly.
"""

    def parse_test_body(self, llm_response: str) -> str:
        """Extract the ```c ... ``` fenced code block.

        Falls back to the raw response if no fence is found AND the
        response starts with a preprocessor directive or a function
        definition. Raises ValueError if extraction fails entirely.
        """
        # Primary: fenced block with `c` language tag
        m = re.search(r"```(?:c|C)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        # Secondary: any fenced block
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if body.startswith("#") or "int main" in body:
                return body + "\n"
        # Tertiary: no fences but looks like C source
        stripped = llm_response.strip()
        if stripped.startswith("#") or "int main" in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a C source block from the LLM response. "
            "Expected ```c ... ``` fenced code block."
        )

    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "tests" / "c"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"test_{test_name}{self.test_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 180,
    ) -> PocOutcome:
        """Compile with clang + sanitizers, then run. Return outcome.

        Compile and run separately so we distinguish:
          * Compile failure  (PocOutcome.fired = False, returncode != 0)
          * Sanitizer hit    (PocOutcome.fired = True,  returncode != 0)
          * Assert failure   (PocOutcome.fired = True,  returncode != 0)
          * Clean pass       (PocOutcome.fired = False, returncode == 0)
        """
        test_path = (
            workspace / "tests" / "c" / f"test_{test_name}{self.test_file_extension}"
        )
        if not test_path.is_file():
            raise FileNotFoundError(
                f"PoC test file not found at {test_path}. Did write_test_file run?"
            )

        # Pseudo-pass detection BEFORE we even compile — saves toolchain time
        body = test_path.read_text(encoding="utf-8", errors="replace")
        for marker in _PSEUDO_PASS_MARKERS:
            if marker in body:
                return PocOutcome(
                    fired=False,
                    test_path=test_path,
                    stdout="",
                    stderr=f"pseudo-pass marker {marker!r} present — refusing to count this as a real test",
                    returncode=-1,
                    duration_s=0.0,
                    framework=self.framework,
                    reason=f"pseudo-pass: contains {marker!r}",
                    metadata={"pseudo_pass": True, "marker": marker},
                )

        # Resolve include path: src/ inside the target repo
        include_dir = target_repo_root / "src"
        if not include_dir.is_dir():
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr=f"src/ not found under {target_repo_root}",
                returncode=-2,
                duration_s=0.0,
                framework=self.framework,
                reason="target repo missing src/ subdir",
            )

        # Discover .c files in src/ (excluding tests/ and vendor/ for cleanliness)
        src_files = [
            str(p) for p in include_dir.rglob("*.c")
            if "vendor" not in p.parts and "tests" not in p.parts
        ]

        # Compile to a uniquely-named binary so concurrent tests don't collide
        bin_path = workspace / "tests" / "c" / f"bin_{test_name}"
        compile_cmd = [
            "clang",
            "-g", "-O0",
            "-fsanitize=address,undefined,signed-integer-overflow",
            "-fno-omit-frame-pointer",
            f"-I{include_dir}",
            str(test_path),
            *src_files,
            "-o", str(bin_path),
        ]

        t0 = time.time()
        try:
            compile_proc = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="clang not installed — install via `apt install clang` or equivalent",
                returncode=-3,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="toolchain missing: clang",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="compile timed out",
                returncode=-4,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="compile timeout",
            )

        if compile_proc.returncode != 0:
            # Compile failure — not a fire signal, just a broken PoC
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=compile_proc.stdout[:4000],
                stderr=compile_proc.stderr[:4000],
                returncode=compile_proc.returncode,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="compile failed — PoC source has errors",
                metadata={"phase": "compile"},
            )

        # Compile OK; run the binary.
        #
        # Merge ASAN/UBSAN tuning options ON TOP of the parent env so we
        # don't strip the user's PATH / LD_LIBRARY_PATH / SHELL etc —
        # which broke ASan's symbolizer on systems where llvm-symbolizer
        # lives outside /usr/bin (e.g. /opt/homebrew/opt/llvm/bin).
        run_env = {**os.environ}
        run_env.update({
            # Disable leak-detection by default to keep noise down;
            # ASan's other detectors (heap-buffer-overflow, etc) stay on.
            "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=42",
            "UBSAN_OPTIONS": "print_stacktrace=1:abort_on_error=0",
        })
        try:
            run_proc = subprocess.run(
                [str(bin_path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="run timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="run timeout",
            )

        duration = time.time() - t0
        stdout = run_proc.stdout[:4000]
        stderr = run_proc.stderr[:4000]

        # Fire detection: scan stderr for sanitizer reports + check exit code
        sanitizer_hit = None
        sanitizer_what = None
        for pat, kind in _SANITIZER_PATTERNS:
            m = re.search(pat, stderr, re.IGNORECASE)
            if m:
                sanitizer_hit = kind
                sanitizer_what = m.group(1) if m.groups() else m.group(0)
                break

        # Also detect an explicit assert failure (assertion failed: ...)
        assert_failure = (
            "Assertion " in stderr
            or "assertion failed" in stderr.lower()
            or "Assertion failed" in stdout
        )

        if sanitizer_hit:
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=f"{sanitizer_hit} caught: {sanitizer_what[:100]}",
                metadata={"sanitizer": sanitizer_hit, "what": sanitizer_what[:200]},
            )
        if assert_failure:
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason="assertion failed",
                metadata={"sanitizer": None, "what": "assert"},
            )
        if run_proc.returncode != 0:
            # Crash without an explicit sanitizer hit — segfault counts as fire
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=f"non-zero exit {run_proc.returncode} (likely crash)",
                metadata={"sanitizer": "unknown", "what": "crash"},
            )

        # Ran clean — bug not reachable from this witness state
        return PocOutcome(
            fired=False,
            test_path=test_path,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            duration_s=duration,
            framework=self.framework,
            reason="ran clean, no sanitizer/assert hit — bug not reachable from witness state",
        )
