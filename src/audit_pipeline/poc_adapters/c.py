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


def build_source_tree_map(target_repo_root: Path) -> str:
    """Walk ``<repo>/src/`` and return a tree of include-relative paths.

    The LLM PoC / harness author needs the actual file layout to write
    correct ``#include`` directives. Without this, the model defaults to
    plausible-but-wrong paths like ``utils/buffer.h`` or
    ``collections/map.c`` when the real paths are ``common/buffer.h`` and
    ``vendor/minihash/map.c``. Walking src/ once at prompt-build time
    eliminates that whole class of grounding error.

    Returns a string like:
        Header files (.h) — usable in ``#include "..."``:
          auth/session.h
          common/buffer.h
          ...
        Source files (.c) — auto-compiled + linked into the test binary:
          auth/session.c
          common/buffer.c
          vendor/minihash/map.c
          ...

    On a missing src/, returns an empty advisory string (caller can
    decide how to surface that — most likely the target_repo_root itself
    is misconfigured).
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
    lines.append("Source files (.c) — auto-compiled + linked into the test binary by the engine build path. You do NOT need to #include these unless the bug is in a static function.")
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
    # `unsigned long long`). Trailing `;` required; `{` banned to exclude
    # definitions and stray code.
    #
    # CRITICAL: every return-type alternative ends in `\s+` (REQUIRED
    # whitespace) NOT `\s*` (optional). Without this, the greedy regex
    # eats all-but-one character of the function name into the return
    # type — `int session_store_init(...)` would parse as return type
    # `int session_store_ini`, name `t`. The `\s+` forces a separator
    # between the last return-type word and the function name.
    #
    # The trailing `|\*\s*` alternative lets the engine consume `*`
    # tokens between the type and the name (e.g. `Buffer *buffer_init`,
    # `char **strv_dup`).
    fn_re = re.compile(
        r"^\s*"
        r"(?P<ret>(?:static\s+|extern\s+|inline\s+|const\s+|unsigned\s+|signed\s+|long\s+|short\s+|"
        r"struct\s+\w+\s*\*?\s+|enum\s+\w+\s+|"
        r"[A-Za-z_]\w*\s*\*?\s+|\*\s*)+)"
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
        debate_context: str | None = None,  # accepted for interface parity; C baseline ignores
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        source_tree_map = build_source_tree_map(target_repo_root)
        c_api_index = build_c_api_index(target_repo_root)
        return f"""You are authoring a Layer-2 Proof-of-Concept C test for the Jelleo audit engine.

Your test will be compiled with:

  clang -g -O0 \\
        -fsanitize=address,undefined,signed-integer-overflow \\
        -fno-omit-frame-pointer \\
        -I {target_repo_root}/src \\
        -o /tmp/poc test_<name>.c <ALL .c files under src/, recursively, except those with their own main()>
  /tmp/poc

The engine's build path AUTOMATICALLY discovers and links every `.c`
file under `src/` (including `src/vendor/*/`). You do NOT need to list
them. Just `#include` the public headers you need.

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

# Source tree map — ACTUAL paths in this target

These are the ONLY valid `#include` paths in this target. **Do NOT use
plausible-but-wrong paths like `utils/buffer.h`, `util/buffer.h`,
`collections/map.h`, `lib/foo.h`, `includes/bar.h`.** Use ONLY paths
that appear below.

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

Write a single self-contained C file `test_<finding_name>.c` that:

1. `#include`s the headers needed to call `{engine_function}` and
   construct the witness state. Use ONLY paths from the Source tree map
   above (e.g. `#include "auth/session.h"`). If you can't find a header
   you need in the map, the target file probably exposes the symbol
   through a different header — re-read the Grounded source above.

   STATIC FUNCTIONS: if `{engine_function}` is declared `static` inside
   a program_*.c file (no header export), you MUST #include that .c
   file directly to reach it. The engine's build path skips standalone
   program_*.c files (they each have their own main() that would
   collide with this test's main). Use this template at the top of your
   test, BEFORE your own `main()`:

       /* Pull in program_a.c so we can call its static functions.
        * Rename its main() to avoid collision with our test main. */
       #define main __unused_main_program_a
       #include "program_a.c"   /* relative to src/ via -I flag */
       #undef main

   Then your test's `main()` calls the static function normally.
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

        # Discover .c files in src/ (excluding tests/ only).
        # CRITICAL: skip any .c file that defines its own top-level `int main`
        # otherwise linking multi-program OSec eval targets (program_a.c,
        # program_b.c, program_c.c, each standalone) explodes with
        # "multiple definition of main". The PoC test file owns main(); the
        # LLM-authored test must #include the relevant program .c directly
        # to reach static functions when calling them through headers won't work.
        #
        # NOTE: vendor/ is INCLUDED in the compile/link path. Earlier the
        # adapter skipped vendor/ on the assumption that vendored deps were
        # header-only (true for c-small's `vendor/minimap/map.h`). c-medium
        # has real vendored .c sources (`vendor/minihash/map.c`,
        # `vendor/ini/ini.c`) that engine source files (db.c, config.c,
        # session.c) call into — skipping them produced linker errors like
        # `undefined reference to map_put`. Leave vendored .c files in.
        _MAIN_RE = re.compile(r"^\s*(?:int|void|static\s+int)\s+main\s*\(", re.M)
        src_files = []
        for p in include_dir.rglob("*.c"):
            if "tests" in p.parts:
                continue
            try:
                if _MAIN_RE.search(p.read_text(encoding="utf-8", errors="replace")):
                    continue
            except OSError:
                pass
            src_files.append(str(p))

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
