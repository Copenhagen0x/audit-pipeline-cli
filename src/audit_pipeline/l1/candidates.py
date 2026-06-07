"""L1 surface labeler — turn each bug-spot into the specific bug-check(s) to run (L1 step 2).

Step 1 (`surfaces.py`) found every bug-prone SURFACE. This step attaches, to each surface, the
specific BUG CLASS(es) worth checking there, producing Candidate records (one per
surface × applicable bug_class). A Candidate is a proto-hypothesis: it already carries the
`class` + `bug_class` + a falsifiable `claim` + a severity hint + the surface location — i.e.
everything the loader (`scoping.load_hypotheses`) requires EXCEPT the stable id (assigned later
in the synthesize step) and dedup.

Design rules (hardened by the 3-agent review 2026-06-06):
  * COMPLETENESS-FIRST: every surface_type maps to >=1 bug_class; a surface with NO mapping is
    NOT dropped — it gets a fallback `unclassified-surface` candidate AND the report is marked
    COVERAGE_INCOMPLETE. A surface yields MULTIPLE candidates where multiple bug classes apply
    (e.g. CPI → unchecked-account + arbitrary-program + authority-not-signer). Arithmetic is
    detail-aware: `/`/`%` add division-by-zero, `as` casts add cast-truncation, `[]` adds
    index-out-of-bounds — so `+` isn't flooded with irrelevant classes. Over-labeling is fine;
    UNDER-labeling (a real bug class never attached) is the cardinal failure.
  * SEVERITY hints are High by default: at L1 we have NOT proven anything, so every surface is
    "worth checking at High priority until disproven" — this stops a `--min-severity High` floor
    from silently pre-dropping money-math surfaces before L2 even runs. The FINAL severity is
    re-derived downstream (`severity.derive_severity`) from bug_class + PoC outcome.
  * LOADER-VALID by construction: `class` ∈ scoping.KNOWN_CLASSES, `bug_class` matches the live
    scoping regex, `claim` >=20 chars. Tests cross-check every spec against the live loader.
  * COVERAGE carries through: the SurfaceReport status/notes propagate; status is validated;
    candidate/notes counts are bounded (an untrusted repo can't OOM the labeler); attacker text
    (file/fn) is sanitized out of the claim (no newline/YAML-injection into the hypothesis set).
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from audit_pipeline.l1.surfaces import (
    ALL_SURFACE_TYPES,
    S_ARITH,
    S_AUTH,
    S_CLOSE,
    S_CPI,
    S_DESERIALIZE,
    S_HANDLER,
    S_INIT,
    S_ORACLE,
    S_PDA,
    S_REALLOC,
    S_REMAINING,
    Surface,
    SurfaceReport,
    extract_surfaces,
)

_VALID_STATUS = ("OK", "COVERAGE_INCOMPLETE", "COVERAGE_UNKNOWN")
_MAX_TOTAL_CANDIDATES = 10_000_000  # bound the labeled set (untrusted repo can't OOM via candidates)
_MAX_NOTES = 5_000
_CLAIM_TEXT_CAP = 160  # cap each attacker-derived claim component


@dataclass(frozen=True)
class BugClassSpec:
    cls: str       # a scoping.KNOWN_CLASSES value (drives L2 dispatch)
    bug_class: str  # kebab id; cross-protocol propagation key
    claim_suffix: str  # appended after the surface locator to form a falsifiable claim
    severity: str = "High"  # L1 hint (pre-proof floor); final severity re-derived downstream


# surface_type -> the bug classes worth checking at that surface. Multiple per surface is
# intentional (completeness). Arithmetic extras are detail-aware (see _arith_extras).
SURFACE_TYPE_BUGCLASSES: dict[str, list[BugClassSpec]] = {
    S_HANDLER: [
        BugClassSpec("authorization", "missing-authorization-check",
                     "is an instruction entrypoint that may run privileged logic without verifying the caller is an authorized signer/owner."),
        BugClassSpec("state-machine", "missing-pause-check",
                     "is an instruction entrypoint that may execute while the program/market should be paused or in a disallowed lifecycle state."),
    ],
    S_AUTH: [
        BugClassSpec("authorization", "authorization-bypass",
                     "is an authority/owner/signer check that may be incomplete, checked against the wrong account, or bypassable."),
        BugClassSpec("account-validation", "missing-writable-check",
                     "touches an account whose is_writable/mutability may not be enforced, allowing reads/writes on an unexpected account."),
    ],
    S_CPI: [
        BugClassSpec("cpi-correctness", "unchecked-cpi-account",
                     "is a cross-program invocation whose passed accounts may not be validated (owner/key/writability)."),
        BugClassSpec("cpi-correctness", "arbitrary-cpi-program",
                     "is a cross-program invocation whose target program id may be attacker-controlled / unverified."),
        BugClassSpec("cpi-correctness", "cpi-authority-not-signer",
                     "is a cross-program invocation whose authority/signer-seeds may be wrong or attacker-supplied (spoofed authority)."),
    ],
    S_ARITH: [
        BugClassSpec("arithmetic_overflow", "arithmetic-overflow",
                     "is an arithmetic operation that may overflow or underflow without a checked_* guard."),
    ],
    S_ORACLE: [
        BugClassSpec("oracle", "oracle-staleness",
                     "reads a price/oracle value that may be stale — no freshness/timestamp bound enforced."),
        BugClassSpec("oracle", "oracle-confidence-unchecked",
                     "reads a price/oracle value without validating its confidence interval / deviation bound."),
        BugClassSpec("oracle", "oracle-price-manipulation",
                     "reads a price/oracle value that may be manipulable within a transaction (insufficient TWAP window / spot price)."),
    ],
    S_PDA: [
        BugClassSpec("pda-derivation", "pda-bump-not-canonical",
                     "derives a PDA whose bump/seeds may not be the canonical, validated derivation."),
        BugClassSpec("pda-derivation", "pda-seed-injection",
                     "derives a PDA from seeds that may include attacker-controlled data, enabling seed collision/forgery."),
    ],
    S_CLOSE: [
        BugClassSpec("state-machine", "account-close-revival",
                     "closes/drains an account whose data may not be zeroed, allowing revival or stale-state reuse."),
        BugClassSpec("account-validation", "close-destination-unvalidated",
                     "closes/drains an account whose lamport-recipient (close destination) may be attacker-controlled / unvalidated."),
    ],
    S_REALLOC: [
        BugClassSpec("state-machine", "realloc-uninitialized-data",
                     "reallocs an account; newly exposed bytes may expose stale data or skip re-validation."),
    ],
    S_DESERIALIZE: [
        BugClassSpec("account-validation", "type-confusion",
                     "deserializes account data without confirming the account's type/discriminator."),
        BugClassSpec("account-validation", "missing-owner-check",
                     "deserializes account data without confirming account.owner == the expected program id."),
    ],
    S_REMAINING: [
        BugClassSpec("account-validation", "remaining-account-substitution",
                     "consumes remaining_accounts that may be attacker-substituted without validation."),
        BugClassSpec("account-validation", "remaining-accounts-length-unchecked",
                     "consumes remaining_accounts whose count/length may be unchecked, letting an attacker pass too few/zero accounts to silently skip validation or loop logic."),
    ],
    S_INIT: [
        BugClassSpec("account-validation", "reinitialization",
                     "initializes an account that may already be initialized, enabling re-init / overwrite."),
        BugClassSpec("account-validation", "missing-rent-exemption",
                     "creates/initializes an account that may not be funded to rent-exemption (account may be reaped)."),
        BugClassSpec("account-validation", "init-if-needed-repeated-write",
                     "uses init-if-needed; the account is touched on every call, so post-init mutations may not be idempotent/guarded."),
    ],
}

# detail-aware EXTRA bug classes for arithmetic surfaces (keyed off Surface.detail = the operator
# / method / form). Keeps `+` from being labeled division-by-zero while still flagging `/`/casts.
_ARITH_DIV = {"/", "%", "/=", "%=", "checked_div", "checked_rem", "wrapping_div", "wrapping_rem",
              "saturating_div", "saturating_rem", "overflowing_div", "overflowing_rem",
              "unchecked_div", "unchecked_rem"}


def _arith_extras(detail: str) -> list[BugClassSpec]:
    extras: list[BugClassSpec] = []
    if detail in _ARITH_DIV:
        extras.append(BugClassSpec("arithmetic_overflow", "division-by-zero",
                                   "is a division/modulo whose divisor may be zero (panic / undefined result)."))
    if detail == "as" or detail.startswith("as "):
        # "as" alone is the whitespace-collapsed form of surfaces.py's `as ` (empty/unparsed
        # cast type) — still a cast, so it must keep the label. Real method names like as_ref
        # use an underscore, never a space, so neither branch false-fires on them.
        extras.append(BugClassSpec("arithmetic_overflow", "cast-truncation",
                                   "is a numeric `as` cast that may silently truncate / change sign."))
    if detail == "index[]":
        extras.append(BugClassSpec("arithmetic_overflow", "index-out-of-bounds",
                                   "is an array/slice index that may be out of bounds (panic)."))
    return extras


# fallback for a surface_type with no mapping (must never silently drop a surface).
_FALLBACK = BugClassSpec("logic", "unclassified-surface",
                         "is a bug-prone surface with no specific bug-class mapping yet — review manually.")


def _specs_for(surface: Surface) -> list[BugClassSpec]:
    base = SURFACE_TYPE_BUGCLASSES.get(surface.surface_type)
    if not base:
        return [_FALLBACK]
    if surface.surface_type == S_ARITH:
        # label off the SANITIZED detail so spec-selection matches the stored Candidate.detail
        # (surfaces.py already strips, but keep the two in lock-step for any future detail form).
        return base + _arith_extras(_san(surface.detail))
    return list(base)


@dataclass
class Candidate:
    surface_type: str
    file: str
    line: int
    enclosing_fn: str | None
    cls: str
    bug_class: str
    claim: str
    severity: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "surface_type": self.surface_type, "file": self.file, "line": self.line,
            # target_file is the loader/dedup key: scoping.load_class_library dedups on
            # (bug_class, target_file, claim-prefix) and filter_hypotheses_by_diff scopes on it.
            # Without it every candidate collides on "" and distinct-file surfaces are dropped.
            "target_file": self.file,
            "enclosing_fn": self.enclosing_fn, "class": self.cls, "bug_class": self.bug_class,
            "claim": self.claim, "severity": self.severity, "detail": self.detail,
        }


@dataclass
class CandidateReport:
    status: str = "OK"  # carried from the SurfaceReport (one of _VALID_STATUS)
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:  # never trust a forged/garbage status
            raise ValueError(f"CandidateReport.status must be one of {_VALID_STATUS}, got {self.status!r}")
        if len(self.notes) > _MAX_NOTES:  # bound an inherited oversized notes list
            self.notes = self.notes[:_MAX_NOTES] + [f"... (notes truncated at {_MAX_NOTES})"]

    @property
    def complete(self) -> bool:
        return self.status == "OK"

    def _mark(self, status: str, note: str) -> None:
        order = {"OK": 0, "COVERAGE_INCOMPLETE": 1, "COVERAGE_UNKNOWN": 2}
        if status not in order:
            raise ValueError(f"_mark: unknown status {status!r}")
        if order[status] > order[self.status]:
            self.status = status
        if len(self.notes) < _MAX_NOTES:
            self.notes.append(note)
        elif len(self.notes) == _MAX_NOTES:
            self.notes.append(f"... (further notes suppressed past {_MAX_NOTES})")

    def by_bug_class(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.candidates:
            counts[c.bug_class] = counts.get(c.bug_class, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "status": self.status,
            "complete": self.complete,
            "total_candidates": len(self.candidates) if self.status == "OK" else None,
            "raw_candidate_count": len(self.candidates),
            "by_bug_class": self.by_bug_class(),
            "notes": self.notes,
        }


def _san(s: str | None) -> str:
    """Sanitize attacker-derived text for inclusion in a claim: collapse ALL whitespace
    (incl. newlines) to single spaces and cap length — so a malicious fn name can't inject
    newlines / YAML structure into the hypothesis set."""
    if not s:
        return ""
    return " ".join(s.split())[:_CLAIM_TEXT_CAP]


# line-break / control chars (incl. Unicode line separators) that could break a YAML scalar.
# Stripped from PATHS — unlike _san, this preserves legitimate single AND multiple spaces in a
# path (dir names can contain spaces) and does NOT truncate at the short claim cap, so a real
# repo-relative path round-trips intact (a truncated path mis-resolves AND collides in the
# (bug_class, target_file, claim) dedup key, silently dropping a distinct-file surface).
_PATH_BAD_RE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]")
_PATH_CAP = 1024  # generous DoS bound only — no real repo path approaches it (OS PATH_MAX < this)


def _san_path(s: str | None) -> str:
    """Sanitize an attacker-derived file path into a CONTAINED, repo-relative POSIX path.

    Two jobs: (1) drop line-break/control chars so the path can't inject YAML structure;
    (2) normalize away anything that could escape the repo root when a downstream step does
    `engine_repo / target_file` — Windows drive prefixes, leading separators, and `..`/`.`
    traversal components — so target_file can never read outside the scanned tree. Legitimate
    paths (surfaces.py derives them via Path.relative_to(root), so they never contain `..`)
    round-trip intact, INCLUDING spaces in directory names (collapsing them would mis-resolve
    the path). The 1024-char cap is a pure DoS backstop on adversarial input — no real OS path
    approaches it — so a trim there is deliberate and NOT treated as a coverage gap."""
    if not s:
        return ""
    cleaned = _PATH_BAD_RE.sub("", s)[:_PATH_CAP].replace("\\", "/")
    parts = []
    for p in cleaned.split("/"):
        # strip a Windows drive prefix on EVERY segment ("C:", and the "C:D:" double form) so a
        # surviving "D:" can't make `engine_repo / target_file` resolve drive-relative on Windows.
        p = re.sub(r"^([A-Za-z]:)+", "", p)
        if p and p not in (".", ".."):
            parts.append(p)
    return "/".join(parts)


def _claim(surface: Surface, spec: BugClassSpec) -> str:
    fn_san = _san(surface.enclosing_fn)  # drive the clause off the sanitized value so a
    fn = f" in fn `{fn_san}`" if fn_san else ""  # whitespace-only fn name doesn't emit an empty ``
    # The LINE NUMBER leads the location so it always lands inside the first 120 chars that
    # scoping.load_class_library canonicalizes for its (bug_class, target_file, claim) dedup.
    # If the (possibly long) file path came first, `:line` could fall outside that window and
    # two surfaces at different lines of the same file would dedup to one — silently dropping a
    # real bug site (under-detection, the cardinal failure).
    return (f"The {surface.surface_type} at line {surface.line} of "
            f"{_san_path(surface.file)}{fn} {spec.claim_suffix}")


def label_surfaces(report: SurfaceReport) -> CandidateReport:
    """Attach bug-class candidate(s) to every surface. Carries (and validates) the scan's
    coverage status; bounds the candidate count; sanitizes attacker text into claims."""
    out = CandidateReport(status=report.status, notes=list(report.notes))
    unmapped: set[str] = set()
    capped = False
    for s in report.surfaces:
        if s.surface_type not in SURFACE_TYPE_BUGCLASSES:
            unmapped.add(s.surface_type)
        for spec in _specs_for(s):
            out.candidates.append(Candidate(
                surface_type=s.surface_type, file=_san_path(s.file), line=s.line,
                enclosing_fn=_san(s.enclosing_fn) or None, cls=spec.cls, bug_class=spec.bug_class,
                claim=_claim(s, spec), severity=spec.severity, detail=_san(s.detail),
            ))
        if len(out.candidates) >= _MAX_TOTAL_CANDIDATES:
            out._mark("COVERAGE_UNKNOWN", f"candidate cap {_MAX_TOTAL_CANDIDATES} exceeded — labeling stopped (possible adversarial input).")
            capped = True
            break
    if unmapped and not capped:
        out._mark("COVERAGE_INCOMPLETE", f"{len(unmapped)} surface type(s) had no bug-class mapping (used fallback) — extend SURFACE_TYPE_BUGCLASSES: {sorted(unmapped)}")
    return out


def label_repo(root: Path) -> CandidateReport:
    """Convenience: scan a repo for surfaces, then label them."""
    return label_surfaces(extract_surfaces(root))


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: candidates.py <repo-root>", file=sys.stderr)
        return 2
    rep = label_repo(Path(argv[0]))
    print(json.dumps(rep.summary(), indent=2))
    print(f"\n-- first candidates (of {len(rep.candidates)}) --")
    for c in rep.candidates[:30]:
        print(f"  {c.bug_class:<32} [{c.cls}] {c.file}:{c.line}")
    if not rep.complete:
        print(f"\nSTATUS: {rep.status} — labeled set is NOT a trustworthy complete inventory.")
    else:
        print(f"\nOK: {len(rep.candidates)} candidates from labeling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
