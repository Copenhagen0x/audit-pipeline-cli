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
  * Anchor account-field detector (_detect_anchor_account_fields) — accepted residuals (all
    OVER-detect or metadata-only; never a dropped surface):
      - UncheckedAccount/AccountInfo are matched by SUBSTRING (a false "unchecked" is harmless
        over-detection); a custom type whose NAME merely contains those strings over-fires
        S_UNCHECKED — L2 confirms. signer/infra/account are matched on the WRAPPER identifier (the
        name before `<`), so a generic parameter like `ProgramState`/`FeeSignerConfig` can NOT
        misclassify-and-skip the field.
      - a TYPE ALIAS of a raw account (`type RawVault = AccountInfo`) is unrecognized → surfaced as
        S_ACCOUNT `kind=unknown` (never dropped) rather than S_UNCHECKED. candidates.py ALSO attaches
        the raw-account bug classes (unchecked-account / missing-owner-check) for kind=unknown, so
        those checks still fire — no cross-file alias resolution, but no lost coverage.
      - `signer` counts as binding: it proves the account SIGNED, not that it equals a stored
        authorized key — `bound=True` here means "authenticated", not "identity-pinned to state".
      - `seeds` binds only WITH `bump`; `constraint=<expr>`, `owner=`, `token::mint`, `mint::*`,
        and `associated_token::mint` alone do NOT suppress (they restrict a property, not which
        account), so an unbound writable account still surfaces. CHECK-doc detection requires the
        Anchor `CHECK:` token (colon); a turbofish with internal commas in a constraint expr may
        over-split into harmless extra entries (binding keys always lead their entry, so none is
        missed).

Deps: tree_sitter + tree_sitter_rust (via entrypoints.py).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from audit_pipeline.l1.entrypoints import (
    _MAX_FILE_BYTES,
    _MAX_PARSE_TIMEOUTS,
    _PARSE_TIMEOUT_S,
    _parse_with_timeout,
    _rs_files,
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
S_UNCHECKED = "unchecked_account"   # raw UncheckedAccount/AccountInfo field in an Accounts struct
S_ACCOUNT = "account_constraint"    # writable Anchor account field with no identity/authority tie

ALL_SURFACE_TYPES = (
    S_HANDLER, S_AUTH, S_CPI, S_ARITH, S_ORACLE, S_PDA,
    S_CLOSE, S_REALLOC, S_DESERIALIZE, S_REMAINING, S_INIT,
    S_UNCHECKED, S_ACCOUNT,
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
        counts: dict[str, int] = dict.fromkeys(ALL_SURFACE_TYPES, 0)
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
    "struct_item",
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


# ---- Anchor account-context field analysis (covers what the attribute detectors can't) ----
# The existing attribute detectors only fire on constraints that are PRESENT (has_one / seeds /
# init / close ...). The dangerous Anchor cases are about what is ABSENT: an UncheckedAccount /
# AccountInfo with no owner/key check, or a writable Account with no identity/authority tie. Those
# fields emit nothing above, so they were invisible. This detector enumerates the FIELDS of every
# `#[derive(Accounts)]` struct and surfaces the missing-constraint cases (completeness-first).
_MAX_FIELDS_PER_STRUCT = 4_000   # DoS bound: an adversarial struct can't make this unbounded
_MAX_ATTR_LOOKBACK = 256         # DoS bound on the preceding-attribute sibling walk (real structs
                                 # rarely stack >20 attrs; 256 leaves wide headroom before giving up)
_ATTR_TEXT_CAP = 8192            # attribute text can be long (multi-seed PDAs + constraint exprs);
                                 # read up to 8KB so a late binding key isn't truncated (still a DoS
                                 # bound — an attribute past 8KB is adversarial, not real Anchor)
_DERIVE_ACCOUNTS_RE = re.compile(r"\bAccounts\b")
_CHECK_DOC_RE = re.compile(r"\bCHECK:")  # Anchor honors EXACTLY `CHECK:` — colon REQUIRED, NO space
                                         # before it (`CHECK :` is not honored), case-SENSITIVE
                                         # (uppercase). Bare/lowercase 'check'/'checkbox'/'CHECK :'
                                         # must NOT match — else a repo could spoof check_doc=True to
                                         # down-weight an unvalidated account.

# Anchor account WRAPPER types, matched on the wrapper IDENTIFIER (the name immediately before a
# `<`), NOT a raw substring — so a generic PARAMETER named e.g. `FeeSignerConfig` or `ProgramState`
# can never misclassify the field (that was a false-negative: the field got skipped as a "signer"
# or "infra" primitive). UncheckedAccount/AccountInfo are matched by substring on purpose: a false
# "unchecked" is harmless over-detection, whereas a false "signer"/"infra" SKIP is the cardinal
# under-detection failure. Box/Option/Cow wrappers are transparent (the inner Anchor wrapper is
# also captured, since it too is followed by `<`).
_SIGNER_WRAPPERS = frozenset({"Signer"})
_INFRA_WRAPPERS = frozenset({"Program", "Sysvar", "Interface"})  # protocol-fixed addresses -> skip
_ACCOUNT_WRAPPERS = frozenset({  # checked + writable matters; SystemAccount/LazyAccount included
    "Account", "AccountLoader", "InterfaceAccount", "SystemAccount", "LazyAccount"})
_WRAPPER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*<")  # identifiers used as generic wrappers
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")  # bare type name (no generics)

# Account-constraint KEYS that genuinely FIX which account is passed (address-equality, PDA-seed
# derivation, signer authentication, has_one link to a stored key, ATA/token-owner derivation).
# These are matched as the KEY of a TOP-LEVEL `#[account(...)]` entry (the token before `=`), NEVER
# as a substring of the whole attribute — so a binding word inside a `constraint = <expr>` value
# (e.g. `ctx.accounts.signer.key()`) can never falsely suppress a surface (that was a cardinal
# false-negative + an adversarial silence vector). Keys that restrict a PROPERTY without fixing
# identity are deliberately EXCLUDED (completeness-first):
#   * `constraint = <expr>` — arbitrary logic; may enforce nothing about identity.
#   * `owner = <program>`   — fixes the owner PROGRAM, not which account (still substitutable).
#   * `token::mint` / `mint::*` — fix the mint, not the token-account OWNER (recipient still free).
# EXACT top-level entry keys that fix identity. Exact match (not substring/prefix) so neither a
# binding word inside a `constraint=<expr>` value NOR a look-alike key (`token::authority_bump`,
# `associated_token::mint`) can falsely suppress a surface. `associated_token::mint` alone fixes the
# mint but NOT the owner (recipient still substitutable) → deliberately absent. `seeds` fixes
# identity only together with `bump` (Anchor verifies the derived PDA only when both are present),
# handled in _account_constraints.
_BINDING_KEYS = frozenset({"has_one", "address",
                           "token::authority", "associated_token::authority"})
# `signer` is intentionally NOT a binding key: it proves the account SIGNED, not that it equals a
# stored authorized key (the classic wrong-signer authorization bypass). It is reported as a
# SEPARATE `signer=` flag in the surface detail so L2 sees "authenticated" distinct from
# "identity-pinned"; a signer-only writable account still surfaces (completeness-first).


def _attr_is_derive_accounts(node: Node, src: bytes) -> bool:
    if node.type != "attribute_item":
        return False
    # read with the larger attribute cap: a long derive list could push `Accounts` past 512 bytes,
    # and a truncated read would silently drop the whole struct (under-detection).
    end = min(node.end_byte, node.start_byte + _ATTR_TEXT_CAP)
    # strip invisibles too: a zero-width char inside `derive`/`Accounts` must not defeat the gate
    # (otherwise the WHOLE struct goes undetected — the cardinal failure).
    t = _strip_invisible(src[node.start_byte:end].decode("utf-8", "replace"))
    return "derive" in t and _DERIVE_ACCOUNTS_RE.search(t) is not None


def _is_accounts_struct(struct_node: Node, src: bytes) -> bool:
    """True if `struct_node` is decorated with `#[derive(..., Accounts, ...)]`. Handles BOTH grammar
    models (mirroring anchor_entrypoints._item_has_program_attr): the attribute as a CHILD of the
    struct node, AND the common model where outer attributes are PRECEDING SIBLINGS. Without the
    child check, a tree-sitter-rust version that nests outer attributes under the item would make the
    whole detector silently emit nothing — the cardinal under-detection failure."""
    for c in struct_node.children:  # grammar variation: attribute modeled as a child of the struct
        if _attr_is_derive_accounts(c, src):
            return True
    # Light separators that CANNOT own a `#[derive(...)]` (skip past them) + comments + ERROR.
    _skip = ("line_comment", "block_comment", "doc_comment", "outer_doc_comment_marker",
             "inner_doc_comment_marker", "ERROR", "use_declaration", "const_item", "type_item",
             "static_item", "extern_crate_declaration")
    cur = struct_node.prev_named_sibling  # common model: attribute is a preceding sibling
    hops = 0
    while cur is not None and hops < _MAX_ATTR_LOOKBACK:
        end = min(cur.end_byte, cur.start_byte + _ATTR_TEXT_CAP)
        txt = _strip_invisible(src[cur.start_byte:end].decode("utf-8", "replace"))
        if txt.startswith("#["):
            # ANY attribute (matched by TEXT, not node type) is transparent — robust to a grammar
            # that models `#[instruction(...)]` as a non-`attribute_item` node. Check it for derive.
            if "derive" in txt and _DERIVE_ACCOUNTS_RE.search(txt):
                return True
        elif cur.type not in _skip:
            break  # a HEAVY item (struct/fn/impl/enum/mod) — it OWNS the derive, so the run ends.
        cur = cur.prev_named_sibling
        hops += 1
    return False


def _field_decl_list(struct_node: Node) -> Node | None:
    for ch in struct_node.named_children:
        if ch.type == "field_declaration_list":
            return ch
    return None


def _anchor_kind(type_text: str) -> str | None:
    """Classify a field's Anchor account type → 'unchecked' | 'signer' | 'infra' | 'account' | None.

    Raw accounts (UncheckedAccount/AccountInfo) are matched by SUBSTRING — a false positive here is
    harmless over-detection. signer/infra/account are matched on the WRAPPER identifier set (names
    immediately before a `<`, or the bare type name when there are no generics), so a generic
    PARAMETER like `ProgramState` / `FeeSignerConfig` can NEVER misclassify the field as a primitive
    and get it skipped (that was the cardinal under-detection failure). `None` = unrecognized type
    (e.g. a type alias); the caller surfaces a writable unrecognized field rather than skipping it.
    SystemAccount/LazyAccount count as 'account' (Anchor does not pin their address, so a writable
    unbound one is substitutable). Program/Sysvar/Interface are true infra (protocol-fixed)."""
    if "UncheckedAccount" in type_text or "AccountInfo" in type_text:
        return "unchecked"
    wrappers = set(_WRAPPER_RE.findall(type_text))
    if not wrappers:  # bare type name with no generics (e.g. a type alias) — use the trailing ident
        m = _TRAILING_IDENT_RE.search(type_text.strip())
        if m:
            wrappers = {m.group(1)}
    if wrappers & _SIGNER_WRAPPERS:
        return "signer"
    if wrappers & _ACCOUNT_WRAPPERS:
        return "account"
    if wrappers & _INFRA_WRAPPERS:
        return "infra"
    return None


def _field_name_and_type(field_node: Node, src: bytes) -> tuple[str, str]:
    name_node = field_node.child_by_field_name("name")
    type_node = field_node.child_by_field_name("type")
    if name_node is None:  # robustness if the grammar field name ever drifts
        name_node = next((c for c in field_node.named_children if c.type == "field_identifier"), None)
    if type_node is None:
        type_node = next(
            (c for c in field_node.named_children
             if c.type in ("generic_type", "type_identifier", "reference_type",
                           "scoped_type_identifier")),
            None,
        )
    name = _node_text_capped(name_node, src) if name_node is not None else "?"
    type_text = _node_text_capped(type_node, src) if type_node is not None else ""
    return name, type_text


def _is_check_doc(node: Node, src: bytes) -> bool:
    """True for an Anchor `/// CHECK:` doc — a `///` LINE doc comment (the only form Anchor honors)
    whose text contains the `CHECK:` token. Detection is by the `///` SIGIL IN THE TEXT only, never
    by node-type/marker children: routing on node type alone let a `// CHECK:` plain comment (with a
    doc-marker child on some grammars) OR a `/** CHECK: */` block comment (modeled as `doc_comment`
    on some grammars) forge check_doc=True and down-weight an unvalidated account in L2. The text
    sigil is the ground truth on the pinned tree-sitter-rust grammar (verified)."""
    # read with the larger cap: a real `/// CHECK:` rationale can run past 512 bytes, and a
    # truncated read would drop the token and mislabel a checked account as unchecked.
    end = min(node.end_byte, node.start_byte + _ATTR_TEXT_CAP)
    # Verify the `///` sigil on the RAW text (only leading whitespace trimmed). Do NOT strip
    # invisibles/whitespace before this check: stripping would FUSE `// /CHECK:` or `////CHECK:`
    # into `///CHECK:` and forge check_doc=True on a comment Anchor does not honor. Require EXACTLY
    # three slashes. The CHECK: token is matched on raw text — an invisible char between CHECK and
    # `:` just yields check_doc=False (conservative; the surface is emitted regardless).
    text = src[node.start_byte:end].decode("utf-8", "replace")
    s = text.lstrip()
    if not (s.startswith("///") and not s.startswith("////")):  # only a 3-slash line doc qualifies
        return False
    return _CHECK_DOC_RE.search(text) is not None


def _field_account_attr(field_node: Node, src: bytes) -> str:
    """Grammar-variant fallback: some tree-sitter-rust versions nest a field's `#[account(...)]`
    attribute as a CHILD of the field_declaration rather than as a preceding sibling in
    field_declaration_list. Scan the field's own children too so the mut/binding analysis is never
    lost (the cardinal failure) — mirrors _field_has_check_doc for the CHECK doc."""
    parts = []
    for c in field_node.children:
        if c.type == "attribute_item":
            end = min(c.end_byte, c.start_byte + _ATTR_TEXT_CAP)
            at = _strip_invisible(src[c.start_byte:end].decode("utf-8", "replace"))
            if at.startswith("#[account("):
                parts.append(at)
    return "".join(parts)


def _field_has_check_doc(field_node: Node, src: bytes) -> bool:
    """A `/// CHECK:` doc placed BETWEEN the `#[account(...)]` attr and the field (Anchor's other
    accepted placement, used in darkdrop's initialize*.rs) can be modeled by some grammar versions
    as a CHILD of the field_declaration rather than a sibling in field_declaration_list — scan the
    field's own children too so check_doc metadata stays correct under either model."""
    return any(
        c.type in ("line_comment", "doc_comment",
                   "outer_doc_comment_marker", "inner_doc_comment_marker") and _is_check_doc(c, src)
        for c in field_node.children
    )


def _strip_invisible(s: str) -> str:
    """Drop ALL whitespace AND Unicode format/control characters (categories Cf/Cc — zero-width
    space U+200B, RLM/LRM, BOM, etc.). Plain `\\s` does NOT cover the zero-width/format chars, so an
    attacker could prefix `#[account(...)]` with one to defeat the `startswith('#[account(')` check
    and silently suppress a surface (under-detection). The text is already cap-bounded by the
    caller, so the per-char pass is cheap."""
    return "".join(c for c in s if not c.isspace() and unicodedata.category(c) not in ("Cf", "Cc"))


def _account_inner(flat: str):
    """Yield the paren-matched inside of each `#[account(...)]` block in the (whitespace-stripped)
    attribute text. Only the Anchor `account` attribute is matched (not `#[my_account(..)]`)."""
    i = 0
    needle = "#[account("
    while True:
        j = flat.find(needle, i)
        if j < 0:
            return
        k = j + len(needle)
        depth = 1
        start = k
        while k < len(flat) and depth > 0:
            c = flat[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            k += 1
        yield flat[start:k - 1]
        i = k


def _split_top_level(inner: str) -> list[str]:
    """Split an `account(...)` inner string on DEPTH-0 commas, tracking ()/[]/{} nesting (NOT <>,
    which would corrupt on `<`/`>` comparison operators inside constraint expressions). Commas
    inside a `seeds = [...]` array or a `foo(a, b)` call stay within their entry."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in inner:
        if c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth = max(0, depth - 1)  # clamp: a malformed unbalanced ')' can't drive depth negative
            cur.append(c)
        elif c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        out.append("".join(cur))
    return out


def _entry_key(entry: str) -> str:
    """The KEY of an `account(...)` entry: the leading identifier path before `=` (or the whole bare
    token, e.g. `mut` / `signer`). Driving the binding check off this — never a raw substring —
    means a binding word inside a `constraint = <expr>` VALUE can't falsely mark the field bound."""
    lhs = entry.split("=", 1)[0]
    m = re.match(r"([A-Za-z_][A-Za-z0-9_:]*)", lhs)
    return m.group(1) if m else ""


def _account_constraints(attr_text: str) -> tuple[bool, bool, bool, bool]:
    """Parse the accumulated `#[account(...)]` text into (is_mut, is_bound, is_signer, has_constraint)
    from the KEYS of the top-level entries. is_bound is True when an identity-FIXING key is present
    (_BINDING_KEYS) or when `seeds` AND `bump` are both present. is_signer / has_constraint are
    reported separately for L2 context (a `signer` authenticates but doesn't pin identity; a
    `constraint = <expr>` may or may not bind, so it doesn't suppress but L2 should see it)."""
    flat = _strip_invisible(attr_text)
    keys: set[str] = set()
    for inner in _account_inner(flat):
        for entry in _split_top_level(inner):
            keys.add(_entry_key(entry))
    # `init`/`init_if_needed`/`close` all create or destroy+drain the account, so they imply writable
    # even without an explicit `mut` (an init-without-PDA or close-without-binding account is a real
    # surface that a bare `mut`-only check would miss).
    is_mut = bool(keys & {"mut", "init", "init_if_needed", "close"})
    # `seeds` pins identity ONLY together with `bump` (Anchor verifies the derived PDA only then);
    # `bump` alone is NOT binding and must never be added to _BINDING_KEYS.
    bound = bool(keys & _BINDING_KEYS) or ("seeds" in keys and "bump" in keys)
    is_signer = "signer" in keys
    has_constraint = "constraint" in keys  # a `constraint = <expr>` is present (may or may not bind)
    return is_mut, bound, is_signer, has_constraint


def _emit_account_field(field_node, attr_text, has_check, rel, src, lines, out):
    name, type_text = _field_name_and_type(field_node, src)
    kind = _anchor_kind(type_text)
    if kind in ("signer", "infra"):
        return  # signers / programs / sysvars / interfaces are the safe, address-fixed primitives
    # combine sibling-accumulated attrs with any nested under the field node (grammar variants).
    is_mut, bound, is_signer, has_constraint = _account_constraints(
        attr_text + _field_account_attr(field_node, src))
    if kind == "unchecked":
        # ALWAYS surface a raw account — Anchor does NO owner/type/key validation on it; the CHECK
        # doc + binding/signer/constraint flags are recorded so L2 can judge whether they suffice.
        out.append(_mk(S_UNCHECKED, rel, field_node, src, lines,
                       f"UncheckedAccount/AccountInfo `{name}` mut={is_mut} check_doc={has_check} "
                       f"bound={bound} signer={is_signer} constraint={has_constraint}"))
        return
    # A recognized Account/AccountLoader/InterfaceAccount is Anchor-validated when read-only, so it
    # surfaces only when WRITABLE + unbound. An UNRECOGNIZED/aliased type (kind is None) might be a
    # raw AccountInfo behind a `type` alias — Anchor does no owner/type check on those even when the
    # address is PDA-bound, so it is ALWAYS surfaced (never silently dropped, regardless of bound),
    # completeness-first. Cross-file alias resolution (is it really raw?) is L2's job.
    emit = (is_mut and not bound) if kind == "account" else True
    if emit:
        label = type_text[:48] if kind == "account" else f"{type_text[:40]} kind=unknown"
        out.append(_mk(S_ACCOUNT, rel, field_node, src, lines,
                       f"mut={is_mut} {label} `{name}` no-auth-constraint "
                       f"bound={bound} signer={is_signer} constraint={has_constraint}"))


def _detect_anchor_account_fields(buckets, rel, src, lines, out):
    """Enumerate fields of every `#[derive(Accounts)]` struct and surface the missing-constraint
    cases: raw UncheckedAccount/AccountInfo (no Anchor validation) and writable accounts with no
    identity/authority tie. Pairs each field with its preceding `#[account(...)]` + `/// CHECK` doc
    the way Rust associates outer attributes/docs with the following item."""
    for st in buckets["struct_item"]:
        if not _is_accounts_struct(st, src):
            continue
        flist = _field_decl_list(st)
        if flist is None:
            continue
        pending_attr = ""
        has_check = False
        nfields = 0
        for ch in flist.children:
            ct = ch.type
            if ct == "attribute_item":
                # read with the larger attribute cap so a late binding key in a long multi-seed/
                # constraint attribute isn't truncated (which would over-fire S_ACCOUNT); strip
                # invisibles so a zero-width char before `#` can't defeat the prefix check.
                end = min(ch.end_byte, ch.start_byte + _ATTR_TEXT_CAP)
                at_flat = _strip_invisible(src[ch.start_byte:end].decode("utf-8", "replace"))
                # cap accumulation at 2x the single-attr cap: real Anchor #[account(...)] blocks are
                # tiny, but this still admits a second stacked binding attr after one large block (so
                # an oversized first attr can't drop a `has_one` in the next) while bounding DoS.
                # A NON-account attribute (#[cfg]/#[allow]/...) does NOT reset has_check: in Rust a
                # `/// CHECK:` doc and an interleaved attribute BOTH bind to the same following field,
                # so the CHECK genuinely documents that field (a field_declaration always resets
                # has_check, so it can never bleed PAST the field it precedes).
                if at_flat.startswith("#[account(") and len(pending_attr) + len(at_flat) <= _ATTR_TEXT_CAP * 2:
                    pending_attr += at_flat
            elif ct in ("line_comment", "doc_comment",
                        "outer_doc_comment_marker", "inner_doc_comment_marker"):
                if _is_check_doc(ch, src):
                    has_check = True
            elif ct == "block_comment":
                # a /* */ comment is benign (NOT an Anchor CHECK doc) — keep BOTH pending_attr and
                # has_check: a real `/// CHECK:` placed before it still applies to the next field.
                pass
            elif ct == "field_declaration":
                nfields += 1
                if nfields > _MAX_FIELDS_PER_STRUCT:
                    break
                # OR-in a CHECK doc that the grammar nested under the field node (doc-after-attr).
                _emit_account_field(ch, pending_attr,
                                    has_check or _field_has_check_doc(ch, src), rel, src, lines, out)
                pending_attr = ""
                has_check = False
            else:
                # Punctuation / ERROR-recovery / any unexpected node. KEEP pending_attr — an outer
                # attr belongs to the FOLLOWING field, so a parse-error token between `#[account(mut)]`
                # and the field must NOT drop the `mut` (missing that surface is the cardinal failure).
                # But RESET has_check: a `/// CHECK:` separated from its field by a parse-error/unknown
                # node is untrustworthy and must not bleed forward as a forged check_doc=True (a
                # benign block_comment is handled above, so only genuinely-unexpected nodes reset).
                # (Non-`#[account(` attributes are handled in the attribute_item branch and do NOT
                # reset has_check — a `/// CHECK:` above a `#[cfg(..)]` still applies to the field.)
                has_check = False


_DETECTORS = (
    _detect_handlers, _detect_authority, _detect_calls, _detect_arithmetic,
    _detect_close, _detect_init_attr, _detect_pda_attr, _detect_remaining_accounts,
    _detect_anchor_account_fields,
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
