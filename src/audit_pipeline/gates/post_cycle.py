"""Post-cycle QA gate — re-runs pre-disclosure gates over every confirmed
finding in a completed cycle BEFORE auto-publish.

Built in response to the 12-audit pipeline review (2026-05-12). The
orchestration audit and the disclosure audit both converged on the same
gap: the six Phase B pre-disclosure gates run at cycle START (freshness
check, hypothesis filtering, PoC symbol gate), but nothing re-runs them
at cycle END before the cycle's HTML/PDF and per-finding emails get
auto-pushed to GitHub + /var/www/jelleo.com/cycles/. Yesterday's
20260511-183154 cycle was lucky to be caught by manual L2.5 triage; an
identical cycle today would auto-publish into the public archive before
operator review unless this gate exists.

The gate is invoked by ``hunt.py`` immediately before the auto-publish
chain. It loads each confirmed finding's PoC + writes a sentinel file
``<cycle_dir>/.publish-blocked`` if any finding fails the re-checks.
``publish_cycle.sh`` and ``publish_cycle_signed.sh`` MUST honour the
sentinel and skip — that change ships in the same commit as this gate.

What the gate checks (cheap, no API spend, deterministic):
  * **symbol_grep**: every PoC's project-specific symbols must grep-exist
    in engine + wrapper source. Catches hallucinated function names that
    would have made it past the cycle-start gate if the PoC was authored
    against a stale snapshot.
  * **PoC file content**: refuse PoCs that contain ``#[ignore]`` or
    ``unimplemented!()`` / ``todo!()`` / ``CANNOT_TEST`` markers — these
    can't have legitimately "fired".

Optionally (opt-in via ``include_behavior_oracle=True``):
  * **behavior_oracle**: for each finding, send the claim + cited code
    window to an independent LLM. Returns CONTRADICT for the wrong-
    direction cluster (yesterday's #04, #09, #12, #14, #16, #18, #19,
    #20). Costs ~$0.001 per finding at Haiku rates; default OFF so the
    cheap checks still gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from audit_pipeline.gates.symbol_grep import check_symbols

# Markers that mean a PoC didn't really test the bug.
_PSEUDO_PASS_MARKERS = (
    "#[ignore]",
    "unimplemented!()",
    "todo!()",
    "CANNOT_TEST",
    "Insufficient source grounding",
)

# WS4 proof-carrying by default: severities that must be PROVEN before a finding may
# be published/disclosed. Compared case-insensitively against the finding row's
# `severity` (stored as the Severity enum's .value, e.g. "Critical"/"High").
_PROOF_REQUIRED_SEVERITIES = frozenset({"CRITICAL", "HIGH"})


def proof_carrying(finding: dict) -> tuple[bool, str]:
    """Is this finding PROVEN — Kani-proved OR demonstrated on-chain via LiteSVM?

    WS4's core predicate. Both signals are persisted per-finding in ``details_json``
    (``kani`` written at hunt persistence; ``litesvm`` added there for exactly this
    gate). Fail-closed: anything other than an explicit positive proof is "not proven"
    — a missing/None/unparseable ``details`` block, a Kani ``cannot_verify`` /
    counterexample / compile-error, a ``--skip-kani`` or daily-cap-halt run, or the
    legacy synth-kani path that records only a returncode. Returns ``(is_proven,
    reason)``.

    "Proven" here means one of two DISTINCT things, per operator decision 2026-07-17:
      * ``details.kani.proved is True``  — the invariant was formally proved, OR
      * ``details.litesvm.fired is True`` — the exploit was reproduced on-chain (L4).
    Many Solana bugs cannot be Kani-modeled but ARE L4-exploit-confirmed, so requiring
    Kani alone would refuse to publish real, demonstrated exploits; requiring EITHER is
    the standard. Note: L2 ``poc_fired`` alone is NOT sufficient — a PoC is a cargo
    test, not a formal proof or an on-chain exploit witness.
    """
    import json

    raw = finding.get("details_json")
    if isinstance(raw, str) and raw:
        try:
            details = json.loads(raw)
        except (ValueError, TypeError):
            return False, "proof status unreadable: details_json is not valid JSON"
    elif isinstance(finding.get("details"), dict):
        details = finding["details"]  # already-deserialized row (test/in-memory callers)
    else:
        return False, "no proof recorded: finding has no details block"

    if not isinstance(details, dict):
        return False, "no proof recorded: details block is not an object"

    kani = details.get("kani")
    if isinstance(kani, dict) and kani.get("proved") is True:
        return True, "Kani formally proved the invariant"

    # L4 runtime witness — require a CONCRETE failure/abort, the same standard across
    # adapters, because a mere "ran without crashing" is the case a mis-authored harness
    # can fake:
    #   * Anchor/Solana records `fired` (test result FAILED + an engine-authored bug
    #     witness), or
    #   * the GENERIC runtime-fuzz adapter (Move/Solidity/C — this pipeline is NOT
    #     Solana-only) records `crash_found` from an inverted-assertion harness that
    #     ABORTED, i.e. `ran_clean is False` — an assertion actually fired.
    # The generic adapter ALSO sets `crash_found=True` for a clean PASS ("ran end-to-end
    # without abort"); that weaker, harness-correctness-dependent case does NOT count as
    # proof on its own — such a finding is held for a Kani proof or an abort witness.
    # (Binding these signals to structured tool output rather than LLM-authored-harness
    # text is a tracked follow-up.)
    litesvm = details.get("litesvm")
    if isinstance(litesvm, dict) and (
        litesvm.get("fired") is True
        or (litesvm.get("crash_found") is True and litesvm.get("ran_clean") is False)
    ):
        return True, "exploit reproduced at runtime — assertion/abort witness (L4)"

    return False, ("not proof-carrying: no Kani proof (kani.proved is not True) and no "
                   "runtime exploit with an abort/failure witness (litesvm.fired, or "
                   "crash_found with ran_clean=False)")


def unproven_block_reason(finding: dict) -> str | None:
    """Reason to HOLD this finding from publish/disclosure, or None if it may proceed.

    THE single source of truth for WS4's severity+proof rule, shared by the post-cycle
    auto-publish gate AND the manual disclosure exits (`issue file` /
    `auto-file-confirmed`) — so proof-carrying holds at EVERY door out of the firm, not
    just auto-publish. A finding is held iff it is Critical/High AND not
    ``proof_carrying``; lower-severity and severity-less rows are never held.
    """
    raw_sev = finding.get("severity")
    sev = str(getattr(raw_sev, "value", raw_sev) or "").upper()
    if sev not in _PROOF_REQUIRED_SEVERITIES:
        return None
    ok, why = proof_carrying(finding)
    return None if ok else why


@dataclass
class PostCycleReport:
    """One row per confirmed finding, plus an aggregate verdict."""
    passed: bool
    n_findings: int
    n_failed: int
    rows: list[dict] = field(default_factory=list)
    duration_s: float = 0.0

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "n_findings": self.n_findings,
            "n_failed": self.n_failed,
            "rows": self.rows,
            "duration_s": round(self.duration_s, 3),
        }


def _check_one_poc(
    poc_path: Path,
    test_name: str,
    search_dirs: list[Path],
) -> dict:
    """Validate one PoC file. Returns a row dict suitable for the report.

    Language is inferred from ``poc_path`` extension:
      * ``.rs``    → full check (pseudo-pass markers + Rust symbol_grep)
      * ``.move``  → pseudo-pass markers only (Move stdlib whitelist
                      not built yet; symbol_grep would false-positive)
      * ``.sol``   → pseudo-pass markers only (same reason)
      * ``.c``     → pseudo-pass markers only (same reason)

    The pseudo-pass check (``#[ignore]``, ``unimplemented!()``,
    ``CANNOT_TEST``, …) IS language-agnostic and catches the most
    important failure mode (LLM stubbed the test).
    """
    if not poc_path.is_file():
        return {"poc_path": str(poc_path), "passed": False,
                "reason": "PoC source file missing on disk"}
    try:
        src = poc_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"poc_path": str(poc_path), "passed": False, "reason": f"read error: {e}"}

    # 1. Pseudo-pass markers — these can't have legitimately fired.
    #    LANGUAGE-AGNOSTIC — catches stubbed tests in any language.
    for marker in _PSEUDO_PASS_MARKERS:
        if marker in src:
            return {
                "poc_path": str(poc_path),
                "test_name": test_name,
                "passed": False,
                "reason": (
                    f"PoC contains pseudo-pass marker {marker!r} — "
                    "a #[ignore]d / unimplemented / cannot-test PoC "
                    "cannot have legitimately confirmed a bug. "
                    "Re-author against grounded source."
                ),
            }

    # 2. Symbol grep — Rust-only. The whitelist + extraction regexes
    #    are tuned for Rust syntax; running them against Move / Solidity
    #    / C would false-positive heavily (Move's `aptos_framework`,
    #    `borrow_global`, etc. would be classified as project symbols
    #    and the gate would block legitimate Aptos fires).
    #    Skip silently for non-Rust; pseudo-pass marker check above is
    #    the cross-language equivalent.
    if poc_path.suffix == ".rs":
        sym_result = check_symbols(
            poc_source=src,
            search_dirs=search_dirs,
            allowed_test_names=frozenset({test_name}),
        )
        if sym_result.passed is False:
            return {
                "poc_path": str(poc_path),
                "test_name": test_name,
                "passed": False,
                "reason": "symbol_grep gate failed: " + sym_result.reason,
                "details": sym_result.details,
            }
    return {
        "poc_path": str(poc_path),
        "test_name": test_name,
        "passed": True,
        "reason": "all checks passed",
    }


def check_post_cycle(
    *,
    cycle_dir: Path,
    confirmed_findings: list[dict],
    engine_src_dir: Path | None = None,
    wrapper_src_dir: Path | None = None,
    require_proof_for_high: bool = True,
) -> PostCycleReport:
    """Re-run cheap pre-disclosure gates over every confirmed finding.

    Args:
        cycle_dir:           ``<workspace>/hunts/<cycle_id>/`` — contains
                             the per-finding PoC sources under ``poc/``.
        confirmed_findings:  list of finding-row dicts (from
                             ``db.list_findings``). Each must have
                             ``hypothesis_id`` (used to derive the PoC
                             filename).
        engine_src_dir:      path to engine ``src/`` for grep
        wrapper_src_dir:     optional wrapper ``src/`` for grep
        require_proof_for_high: WS4 proof-carrying gate (default ON). When set, a
                             Critical/High finding that is NOT proof-carrying
                             (see ``proof_carrying`` — no Kani proof, no on-chain
                             LiteSVM exploit) FAILS the gate, so its cycle is held
                             from auto-publish. Signing already happened one step
                             earlier, so the proof status is committed in the signed
                             record; this only holds PUBLISH. Lower-severity findings
                             and findings with no severity are never proof-gated.
                             The operator escape hatch is ``hunt --allow-unproven-publish``.

    Returns:
        ``PostCycleReport`` with ``passed=True`` only when every confirmed
        finding's PoC clears the cheap checks AND (when ``require_proof_for_high``)
        every Critical/High finding is proof-carrying. Caller decides what to do:
        ``hunt.py`` writes ``.publish-blocked`` sentinel and aborts
        auto-publish on failure.
    """
    t0 = time.time()
    search_dirs = [d for d in (engine_src_dir, wrapper_src_dir) if d and d.is_dir()]
    rows: list[dict] = []
    # POST-AUDIT FIX: was reimplementing slug locally with no length cap,
    # diverging from canonical `slug_for_hypothesis` (which truncates at
    # 60 chars). For hypothesis IDs > 60 chars (19 such hyps in the live
    # library), post_cycle looked up `test_<78chars>.rs` while hunt +
    # poc_llm wrote `test_<60chars>.rs` — file not found → publish
    # falsely blocked. Use the canonical helper everywhere.
    #
    # POST-AUDIT FIX #2 (Aptos cycle 20260513-191318): the reconstructed
    # path was ALSO hardcoded to ``cycle_dir/poc/test_<slug>.rs``, but
    # Aptos PoCs live at ``workspace/tests/aptos/test_<slug>.move``,
    # Solidity at ``workspace/tests/solidity/test_<slug>.t.sol``, etc.
    # If the DB finding row has ``poc_path`` populated (it should — the
    # adapter writes it via record_finding), use that authoritative
    # value. Fall back to the canonical Solana path for findings that
    # predate this fix.
    from audit_pipeline.utils.slug import slug_for_hypothesis
    for f in confirmed_findings:
        hyp_id = f.get("hypothesis_id") or ""
        slug = slug_for_hypothesis(hyp_id)
        test_name = f"test_{slug}"
        db_poc_path = f.get("poc_path") or ""
        if db_poc_path and Path(db_poc_path).is_absolute():
            # Patch #11 (audit HIGH e971bf01): poc_path from a DB row
            # is attacker-influenceable (any code path that calls
            # record_finding with a crafted poc_path would land here).
            # An absolute path like /etc/passwd was previously trusted
            # and read by _check_one_poc, leaking its content into the
            # QA report. Round-1: confine to WORKSPACE root (cycle_dir's
            # grandparent — workspace/hunts/<cid> → workspace) so
            # legitimate Aptos/Solidity PoC paths under
            # workspace/tests/<lang>/ still resolve, but /etc/passwd
            # is rejected and falls back to the canonical layout.
            #
            # P11 R1 (code-reviewer MEDIUM + goober MEDIUM): two fixes:
            #   1. Depth-guard `cycle_dir.parent.parent` — if `cycle_dir`
            #      is shallow (e.g. `/CYC123` depth-1), `.parent.parent`
            #      collapses to `/` and `relative_to("/")` succeeds for
            #      EVERY absolute path → no protection. Assert at least
            #      3 path parts (workspace/hunts/cid) before computing.
            #   2. Use the RESOLVED form for the downstream `poc_path`
            #      assignment, not the original `db_poc_path`. The
            #      resolved form follows symlinks consistently with the
            #      check; the original form could re-resolve at read
            #      time and land elsewhere (TOCTOU + dangling-symlink
            #      windows-junction).
            cycle_resolved = cycle_dir.resolve(strict=False)
            if len(cycle_resolved.parts) < 4:
                # depth(workspace) >= 1, +hunts +cid = at least 4 parts.
                # Shallow cycle_dir → refuse to trust the gate and fall
                # back to canonical layout (which is rooted at the
                # caller-supplied cycle_dir, so it's safe).
                poc_path = cycle_dir / "poc" / f"{test_name}.rs"
            else:
                workspace_root = cycle_resolved.parent.parent
                try:
                    _resolved = Path(db_poc_path).resolve(strict=False)
                    _resolved.relative_to(workspace_root)
                    poc_path = _resolved
                except (ValueError, OSError):
                    # Path escapes workspace — refuse, fall back to
                    # canonical layout. The fallback path is also
                    # under cycle_dir → workspace, so it's safe.
                    poc_path = cycle_dir / "poc" / f"{test_name}.rs"
        else:
            # Fallback for legacy rows: assume Solana `.rs` layout.
            poc_path = cycle_dir / "poc" / f"{test_name}.rs"
        row = _check_one_poc(poc_path, test_name, search_dirs)
        row["hypothesis_id"] = hyp_id
        row["finding_id"] = f.get("id")

        # WS4 proof-carrying gate — Critical/High only, default ON. A finding may pass
        # the PoC re-check yet still not be PROVEN; both must hold to publish.
        # Normalize via .value FIRST: `str(Severity.CRITICAL)` is "Severity.CRITICAL",
        # which would silently miss the set (fail-OPEN) if an in-memory caller passed
        # the enum object rather than the stored "Critical" string.
        raw_sev = f.get("severity")
        sev = str(getattr(raw_sev, "value", raw_sev) or "").upper()
        if require_proof_for_high and sev in _PROOF_REQUIRED_SEVERITIES:
            proven, why = proof_carrying(f)
            row["proof_required"] = True
            row["proof_carrying"] = proven
            row["proof_detail"] = why
            if not proven:
                # Hold publish. Combine with any PoC reason rather than clobber it, so
                # a finding that fails BOTH reports both.
                prior = row.get("reason")
                row["passed"] = False
                row["reason"] = f"{prior} | {why}" if prior else why
        else:
            row["proof_required"] = False

        rows.append(row)

    n_failed = sum(1 for r in rows if not r.get("passed"))
    return PostCycleReport(
        passed=(n_failed == 0),
        n_findings=len(rows),
        n_failed=n_failed,
        rows=rows,
        duration_s=time.time() - t0,
    )


PUBLISH_BLOCKED_SENTINEL = ".publish-blocked"


def write_block_sentinel(cycle_dir: Path, report: PostCycleReport) -> Path:
    """Write a sentinel into the cycle dir that publish_cycle.sh skips on.

    The sentinel includes the JSON report so ``publish_cycle.sh`` can
    print the reason for skipping (helps operators debug).
    """
    import json
    sentinel = cycle_dir / PUBLISH_BLOCKED_SENTINEL
    sentinel.write_text(
        json.dumps(report.to_json(), indent=2),
        encoding="utf-8",
    )
    return sentinel


__all__ = [
    "PostCycleReport",
    "PUBLISH_BLOCKED_SENTINEL",
    "check_post_cycle",
    "write_block_sentinel",
]
