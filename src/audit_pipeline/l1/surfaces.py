"""L1 bug-spot finder — enumerate every bug-prone SURFACE in a Rust repo (L1 step 1).

Reads a repo with tree-sitter and lists, BY TYPE, every place a bug typically hides:
instruction handler, authority/signer/owner check, CPI (cross-program invoke), arithmetic
op, oracle/price read, PDA derivation, account close, account realloc, account-data
deserialize/unpack, remaining_accounts iteration, account init. Each hit becomes a Surface
record (type, file, 1-indexed line, enclosing fn, short snippet) that later L1 stages turn
into bug-check hypotheses.

Design rules (carried from the L1 plan + entrypoints.py red-team, hardened by the 3-agent
review 2026-06-06):
  * COMPLETENESS-FIRST: emit EVERY candidate. The cheap FP-filter trims later; this layer
    never drops for cost. High volume is expected and fine. A MISSED surface (false negative)
    is the cardinal failure; over-detection is acceptable.
  * NEVER a silent clean/empty inventory. tree-sitter missing / files skipped / parse timeout /
    0 files / 0 parsed / a per-file or total cap hit → status COVERAGE_UNKNOWN / INCOMPLETE.
    Consumers gate on `status`/`complete`, never read "N surfaces" off a non-OK scan.
  * MACRO BLINDNESS is disclosed, not hidden: tree-sitter sees source, NOT macro-expanded
    items. Surfaces inside `#[program]`/declarative-macro expansions are not enumerated; a
    standing note says so on every report (a silent clean inventory over macro-hidden code
    is the cardinal sin).
  * Detection is NODE-WALK based (classify real AST nodes), NOT S-expression queries — those
    fail silently on grammar/version drift. One capped single-pass traversal per file bounds
    DoS (a malicious repo can't OOM/hang us via huge ASTs or millions of surfaces).
  * Reuses the hardened traversal + parse helpers from entrypoints.py (SINGLE SOURCE).

Known detector limitations (accepted — over-detection is fine, these are residual UNDER-detects
that the downstream FP-filter + L2-L7 + macro note cover; documented so they're not silent):
  * a CPI invoke reached only via a LOCAL alias (`use ...invoke as inv; inv(...)`) needs import
    resolution we don't do — missed unless the alias text contains an invoke* name;
  * classification (not detection) edges: `pyth_acct.load()` is typed deserialize (not oracle) —
    the surface IS emitted; `system_program::transfer(...)`-as-account-drain is typed cpi (not
    account_close). Downstream must not single-type-filter for completeness.
  * the per-file/total surface caps truncate by line order under DoS — flagged COVERAGE_INCOMPLETE.
  * a bare raw-pointer dereference with NO cast/call (`unsafe { *ptr }`, ptr already typed) yields
    no surface; the common form `*(p as *const T)` IS caught (the cast → arithmetic surface).

Deps: tree_sitter + tree_sitter_rust (via entrypoints.py).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from audit_pipeline.l1.entrypoints import (
    _MAX_FILE_BYTES,
    _MAX_PARSE_TIMEOUTS,
    _PARSE_TIMEOUT_S,
    _parse_with_timeout,
    _rs_files,
    _text,
)

# ---- surface-type vocabulary (kebab-case, stable; step 2 maps these to bug classes) ----
S_HANDLER = "instruction_handler"
S_AUTH = "authority_check"
S_CPI = "cpi"
S_ARITH = "arithmetic"
S_ORACLE = "oracle_read"
S_PDA = "pda_derivation"
S_CLOSE = "account_close"
S_REALLOC = "account_realloc"
S_DESERIALIZE = "deserialize"
S_REMAINING = "remaining_accounts"
S_INIT = "account_init"

ALL_SURFACE_TYPES = (
    S_HANDLER, S_AUTH, S_CPI, S_ARITH, S_ORACLE, S_PDA,
    S_CLOSE, S_REALLOC, S_DESERIALIZE, S_REMAINING, S_INIT,
)

# ---- DoS bounds (per-file AST traversal, per-file surfaces, whole-scan surfaces) ----
_MAX_AST_NODES = 2_000_000
_MAX_SURFACES_PER_FILE = 50_000
_MAX_TOTAL_SURFACES = 5_000_000
_MAX_NOTES = 5_000  # bound the notes list (problem-file floods can't OOM via notes)
_TEXT_CAP = 512  # cap callee/attr text slices so a megabyte-long path can't blow memory

# ---- trigger name sets (last-segment of a call / method name) ----
_CPI_CALLS = {"invoke", "invoke_signed", "invoke_unchecked", "invoke_signed_unchecked"}
_CPI_PATHS = ("CpiContext", "::cpi::")  # CpiContext::new[_with_signer](...), <prog>::cpi::transfer(...)
_PDA_CALLS = {"find_program_address", "create_program_address", "try_find_program_address",
              "create_program_address_unchecked"}
_DESERIALIZE_CALLS = {
    "try_from_slice", "deserialize", "deserialize_reader", "try_deserialize",
    "try_deserialize_unchecked", "unpack", "unpack_unchecked", "unpack_from_slice",
    "from_bytes", "try_from_bytes", "try_from_bytes_mut", "load", "load_mut", "load_init",
    "from_account_info", "from_account_info_mut", "cast", "cast_mut", "cast_slice",
    "pod_read_unaligned", "pod_read", "read_unaligned", "transmute", "transmute_copy",
}
_REALLOC_CALLS = {"realloc"}
_INIT_CALLS = {"create_account", "create_account_with_seed", "allocate", "assign"}
_ARITH_METHODS = {
    f"{op}_{kind}" for kind in ("add", "sub", "mul", "div", "rem", "pow", "neg",
                                "shl", "shr", "add_signed")
    for op in ("checked", "wrapping", "saturating", "overflowing", "unchecked")
} | {"pow", "sum", "product"}  # bare arithmetic reducers/exponent are overflow surfaces too
_ORACLE_HINTS = {
    "get_price", "get_price_no_older_than", "get_price_unchecked", "get_ema_price",
    "get_ema_price_no_older_than", "load_price", "load_price_feed_from_account_info",
    "latest_round_data", "latest_timestamp", "get_current_price", "price", "price_feed",
    "aggregator", "get_result", "get_value",
}
_ORACLE_PATH_HINTS = ("pyth", "switchboard", "chainlink", "oracle")
_AUTH_MACRO_NAMES = {"ensure", "access_control", "invariant", "bail", "bail_if", "only_owner"}
_AUTH_FIELDS = {"is_signer", "is_writable", "owner", "authority", "admin", "key"}
_ARITH_OPS = {"+", "-", "*", "/", "%", "<<", ">>"}  # shifts panic/wrap on out-of-range too
_ARITH_ASSIGN_OPS = {"+=", "-=", "*=", "/=", "%=", "<<=", ">>="}
_HANDLER_PARAM_HINTS = ("AccountInfo", "Context<", "accounts", "program_id", "&[AccountInfo")
_HANDLER_NAME_HINTS = ("handle_", "process_", "handler", "_ix", "instruction")

_MACRO_NOTE = ("NOTE: macro-generated code (proc-macros like #[program], declarative macros that "
               "expand to items/handlers) is NOT parsed by tree-sitter — surfaces inside macro "
               "expansions are not enumerated. Verify macro-heavy programs manually / via IDL.")


@dataclass
class Surface:
    surface_type: str
    file: str                 # repo-relative, forward-slash
    line: int                 # 1-indexed
    enclosing_fn: str | None  # the function this surface sits in (None at module scope)
    detail: str               # short classifier (callee/operator/field/keyword)
    snippet: str              # the matched source line, trimmed


@dataclass
class SurfaceReport:
    status: str = "OK"  # OK | COVERAGE_UNKNOWN | COVERAGE_INCOMPLETE
    surfaces: list[Surface] = field(default_factory=list)
    files_scanned: int = 0
    skipped_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.status == "OK"

    def _mark(self, status: str, note: str) -> None:
        # Escalate severity: UNKNOWN dominates INCOMPLETE dominates OK; never downgrade.
        order = {"OK": 0, "COVERAGE_INCOMPLETE": 1, "COVERAGE_UNKNOWN": 2}
        if status not in order:
            raise ValueError(f"_mark: unknown status {status!r}")
        if order[status] > order[self.status]:
            self.status = status
        # Bound the notes list (a repo of millions of problem files can't OOM via notes); the
        # STATUS escalation above always happens — only note accumulation stops.
        if len(self.notes) < _MAX_NOTES:
            self.notes.append(note)
        elif len(self.notes) == _MAX_NOTES:
            self.notes.append(f"... (further notes suppressed past {_MAX_NOTES})")

    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {t: 0 for t in ALL_SURFACE_TYPES}
        for s in self.surfaces:
            counts[s.surface_type] = counts.get(s.surface_type, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "status": self.status,
            "complete": self.complete,
            # total_surfaces is a coverage figure ONLY when status == OK; surfaced as None
            # otherwise so a consumer can't read "N surfaces, done" off a non-OK scan.
            "total_surfaces": len(self.surfaces) if self.status == "OK" else None,
            "raw_surface_count": len(self.surfaces),
            "by_type": self.by_type(),
            "files_scanned": self.files_scanned,
            "skipped_files": len(self.skipped_files),
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- helpers
def _line(node: Node) -> int:
    return node.start_point[0] + 1


_MAX_ENCLOSING_DEPTH = 300  # parent-walk depth bound (adversarial deep nesting — CPU DoS guard)


def _enclosing_fn(node: Node, src: bytes) -> str | None:
    """Name of the nearest enclosing `fn` (None at module scope). Iterative parent walk, depth-
    and text-bounded so an adversarial deeply-nested AST can't make this O(depth) per surface."""
    cur = node.parent
    depth = 0
    while cur is not None and depth < _MAX_ENCLOSING_DEPTH:
        if cur.type == "function_item":
            nm = cur.child_by_field_name("name")
            return _node_text_capped(nm, src) if nm is not None else None
        cur = cur.parent
        depth += 1
    return None


def _node_text_capped(node: Node | None, src: bytes) -> str:
    """Text of a node, capped at _TEXT_CAP bytes (an adversarial repo can write a 1MB path)."""
    if node is None:
        return ""
    end = min(node.end_byte, node.start_byte + _TEXT_CAP)
    return src[node.start_byte:end].decode("utf-8", "replace").strip()


def _callee_name(call: Node, src: bytes) -> str | None:
    """Last-segment name of a call's target: free fn (`a::b()` -> b), method (`x.m()` -> m),
    turbofish (`f::<T>()` / `x.m::<T>()` -> f / m — unwrap the generic_function wrapper)."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    while fn is not None and fn.type == "parenthesized_expression":  # (invoke)(...) / ((f))(...)
        fn = next(iter(fn.named_children), None)
    if fn is None:
        return None
    if fn.type == "generic_function":  # foo::<T>() / a.b::<T>() — unwrap to the callee
        inner = fn.child_by_field_name("function")
        if inner is not None:
            fn = inner
    if fn.type == "field_expression":  # method call: receiver.method(...)
        f = fn.child_by_field_name("field")
        return _node_text_capped(f, src) if f is not None else None
    return _node_text_capped(fn, src).split("::")[-1].split("<")[0].strip()


def _snippet(src_lines: list[str], line: int) -> str:
    if 1 <= line <= len(src_lines):
        return src_lines[line - 1].strip()[:200]
    return ""


def _mk(stype: str, rel: str, node: Node, src: bytes, src_lines: list[str], detail: str) -> Surface:
    ln = _line(node)
    return Surface(
        surface_type=stype, file=rel, line=ln,
        enclosing_fn=_enclosing_fn(node, src), detail=detail[:120],
        snippet=_snippet(src_lines, ln),
    )


# ----------------------------------------------------- single-pass capped node collection
_WANTED_TYPES = (
    "call_expression", "binary_expression", "compound_assignment_expr",
    "type_cast_expression", "macro_invocation", "field_expression",
    "attribute_item", "assignment_expression", "function_item", "index_expression",
)


def _collect(root: Node, cap: int) -> tuple[dict[str, list[Node]], bool]:
    """ONE iterative traversal → nodes bucketed by type. Returns (buckets, truncated). Bails at
    `cap` nodes so an adversarial AST can't make the per-file scan run unbounded (DoS)."""
    buckets: dict[str, list[Node]] = {t: [] for t in _WANTED_TYPES}
    stack = [root]
    n = 0
    while stack:
        node = stack.pop()
        n += 1
        if n > cap:
            return buckets, True
        b = buckets.get(node.type)
        if b is not None:
            b.append(node)
        stack.extend(node.children)
        if len(stack) > cap:  # bound peak stack: a single huge-fanout node (millions of children)
            return buckets, True
    return buckets, False


# --------------------------------------------------------------------------- detectors
def _detect_handlers(buckets, rel, src, lines, out):
    for fn in buckets["function_item"]:
        nm = fn.child_by_field_name("name")
        if nm is None:
            continue
        name = _node_text_capped(nm, src)  # capped: an adversarial 8MB identifier can't OOM us
        params = fn.child_by_field_name("parameters")
        ptext = _node_text_capped(params, src) if params is not None else ""
        if any(h in ptext for h in _HANDLER_PARAM_HINTS) or any(h in name for h in _HANDLER_NAME_HINTS):
            out.append(_mk(S_HANDLER, rel, fn, src, lines, name))


def _detect_authority(buckets, rel, src, lines, out):
    for fe in buckets["field_expression"]:
        f = fe.child_by_field_name("field")
        fname = _node_text_capped(f, src) if f is not None else ""
        if fname in _AUTH_FIELDS:
            out.append(_mk(S_AUTH, rel, fe, src, lines, "." + fname))
    for m in buckets["macro_invocation"]:
        mac = m.child_by_field_name("macro")
        mname = (_node_text_capped(mac, src).split("::")[-1] if mac is not None else "")
        if mname.startswith("require") or mname.startswith("assert") or mname in _AUTH_MACRO_NAMES:
            out.append(_mk(S_AUTH, rel, m, src, lines, mname + "!"))
    for at in buckets["attribute_item"]:
        t = _node_text_capped(at, src)
        if "account(" in t.replace(" ", "") and any(
            k in t for k in ("signer", "has_one", "constraint", "address", "owner")
        ):
            out.append(_mk(S_AUTH, rel, at, src, lines, "#[account(...)]"))


def _detect_calls(buckets, rel, src, lines, out):
    """One pass over call_expression nodes → CPI / PDA / deserialize / realloc / init / oracle /
    checked-arith-method surfaces. (Nested calls are in the bucket too, so `token::transfer(
    CpiContext::new(...))` yields a CPI surface via the inner CpiContext::new call.)"""
    for c in buckets["call_expression"]:
        name = _callee_name(c, src)
        if not name:
            continue
        full = _node_text_capped(c.child_by_field_name("function"), src)
        full_l = full.lower()
        if name in _CPI_CALLS or any(p in full for p in _CPI_PATHS):
            out.append(_mk(S_CPI, rel, c, src, lines, full))
        elif name in _PDA_CALLS:
            out.append(_mk(S_PDA, rel, c, src, lines, name))
        elif name in _REALLOC_CALLS:
            out.append(_mk(S_REALLOC, rel, c, src, lines, name))
        elif name in _INIT_CALLS:
            out.append(_mk(S_INIT, rel, c, src, lines, name))
        elif name in _DESERIALIZE_CALLS:
            out.append(_mk(S_DESERIALIZE, rel, c, src, lines, name))
        elif name in _ARITH_METHODS:
            out.append(_mk(S_ARITH, rel, c, src, lines, name))
        elif name in _ORACLE_HINTS or any(h in full_l for h in _ORACLE_PATH_HINTS):
            out.append(_mk(S_ORACLE, rel, c, src, lines, name))


def _detect_arithmetic(buckets, rel, src, lines, out):
    """Raw `+ - * / %`, compound assigns, `as` casts, and array indexing — overflow/truncation/
    bounds surfaces."""
    for b in buckets["binary_expression"]:
        op = b.child_by_field_name("operator")
        op_t = _node_text_capped(op, src) if op is not None else ""
        if op_t in _ARITH_OPS:
            out.append(_mk(S_ARITH, rel, b, src, lines, op_t))
    for a in buckets["compound_assignment_expr"]:
        op = a.child_by_field_name("operator")
        op_t = _node_text_capped(op, src) if op is not None else ""
        if op_t in _ARITH_ASSIGN_OPS:
            out.append(_mk(S_ARITH, rel, a, src, lines, op_t))
    for c in buckets["type_cast_expression"]:
        ty = c.child_by_field_name("type")
        out.append(_mk(S_ARITH, rel, c, src, lines, "as " + (_node_text_capped(ty, src) if ty is not None else "")))
    for ix in buckets["index_expression"]:
        out.append(_mk(S_ARITH, rel, ix, src, lines, "index[]"))


def _detect_close(buckets, rel, src, lines, out):
    """Account closing / lamport draining: ANY write to a `lamports` lvalue (`= 0`, `= x`,
    `+=`, `-=`), `.close(`/`.close_account(`, Anchor `#[account(close = ...)]`."""
    for a in buckets["assignment_expression"]:
        left = a.child_by_field_name("left")
        if left is not None and "lamports" in _node_text_capped(left, src).lower():
            out.append(_mk(S_CLOSE, rel, a, src, lines, "lamports = ..."))
    for a in buckets["compound_assignment_expr"]:
        left = a.child_by_field_name("left")
        if left is not None and "lamports" in _node_text_capped(left, src).lower():
            out.append(_mk(S_CLOSE, rel, a, src, lines, "lamports +=/-= ..."))
    for c in buckets["call_expression"]:
        if _callee_name(c, src) in ("close", "close_account"):
            out.append(_mk(S_CLOSE, rel, c, src, lines, "close()"))
    for at in buckets["attribute_item"]:
        t = _node_text_capped(at, src).replace(" ", "")
        if "account(" in t and "close=" in t:
            out.append(_mk(S_CLOSE, rel, at, src, lines, "#[account(close=...)]"))


def _detect_init_attr(buckets, rel, src, lines, out):
    """Anchor `#[account(init...)]` / `init_if_needed` — account-creation surfaces."""
    for at in buckets["attribute_item"]:
        t = _node_text_capped(at, src).replace(" ", "")
        if "account(" in t and ("init," in t or "init)" in t or "init_if_needed" in t):
            out.append(_mk(S_INIT, rel, at, src, lines, "#[account(init...)]"))


def _detect_pda_attr(buckets, rel, src, lines, out):
    """Anchor `#[account(seeds = [...], bump)]` — PDA derivation done at macro-expansion time,
    so it never appears as a find_program_address CALL (a pure-Anchor PDA gap, red-team R1)."""
    for at in buckets["attribute_item"]:
        t = _node_text_capped(at, src).replace(" ", "")
        if "account(" in t and "seeds=" in t:
            out.append(_mk(S_PDA, rel, at, src, lines, "#[account(seeds=...)]"))


def _detect_remaining_accounts(buckets, rel, src, lines, out):
    """Any access to `remaining_accounts` (manual account iteration is a classic bug surface)."""
    for fe in buckets["field_expression"]:
        f = fe.child_by_field_name("field")
        if f is not None and _node_text_capped(f, src) == "remaining_accounts":
            out.append(_mk(S_REMAINING, rel, fe, src, lines, ".remaining_accounts"))


_DETECTORS = (
    _detect_handlers, _detect_authority, _detect_calls, _detect_arithmetic,
    _detect_close, _detect_init_attr, _detect_pda_attr, _detect_remaining_accounts,
)


def scan_tree(rel: str, src: bytes, tree) -> tuple[list[Surface], bool]:
    """Run every detector over one parsed file (single capped traversal). Returns
    (surfaces, truncated) — truncated=True if the AST-node or per-file surface cap was hit."""
    # per-line width cap so a single multi-MB line can't make snippet slicing materialize huge
    # strings (DoS); snippet itself is further trimmed to 200 in _snippet.
    src_lines = [ln[:512] for ln in src.decode("utf-8", "replace").splitlines()]
    buckets, truncated = _collect(tree.root_node, _MAX_AST_NODES)
    out: list[Surface] = []
    for det in _DETECTORS:
        det(buckets, rel, src, src_lines, out)
    out.sort(key=lambda s: (s.line, s.surface_type, s.detail))
    if len(out) > _MAX_SURFACES_PER_FILE:
        out = out[:_MAX_SURFACES_PER_FILE]
        truncated = True
    return out, truncated


def extract_surfaces(root: Path) -> SurfaceReport:
    """Enumerate every bug-prone surface under `root`. Coverage-safe: any skip/timeout/parse
    failure/cap-hit escalates `status`; an all-fail or empty scan is COVERAGE_UNKNOWN, never a
    silent clean inventory. A standing macro-blindness note is on every report."""
    root = Path(root)
    rep = SurfaceReport()
    rep.notes.append(_MACRO_NOTE)  # on EVERY return path (disclose macro blindness, never silent)

    files, skipped = _rs_files(root)
    rep.skipped_files = list(skipped)
    if skipped:
        rep._mark("COVERAGE_INCOMPLETE", f"{len(skipped)} file(s) refused for safety (symlink/escape).")
    if not files:
        rep._mark("COVERAGE_UNKNOWN", "no .rs files enumerated under root — nothing scanned (empty/unreadable/non-Rust repo?).")
        return rep

    parse_failures = 0
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        try:
            data = p.read_bytes()
        except OSError:
            rep.skipped_files.append(rel + " (unreadable at read time)")
            rep._mark("COVERAGE_INCOMPLETE", f"{rel}: unreadable at read time — not scanned.")
            continue
        if len(data) > _MAX_FILE_BYTES:
            rep.skipped_files.append(rel + " (>8MB)")
            rep._mark("COVERAGE_INCOMPLETE", f"{rel}: skipped (>8MB) — not scanned.")
            continue
        tree = _parse_with_timeout(data)
        if tree is None:
            parse_failures += 1
            rep.skipped_files.append(rel + " (parse timeout/error)")
            rep._mark("COVERAGE_INCOMPLETE", f"{rel}: parse exceeded {_PARSE_TIMEOUT_S}s or failed — not scanned.")
            if parse_failures >= _MAX_PARSE_TIMEOUTS:
                rep._mark("COVERAGE_UNKNOWN", f"aborted: {parse_failures} files failed/timed out parsing (possible adversarial input) — scan not trustworthy.")
                return rep
            continue
        surfaces, truncated = scan_tree(rel, data, tree)
        if truncated:
            rep.skipped_files.append(rel + " (AST/surface cap)")
            rep._mark("COVERAGE_INCOMPLETE", f"{rel}: AST-node/surface cap hit — only a partial surface set for this file.")
        rep.files_scanned += 1
        rep.surfaces.extend(surfaces)
        if len(rep.surfaces) > _MAX_TOTAL_SURFACES:
            rep._mark("COVERAGE_UNKNOWN", f"total surface cap {_MAX_TOTAL_SURFACES} exceeded — remaining files not scanned (possible adversarial input).")
            return rep

    if rep.files_scanned == 0:
        rep._mark("COVERAGE_UNKNOWN", "0 files parsed successfully — scan not trustworthy.")
    return rep


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: surfaces.py <repo-root>", file=sys.stderr)
        return 2
    rep = extract_surfaces(Path(argv[0]))
    print(json.dumps(rep.summary(), indent=2))
    print(f"\n-- first surfaces (of {len(rep.surfaces)}) --")
    for s in rep.surfaces[:40]:
        loc = f"{s.file}:{s.line}"
        print(f"  {s.surface_type:<20} {loc:<48} {s.detail}")
    if not rep.complete:
        print(f"\nSTATUS: {rep.status} — scan is NOT a trustworthy complete inventory. See notes.")
    else:
        print(f"\nOK: {len(rep.surfaces)} surfaces across {rep.files_scanned} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
