"""L1 coverage count (step 7) — "checked X of Y instructions; here's what we couldn't reach."

Ties the DENOMINATOR (every instruction the program exposes — from the native reader
`entrypoints.extract_native` or the Anchor reader `anchor_entrypoints.extract_anchor`) to the
NUMERATOR (which of those instruction handlers the L1 surface scan actually found bug-spots in).
The whole point of L1 is to never silently read 100%: this report makes the gap explicit.

Coverage-safe contract (the cardinal sin is OVER-claiming coverage):
  * A coverage PERCENTAGE is reported ONLY when status == OK (the denominator AND the numerator
    are both complete). If the entrypoint authority is UNKNOWN/INCOMPLETE, or the surface scan is
    UNKNOWN/INCOMPLETE, coverage_pct is None — you can never read "100%" off partial data. The
    `uncovered` list is ALWAYS available so a partial run still shows what was not reached.
  * An instruction is "covered" ONLY on a precise (handler_fn, handler_file) match against a
    surface's (enclosing_fn, file). Matching by name alone could let a surface in a same-named
    function in another file falsely mark an instruction covered — an over-claim. If a handler's
    file is unknown (unresolved native handler), the instruction is conservatively UNCOVERED.
  * Known limitation (conservative, never over-claims): coverage is by the DIRECT enclosing
    function. An instruction whose handler delegates all logic to callees may read as uncovered
    even though a callee has surfaces — under-claiming is safe; over-claiming is the sin.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from audit_pipeline.l1.anchor_entrypoints import extract_anchor
from audit_pipeline.l1.candidates import _VALID_STATUS
from audit_pipeline.l1.entrypoints import EntrypointAuthority, extract_native
from audit_pipeline.l1.surfaces import SurfaceReport, extract_surfaces

_ORDER = {"OK": 0, "COVERAGE_INCOMPLETE": 1, "COVERAGE_UNKNOWN": 2}
_MAX_NOTES = 5_000


def _worst(*statuses: str) -> str:
    """The most-severe status (UNKNOWN > INCOMPLETE > OK). Unknown strings count as UNKNOWN."""
    return max(statuses, key=lambda s: _ORDER.get(s, 2))


@dataclass
class InstructionCoverage:
    instruction: str
    handler: str | None
    handler_file: str | None
    covered: bool
    surface_count: int

    def to_dict(self) -> dict:
        return {
            "instruction": self.instruction, "handler": self.handler,
            "handler_file": self.handler_file, "covered": self.covered,
            "surface_count": self.surface_count,
        }


@dataclass
class CoverageReport:
    status: str = "OK"
    program_kind: str = "native"
    items: list[InstructionCoverage] = field(default_factory=list)
    authority_status: str = "OK"
    surface_status: str = "OK"
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:
            raise ValueError(f"CoverageReport.status must be one of {_VALID_STATUS}, got {self.status!r}")
        if len(self.notes) > _MAX_NOTES:
            self.notes = self.notes[:_MAX_NOTES] + [f"... (notes truncated at {_MAX_NOTES})"]

    @property
    def complete(self) -> bool:
        return self.status == "OK"

    def covered(self) -> list[InstructionCoverage]:
        return [i for i in self.items if i.covered]

    def uncovered(self) -> list[InstructionCoverage]:
        return [i for i in self.items if not i.covered]

    def summary(self) -> dict:
        n = len(self.items)
        n_cov = len(self.covered())
        # percentage ONLY on a fully-OK run; never a % over an unknown/incomplete denominator.
        pct = round(100.0 * n_cov / n, 1) if (self.status == "OK" and n) else None
        return {
            "status": self.status,
            "complete": self.complete,
            "program_kind": self.program_kind,
            "declared_instructions": n,
            "covered_instructions": n_cov,
            "uncovered_instructions": n - n_cov,
            "coverage_pct": pct,
            "authority_status": self.authority_status,
            "surface_status": self.surface_status,
            "uncovered": [i.instruction for i in self.uncovered()],
            "notes": self.notes,
        }


_TRANSITIVE_NOTE = ("coverage is by DIRECT enclosing function — an instruction whose handler "
                    "delegates to callees may read uncovered (conservative; never over-claims).")


def coverage(authority: EntrypointAuthority, surfaces: SurfaceReport) -> CoverageReport:
    """Cross the instruction authority (denominator) with the surface scan (numerator)."""
    # precise (fn, file) pairs that the surface scan actually found bug-spots in
    covered_pairs: set[tuple[str, str]] = set()
    counts: Counter = Counter()
    for s in surfaces.surfaces:
        if s.enclosing_fn:
            covered_pairs.add((s.enclosing_fn, s.file))
            counts[(s.enclosing_fn, s.file)] += 1

    items: list[InstructionCoverage] = []
    for e in authority.instructions:
        handler = e.handler or e.instruction
        hf = e.handler_file
        # covered ONLY on an exact (handler, handler_file) match; unknown file => conservative miss
        covered = bool(hf) and (handler, hf) in covered_pairs
        items.append(InstructionCoverage(
            instruction=e.instruction, handler=handler, handler_file=hf,
            covered=covered, surface_count=counts.get((handler, hf), 0) if hf else 0,
        ))

    status = _worst(authority.status, surfaces.status)
    # no instructions enumerated => the denominator is unknown, never a clean 100%/0%
    if not authority.instructions:
        status = _worst(status, "COVERAGE_UNKNOWN")

    # assemble ALL notes up front so __post_init__ caps them (no post-construction mutation).
    # carry the authority's own notes through so file-skip / ambiguity / multi-program detail is
    # not lost to a downstream reader who only sees the coverage report.
    notes = [_TRANSITIVE_NOTE]
    if authority.status != "OK":
        notes.append(f"instruction authority is {authority.status} — the denominator is not a "
                     f"trustworthy complete instruction set.")
    if surfaces.status != "OK":
        notes.append(f"surface scan is {surfaces.status} — the numerator (covered set) is partial.")
    notes += [f"authority: {n}" for n in authority.notes]
    notes += [f"surface: {n}" for n in surfaces.notes]  # surface-scan skip/parse detail too
    return CoverageReport(
        status=status, program_kind=authority.program_kind, items=items,
        authority_status=authority.status, surface_status=surfaces.status, notes=notes,
    )


def coverage_repo(root: Path) -> CoverageReport:
    """End-to-end coverage on a repo: pick the right instruction reader (native vs Anchor),
    run the surface scan, and cross them."""
    root = Path(root)
    anchor = extract_anchor(root)
    native = extract_native(root)
    surfaces = extract_surfaces(root)

    if anchor.instructions and native.instructions:
        # AMBIGUOUS: both an Anchor #[program] AND a native Instruction dispatch enumerated. Do NOT
        # pick one — a crafted decoy (a tiny #[program] beside a large native dispatch, or vice
        # versa) could silently SHRINK the denominator and inflate coverage. Instead take the UNION
        # of both instruction sets (over-inclusive denominator = safe) and force COVERAGE_UNKNOWN so
        # no percentage is reported off an untrustworthy program-kind determination.
        authority = EntrypointAuthority(program_kind="ambiguous", status="COVERAGE_UNKNOWN")
        authority.instructions = list(anchor.instructions) + list(native.instructions)
        authority.skipped_files = list(anchor.skipped_files) + list(native.skipped_files)
        authority.notes = (
            ["ambiguous program kind — both an Anchor #[program] and a native Instruction dispatch "
             "enumerated; denominator is the UNION of both. Verify the program kind."]
            + [f"anchor: {n}" for n in anchor.notes]
            + [f"native: {n}" for n in native.notes]
        )
    elif anchor.instructions:
        authority = anchor
    elif native.instructions:
        authority = native
    else:
        # neither reader enumerated instructions — denominator unknown. Both readers mark a non-OK
        # status when they find none, so coverage() reports an honest UNKNOWN (never a clean 0-of-0).
        # Carry native (its program_kind label is moot under UNKNOWN) plus the anchor notes for context.
        authority = native
        authority.notes = list(native.notes) + [f"anchor: {n}" for n in anchor.notes]

    return coverage(authority, surfaces)


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: coverage.py <repo-root>", file=sys.stderr)
        return 2
    rep = coverage_repo(Path(argv[0]))
    s = rep.summary()
    print(json.dumps(s, indent=2))
    if rep.uncovered():
        print(f"\n-- uncovered instructions ({len(rep.uncovered())}) --")
        for i in rep.uncovered()[:60]:
            print(f"  {i.instruction:<32} handler={i.handler} @ {i.handler_file}")
    if not rep.complete:
        print(f"\nSTATUS: {rep.status} — coverage is NOT a trustworthy complete measurement "
              f"(authority={rep.authority_status}, surfaces={rep.surface_status}).")
    else:
        print(f"\nOK: covered {len(rep.covered())}/{len(rep.items)} instructions "
              f"({s['coverage_pct']}%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
