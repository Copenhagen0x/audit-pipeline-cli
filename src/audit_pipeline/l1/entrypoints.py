"""Native Solana entrypoint AUTHORITY extractor (L1 P0).

A native Solana program's authoritative instruction set is its `Instruction` enum plus
the `process_instruction` match that routes each variant to a handler fn. That pair is
the "native IDL" — the independent ground truth the L1 coverage denominator is built on,
so coverage can never silently read 100% over entrypoints we failed to enumerate
(red-team Critical C1/C2; see architecture/l1-surface-coverage-plan.md §13/§14).

Uses tree-sitter (real AST), not regex — the dispatch is plain source on native programs,
so unlike Anchor `#[program]` macros it is fully visible to the parser.

Output contract (the denominator):
  - every Instruction variant that has NO routing arm  -> COVERAGE FAILURE (dead/unrouted)
  - every routed variant whose handler fn can't be resolved -> COVERAGE FAILURE
  - a `_ => ...` catch-all is recorded (it can swallow variants silently)

This module is intentionally dependency-light: tree_sitter + tree_sitter_rust only.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import tree_sitter_rust
from tree_sitter import Language, Node, Parser

_RUST = Language(tree_sitter_rust.language())


def _parser() -> Parser:
    return Parser(_RUST)


def _text(n: Node, src: bytes) -> str:
    return src[n.start_byte : n.end_byte].decode("utf-8", "replace")


def _walk(node: Node, kind: str, out: list[Node]) -> None:
    if node.type == kind:
        out.append(node)
    for c in node.children:
        _walk(c, kind, out)


@dataclass
class Entrypoint:
    instruction: str                 # Instruction enum variant name (authoritative)
    handler: str | None = None       # handler fn named in the routing arm
    handler_file: str | None = None  # resolved fn definition location
    handler_line: int | None = None
    routed: bool = False             # has a match arm routing it
    arm_file: str | None = None
    arm_line: int | None = None

    @property
    def coverage_failure(self) -> bool:
        # An entrypoint is a denominator FAILURE if it is declared but not routed,
        # or routed but its handler body cannot be located.
        return (not self.routed) or (self.handler is not None and self.handler_file is None)


@dataclass
class EntrypointAuthority:
    program_kind: str = "native"
    instructions: list[Entrypoint] = field(default_factory=list)
    enum_file: str | None = None
    enum_line: int | None = None
    dispatch_file: str | None = None
    dispatch_line: int | None = None
    catch_all_present: bool = False
    catch_all_loc: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Entrypoint]:
        return [e for e in self.instructions if e.coverage_failure]

    def summary(self) -> dict:
        return {
            "program_kind": self.program_kind,
            "declared_instructions": len(self.instructions),
            "routed": sum(1 for e in self.instructions if e.routed),
            "handler_resolved": sum(1 for e in self.instructions if e.handler_file),
            "coverage_failures": len(self.failures),
            "catch_all_present": self.catch_all_present,
            "enum_at": f"{self.enum_file}:{self.enum_line}" if self.enum_file else None,
            "dispatch_at": f"{self.dispatch_file}:{self.dispatch_line}" if self.dispatch_file else None,
            "notes": self.notes,
        }


def _rs_files(root: Path) -> list[Path]:
    skip = {"target", "node_modules", ".git", "build", "tests"}
    return [p for p in root.rglob("*.rs") if not (skip & set(p.parts))]


def _variant_name_of_pattern(pat: Node, src: bytes) -> str | None:
    """Variant name from a match-arm pattern like `Instruction::Deposit { amount }`.

    Returns None for a wildcard `_` (catch-all). Looks for a scoped identifier whose
    leading segment is `Instruction` and returns its final segment.
    """
    if pat.type in ("wildcard_pattern",) or _text(pat, src).strip() == "_":
        return None
    # Unit variants (`Instruction::Foo`) parse as scoped_identifier; struct/tuple variants
    # (`Instruction::Foo { .. }` / `Instruction::Foo(..)`) carry their path as a
    # scoped_TYPE_identifier — collect BOTH or struct-variant arms are silently missed.
    scoped: list[Node] = []
    _walk(pat, "scoped_identifier", scoped)
    _walk(pat, "scoped_type_identifier", scoped)
    for s in scoped:
        t = _text(s, src)
        if "Instruction::" in t or t.startswith("Instruction::"):
            return t.split("::")[-1].strip()
    # Fallback: a bare scoped path `A::B` -> take last segment if it looks like a variant.
    if scoped:
        return _text(scoped[0], src).split("::")[-1].strip()
    return None


def _handler_of_arm_value(value: Node, src: bytes) -> str | None:
    """First handler fn called on the RHS of a match arm (handle_* / process_*)."""
    calls: list[Node] = []
    _walk(value, "call_expression", calls)
    for c in calls:
        fn = c.child_by_field_name("function")
        if fn is None:
            continue
        name = _text(fn, src).split("::")[-1].strip()
        if name.startswith(("handle_", "process_", "ix_", "exec_")):
            return name
    # Fallback: the first call's function name regardless of prefix.
    for c in calls:
        fn = c.child_by_field_name("function")
        if fn is not None:
            return _text(fn, src).split("::")[-1].strip()
    return None


def _find_instruction_enum(trees, src_by_file):
    """Return (variant_names, file, line) for the `Instruction` enum, else ([],None,None)."""
    for path, tree in trees:
        src = src_by_file[path]
        enums: list[Node] = []
        _walk(tree.root_node, "enum_item", enums)
        for e in enums:
            name_node = e.child_by_field_name("name")
            if name_node is None or _text(name_node, src) != "Instruction":
                continue
            variants: list[Node] = []
            _walk(e, "enum_variant", variants)
            names = []
            for v in variants:
                vn = v.child_by_field_name("name")
                if vn is not None:
                    names.append(_text(vn, src))
            return names, path, e.start_point[0] + 1
    return [], None, None


def _find_dispatch(trees, src_by_file, enum_variants):
    """Find the match arms that route `Instruction::Variant => handler(...)`.

    Returns (arm_map, catch_all_loc, dispatch_file, dispatch_line) where arm_map is
    {variant_name: (handler_name, file, line)}. Scores each match by how many arm-variants
    belong to the Instruction enum, so the real dispatch wins over unrelated matches
    (e.g. error-code mapping `ProgramError::Custom => ...`, which has zero enum overlap).
    """
    enum_set = set(enum_variants)
    best = None  # (score, arm_map, catch_all_loc, file, line)
    for path, tree in trees:
        src = src_by_file[path]
        matches: list[Node] = []
        _walk(tree.root_node, "match_expression", matches)
        for m in matches:
            arm_map: dict[str, tuple] = {}
            catch_all_loc = None
            arms: list[Node] = []
            _walk(m, "match_arm", arms)
            for a in arms:
                pat = a.child_by_field_name("pattern")
                val = a.child_by_field_name("value")
                if pat is None:
                    pat = a.children[0] if a.children else None
                if pat is None:
                    continue
                variant = _variant_name_of_pattern(pat, src)
                if variant is None:
                    catch_all_loc = f"{path}:{a.start_point[0] + 1}"
                    continue
                handler = _handler_of_arm_value(val, src) if val is not None else None
                arm_map[variant] = (handler, path, a.start_point[0] + 1)
            # Score = arms that BOTH name an Instruction variant AND route to a real
            # handler fn. This separates the true dispatch (-> handle_*/process_*) from
            # other full-variant matches like an instruction ENCODER (-> out.push(...)).
            score = sum(
                1 for v, (h, _f, _l) in arm_map.items()
                if (not enum_set or v in enum_set)
                and h and h.startswith(("handle_", "process_", "exec_", "ix_"))
            )
            if score > 0 and (best is None or score > best[0]):
                best = (score, arm_map, catch_all_loc, path, m.start_point[0] + 1)
    if best is None:
        return {}, None, None, None
    return best[1], best[2], best[3], best[4]


def _index_functions(trees, src_by_file) -> dict[str, tuple[str, int]]:
    """Map fn name -> (file, line) for every function_item (first definition wins)."""
    idx: dict[str, tuple[str, int]] = {}
    for path, tree in trees:
        src = src_by_file[path]
        fns: list[Node] = []
        _walk(tree.root_node, "function_item", fns)
        for f in fns:
            nm = f.child_by_field_name("name")
            if nm is None:
                continue
            name = _text(nm, src)
            idx.setdefault(name, (path, f.start_point[0] + 1))
    return idx


def extract_native(root: Path) -> EntrypointAuthority:
    """Build the native entrypoint authority for the program rooted at `root`."""
    root = Path(root)
    parser = _parser()
    trees = []
    src_by_file: dict[str, bytes] = {}
    for p in _rs_files(root):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if len(data) > 8 * 1024 * 1024:  # skip pathologically large/generated files
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        src_by_file[rel] = data
        trees.append((rel, parser.parse(data)))

    auth = EntrypointAuthority()
    variants, enum_file, enum_line = _find_instruction_enum(trees, src_by_file)
    auth.enum_file, auth.enum_line = enum_file, enum_line
    arm_map, catch_all_loc, disp_file, disp_line = _find_dispatch(trees, src_by_file, variants)
    auth.dispatch_file, auth.dispatch_line = disp_file, disp_line
    auth.catch_all_present = catch_all_loc is not None
    auth.catch_all_loc = catch_all_loc
    fn_index = _index_functions(trees, src_by_file)

    if not variants:
        auth.notes.append("NO `Instruction` enum found — not a native dispatch program, or enum is named differently (needs Anchor-IDL path or a name hint).")
    if not arm_map:
        auth.notes.append("NO dispatch match found — process_instruction routing not located (delegation depth or non-standard dispatch).")

    seen = set()
    for v in variants:
        seen.add(v)
        handler, af, al = arm_map.get(v, (None, None, None))
        hf = hl = None
        if handler and handler in fn_index:
            hf, hl = fn_index[handler]
        auth.instructions.append(Entrypoint(
            instruction=v, handler=handler, handler_file=hf, handler_line=hl,
            routed=v in arm_map, arm_file=af, arm_line=al,
        ))
    # Routed variants not present in the enum (enum found elsewhere / drift) — surface them.
    for v in arm_map:
        if v not in seen:
            handler, af, al = arm_map[v]
            hf, hl = fn_index.get(handler, (None, None)) if handler else (None, None)
            auth.instructions.append(Entrypoint(
                instruction=v, handler=handler, handler_file=hf, handler_line=hl,
                routed=True, arm_file=af, arm_line=al,
            ))
            auth.notes.append(f"routed variant `{v}` not found in the Instruction enum (possible drift / enum elsewhere)")
    return auth


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: entrypoints.py <program-src-root>", file=sys.stderr)
        return 2
    auth = extract_native(Path(argv[0]))
    print(json.dumps(auth.summary(), indent=2))
    print("\n-- entrypoints (instruction -> handler @ loc | routed | FAILURE?) --")
    for e in auth.instructions:
        flag = "  <-- COVERAGE FAILURE" if e.coverage_failure else ""
        loc = f"{e.handler_file}:{e.handler_line}" if e.handler_file else "(unresolved)"
        print(f"  {e.instruction:<34} -> {e.handler or '(no handler)':<34} @ {loc} | routed={e.routed}{flag}")
    if auth.failures:
        print(f"\nDENOMINATOR: {len(auth.failures)} coverage failure(s) of {len(auth.instructions)} declared instructions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
