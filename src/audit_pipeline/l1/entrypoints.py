"""Native Solana entrypoint AUTHORITY extractor (L1 P0).

A native Solana program's authoritative instruction set is its `Instruction` enum plus
the dispatch `match` that routes each variant to a handler fn. That pair is the "native
IDL" — the independent ground truth the L1 coverage denominator is built on, so coverage
can never silently read 100% over entrypoints we failed to enumerate
(red-team Critical C1/C2; see architecture/l1-surface-coverage-plan.md §13/§14).

Hard rules learned from the L1-code red-team (3-agent, 2026-06-06):
  * NEVER return a clean-looking empty authority. If the enum or dispatch can't be found,
    or files were skipped/timed-out, `status` is COVERAGE_UNKNOWN / COVERAGE_INCOMPLETE —
    never silent "0 failures of 0 instructions". Consumers MUST gate on `status`/`authority_complete`.
  * Untrusted target code: no symlink/junction traversal, per-file parse timeout,
    deterministic (sorted) file order, containment-checked under root.
  * Dispatch selection must not depend on handler NAMING (handlers may be `do_`/`run_`), on the
    dispatch fn's name, or on free-fn vs `impl` method (SPL `impl Processor::process`). We exclude
    `self`-scrutinee matches (encode/decode impls) and rank candidate matches by OBJECTIVE signals
    only — (enum-overlap, #arms-that-call-a-handler); the middle term separates a real dispatch
    from a same-shape encoder/error-classifier (its arms return literals). A genuine tie is flagged
    ambiguous (COVERAGE_INCOMPLETE), never guessed by name.
    The instruction enum NAME is parameterized; or-pattern arms (`A | B => h()`) route every branch.

Scope: this is the TOP-LEVEL entrypoint denominator (the `Instruction` enum + its dispatch).
It is NOT a whole-program coverage oracle. Known limitations:
  * type-aliased enum used in the dispatch (`use Instruction as Ix; match { Ix::A }`) — UNKNOWN;
  * macro-generated / u8-discriminant dispatch with no `match` over the enum — UNKNOWN;
  * two equally-ranked dispatch matches routing differently — INCOMPLETE (ambiguous);
  * a dispatch that routes MULTIPLE distinct enum variants through a `_` catch-all (instead of
    naming them) scores below a fully-explicit same-enum non-dispatch match (validator/logger),
    which can then win selection and MISLABEL handlers — the variant COUNT stays correct (built
    from the enum) and unhandled variants still surface as coverage_failures; typical dispatches
    name every variant and are unaffected. Follow-up: count catch-all coverage in the score metric;
  * INTRA-INSTRUCTION sub-dispatch (a nested match inside one handler) is deliberately NOT
    classified here — distinguishing it from an ordinary `match cfg {..}` is statically
    undecidable, so a direct `=> match {..}` arm surfaces as a coverage_failure (best-effort
    handler = None) and wrapped sub-dispatch is left to the dedicated secondary-dispatch
    surface generator. The top-level entrypoint count stays correct in all cases.

Deps: tree_sitter + tree_sitter_rust only.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_rust
from tree_sitter import Language, Node, Parser

_RUST = Language(tree_sitter_rust.language())
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_PARSE_TIMEOUT_S = 10.0
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_PARSE_TIMEOUTS = 16  # abort to COVERAGE_UNKNOWN past this many parse failures (DoS guard)
# Only non-deployable artifacts: VCS internals + build output + JS deps. These can NEVER
# hold the on-chain program source, so skipping them silently is safe — and skipping
# `target/` is REQUIRED (it holds compiled deps whose own `Instruction` enums would poison
# enumeration). We deliberately do NOT skip `tests/`/`build/` by name (red-team R3): a real
# dispatch/handler could live there, and a stray second enum there must surface as
# COVERAGE_UNKNOWN (ambiguous) rather than be hidden — silent coverage evasion is the cardinal sin.
_SKIP_DIRS = {"target", "node_modules", ".git"}


# --- traversal hardening (transplanted from commands/propagate_ast.py; keep in sync) ---
def _is_link_or_junction(p: Path) -> bool:
    """True for POSIX symlinks OR Windows reparse points (junctions/mounts/cloud stubs).
    We refuse to follow any of them when reading an untrusted target repo."""
    try:
        if p.is_symlink():
            return True
    except OSError:
        return True
    try:
        st = os.lstat(p)
    except OSError:
        return True
    flags = getattr(st, "st_file_attributes", 0)
    return bool(flags & _FILE_ATTRIBUTE_REPARSE_POINT)


def _normalize_for_compare(p: Path) -> Path:
    """Strip the Windows extended-path UNC prefix so `relative_to` containment is reliable."""
    s = os.fspath(p)
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    if s.startswith("\\\\?\\"):
        rest = s[len("\\\\?\\"):]
        if not rest:
            return Path(s)
        if rest.startswith("UNC\\"):
            return Path("\\\\" + rest[len("UNC\\"):])
        return Path(rest)
    return Path(s)


def _rglob_no_follow(root: Path, pattern: str):
    """rglob that does not recurse into symlinked dirs (3.13+ kwarg; legacy fallback)."""
    try:
        return root.rglob(pattern, recurse_symlinks=False)
    except TypeError:
        return root.rglob(pattern)


def _text(n: Node, src: bytes) -> str:
    return src[n.start_byte : n.end_byte].decode("utf-8", "replace")


def _walk(node: Node, kind: str, out: list[Node]) -> None:
    """Collect every descendant of `node` whose type == kind, in pre-order DFS.

    ITERATIVE (explicit stack) so a deeply-nested adversarial AST can't blow Python's
    recursion limit and crash extract_native PAST the status guards (red-team R3 — an
    uncaught RecursionError would defeat the no-silent-false-clean contract). Children
    are pushed reversed so they pop left-to-right, reproducing the old recursive order
    exactly (determinism preserved)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            out.append(n)
        stack.extend(reversed(n.children))


def _calls_no_nested_match(value: Node) -> list[Node]:
    """call_expression descendants of an arm `value`, NOT descending into a nested match or
    closure: a sub-dispatch's scrutinee/calls (`match get_sub() {..}`) must not be attributed
    as THIS arm's handler (red-team R4 — it picked `get_sub` over the real handler). Pre-order
    so the first call is the arm's primary call. If the arm value IS itself a match/closure,
    it delegates (no single handler) -> []."""
    if value.type in ("match_expression", "closure_expression"):
        return []
    calls: list[Node] = []
    stack = [value]
    while stack:
        n = stack.pop()
        if n is not value and n.type in ("match_expression", "closure_expression"):
            continue
        if n.type == "call_expression":
            calls.append(n)
        stack.extend(reversed(n.children))
    return calls


def _enclosing_mod_names(node: Node, src: bytes) -> set[str]:
    """Names of all `mod`/`impl` ancestors of `node` (to spot entrypoint/test shims)."""
    names: set[str] = set()
    cur = node.parent
    while cur is not None:
        if cur.type == "mod_item":
            nm = cur.child_by_field_name("name")
            if nm is not None:
                names.add(_text(nm, src))
        cur = cur.parent
    return names


@dataclass
class Entrypoint:
    instruction: str
    handler: str | None = None
    handler_file: str | None = None
    handler_line: int | None = None
    routed: bool = False
    arm_file: str | None = None
    arm_line: int | None = None

    @property
    def coverage_failure(self) -> bool:
        # FAILURE if: not routed; OR routed but no handler call extracted (panic!/macro/stub);
        # OR a handler was named but its definition can't be located.
        if not self.routed:
            return True
        if self.handler is None:
            return True
        return self.handler_file is None


@dataclass
class EntrypointAuthority:
    program_kind: str = "native"
    status: str = "OK"  # OK | COVERAGE_UNKNOWN | COVERAGE_INCOMPLETE
    instructions: list[Entrypoint] = field(default_factory=list)
    enum_file: str | None = None
    enum_line: int | None = None
    dispatch_file: str | None = None
    dispatch_line: int | None = None
    catch_all_present: bool = False
    catch_all_loc: str | None = None
    skipped_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def authority_complete(self) -> bool:
        return self.status == "OK"

    @property
    def failures(self) -> list[Entrypoint]:
        return [e for e in self.instructions if e.coverage_failure]

    def _mark(self, status: str, note: str) -> None:
        # Escalate severity: UNKNOWN dominates INCOMPLETE dominates OK; never downgrade.
        order = {"OK": 0, "COVERAGE_INCOMPLETE": 1, "COVERAGE_UNKNOWN": 2}
        # Fail LOUD on a bad status (red-team R3): the old `.get(status, 0)` silently
        # treated a typo'd status as OK-level and never escalated — a silent under-report.
        # All call sites pass literals, so this only ever fires as a programmer error in tests.
        if status not in order:
            raise ValueError(f"_mark: unknown status {status!r} (must be one of {sorted(order)})")
        if order[status] > order[self.status]:
            self.status = status
        self.notes.append(note)

    def summary(self) -> dict:
        return {
            "program_kind": self.program_kind,
            "status": self.status,
            "authority_complete": self.authority_complete,
            "declared_instructions": len(self.instructions),
            "routed": sum(1 for e in self.instructions if e.routed),
            "handler_resolved": sum(1 for e in self.instructions if e.handler_file),
            # coverage_failures is meaningful ONLY when status == OK; surfaced as None otherwise
            # so a consumer can never read "0 failures" off an unknown/incomplete authority.
            "coverage_failures": len(self.failures) if self.status == "OK" else None,
            "skipped_files": len(self.skipped_files),
            "catch_all_present": self.catch_all_present,
            "enum_at": f"{self.enum_file}:{self.enum_line}" if self.enum_file else None,
            "dispatch_at": f"{self.dispatch_file}:{self.dispatch_line}" if self.dispatch_file else None,
            "notes": self.notes,
        }


def _rs_files(root: Path) -> tuple[list[Path], list[str]]:
    """Deterministic, symlink/junction-guarded, containment-checked .rs files under root.

    Returns (files, skipped) where skipped lists paths refused for safety (symlinks,
    out-of-root, unreadable) so the caller can mark COVERAGE_INCOMPLETE.
    """
    try:
        root_res = _normalize_for_compare(root.resolve(strict=False))
    except OSError:
        # Don't leak the absolute root into a (possibly published) report (red-team R3).
        return [], ["<root unresolvable — unreadable path or too long>"]
    files: list[Path] = []
    skipped: list[str] = []
    for p in _rglob_no_follow(root, "*.rs"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if _SKIP_DIRS & set(rel.parts):  # relative parts only — never the absolute prefix
            continue
        if _is_link_or_junction(p):
            skipped.append(str(rel) + " (symlink/junction refused)")
            continue
        try:
            p_res = _normalize_for_compare(p.resolve(strict=False))
            p_res.relative_to(root_res)  # containment: resolved path must stay under root
        except (OSError, ValueError):
            skipped.append(str(rel) + " (escapes root / unreadable)")
            continue
        files.append(p)
    return sorted(files), skipped


def _parse_with_timeout(data: bytes):
    """Parse `data` in a worker thread with a wall-clock bound; None on timeout/error.

    Must NOT use `with ThreadPoolExecutor(...)` — its `__exit__` does shutdown(wait=True),
    which re-blocks on a runaway (uninterruptible C) parse and defeats the very timeout we
    set (red-team M1). We shut down non-blocking so a hung worker leaks rather than freezing
    the whole extraction. Because a leaked worker may still be inside `parser.parse`, it
    must own a PRIVATE Parser — sharing one with the next file would race on the same C
    object (red-team R3). Any parse exception ALSO maps to None so one malformed file can't
    crash enumeration; the caller then marks that file COVERAGE_INCOMPLETE.
    """
    try:
        ex = ThreadPoolExecutor(max_workers=1)
    except RuntimeError:
        return None  # OS refused a new thread (resource limit) — treat file as un-enumerated
    try:
        return ex.submit(lambda: Parser(_RUST).parse(data)).result(timeout=_PARSE_TIMEOUT_S)
    except FuturesTimeout:
        return None
    except Exception:  # noqa: BLE001 — any parse failure is "un-enumerable", never fatal
        return None
    finally:
        ex.shutdown(wait=False)  # never wait on a possibly-hung worker


def _variant_names_of_pattern(pat: Node, src: bytes, enum_set: set[str], enum_name: str) -> list[str]:
    """Variant name(s) routed by a match-arm pattern. [] for wildcard/binding.

    Unwraps the tree-sitter `match_pattern` wrapper to the pattern PROPER, so the `if <guard>`
    expression it also holds is never read — `X::A if f(X::B) => h` must route A, not B
    (red-team R5 over-count). Accepts:
      * QUALIFIED `EnumName::V` / `Self::V` — the segment immediately before V must equal the
        resolved enum name or `Self` (a substring/foreign-enum match was an injection vector, R4);
      * BARE `V` / `V(..)` / `V{..}` — only when V is a DECLARED variant (in enum_set); that
        tells a `use Instruction::*`-imported variant apart from an ordinary binding (R5 under-count).
    Recurses through or-/reference-/tuple-/parenthesized patterns; for tuple/struct patterns it
    takes ONLY the constructor head so inner bindings are never mistaken for variants.
    A type-aliased enum (qualifier ≠ enum name, name ∉ enum_set) is intentionally NOT matched —
    it degrades to COVERAGE_UNKNOWN (safe), never a wrong OK."""
    t = pat.type
    if t == "match_pattern":  # wrapper = <pattern> [if <guard>]; descend ONLY into the pattern,
        nc = pat.named_children  # NOT the guard expression (first named child is the pattern)
        return _variant_names_of_pattern(nc[0], src, enum_set, enum_name) if nc else []
    out: list[str] = []

    def _push(v: str) -> None:
        if v and v not in out:
            out.append(v)

    if t in ("scoped_identifier", "scoped_type_identifier"):
        segs = [s.strip() for s in _text(pat, src).split("::")]
        if (segs[-2] if len(segs) >= 2 else None) in (enum_name, "Self"):
            _push(segs[-1])
    elif t in ("identifier", "type_identifier"):
        v = _text(pat, src).strip()
        if v in enum_set:  # bare use-imported variant, NOT a binding
            _push(v)
    elif t in ("tuple_struct_pattern", "struct_pattern"):
        for c in pat.children:  # constructor head only (skip inner bindings/fields)
            if c.type in ("scoped_identifier", "scoped_type_identifier", "identifier", "type_identifier"):
                for v in _variant_names_of_pattern(c, src, enum_set, enum_name):
                    _push(v)
                break
    elif t == "captured_pattern":  # `binding @ <pattern>` — variant is the PATTERN, not the binding
        nc = pat.named_children    # (red-team R7: a blanket recurse over-counted a binding named like a variant)
        if nc:
            for v in _variant_names_of_pattern(nc[-1], src, enum_set, enum_name):
                _push(v)
    elif t in ("or_pattern", "reference_pattern", "tuple_pattern", "parenthesized_pattern", "slice_pattern"):
        for c in pat.children:
            if c.is_named:  # skip punctuation tokens (| & ( ))
                for v in _variant_names_of_pattern(c, src, enum_set, enum_name):
                    _push(v)
    # else: any other node type (range_pattern, macro, etc.) -> [] (conservative under-count,
    # surfaces as a coverage_failure — never an over-count of a binding/range endpoint; red-team R7).
    return out


def _handler_of_arm_value(value: Node, fn_index: dict, src: bytes) -> str | None:
    """Handler = the first FREE-function call in the arm whose name is a defined fn.

    Naming-agnostic (handlers may be `do_`/`run_`). A method call (`out.push(...)`,
    whose function is a field_expression) is NOT a handler — that excludes encoders.
    Does not descend into a nested match/closure (red-team R4 — a sub-dispatch's scrutinee
    must not be mistaken for this arm's handler).
    """
    calls = _calls_no_nested_match(value)
    for c in calls:
        fn = c.child_by_field_name("function")
        if fn is None or fn.type not in ("identifier", "scoped_identifier"):
            continue  # skip method calls (field_expression) like out.push(..)
        name = _text(fn, src).split("::")[-1].strip()
        if name in fn_index:
            return name
    # Fall back to the first free-fn call even if unresolved (still records intent).
    for c in calls:
        fn = c.child_by_field_name("function")
        if fn is not None and fn.type in ("identifier", "scoped_identifier"):
            return _text(fn, src).split("::")[-1].strip()
    return None


def _find_instruction_enums(trees, src_by_file, name: str):
    """All enums named `name`. Returns list of (variants, file, line)."""
    found = []
    for path, tree in trees:
        src = src_by_file[path]
        enums: list[Node] = []
        _walk(tree.root_node, "enum_item", enums)
        for e in enums:
            nm = e.child_by_field_name("name")
            if nm is None or _text(nm, src) != name:
                continue
            variants = []
            vnodes: list[Node] = []
            body = e.child_by_field_name("body")  # enum_variant_list — variants only,
            if body is not None:                  # never descend into discriminant exprs
                _walk(body, "enum_variant", vnodes)
            for v in vnodes:
                vn = v.child_by_field_name("name")
                if vn is not None:
                    variants.append(_text(vn, src))
            found.append((variants, path, e.start_point[0] + 1))
    return found


def _index_functions(trees, src_by_file) -> dict[str, list[tuple]]:
    """name -> list of (file, line, is_shim) for every function_item.

    is_shim = defined inside a mod named `entrypoint` (BPF shim) — de-prioritized so a
    handler resolves to the business-logic def, not the shim, when names collide.
    """
    idx: dict[str, list[tuple]] = {}
    for path, tree in trees:
        src = src_by_file[path]
        fns: list[Node] = []
        _walk(tree.root_node, "function_item", fns)
        for f in fns:
            nm = f.child_by_field_name("name")
            if nm is None:
                continue
            mods = _enclosing_mod_names(f, src)
            is_shim = "entrypoint" in mods
            idx.setdefault(_text(nm, src), []).append((path, f.start_point[0] + 1, is_shim))
    return idx


def _resolve_fn(name: str, fn_index: dict):
    """Pick the best definition of `name`: prefer non-shim. Returns (file, line, ambiguous)."""
    defs = fn_index.get(name)
    if not defs:
        return None, None, False
    non_shim = [d for d in defs if not d[2]]
    pick = non_shim[0] if non_shim else defs[0]
    ambiguous = len(non_shim) > 1 or (not non_shim and len(defs) > 1)
    return pick[0], pick[1], ambiguous


def _find_dispatch(trees, src_by_file, enum_set: set[str], fn_index: dict, enum_name: str):
    """The match that routes Instruction variants to handlers.

    Excludes `self`-scrutinee matches (encode/decode impl methods). Only DIRECT arms of each
    match block are read (red-team R4 — `_walk` previously pulled in nested-match arms).
    Returns (arm_map, catch_all_loc, file, line, ambiguous, multi_handler) where
    arm_map = {variant: (handler, af, al)}.

    Rank ladder (OBJECTIVE signals only — deterministic: max rank, then lowest (file, line)):
      (1) enum-overlap score — arms whose variant ∈ the enum (naming-agnostic);
      (2) handler_calls — arms that CALL a handler fn. Separates a real dispatch from a
          same-shape encoder/serializer/error-classifier (those arms return literals). Robust
          to the dispatch living in an `impl` (SPL `impl Processor::process`) and to naming.
    No enclosing-fn-NAME nudge: a `dispatch`/`process`-named encoder could beat a real dispatch
    named `route`/`execute` and pick the wrong handlers (red-team R10). A genuine tie now goes to
    `ambiguous` -> COVERAGE_INCOMPLETE (flag) rather than a name guess.
    `ambiguous` = another candidate ties the top rank with a DIFFERENT routing (two plausible
    dispatches — flag, never silently pick one). `multi_handler` = variants routed by
    guarded/duplicate arms to >1 handler (only the first is recorded — flag the rest).

    SCOPE (red-team R6-R9): this enumerates the TOP-LEVEL entrypoint denominator only.
    Handler resolution is BEST-EFFORT and does not descend into a nested match (via
    `_handler_of_arm_value`/`_calls_no_nested_match`), so an arm whose value IS a nested
    sub-dispatch naturally resolves to no handler -> coverage_failure (flagged). We do NOT try
    to classify a nested match as "instruction sub-dispatch" vs "internal value/state match" —
    that is statically undecidable (every heuristic either floods INCOMPLETE on ordinary
    `match cfg {..}` or misses a use-imported sub-dispatch). Intra-instruction sub-dispatch
    enumeration is the dedicated secondary-dispatch surface generator's job (see the plan).
    """
    candidates = []  # (rank, arm_map, catch_all_loc, file, line, multi_handler)
    for path, tree in trees:
        src = src_by_file[path]
        matches: list[Node] = []
        _walk(tree.root_node, "match_expression", matches)
        for m in matches:
            scrut = m.child_by_field_name("value")
            if scrut is not None and _text(scrut, src).strip().lstrip("*&").strip() == "self":
                continue  # encode()/decode() impl on the enum — not the dispatch
            body = m.child_by_field_name("body")
            arms = [c for c in body.children if c.type == "match_arm"] if body is not None else []
            arm_map: dict[str, tuple] = {}
            multi_handler: set[str] = set()
            catch_all_loc = None
            for a in arms:
                pat = a.child_by_field_name("pattern")
                if pat is None:
                    continue
                val = a.child_by_field_name("value")
                variants = _variant_names_of_pattern(pat, src, enum_set, enum_name)
                if not variants:
                    if _text(pat, src).strip() == "_":  # match_pattern text for a `_` arm
                        catch_all_loc = f"{path}:{a.start_point[0] + 1}"
                    continue
                # Best-effort handler (None for a direct `=> match {..}` arm -> coverage_failure).
                handler = _handler_of_arm_value(val, fn_index, src) if val is not None else None
                for variant in variants:  # or-pattern: each branch is its own routed variant
                    if variant in arm_map:
                        if arm_map[variant][0] != handler:
                            multi_handler.add(variant)  # guarded/dup arm — 2nd handler hidden
                        continue
                    arm_map[variant] = (handler, path, a.start_point[0] + 1)
            score = sum(1 for v in arm_map if v in enum_set) if enum_set else len(arm_map)
            if score <= 0:
                continue
            handler_calls = sum(1 for (h, _f, _l) in arm_map.values() if h is not None)
            rank = (score, handler_calls)
            candidates.append((rank, arm_map, catch_all_loc, path, m.start_point[0] + 1, multi_handler))
    if not candidates:
        return {}, None, None, None, False, set()
    candidates.sort(key=lambda c: (tuple(-x for x in c[0]), c[3], c[4]))  # best first, deterministic
    best = candidates[0]
    best_score = best[0][0]
    best_routing = {v: h for v, (h, _f, _l) in best[1].items()}
    # Ambiguous if ANOTHER candidate with the SAME enum-overlap score is ALSO dispatch-shaped
    # (handler_calls > 0) and routes DIFFERENTLY. Keying on score (not the full rank) is
    # deliberate (red-team R11): a real dispatch with an inline sub-dispatch arm has a lower
    # handler_calls than a same-coverage helper-calling encoder/logger, so it would never tie the
    # full rank — handler_calls alone would silently hand the win to the encoder. Two
    # full-coverage handler-calling matches that route differently are genuinely ambiguous → flag,
    # never pick by handler_calls margin. A literal-returning encoder (handler_calls 0) is NOT
    # dispatch-shaped, so it still loses cleanly and does not trip this.
    ambiguous = any(
        c[0][0] == best_score and c[0][1] > 0
        and {v: h for v, (h, _f, _l) in c[1].items()} != best_routing
        for c in candidates[1:]
    )
    return best[1], best[2], best[3], best[4], ambiguous, best[5]


def extract_native(root: Path, instruction_enum_name: str = "Instruction") -> EntrypointAuthority:
    """Build the native entrypoint authority for the program rooted at `root`."""
    root = Path(root)
    auth = EntrypointAuthority()
    # SCOPE caveat appended at construction (red-team R6-R10) so EVERY return path — including the
    # early-exit UNKNOWN/INCOMPLETE ones — carries it: this authority is the TOP-LEVEL
    # `instruction_enum_name` entrypoint denominator ONLY. It does NOT enumerate intra-instruction
    # sub-dispatch (a nested match inside a handler) or out-of-band routing (u8-discriminant,
    # macro-generated, a second decoy enum) — those are the dedicated secondary-dispatch /
    # discriminant surface generators' job. status OK ≠ "every on-chain code path".
    auth.notes.append(f"SCOPE: TOP-LEVEL `{instruction_enum_name}` entrypoint denominator only; intra-instruction sub-dispatch and out-of-band (discriminant/macro) routing are NOT enumerated here — combine with the secondary-dispatch / discriminant surface generators.")

    files, skipped = _rs_files(root)
    auth.skipped_files = list(skipped)
    if skipped:
        auth._mark("COVERAGE_INCOMPLETE", f"{len(skipped)} file(s) refused for safety (symlink/escape).")

    trees = []
    src_by_file: dict[str, bytes] = {}
    parse_failures = 0
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        try:
            data = p.read_bytes()
        except OSError:
            # Don't silently drop (red-team R4): a file that passed _rs_files but is now
            # unreadable (race/permission) must still signal incompleteness.
            auth.skipped_files.append(rel + " (unreadable at read time)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel}: unreadable at read time (race/permission) — not enumerated.")
            continue
        if len(data) > _MAX_FILE_BYTES:
            auth.skipped_files.append(rel + " (>8MB)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel}: skipped (>8MB) — not enumerated.")
            continue
        tree = _parse_with_timeout(data)
        if tree is None:
            parse_failures += 1
            auth.skipped_files.append(rel + " (parse timeout/error)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel}: parse exceeded {_PARSE_TIMEOUT_S}s or failed — not enumerated.")
            # Bound leaked worker threads from pathological/adversarial input (red-team R3):
            # too many failures means the run is no longer trustworthy — bail to UNKNOWN.
            if parse_failures >= _MAX_PARSE_TIMEOUTS:
                auth._mark("COVERAGE_UNKNOWN", f"aborted enumeration: {parse_failures} files failed/timed out parsing (possible adversarial input or resource exhaustion) — authority not trustworthy.")
                return auth
            continue
        src_by_file[rel] = data
        trees.append((rel, tree))

    enums = _find_instruction_enums(trees, src_by_file, instruction_enum_name)
    if not enums:
        auth._mark("COVERAGE_UNKNOWN", f"no `{instruction_enum_name}` enum found — cannot establish the entrypoint denominator (rename? macro-generated? Anchor?).")
        return auth
    if len(enums) > 1:
        enums.sort(key=lambda e: (-len(e[0]), e[1], e[2]))  # deterministic note ordering
        locs = ", ".join(f"{e[1]}:{e[2]}" for e in enums)
        # Do NOT point enum_file/line at one enum on an ambiguous authority — a decoy could
        # be the largest and would misdirect a human reviewer (red-team R3). Locs are in the note.
        auth._mark(
            "COVERAGE_UNKNOWN",
            f"{len(enums)} `{instruction_enum_name}` enums found ({locs}) — ambiguous; cannot "
            f"pick a single denominator. Pass a disambiguating target_file/hint. Instructions "
            f"and enum_file left EMPTY by contract (no single authority on an ambiguous match).",
        )
        return auth
    variants, auth.enum_file, auth.enum_line = enums[0]
    enum_set = set(variants)
    if not variants:
        # A zero-variant `Instruction` enum can't be a trustworthy denominator: the real
        # surface may be macro-generated or u8-discriminant dispatched (red-team R5). Flag,
        # never report a clean "0 instructions, 0 failures".
        auth._mark("COVERAGE_UNKNOWN", f"`{instruction_enum_name}` enum declares ZERO variants — denominator not trustworthy (macro-generated variants? discriminant/u8 dispatch?). Verify the dispatch mechanism.")
        return auth

    fn_index = _index_functions(trees, src_by_file)
    arm_map, catch_all_loc, disp_file, disp_line, ambiguous_dispatch, multi_handler_variants = \
        _find_dispatch(trees, src_by_file, enum_set, fn_index, instruction_enum_name)
    auth.dispatch_file, auth.dispatch_line = disp_file, disp_line
    auth.catch_all_present = catch_all_loc is not None
    auth.catch_all_loc = catch_all_loc

    if not arm_map:
        auth._mark("COVERAGE_UNKNOWN", "Instruction enum found but no dispatch match located — routing not identified (delegation depth / non-standard dispatch).")
        return auth
    if ambiguous_dispatch:
        auth._mark("COVERAGE_INCOMPLETE", "multiple equally-ranked dispatch matches with DIFFERENT routing — cannot pick one authoritatively; disambiguate (target_file/hint) and re-run.")
    if multi_handler_variants:
        auth._mark("COVERAGE_INCOMPLETE", f"{len(multi_handler_variants)} variant(s) routed by guarded/duplicate arms to multiple handlers; only the first is recorded — verify the rest: {sorted(multi_handler_variants)[:8]}")

    ambiguous_handlers = []
    for v in variants:
        handler, af, al = arm_map.get(v, (None, None, None))
        hf = hl = None
        if handler:
            hf, hl, amb = _resolve_fn(handler, fn_index)
            if amb:
                ambiguous_handlers.append(handler)
        auth.instructions.append(Entrypoint(
            instruction=v, handler=handler, handler_file=hf, handler_line=hl,
            routed=v in arm_map, arm_file=af, arm_line=al,
        ))
    if ambiguous_handlers:
        auth._mark("COVERAGE_INCOMPLETE", f"{len(ambiguous_handlers)} handler name(s) have multiple non-shim definitions; resolution may be wrong: {sorted(set(ambiguous_handlers))[:8]}")

    # tree-sitter sees all `#[cfg(...)]` branches; the denominator may include
    # compile-time-conditional variants. Flag it (don't silently treat as deployed).
    # (SCOPE caveat is appended at construction so it's on every path — see top of fn.)
    auth.notes.append("NOTE: cfg-gated variants/handlers are included (all cfg branches parsed); verify against the build profile.")
    return auth


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: entrypoints.py <program-src-root> [InstructionEnumName]", file=sys.stderr)
        return 2
    name = argv[1] if len(argv) > 1 else "Instruction"
    auth = extract_native(Path(argv[0]), name)
    print(json.dumps(auth.summary(), indent=2))
    print("\n-- entrypoints (instruction -> handler @ loc | routed | FAILURE?) --")
    for e in auth.instructions:
        flag = "  <-- COVERAGE FAILURE" if e.coverage_failure else ""
        loc = f"{e.handler_file}:{e.handler_line}" if e.handler_file else "(unresolved)"
        print(f"  {e.instruction:<34} -> {e.handler or '(no handler)':<34} @ {loc} | routed={e.routed}{flag}")
    if not auth.authority_complete:
        print(f"\nSTATUS: {auth.status} — authority is NOT trustworthy as a denominator. See notes.")
    elif auth.failures:
        print(f"\nDENOMINATOR: {len(auth.failures)} coverage failure(s) of {len(auth.instructions)} declared instructions.")
    else:
        print(f"\nOK: {len(auth.instructions)} instructions, all routed + resolved, 0 failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
