"""L1 step 5 — synthesize the loader-ready hypotheses.yaml from the filtered candidates.

Steps 1-4 produced a coverage-safe set of KEPT candidates. This step turns them into the exact
artifact the rest of the scanner already consumes: a hypotheses.yaml that scoping.load_hypotheses
accepts, with stable unique ids, near-duplicate dedup, and the L1 provenance preserved as extra
fields. This is the seam where L1 bolts onto the FRONT of the existing L2-L7 funnel — nothing
downstream changes; it just receives a complete auto-generated list instead of a hand-capped one.

Contract:
  * LOADER-VALID by construction: every emitted hypothesis passes scoping._normalize_and_validate
    and the whole doc round-trips through scoping.load_hypotheses (ids unique, required fields
    present). Tested against the live loader.
  * DEDUP matches the downstream key: near-duplicates are collapsed on
    (bug_class, target_file, canonical-claim[:120]) — the same key scoping.load_class_library
    uses — so a surface that produced the same check twice isn't dispatched (and over-counted) N
    times. Stable sequential ids are assigned AFTER dedup.
  * COVERAGE-SAFE: the upstream status (OK / COVERAGE_INCOMPLETE / COVERAGE_UNKNOWN) and the
    filter spend carry through. When status != OK the list is still emitted (never a silent clean
    empty set) but `complete` is False so callers gate on it. The yaml carries an `l1_meta` block
    (status + counts) for the operator; load_hypotheses ignores unknown top-level keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from audit_pipeline.l1.candidates import _VALID_STATUS, Candidate
from audit_pipeline.l1.entrypoints import _is_link_or_junction  # reuse the symlink/junction guard
from audit_pipeline.l1.suppress import suppress_repo
from audit_pipeline.l1.triage_filter import CompleteFn, FilterReport, filter_candidates
from audit_pipeline.scoping import _ID_RE  # validate generated ids against the live loader regex

_MAX_NOTES = 5_000
_ID_PREFIX = "L1"  # ids look like L1-<salt><seq>-<bug_class>; matches scoping._ID_RE
_MAX_BUGCLASS_IN_ID = 32  # cap the bug_class slug in the id (seq disambiguates) so long ids can't
                          # blow Windows MAX_PATH when downstream embeds the id in artifact filenames


def _canon_claim(claim: str) -> str:
    """Mirror scoping.load_class_library's claim canonicalization (lowercase, collapse ws, 120)."""
    return " ".join((claim or "").lower().split())[:120]


def _dedup_key(c: Candidate) -> tuple[str, str, str]:
    d = c.to_dict()
    return (
        (c.bug_class or "").strip().lower(),
        (d.get("target_file") or c.file or "").strip().lower(),
        _canon_claim(c.claim),
    )


def _hyp_from_candidate(c: Candidate, seq: int, salt: str = "") -> dict:
    """Build one loader-valid hypothesis dict from a candidate, with a stable unique id and the
    L1 provenance preserved (additionalProperties=True downstream, so these ride along).

    The optional `salt` (a short per-repo hash) makes ids unique ACROSS independently-generated
    L1 files, so two `hypotheses.l1.yaml` from different repos can't collide on `L1-00001-...`
    if both are ever loaded by scoping.load_class_library."""
    h = c.to_dict()  # surface_type, file, target_file, line, enclosing_fn, class, bug_class, claim, severity, detail
    bug = (c.bug_class or "x")[:_MAX_BUGCLASS_IN_ID]
    h["id"] = f"{_ID_PREFIX}-{salt}{seq:05d}-{bug}"
    if not _ID_RE.match(h["id"]):  # loud + early: never emit an id the loader will reject
        raise ValueError(f"synthesized id {h['id']!r} does not match loader regex {_ID_RE.pattern}")
    h["l1_source"] = "surface-coverage"
    return h


@dataclass
class SynthReport:
    status: str = "OK"
    hypotheses: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_kept: int = 0          # candidates in before dedup
    duplicates_collapsed: int = 0
    spend_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUS:
            raise ValueError(f"SynthReport.status must be one of {_VALID_STATUS}, got {self.status!r}")
        self.spend_usd = float(self.spend_usd or 0.0)  # never None -> safe for :.4f formatting
        if len(self.notes) > _MAX_NOTES:
            self.notes = self.notes[:_MAX_NOTES] + [f"... (notes truncated at {_MAX_NOTES})"]

    @property
    def complete(self) -> bool:
        return self.status == "OK"

    def is_false_clean(self) -> bool:
        """ZERO hypotheses AND status != OK — 'found nothing' is UNTRUSTWORTHY here (parser
        failure / wrong language / everything skipped), not a clean empty result. A caller must
        ABORT rather than launch a deceptively-green cycle on an empty, incomplete inventory."""
        return not self.hypotheses and not self.complete

    def summary(self) -> dict:
        return {
            "status": self.status,
            "complete": self.complete,
            "total_hypotheses": len(self.hypotheses) if self.status == "OK" else None,
            "raw_hypothesis_count": len(self.hypotheses),
            "raw_kept": self.raw_kept,
            "duplicates_collapsed": self.duplicates_collapsed,
            "spend_usd": round(self.spend_usd, 6),
            "notes": self.notes,
        }


def synthesize(report: FilterReport, *, id_salt: str = "") -> SynthReport:
    """Turn a step-4 FilterReport's kept candidates into loader-valid hypotheses (deduped, ids).
    `id_salt` (a short per-repo hash, supplied by synthesize_repo) namespaces the ids so two
    independently-generated L1 files can't collide if both are ever loaded together."""
    out = SynthReport(status=report.status, notes=list(report.notes), spend_usd=report.spend_usd)
    kept = report.kept()
    out.raw_kept = len(kept)
    seen: set[tuple[str, str, str]] = set()
    seq = 1
    for c in kept:
        key = _dedup_key(c)
        if key in seen:
            out.duplicates_collapsed += 1
            continue
        seen.add(key)
        out.hypotheses.append(_hyp_from_candidate(c, seq, id_salt))
        seq += 1
    if out.duplicates_collapsed and len(out.notes) < _MAX_NOTES:
        out.notes.append(f"step5: collapsed {out.duplicates_collapsed} near-duplicate candidate(s) "
                         f"on (bug_class, target_file, claim).")
    return out


def synthesize_repo(root: Path, *, complete_fn: CompleteFn | None = None, run_filter: bool = True) -> SynthReport:
    """End-to-end steps 1-5 on a repo. run_filter=True runs the step-4 cheap-model false-alarm
    filter first (production default); run_filter=False stops after step 3 (deterministic, no LLM,
    so more candidates survive — useful for a no-cost dry run)."""
    supp = suppress_repo(root)
    rep = filter_candidates(
        supp.active(), root,
        complete_fn=complete_fn, status=supp.status,
    ) if run_filter else _passthrough_filter(supp.active(), supp.status, supp.notes)
    # deterministic per-repo salt so ids are unique across independently-generated L1 files
    salt = hashlib.sha1(str(root).encode("utf-8", "replace")).hexdigest()[:6]
    return synthesize(rep, id_salt=salt)


def _passthrough_filter(active: list[Candidate], status: str, notes: list[str]) -> FilterReport:
    """Wrap step-3 survivors as a FilterReport with nothing dropped (no LLM run)."""
    from audit_pipeline.l1.triage_filter import FilterVerdict
    out = FilterReport(status=status, notes=list(notes))
    out.verdicts = [FilterVerdict(c, dropped=False, reason=None) for c in active]
    if len(out.notes) < _MAX_NOTES:
        out.notes.append("step4 skipped (run_filter=False) — no false-alarm filtering ran.")
    return out


def write_yaml(report: SynthReport, path: Path) -> Path:
    """Write the synthesized hypotheses to `path` as the loader-ready doc. Returns the path.

    Hardened because the default output is INSIDE the (untrusted) scanned repo: (1) create the
    parent dir; (2) REFUSE to write through a symlink/junction planted at the destination — an
    attacker repo could place one at <repo>/hypotheses.l1.yaml to redirect the write outside;
    (3) write atomically (temp file in the same dir + os.replace) so a crash mid-write never
    leaves a half-written or clobbered file, and replacing a symlink swaps the NAME (the link
    target is never followed)."""
    doc = {
        "l1_meta": {  # informational; scoping.load_hypotheses ignores unknown top-level keys
            "status": report.status,
            "complete": report.complete,
            "hypothesis_count": len(report.hypotheses),
            "duplicates_collapsed": report.duplicates_collapsed,
            "spend_usd": round(report.spend_usd, 6),
            "generated_by": "l1-surface-coverage",
        },
        "hypotheses": report.hypotheses,
    }
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # only a PRE-EXISTING symlink/junction at the destination is a risk; a not-yet-existing
    # target is the normal case (lexists is False for it, so we don't false-refuse a fresh write).
    if os.path.lexists(path) and _is_link_or_junction(path):
        raise OSError(f"refusing to write through a symlink/junction at {path}")
    if path.is_dir():  # directory-squat: a repo could pre-create the dest as a dir; fail clearly
        raise OSError(f"refusing to write: destination {path} is a directory")
    fd, tmpname = tempfile.mkstemp(dir=str(path.parent), suffix=".l1.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
        os.replace(tmpname, path)  # atomic; replaces the name, never follows a symlink target
    except BaseException:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
    return path


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: synthesize.py <repo-root> [out.yaml] [--no-filter]", file=sys.stderr)
        return 2
    run_filter = "--no-filter" not in argv
    args = [a for a in argv if a != "--no-filter"]
    root = Path(args[0])
    rep = synthesize_repo(root, run_filter=run_filter)
    print(json.dumps(rep.summary(), indent=2))
    if len(args) > 1:
        out = write_yaml(rep, Path(args[1]))
        print(f"\nwrote {len(rep.hypotheses)} hypotheses -> {out}")
    if not rep.complete:
        print(f"\nSTATUS: {rep.status} — hypothesis set is NOT a trustworthy complete inventory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
