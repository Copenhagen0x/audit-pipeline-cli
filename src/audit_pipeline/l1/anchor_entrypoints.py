"""Anchor entrypoint AUTHORITY extractor (L1 step 6).

The native reader (`entrypoints.py`) reconstructs a native program's instruction set from its
`Instruction` enum + dispatch match. Anchor programs have no such hand-written dispatch: the
`#[program]` macro turns every `pub fn` in the annotated module into a routed instruction. So the
authoritative Anchor instruction set is exactly the set of `pub fn`s inside the `#[program]`
module — that is the coverage denominator here.

Mirrors entrypoints.py's hard rules (reuses its hardened helpers verbatim):
  * NEVER a clean-looking empty authority. No `#[program]` module found -> COVERAGE_UNKNOWN (it's
    a native program, or the module is macro-hidden), never "0 instructions OK". Found the module
    but zero pub fns, multiple `#[program]` modules, skipped/timed-out files -> COVERAGE_INCOMPLETE.
    Consumers gate on `status` / `authority_complete`.
  * Untrusted target code: symlink/junction-refused, per-file parse timeout, single-pass capped
    traversal, deterministic (sorted) file order, containment-checked under root — all inherited
    from entrypoints.py so the two readers can't drift.
  * Detection is by the `#[program]` ATTRIBUTE, not the module's NAME (it can be anything), and we
    accept the attribute whether tree-sitter attaches it as a child of the mod_item or as a
    preceding sibling. Instruction fns are detected by `pub` visibility, not by naming.

Known limitations (each degrades to UNKNOWN/INCOMPLETE, never a wrong OK):
  * a `#[program]` module produced by a macro (no literal `#[program] mod`) — UNKNOWN;
  * instruction fns generated inside the module by a macro — not enumerable (the pub fns we CAN
    see are still counted; if none are visible we mark INCOMPLETE);
  * a workspace with multiple `#[program]` modules — all are enumerated (over-inclusive = safe) and
    the run is marked INCOMPLETE so the operator knows the denominator spans >1 program.

Deps: reuses entrypoints.py (tree_sitter + tree_sitter_rust only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tree_sitter import Node

from audit_pipeline.l1.entrypoints import (
    _MAX_FILE_BYTES,
    _MAX_PARSE_TIMEOUTS,
    Entrypoint,
    EntrypointAuthority,
    _parse_with_timeout,
    _rs_files,
    _text,
    _walk,
)


def _norm_attr(node: Node, src: bytes) -> str:
    """Whitespace-stripped attribute text, so `#[program]` / `# [ program ]` both normalize."""
    return "".join(_text(node, src).split())


def _item_has_program_attr(item: Node, src: bytes) -> bool:
    """True if `item` carries the Anchor `#[program]` attribute — accepting the attribute either
    as a child of the item OR as a contiguous preceding sibling (tree-sitter grammar variations)."""
    for c in item.children:
        if c.type == "attribute_item" and _norm_attr(c, src) == "#[program]":
            return True
    # walk preceding siblings, SKIPPING interleaved doc/line/block comments (a `///` doc line
    # between #[program] and the mod is a common, valid pattern that must not hide detection).
    sib = item.prev_named_sibling
    while sib is not None:
        if sib.type == "attribute_item":
            if _norm_attr(sib, src) == "#[program]":
                return True
            sib = sib.prev_named_sibling
        elif sib.type in ("line_comment", "block_comment", "doc_comment"):
            sib = sib.prev_named_sibling  # keep looking past the comment
        else:
            break  # a real item ended the attribute/comment chain
    return False


def _program_mods(root_node: Node, src: bytes) -> list[Node]:
    """Every `#[program]`-annotated `mod_item` in the tree (usually exactly one)."""
    mods: list[Node] = []
    items: list[Node] = []
    _walk(root_node, "mod_item", items)
    for m in items:
        if _item_has_program_attr(m, src):
            mods.append(m)
    return mods


def _pub_fns_in_mod(mod: Node, src: bytes) -> tuple[list[tuple[str, int]], int]:
    """(name, line) for each `pub fn` DIRECTLY in the module body, plus the count of non-pub fns
    seen (informational). Anchor only exposes `pub fn`s as instructions, so non-pub fns are not
    counted but are reported so the operator can see what was excluded."""
    body = mod.child_by_field_name("body")
    if body is None:
        return [], 0
    out: list[tuple[str, int]] = []
    non_pub = 0
    for c in body.children:
        if c.type != "function_item":
            continue
        # EXACT `pub` only — Anchor routes bare `pub fn`. pub(crate)/pub(super)/pub(in ..) are NOT
        # exported instructions; counting them would inflate the denominator with phantoms.
        is_pub = any(ch.type == "visibility_modifier" and _text(ch, src).strip() == "pub"
                     for ch in c.children)
        name_node = c.child_by_field_name("name")
        if name_node is None:
            continue
        if not is_pub:
            non_pub += 1
            continue
        out.append((_text(name_node, src).strip(), name_node.start_point[0] + 1))
    return out, non_pub


def extract_anchor(root: Path) -> EntrypointAuthority:
    """Enumerate the Anchor instruction authority (every pub fn in the #[program] module)."""
    auth = EntrypointAuthority(program_kind="anchor")
    # standing scope caveat on EVERY return path (mirrors the native reader): the authority is the
    # pub fns visible in #[program]; macro-generated instructions are not statically enumerable.
    auth.notes.append("SCOPE: Anchor instruction authority = bare `pub fn`s in the #[program] "
                      "module body; macro-generated instructions are not enumerable (such a program "
                      "degrades to COVERAGE_INCOMPLETE/UNKNOWN, never a silent OK).")
    files, skipped = _rs_files(root)
    auth.skipped_files = list(skipped)
    if skipped:
        auth._mark("COVERAGE_INCOMPLETE", f"{len(skipped)} file(s) skipped (symlink/escape/unreadable).")

    timeouts = 0
    found: list[tuple[Node, str, bytes]] = []  # (program_mod, rel_path, src)
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        try:
            data = p.read_bytes()
        except OSError:
            auth.skipped_files.append(rel + " (unreadable)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel} (unreadable).")
            continue
        if len(data) > _MAX_FILE_BYTES:
            auth.skipped_files.append(rel + " (too large)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel} (too large, skipped).")
            continue
        tree = _parse_with_timeout(data)
        if tree is None:
            timeouts += 1
            auth.skipped_files.append(rel + " (parse failed/timed out)")
            auth._mark("COVERAGE_INCOMPLETE", f"{rel} (parse failed/timed out).")
            if timeouts >= _MAX_PARSE_TIMEOUTS:  # match entrypoints.py/surfaces.py exactly
                auth._mark("COVERAGE_UNKNOWN", f"parse failures reached {_MAX_PARSE_TIMEOUTS} — aborting (possible adversarial input).")
                return auth
            continue
        for mod in _program_mods(tree.root_node, data):
            found.append((mod, rel, data))

    if not found:
        auth._mark("COVERAGE_UNKNOWN",
                   "no #[program] module found — not an Anchor program (try the native reader) or it is macro-hidden.")
        return auth
    if len(found) > 1:
        auth._mark("COVERAGE_INCOMPLETE",
                   f"{len(found)} #[program] modules found (workspace?) — enumerated all; denominator spans >1 program.")
        auth.notes.append("program mods at: " + ", ".join(f"{rel}:{m.start_point[0] + 1}" for m, rel, _ in found))
        auth.dispatch_file, auth.dispatch_line = None, None  # ambiguous across >1 program
    else:
        # single program: the module location is the authority anchor (reuse the dispatch_* fields)
        auth.dispatch_file, auth.dispatch_line = found[0][1], found[0][0].start_point[0] + 1

    total_non_pub = 0
    for mod, rel, src in found:
        fns, non_pub = _pub_fns_in_mod(mod, src)
        total_non_pub += non_pub
        for name, line in fns:
            auth.instructions.append(Entrypoint(
                instruction=name, handler=name, handler_file=rel, handler_line=line,
                routed=True, arm_file=rel, arm_line=line,  # Anchor macro routes every pub fn
            ))
    _name_counts: dict[str, int] = {}
    for e in auth.instructions:
        _name_counts[e.instruction] = _name_counts.get(e.instruction, 0) + 1
    _dups = sorted(n for n, c in _name_counts.items() if c > 1)
    if _dups:
        auth.notes.append(f"duplicate instruction name(s) across enumerated programs: {_dups}")
    if total_non_pub:
        # informational only — a private OR restricted-visibility (pub(crate)/pub(super)/pub(in ..))
        # fn inside #[program] is legitimate and is NOT a routed Anchor instruction, so excluding it
        # is correct and must not degrade coverage status.
        auth.notes.append(
            f"{total_non_pub} fn(s) inside #[program] that are not bare `pub` (private or "
            f"restricted-visibility) excluded — not Anchor instructions.")
    if not auth.instructions:
        auth._mark("COVERAGE_INCOMPLETE",
                   "found #[program] but enumerated zero pub instruction fns (macro-generated?).")
    return auth


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: anchor_entrypoints.py <repo-root>", file=sys.stderr)
        return 2
    auth = extract_anchor(Path(argv[0]))
    print(json.dumps(auth.summary(), indent=2))
    print(f"\n-- instructions ({len(auth.instructions)}) --")
    for e in auth.instructions[:60]:
        print(f"  {e.instruction:<32} {e.handler_file}:{e.handler_line}")
    if not auth.authority_complete:
        print(f"\nSTATUS: {auth.status} — NOT a trustworthy complete Anchor instruction set.")
    else:
        print(f"\nOK: {len(auth.instructions)} Anchor instructions enumerated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
