"""WS4 — proof-carrying by default.

Before this, the engine confirmed a Critical/High bug and cryptographically signed it
as audited WITHOUT ever checking the formal-proof outcome — a Kani-disproved / cannot-
model / never-run finding was published identically to a proven one. WS4 makes proof
gate PUBLISH (not signing): a Critical/High finding must be PROVEN — Kani-proved OR
demonstrated on-chain via LiteSVM — before its cycle auto-publishes. Operator decision
2026-07-17: EITHER signal suffices (many Solana bugs can't be Kani-modeled but ARE
L4-exploit-confirmed); the un-provable case is signed-but-held, with an explicit
`--allow-unproven-publish` escape hatch.

Findings carry `details_json` as a JSON STRING (that's how the DB stores it), so the
predicate tests feed exactly that. The gate tests stage a REAL passing PoC so proof
status is the only variable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit_pipeline.gates.post_cycle import (
    check_post_cycle,
    proof_carrying,
    unproven_block_reason,
)


def _details(**kw) -> str:
    """A DB-shaped details_json string (what list_findings returns on the row)."""
    return json.dumps(kw)


# ---- the predicate ---------------------------------------------------------

def test_kani_proved_is_proof_carrying():
    ok, why = proof_carrying({"details_json": _details(kani={"proved": True})})
    assert ok and "Kani" in why


def test_litesvm_fired_is_proof_carrying():
    ok, why = proof_carrying({"details_json": _details(litesvm={"fired": True})})
    assert ok and "runtime" in why.lower()


def test_litesvm_crash_found_with_abort_is_proof_carrying():
    # The generic (non-Solana) runtime-fuzz adapter records `crash_found`, not `fired`.
    # A real Move/Solidity/C exploit whose inverted-assertion harness ABORTED
    # (ran_clean=False) is a concrete witness and must count, or it's silently buried.
    ok, why = proof_carrying({"details_json": _details(
        litesvm={"crash_found": True, "ran_clean": False})})
    assert ok and "runtime" in why.lower()


def test_litesvm_crash_found_clean_pass_is_not_proof_alone():
    # The adapter ALSO sets crash_found=True for a clean PASS ("ran end-to-end without
    # abort") — the weaker, mis-authored-harness-prone case. It must NOT count as proof
    # on its own (fail-closed for a disclosure gate): hold for Kani or an abort witness.
    ok, _ = proof_carrying({"details_json": _details(
        litesvm={"crash_found": True, "ran_clean": True})})
    assert ok is False


def test_litesvm_ran_clean_is_not_proof_carrying():
    # crash_found False / ran_clean True = L4 ran and did NOT witness a bug.
    ok, _ = proof_carrying({"details_json": _details(
        litesvm={"crash_found": False, "ran_clean": True})})
    assert ok is False


def test_either_signal_alone_suffices():
    # The operator chose Kani OR LiteSVM, so a finding with only the on-chain exploit
    # (common for Solana, which often can't be Kani-modeled) is proof-carrying.
    assert proof_carrying({"details_json": _details(litesvm={"fired": True},
                                                    kani={"proved": False})})[0] is True


def test_kani_counterexample_is_not_proof_carrying():
    # proved=False with a counterexample means Kani DISPROVED it — not carrying.
    ok, _ = proof_carrying({"details_json": _details(
        kani={"proved": False, "counterexample": True})})
    assert ok is False


def test_kani_cannot_verify_is_not_proof_carrying():
    ok, _ = proof_carrying({"details_json": _details(
        kani={"proved": False, "cannot_verify": True})})
    assert ok is False


def test_legacy_synth_kani_returncode_only_is_not_proof_carrying():
    # The legacy path stores {returncode, harness_dir} with NO `proved` — rc==0 means
    # the harness compiled, NOT that the invariant was proved. Must not count.
    ok, _ = proof_carrying({"details_json": _details(kani={"returncode": 0})})
    assert ok is False


def test_poc_fired_alone_is_not_proof_carrying():
    # A PoC (a cargo test) is neither a formal proof nor an on-chain exploit witness.
    ok, _ = proof_carrying({"poc_fired": True, "details_json": _details(triage={"x": 1})})
    assert ok is False


def test_missing_details_is_not_proof_carrying_failclosed():
    assert proof_carrying({})[0] is False
    assert proof_carrying({"details_json": None})[0] is False


def test_unparseable_details_json_is_not_proof_carrying():
    ok, why = proof_carrying({"details_json": "{not valid json"})
    assert ok is False and "unreadable" in why


def test_accepts_already_deserialized_details_dict():
    # In-memory callers may pass `details` as a dict rather than a JSON string.
    assert proof_carrying({"details": {"kani": {"proved": True}}})[0] is True


# ---- the gate --------------------------------------------------------------

@pytest.fixture
def cycle_with_engine(tmp_path: Path) -> tuple[Path, Path]:
    cycle = tmp_path / "hunts" / "20260717-cycle1"
    (cycle / "poc").mkdir(parents=True)
    engine_src = tmp_path / "engine" / "src"
    engine_src.mkdir(parents=True)
    (engine_src / "lib.rs").write_text(
        "pub fn compute_trade_pnl(a: i128, b: i128) -> i128 { 0 }\n")
    return cycle, engine_src


def _finding(cycle: Path, hyp: str, *, severity: str | None, details_json: str | None,
             poc_body: str | None = None) -> dict:
    """Stage a REAL passing PoC (cites a real engine symbol, no pseudo-pass markers)
    unless poc_body overrides it, and return the matching finding row."""
    from audit_pipeline.utils.slug import slug_for_hypothesis

    slug = slug_for_hypothesis(hyp)
    body = poc_body if poc_body is not None else (
        f"#[test]\nfn test_{slug}() {{ compute_trade_pnl(1, 2); }}\n")
    (cycle / "poc" / f"test_{slug}.rs").write_text(body)
    return {"id": hash(hyp) & 0xffff, "hypothesis_id": hyp, "poc_fired": True,
            "severity": severity, "details_json": details_json}


def test_critical_unproven_holds_publish(cycle_with_engine):
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "C1", severity="Critical", details_json=_details(kani={"returncode": 0}))
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is False                       # cycle held from publish
    assert rep.rows[0]["proof_required"] is True
    assert rep.rows[0]["proof_carrying"] is False
    assert "not proof-carrying" in rep.rows[0]["reason"]


def test_critical_kani_proved_publishes(cycle_with_engine):
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "C1", severity="Critical", details_json=_details(kani={"proved": True}))
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is True
    assert rep.rows[0]["proof_carrying"] is True


def test_high_litesvm_exploited_publishes(cycle_with_engine):
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "H1", severity="High", details_json=_details(litesvm={"fired": True}))
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is True


def test_medium_unproven_is_not_gated(cycle_with_engine):
    # Only Critical/High are proof-gated — a Medium publishes on its PoC alone.
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "M1", severity="Medium", details_json=_details(kani={"returncode": 0}))
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is True
    assert rep.rows[0]["proof_required"] is False


def test_no_severity_is_not_gated(cycle_with_engine):
    # Legacy/severity-less rows keep the pre-WS4 behavior (this is why existing tests
    # still pass): no severity => not proof-gated.
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "N1", severity=None, details_json=None)
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is True
    assert rep.rows[0]["proof_required"] is False


def test_escape_hatch_allows_unproven_critical(cycle_with_engine):
    # `hunt --allow-unproven-publish` => require_proof_for_high=False.
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "C1", severity="Critical", details_json=_details(kani={"returncode": 0}))
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src,
                           require_proof_for_high=False)
    assert rep.passed is True
    assert rep.rows[0]["proof_required"] is False


def test_failing_poc_and_unproven_reports_both_reasons(cycle_with_engine):
    # A finding that fails the PoC re-check AND is unproven must surface BOTH, not clobber.
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "C1", severity="Critical", details_json=_details(kani={"returncode": 0}),
                 poc_body="#[test]\nfn test_c1() { unimplemented!() }\n")   # pseudo-pass marker
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is False
    reason = rep.rows[0]["reason"]
    assert "pseudo-pass" in reason and "not proof-carrying" in reason


def test_proven_critical_still_blocked_if_poc_is_bad(cycle_with_engine):
    # Proof is ADDITIVE to the PoC check, not a replacement — a proven finding whose PoC
    # is stubbed must still be blocked.
    cycle, engine_src = cycle_with_engine
    f = _finding(cycle, "C1", severity="Critical", details_json=_details(kani={"proved": True}),
                 poc_body="#[test]\nfn test_c1() { todo!() }\n")
    rep = check_post_cycle(cycle_dir=cycle, confirmed_findings=[f], engine_src_dir=engine_src)
    assert rep.passed is False
    assert "pseudo-pass" in rep.rows[0]["reason"]


# ---- the theater-killer: REAL DB round-trip (no hand-crafted rows) ----------

def test_real_db_roundtrip_severity_and_litesvm_persist_and_gate(tmp_path):
    """The gates flagged that all other tests hand-craft the finding dict, so the two
    things PRODUCTION depends on — severity stored as the gate-readable string, and
    litesvm actually landing in details_json — had ZERO coverage. This drives a REAL
    FindingsDB: persist a Critical whose only proof is litesvm.fired, read it back the
    way hunt's gate does (list_findings), and assert the gate PUBLISHES it; then a
    Critical with no proof and assert it's HELD."""
    from audit_pipeline.db import open_findings_db
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity

    (tmp_path / "workspace.json").write_text("{}", encoding="utf-8")
    db = open_findings_db(tmp_path)
    tid = db.upsert_target(name="perc")
    db.insert_cycle(target_id=tid, cycle_id="cy-1")

    # Persist EXACTLY as hunt does: Severity enum in, details dict with a litesvm block.
    db.upsert_finding(target_id=tid, cycle_id="cy-1", hypothesis_id="PROVEN",
                      verdict="TRUE", confidence="high", severity=Severity.CRITICAL,
                      status=Status.CONFIRMED, poc_fired=True,
                      details={"kani": {"returncode": 0}, "litesvm": {"fired": True}})
    db.upsert_finding(target_id=tid, cycle_id="cy-1", hypothesis_id="UNPROVEN",
                      verdict="TRUE", confidence="high", severity=Severity.CRITICAL,
                      status=Status.CONFIRMED, poc_fired=True,
                      details={"kani": {"returncode": 0}, "litesvm": {"fired": False}})

    rows = {r["hypothesis_id"]: r for r in db.list_findings(limit=100)}
    # 1. severity stored as the gate-readable value, not "Severity.CRITICAL".
    assert rows["PROVEN"]["severity"] == "Critical"
    # 2. litesvm actually persisted into details_json (the wiring the gate depends on).
    assert '"litesvm"' in rows["PROVEN"]["details_json"]
    # 3. the gate reads the real rows and decides correctly.
    assert proof_carrying(rows["PROVEN"])[0] is True
    assert proof_carrying(rows["UNPROVEN"])[0] is False
    assert unproven_block_reason(rows["PROVEN"]) is None
    assert unproven_block_reason(rows["UNPROVEN"]) is not None


def test_enum_severity_object_still_gates(tmp_path):
    # A caller passing the Severity ENUM (not its .value string) must still gate —
    # str(Severity.CRITICAL) is "Severity.CRITICAL", which naively fails OPEN.
    from audit_pipeline.severity import Severity
    assert unproven_block_reason(
        {"severity": Severity.CRITICAL, "details_json": _details(kani={"returncode": 0})}
    ) is not None


def test_unproven_block_reason_only_gates_high_severity():
    # The shared helper (used by the disclosure exits) must match the gate: only
    # Critical/High are held; Medium/None pass.
    unproven = _details(kani={"returncode": 0})
    assert unproven_block_reason({"severity": "Critical", "details_json": unproven}) is not None
    assert unproven_block_reason({"severity": "High", "details_json": unproven}) is not None
    assert unproven_block_reason({"severity": "Medium", "details_json": unproven}) is None
    assert unproven_block_reason({"severity": None, "details_json": None}) is None


# ---- the OTHER exits: disclosure must honor the gate too --------------------

def _db_with_finding(tmp_path: Path, *, severity, details, hyp: str = "H"):
    from audit_pipeline.db import open_findings_db
    from audit_pipeline.lifecycle import Status

    (tmp_path / "workspace.json").write_text("{}", encoding="utf-8")
    db = open_findings_db(tmp_path)
    tid = db.upsert_target(name="perc")
    db.insert_cycle(target_id=tid, cycle_id="cy-1")
    db.upsert_finding(target_id=tid, cycle_id="cy-1", hypothesis_id=hyp,
                      verdict="TRUE", confidence="high", severity=severity,
                      status=Status.CONFIRMED, poc_fired=True, details=details)
    return db


def test_issue_file_refuses_unproven_critical(tmp_path):
    # The GitHub-issue exit was previously ungated — an unproven Critical could be
    # disclosed publicly. It must now refuse (fail-closed) before touching gh.
    from click.testing import CliRunner

    from audit_pipeline.cli import main
    from audit_pipeline.severity import Severity

    db = _db_with_finding(tmp_path, severity=Severity.CRITICAL,
                          details={"litesvm": {"fired": False}})
    fid = db.list_findings(limit=10)[0]["id"]
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "issue", "file",
                                    "--finding-id", str(fid), "--repo", "o/r", "--dry-run"])
    assert res.exit_code != 0
    out = " ".join(res.output.split())
    assert "not proof-carrying" in out and "Refusing to disclose" in out


def test_auto_file_skips_unproven_critical(tmp_path):
    # Batch auto-file must SKIP (not crash on) an unproven Critical — nothing eligible,
    # nothing filed, no gh call.
    from click.testing import CliRunner

    from audit_pipeline.cli import main
    from audit_pipeline.severity import Severity

    _db_with_finding(tmp_path, severity=Severity.CRITICAL,
                     details={"litesvm": {"fired": False}})
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "issue", "auto-file-confirmed",
                                    "--cycle-id", "cy-1", "--repo", "o/r"])
    assert res.exit_code == 0, res.output
    out = " ".join(res.output.split())
    assert "not proof-carrying" in out          # surfaced as a skip reason
    assert "survived auto-file gates" in out    # nothing eligible


def test_notify_critical_refuses_unproven_email(tmp_path):
    """The customer-email door. publish_cycle.sh selects on status='confirmed' (which
    includes L4-refuted rows the cycle gate no longer holds after the fix pass), so this
    door must proof-gate itself or it emails a customer 'exploit confirmed' for a bug the
    pipeline's own L4 refuted. Regression opened by narrowing the cycle gate."""
    from click.testing import CliRunner

    from audit_pipeline.cli import main
    from audit_pipeline.severity import Severity

    db = _db_with_finding(tmp_path, severity=Severity.CRITICAL,
                          details={"litesvm": {"fired": False}})  # L4-refuted
    fid = db.list_findings(limit=10)[0]["id"]
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "notify", "critical",
                                    "--finding-id", str(fid), "--dry-run"])
    assert res.exit_code != 0, res.output
    out = " ".join(res.output.split())
    assert "not proof-carrying" in out and "Refusing to email" in out


def test_notify_critical_sends_proven_email(tmp_path):
    # A proven Critical passes the gate (dry-run, so no SMTP).
    from click.testing import CliRunner

    from audit_pipeline.cli import main
    from audit_pipeline.severity import Severity

    db = _db_with_finding(tmp_path, severity=Severity.CRITICAL,
                          details={"litesvm": {"fired": True}})   # proven
    fid = db.list_findings(limit=10)[0]["id"]
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "notify", "critical",
                                    "--finding-id", str(fid), "--dry-run"])
    # Passes the proof gate — no "Refusing to email". (May no-op on notifier config,
    # but the proof gate must not be the thing that stops it.)
    assert "Refusing to email" not in res.output
