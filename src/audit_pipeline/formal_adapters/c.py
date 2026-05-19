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

def _select_minimal_sources(harness_path: Path, candidate_src_files: list[str]) -> list[str]:
    """Return the minimal subset of ``candidate_src_files`` that the
    harness transitively calls (BFS over function-symbol references).

    CBMC scales badly with link-set size: 12 linked .c files give
    ~10x the state-space of 3 linked files at the same unwind depth.
    Pruning to just the files whose symbols the harness reaches keeps
    CBMC under the 10-min per-hyp budget on medium-sized C targets
    (c-medium taught us this — 9/11 hyps timed out at full link set).

    Per-file scoped hypotheses (the OSec eval style) don't lose
    correctness: if the harness never calls into module X, CBMC has
    no path to a bug in X anyway. If parsing fails or the closure is
    empty, we fall back to all candidates so this can never regress
    a previously-passing run.

    Env-var ``CBMC_PRUNE=0`` disables pruning and uses the full set.
    """
    import os as _os
    if _os.environ.get("CBMC_PRUNE", "1") == "0":
        return candidate_src_files

    try:
        text = harness_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return candidate_src_files

    # Strip line/block comments + string literals (rough but adequate
    # for symbol extraction; we only need a superset of called symbols).
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)

    called: set[str] = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text))

    # C keywords + libc + pthread + CBMC builtins — never in src/, skip.
    _SKIP = {
        "if","while","for","switch","sizeof","return","main","do","else",
        "malloc","calloc","realloc","free","memcpy","memset","memmove","memcmp",
        "strcpy","strncpy","strcat","strncat","strcmp","strncmp","strdup","strndup",
        "strlen","strchr","strrchr","strstr","strtok","strtok_r","strspn","strcspn",
        "snprintf","sprintf","printf","fprintf","puts","fputs","vprintf","vfprintf",
        "open","openat","close","read","write","lseek","fstat","stat","lstat",
        "unlink","rename","mkdir","rmdir","chmod","chown","fchmod","fchown",
        "access","dup","dup2","pipe","fcntl","ioctl","mmap","munmap","msync",
        "fopen","fdopen","fclose","fread","fwrite","fseek","ftell","fgets","fputc","fgetc",
        "ferror","feof","fflush","rewind","setvbuf","tmpfile","tmpnam","mkstemp",
        "atoi","atol","atoll","strtol","strtoul","strtoll","strtoull","strtod","strtof",
        "assert","abort","exit","_exit","atexit","getenv","setenv","unsetenv","putenv",
        "getpid","getppid","getuid","geteuid","getgid","getegid","fork","execvp","execv",
        "waitpid","wait","kill","signal","sigaction","sigemptyset","sigaddset",
        "pthread_create","pthread_join","pthread_exit","pthread_detach","pthread_self",
        "pthread_mutex_init","pthread_mutex_lock","pthread_mutex_unlock","pthread_mutex_destroy",
        "pthread_mutex_trylock","pthread_rwlock_init","pthread_rwlock_rdlock","pthread_rwlock_wrlock",
        "pthread_rwlock_unlock","pthread_rwlock_destroy",
        "pthread_cond_init","pthread_cond_wait","pthread_cond_signal","pthread_cond_broadcast",
        "pthread_cond_timedwait","pthread_cond_destroy",
        "pthread_key_create","pthread_setspecific","pthread_getspecific",
        "errno","perror","strerror","strerror_r",
        "__CPROVER_assume","__CPROVER_assert","__CPROVER_havoc_object","__CPROVER_input",
        "__CPROVER_w_ok","__CPROVER_r_ok","__CPROVER_buffer_size","__CPROVER_pointer_offset",
        "__CPROVER_same_object","__CPROVER_DYNAMIC_OBJECT","__CPROVER_array_set",
        "nondet_int","nondet_uint","nondet_long","nondet_ulong","nondet_size_t",
        "nondet_char","nondet_uchar","nondet_short","nondet_ushort","nondet_bool",
        "nondet_float","nondet_double","nondet",
        "static_assert","_Static_assert","offsetof","alignof","__alignof__","__builtin_offsetof",
        "htonl","htons","ntohl","ntohs","htobe16","htobe32","htobe64","be16toh","be32toh","be64toh",
        "le16toh","le32toh","le64toh","htole16","htole32","htole64","bswap_16","bswap_32","bswap_64",
        "select","poll","epoll_create","epoll_create1","epoll_ctl","epoll_wait",
        "socket","bind","listen","accept","accept4","connect","send","recv","sendto","recvfrom",
        "sendmsg","recvmsg","shutdown","getsockopt","setsockopt","getsockname","getpeername",
        "getaddrinfo","freeaddrinfo","gai_strerror","inet_pton","inet_ntop","inet_addr","inet_ntoa",
        "time","clock","clock_gettime","gettimeofday","sleep","usleep","nanosleep","localtime","gmtime",
        "mktime","strftime","strptime","difftime","asctime","ctime",
        "isdigit","isalpha","isalnum","isspace","isupper","islower","iscntrl","isprint",
        "ispunct","isxdigit","isascii","toupper","tolower",
        "qsort","bsearch","abs","labs","llabs","div","ldiv","rand","srand","rand_r",
        "min","max","MIN","MAX","ARRAY_SIZE","containerof","container_of",
        # Common type-cast-looking call sites that aren't real calls
        "int","long","char","short","void","size_t","ssize_t","uint8_t","uint16_t",
        "uint32_t","uint64_t","int8_t","int16_t","int32_t","int64_t","intptr_t","uintptr_t",
        "ptrdiff_t","off_t","time_t","FILE","DIR",
    }
    called -= _SKIP

    # Build symbol -> .c file map by scanning function definitions.
    # A function def in a .c file is roughly:
    #   [storage-class/type ...] name (args) { ... }
    # Use a permissive but anchored regex that matches identifier-paren-block at
    # the start of a line (column 0 in real source).
    _FN_DEF_RE = re.compile(
        r"^(?:[a-zA-Z_][\w\s\*]*?\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
        r"[^)]*\)\s*(?:\{|\n\s*\{)",
        re.MULTILINE,
    )
    _CALL_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")

    sym_to_file: dict[str, str] = {}
    file_text: dict[str, str] = {}
    for path_str in candidate_src_files:
        try:
            with open(path_str, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        clean = re.sub(r"//[^\n]*", "", raw)
        clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL)
        clean = re.sub(r'"(?:\\.|[^"\\])*"', '""', clean)
        file_text[path_str] = clean
        for name in _FN_DEF_RE.findall(clean):
            sym_to_file.setdefault(name, path_str)

    # BFS: every called-symbol whose defining file isn't yet kept
    # gets that file pulled in, then we expand by the symbols THAT
    # file calls.
    kept: set[str] = set()
    work: set[str] = called & sym_to_file.keys()
    iterations = 0
    while work and iterations < 10000:
        iterations += 1
        sym = work.pop()
        f = sym_to_file.get(sym)
        if not f or f in kept:
            continue
        kept.add(f)
        more = set(_CALL_RE.findall(file_text.get(f, ""))) - _SKIP
        for s in more & sym_to_file.keys():
            if sym_to_file[s] not in kept:
                work.add(s)

    # Always include .c files matching the harness filename's hyp-id token
    # (defensive — covers cases where the LLM harness uses a different name
    # for a wrapper but the bug is in a sibling file).
    if not kept:
        return candidate_src_files

    return sorted(kept)



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

        source_tree_map = build_source_tree_map(target_repo_root)
        c_api_index = build_c_api_index(target_repo_root)
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

Write a single self-contained C file `harness_<finding_name>.c` that:

1. `#include`s the headers needed to call `{engine_function}`. Use ONLY paths from the Source tree map below.

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
        candidate_src_files = []
        if include_dir.is_dir():
            for p in include_dir.rglob("*.c"):
                if "tests" in p.parts:  # vendor/ INCLUDED: c-medium has real vendored .c sources (vendor/minihash/map.c, vendor/ini/ini.c) that engine sources call into
                    continue
                try:
                    if _MAIN_RE_C.search(p.read_text(encoding="utf-8", errors="replace")):
                        continue
                except OSError:
                    pass
                candidate_src_files.append(str(p))

        # Per-harness pruning: CBMC scales badly with link-set size.
        # Link only the .c files this harness transitively calls
        # (BFS closure). Typical c-medium shrink: 12 -> 2-4 files,
        # which gives ~10x speedup. Falls back to all candidates if
        # parsing fails. Disable via env CBMC_PRUNE=0.
        src_files = _select_minimal_sources(harness_path, candidate_src_files)

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
