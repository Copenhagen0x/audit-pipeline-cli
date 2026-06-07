"""L1 step 3 — drop ONLY the provably-safe candidates (deterministic, no model).

Step 2 (`candidates.py`) emitted a candidate for every (surface x applicable bug_class) —
completeness-first, deliberately over-labeling. This step removes only candidates that are
*structurally, provably* safe: where the bug class cannot occur given the code form itself.
Everything uncertain is KEPT.

Why so conservative: under-detection (a real bug whose candidate we dropped) is the cardinal
failure — it is exactly the recall miss this whole layer exists to fix. A wrong drop here is
worse than a thousand false alarms (the cheap-model false-alarm filter + paranoid pass in
step 4 trims those safely). So this step fires ONLY on a positive, airtight safe-signal; the
absence of a signal always means keep.

Contract (coverage-safe, mirrors candidates.py):
  * NEVER delete. A suppressed candidate is KEPT in the report, flagged with a written reason
    and the rule id that fired, so coverage accounting (step 7) can report "checked X,
    suppressed-as-safe Y" and an auditor can see exactly what was dropped and why. Every input
    candidate appears in the output exactly once — nothing silently vanishes.
  * Suppression requires a POSITIVE provable-safe signal; no signal => keep (suppressed=False).
  * The scan's coverage status carries through unchanged — suppression is normal operation,
    not a coverage gap, so it never downgrades OK.

KNOWN LIMITATIONS / TRUST ASSUMPTIONS (read before adding rules):
  * SCOPE OF A SUPPRESSION: suppressing the arithmetic-overflow candidate means only that the
    *overflow hypothesis* is provably inapplicable at that surface — NOT that the surface is
    bug-free. A `checked_add(..).unwrap()` can still panic-DoS; a `checked_shl(n).unwrap()` can
    panic on an oversized shift. L1 does not model those panic classes today (surfaces.py has no
    `.unwrap()`/`.expect()` detector), so they are a PRE-EXISTING upstream gap — this step does
    not create or worsen it (there was never a candidate pointing at the unwrap). Tracked as a
    separate follow-up to add panic-on-unwrap detection in steps 1-2.
  * SYNTAX-ONLY CLASSIFICATION (shadowing): `detail` is the bare call name from surfaces.py
    `_callee_name` with no type/trait resolution. An attacker who defines an inherent method
    named `checked_add` that does UNCHECKED math would have its overflow candidate suppressed
    here (false-safe). Blast radius is bounded — one surface per shadow, and naming an inherent
    method `checked_add` is itself suspicious and draws human-auditor attention — and the same
    bare-name assumption underlies all of surfaces.py/candidates.py, not just this rule. Type
    resolution is not tractable at the tree-sitter level; accepted as a known limitation. Because
    suppressed candidates are KEPT (flagged, never deleted), a later stage can always resurrect
    them.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from audit_pipeline.l1.candidates import (
    _VALID_STATUS,
    Candidate,
    CandidateReport,
    label_repo,
)

_MAX_NOTES = 5_000
_MAX_REASON_LEN = 1_024  # cap the emitted suppress_reason (bounded by construction; backstop)


# ---- provably-safe rules -----------------------------------------------------
# Each rule: (Candidate) -> reason str when the candidate is provably safe, else None.
# Rules must be AIRTIGHT — true for the code form regardless of surrounding context. When in
# doubt, return None (keep). Add rules here as new airtight signals are found.

# checked_* arithmetic returns Option/Result on overflow, so the value can NEVER silently
# overflow into a wrong number — which makes the arithmetic-overflow claim ("overflow/underflow
# WITHOUT a checked_* guard") literally false at this surface. ONLY checked_* qualifies:
#   * wrapping_*    — wraps silently (a classic logic bug) -> KEEP
#   * saturating_*  — clamps silently (a known accounting-bug pattern) -> KEEP
#   * overflowing_* — reports overflow via a bool the caller may ignore -> KEEP
#   * unchecked_*   — UB, the dangerous form -> KEEP
_CHECKED_OVERFLOW_SAFE = {
    f"checked_{k}" for k in ("add", "sub", "mul", "div", "rem", "pow", "neg",
                             "shl", "shr", "add_signed")
}


def _rule_checked_arith(c: Candidate) -> str | None:
    if c.bug_class == "arithmetic-overflow" and c.detail in _CHECKED_OVERFLOW_SAFE:
        return (f"uses `{c.detail}`: checked arithmetic returns Option/Result on overflow, so "
                f"the value cannot silently overflow — the arithmetic-overflow hypothesis "
                f"('overflow without a checked_* guard') is provably inapplicable here. This does "
                f"NOT assert the call site is bug-free: a panic-on-unwrap of the returned None, "
                f"or a same-named user-defined method (shadowing), is out of this rule's scope "
                f"(see module KNOWN LIMITATIONS).")
    return None


# (rule_id, rule_fn) — ordered; the first rule that fires wins.
_SAFE_RULES: list[tuple[str, Callable[[Candidate], str | None]]] = [
    ("checked-arith", _rule_checked_arith),
]


@dataclass(frozen=True)
class Verdict:
    candidate: Candidate
    suppressed: bool
    reason: str | None  # why provably-safe — only set when suppressed
    rule: str | None    # id of the rule that fired — only set when suppressed

    def to_dict(self) -> dict:
        d = self.candidate.to_dict()
        d["suppressed"] = self.suppressed
        if self.suppressed:
            # cap the emitted reason (defense-in-depth before any YAML-emitting integration).
            d["suppress_reason"] = (self.reason or "")[:_MAX_REASON_LEN]
            d["suppress_rule"] = self.rule
        return d


@dataclass
class SuppressionReport:
    status: str = "OK"  # carried from the CandidateReport (one of _VALID_STATUS)
    verdicts: list[Verdict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:  # never trust a forged/garbage status
            raise ValueError(f"SuppressionReport.status must be one of {_VALID_STATUS}, got {self.status!r}")
        if len(self.notes) > _MAX_NOTES:
            self.notes = self.notes[:_MAX_NOTES] + [f"... (notes truncated at {_MAX_NOTES})"]

    @property
    def complete(self) -> bool:
        return self.status == "OK"

    def active(self) -> list[Candidate]:
        """The candidates that survive to the next stage (everything not provably-safe)."""
        return [v.candidate for v in self.verdicts if not v.suppressed]

    def suppressed_verdicts(self) -> list[Verdict]:
        # NB: named *_verdicts (not `suppressed`) to avoid shadowing Verdict.suppressed (a bool);
        # a bare `report.suppressed` would otherwise read as an always-truthy bound method.
        return [v for v in self.verdicts if v.suppressed]

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.verdicts:
            if v.suppressed and v.rule:
                counts[v.rule] = counts.get(v.rule, 0) + 1
        return counts

    def summary(self) -> dict:
        n_active = len(self.active())
        return {
            "status": self.status,
            "complete": self.complete,
            # total active is trustworthy only when coverage is OK (mirrors candidates.py).
            "total_active": n_active if self.status == "OK" else None,
            "raw_candidate_count": len(self.verdicts),
            "suppressed_count": len(self.verdicts) - n_active,
            "by_rule": self.by_rule(),
            "notes": self.notes,
        }


def suppress_candidates(report: CandidateReport) -> SuppressionReport:
    """Annotate every candidate as provably-safe (suppressed, with a reason) or kept. Never
    deletes; carries the coverage status; records a summary note when anything is suppressed."""
    out = SuppressionReport(status=report.status, notes=list(report.notes))
    for c in report.candidates:
        reason: str | None = None
        rule_id: str | None = None
        for rid, rule in _SAFE_RULES:
            r = rule(c)
            if r is not None:  # None is the documented "keep" sentinel (an empty reason ≠ keep)
                if not r:  # a fired rule MUST give a non-empty reason — guards silent over-suppression
                    raise ValueError(f"safe-rule {rid!r} returned an empty reason; rules must "
                                     f"return None (keep) or a non-empty reason (suppress)")
                reason, rule_id = r, rid
                break  # first airtight signal wins; one reason is enough
        out.verdicts.append(Verdict(candidate=c, suppressed=reason is not None,
                                    reason=reason, rule=rule_id))
    n_supp = len(out.verdicts) - len(out.active())
    if n_supp and len(out.notes) < _MAX_NOTES:
        out.notes.append(f"step3: suppressed {n_supp}/{len(out.verdicts)} provably-safe "
                         f"candidate(s) (kept, flagged): {out.by_rule()}")
    return out


def suppress_repo(root: Path) -> SuppressionReport:
    """Convenience: scan + label a repo, then suppress the provably-safe candidates."""
    return suppress_candidates(label_repo(root))


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: suppress.py <repo-root>", file=sys.stderr)
        return 2
    rep = suppress_repo(Path(argv[0]))
    print(json.dumps(rep.summary(), indent=2))
    print(f"\n-- sample suppressed (of {len(rep.suppressed_verdicts())}) --")
    for v in rep.suppressed_verdicts()[:20]:
        print(f"  [{v.rule}] {v.candidate.bug_class:<22} {v.candidate.file}:{v.candidate.line}")
    if not rep.complete:
        print(f"\nSTATUS: {rep.status} — NOT a trustworthy complete inventory.")
    else:
        print(f"\nOK: {len(rep.active())} active / {len(rep.verdicts)} total after suppression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
