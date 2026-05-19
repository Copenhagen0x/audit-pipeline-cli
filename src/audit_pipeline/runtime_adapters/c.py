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


def build_source_tree_map(target_repo_root: Path) -> str:
    """Walk ``<repo>/src/`` and return a tree of include-relative paths.

    The LLM author needs the actual file layout to write correct
    ``#include`` directives. Without this, the model defaults to
    plausible-but-wrong paths like ``utils/buffer.h`` or
    ``collections/map.c``. Walking src/ once at prompt-build time
    eliminates that whole class of grounding error.
    """
    src_dir = target_repo_root / "src"
    if not src_dir.is_dir():
        return "(no src/ subdirectory found under target repo root)"
    headers: list[str] = []
    sources: list[str] = []
    for p in sorted(src_dir.rglob("*.h")):
        try:
            headers.append(str(p.relative_to(src_dir)).replace("\\", "/"))
        except ValueError:
            continue
    for p in sorted(src_dir.rglob("*.c")):
        try:
            sources.append(str(p.relative_to(src_dir)).replace("\\", "/"))
        except ValueError:
            continue
    lines = ['Header files (.h) — usable in `#include "..."`:']
    for h in headers:
        lines.append(f"  {h}")
    lines.append("")
    lines.append("Source files (.c) — auto-compiled + linked into the binary by the engine build path. You do NOT need to #include these unless the bug is in a static function.")
    for c in sources:
        lines.append(f"  {c}")
    return "\n".join(lines)


def build_c_api_index(target_repo_root: Path) -> str:
    """Extract function declarations, struct fields, and enum values from
    every ``.h`` file under ``<repo>/src/`` and return them as a compact
    API reference.

    Mirror of the Move sig-index from
    ``engine_signature_grounding_2026_05_15.md`` — without this, the L2
    PoC author hallucinates plausible-but-wrong API symbols like
    ``buffer_new()`` (real: ``buffer_init(Buffer*, size_t)``) or
    ``JOB_KIND_TRANSFER`` (real enum values: ``JOB_NONE``,
    ``JOB_EMAIL``, etc.).

    Parses each .h via permissive regex — no Tree-sitter, no full C
    parser. Catches the 95% case: extern function declarations, struct
    definitions, typedef enum bodies. Macros, function pointers, and
    deeply nested constructs may be missed; the prompt instructs the
    LLM to fall back to reading the .h directly if a symbol it needs
    isn't in the index.
    """
    src_dir = target_repo_root / "src"
    if not src_dir.is_dir():
        return "(no src/ subdirectory found — API index unavailable)"

    # Strip C block + line comments so they don't confuse the regexes.
    block_re = re.compile(r"/\*.*?\*/", re.DOTALL)
    line_re = re.compile(r"//[^\n]*")

    # Function declarations: `<type-spec> <name>(<args>);` at file scope.
    # Type-spec can be multi-word (e.g. `static int`, `const char *`,
    # `unsigned long long`). We require the trailing `;` and ban `{` to
    # exclude definitions and stray code.
    fn_re = re.compile(
        r"^\s*"
        r"(?P<ret>(?:static\s+|extern\s+|inline\s+|const\s+|unsigned\s+|signed\s+|long\s+|short\s+|struct\s+\w+\s*\*?\s*|enum\s+\w+\s*|"
        r"[A-Za-z_]\w*\s*\*?\s*)+)"
        r"(?P<name>[A-Za-z_]\w*)\s*"
        r"\((?P<args>[^;{}]*)\)\s*;",
        re.M,
    )

    # Struct typedefs: `typedef struct Name { ... } Alias;` OR `struct Name { ... };`
    struct_re = re.compile(
        r"(?:typedef\s+)?struct\s+(?P<name>\w+)\s*\{(?P<body>[^}]*)\}\s*(?P<alias>\w*)\s*;",
        re.DOTALL,
    )

    # Enum typedefs: `typedef enum Name { V1, V2 = 3, ... } Alias;`
    enum_re = re.compile(
        r"(?:typedef\s+)?enum\s+(?P<name>\w+)?\s*\{(?P<body>[^}]*)\}\s*(?P<alias>\w*)\s*;",
        re.DOTALL,
    )

    sections: list[str] = []
    for h_path in sorted(src_dir.rglob("*.h")):
        try:
            text = h_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Strip comments before regex matching
        text = block_re.sub(" ", text)
        text = line_re.sub(" ", text)

        rel = str(h_path.relative_to(src_dir)).replace("\\", "/")
        block_lines: list[str] = []

        # Structs
        for m in struct_re.finditer(text):
            name = m.group("name") or "(anon)"
            alias = m.group("alias") or ""
            label = name if not alias or alias == name else f"{name} (typedef {alias})"
            # Compress whitespace in body; keep field list as a one-liner.
            body = re.sub(r"\s+", " ", m.group("body").strip())
            block_lines.append(f"  struct {label} {{ {body} }};")

        # Enums
        for m in enum_re.finditer(text):
            name = m.group("name") or "(anon)"
            alias = m.group("alias") or ""
            label = name if not alias or alias == name else f"{name} (typedef {alias})"
            body = re.sub(r"\s+", " ", m.group("body").strip())
            block_lines.append(f"  enum {label} {{ {body} }};")

        # Function declarations
        for m in fn_re.finditer(text):
            ret = re.sub(r"\s+", " ", m.group("ret").strip())
            name = m.group("name")
            args = re.sub(r"\s+", " ", m.group("args").strip())
            # Skip noise: declarations whose "return type" is actually a
            # control-flow keyword or a local identifier we mistakenly
            # captured (e.g. `if (cond)` somehow leaking through).
            if name in {"if", "while", "for", "switch", "return", "sizeof",
                        "do", "else", "static", "extern", "const", "struct",
                        "enum", "union", "typedef", "void"}:
                continue
            block_lines.append(f"  {ret} {name}({args});")

        if block_lines:
            sections.append(f"// {rel}")
            sections.extend(block_lines)
            sections.append("")

    if not sections:
        return "(no API symbols extracted from src/*.h)"
    return "\n".join(sections).rstrip()

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

        source_tree_map = build_source_tree_map(target_repo_root)
        c_api_index = build_c_api_index(target_repo_root)
        return f"""You are authoring an AFL++ fuzz harness for the Jelleo audit engine.

The harness will be compiled with `afl-clang-fast` and ASan/UBSan/
SignedOverflowSan, then run with `afl-fuzz` for a bounded time
budget. AFL++ mutates stdin, tracks coverage, and saves to disk any
input that causes the harness to ABORT — including ASan/UBSan
sanitizer-aborts and explicit `abort()` calls.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}

# Grounded source

{source_context}

# Choosing the right harness shape — read carefully

A "crash" in AFL terminology is ANY non-zero abnormal termination
(SIGSEGV, SIGABRT, sanitizer abort). The harness's job is to feed the
function under test attacker-shaped inputs until ONE such termination
fires. There are TWO valid shapes — pick the one that matches the bug
class:

  * **MEMORY-SAFETY HARNESS** (out-of-bounds write/read, use-after-
    free, double-free, format-string injection, null-deref, integer-
    overflow-then-allocate). The sanitizers do the bug-detection;
    YOUR harness just needs to DRIVE the function with attacker-
    controlled bytes. Do NOT add a custom `abort()` — ASan/UBSan
    aborts the process automatically when the bug fires, AFL records
    the crash, you're done. Example for an off-by-one in parse_frame:

        unsigned char buf[4096];
        ssize_t n = read(0, buf, sizeof(buf));
        if (n < 3) return 0;
        Frame f;
        (void)parse_frame(buf, (size_t)n, &f);   /* ASan catches OOB inside */
        return 0;

  * **LOGIC-INVARIANT HARNESS** (missing privilege check, save/load
    divergence, predictable output, broken role gate). The sanitizers
    will NOT catch these because they're not memory bugs. YOUR harness
    drives the function and then explicitly checks the caller-visible
    security contract; on violation, call `abort()` so AFL records
    that input as a crash. The aborted predicate must encode the
    function's RETURN value or DOCUMENTED OUTPUT — NOT a random
    internal struct field. Example for a missing-role-gate bug:

        Session s;
        unsigned char buf[32];
        ssize_t n = read(0, buf, sizeof(buf));
        if (n < 8) return 0;
        uint64_t tok = *(uint64_t *)buf;
        session_create(&s, "attacker", tok, 3600);
        int ok = session_check(&s, s.token, time(NULL));
        /* Caller assumes ok==1 implies admin. session_check doesn't
         * check role, so a non-admin session also returns ok==1 — that's
         * the bug. abort() lets AFL log this as a crash. */
        if (ok && !s.is_admin) abort();

If unsure which shape to pick: prefer MEMORY-SAFETY HARNESS — it's
harder to get wrong because the sanitizers do the work.

# Source tree map — ACTUAL paths in this target

These are the ONLY valid `#include` paths in this target. **Do NOT use plausible-but-wrong paths like `utils/buffer.h`, `util/buffer.h`, `collections/map.h`, `lib/foo.h`, `includes/bar.h`.** Use ONLY paths that appear below.

{source_tree_map}

# C API index — ACTUAL function signatures + struct fields + enum values

Auto-extracted from every `.h` in `src/`. **Use ONLY symbols that
appear below.** Do NOT invent function names (e.g. `buffer_new()` —
not real; use the actual `buffer_init`). Do NOT invent enum values
(e.g. `JOB_KIND_TRANSFER` — not real; use the actual `JOB_EMAIL`).
Match argument counts EXACTLY — passing 1 arg to a 2-arg function
will fail at compile time.

{c_api_index}

# Your task

Write `afl_<finding_name>.c` that:

1. Reads input from stdin into a buffer with `read(0, buf, ...)`.
   AFL feeds the harness via stdin by default; the harness must
   exit cleanly on inputs too short to construct the function args
   so AFL can mutate quickly.

   STATIC FUNCTIONS: if the engine_function is declared `static` inside
   a program_*.c file (no header export), you MUST #include that .c
   file directly to reach it. The engine build path skips standalone
   program_*.c files (each has its own main() that would collide).
   Use this prelude BEFORE your own main():

       #define main __unused_main_program_X
       #include "program_X.c"
       #undef main

2. Decodes the input bytes into the function's argument types
   (lengths, pointers, structs). Keep the decoder simple — AFL's
   coverage feedback works best when the input-to-args mapping is
   short and deterministic.
3. Calls the function. Picks the right HARNESS SHAPE above:
     - Memory-safety bug → no custom abort, let ASan fire.
     - Logic-invariant bug → check the caller-visible contract and
       `abort()` on violation.
4. Exit cleanly on inputs that don't satisfy the function's
   preconditions (return 0). Speed matters — AFL prefers fast
   harnesses so it can throw more mutations per second.

# Important

* DO NOT write `if (f.field > SIZE) abort()` for memory-safety bugs.
  That checks an unrelated field, the actual OOB/UAF fires un-
  detected, and AFL reports 0 crashes after the full time budget.
  Let the sanitizers detect memory bugs; only use `abort()` for
  logic invariants the sanitizers can't see.
* Pin any "interesting" input position the hypothesis names (e.g.,
  if the bug needs the length byte to equal 64, you can either let
  AFL find that input naturally or seed the corpus — but do NOT hard-
  code the value in the harness; AFL needs to be able to mutate it).

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
        # Skip .c files with their own top-level int main() — otherwise
        # linking multi-program OSec eval targets explodes with "multiple
        # definition of main". The harness owns main(); the LLM must
        # #include the program file directly to call its static funcs.
        _MAIN_RE_C = re.compile(r"^\s*(?:int|void|static\s+int)\s+main\s*\(", re.M)
        src_files = []
        if include_dir.is_dir():
            for p in include_dir.rglob("*.c"):
                if "tests" in p.parts:  # vendor/ INCLUDED: c-medium has real vendored .c sources that engine sources call into
                    continue
                try:
                    if _MAIN_RE_C.search(p.read_text(encoding="utf-8", errors="replace")):
                        continue
                except OSError:
                    pass
                src_files.append(str(p))
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
