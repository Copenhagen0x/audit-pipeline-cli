"""WS1 `converge` orchestrator — unit + loop + real-DB coverage.

converge wraps `hunt` in an outer loop, derives siblings from what each round
confirmed, and dispatches those next — stopping only when it can honestly claim
convergence (and exiting non-zero when it cannot).

READ THIS BEFORE ADDING A MOCK. Three total-failure bugs shipped past an all-green
suite here because the mocks replaced the exact code that was broken:
  - mocking `subprocess.run` accepts ANY argv, so an argv-SHAPE bug was invisible
    -> `test_round2_argv_parses_against_the_real_hunt_cli` parses against real hunt;
  - mocking `open_findings_db` replaced the DB-path resolution that silently diverged
    from hunt's, making converge blind to hunt's cycles -> `test_db_root_*` +
    `test_real_db_roundtrip_*` use a REAL FindingsDB on a real eval-layout tree;
  - hand-planting sibling YAML proved converge can dispatch siblings that exist, never
    that siblings ever COME to exist (hunt fires no derivation hook) ->
    `test_derives_siblings_from_confirmed_findings` pins that converge derives them.
A green suite is not evidence. Prefer the real object.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

import audit_pipeline.commands.converge as cv
from audit_pipeline.cli import main
from audit_pipeline.commands.hunt import hunt_cmd

# ---- arg stripping ---------------------------------------------------------

def test_strip_drops_surface_scan_AND_its_path():
    # hunt defines --surface-scan as click.Path(exists=True, file_okay=False) — it
    # takes a VALUE. Stripping only the flag name leaves the path as a stray
    # positional in round 2+, which click rejects ("Got unexpected extra argument").
    args = ["--surface-scan", "/repo/path", "--source-repo", "o/r", "--skip-poc"]
    assert cv._strip_converge_owned(args) == ["--source-repo", "o/r", "--skip-poc"]


def test_strip_removes_every_hyp_source_flag_keeps_rest():
    args = ["--surface-scan", "/p", "--hypotheses", "h.yaml", "-h", "other.yaml",
            "--protocol-class", "perp_dex", "--skip-poc"]
    assert cv._strip_converge_owned(args) == ["--skip-poc"]


def test_strip_removes_resume_cycle():
    # A resumed cycle pre-exists the before-snapshot -> never registers as new -> the
    # round's real confirmed findings would silently count as 0.
    assert cv._strip_converge_owned(["--resume-cycle", "20260101-000000", "--skip-poc"]) \
        == ["--skip-poc"]


def test_strip_handles_equals_form():
    args = ["--hypotheses=h.yaml", "--protocol-class=amm_cp", "--surface-scan=/p",
            "--resume-cycle=c1", "--max-concurrent=4"]
    assert cv._strip_converge_owned(args) == ["--max-concurrent=4"]


def test_round2_argv_parses_against_the_real_hunt_cli(tmp_path):
    """converge's round-2 argv must PARSE against the real hunt CLI. `make_context`
    runs click's parser without executing, so a stray positional surfaces here rather
    than as a mystery "round 2 hunt exited 2" at runtime."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lib = tmp_path / "round2.yaml"
    lib.write_text("hypotheses: []", encoding="utf-8")
    passthrough = cv._strip_converge_owned(
        ["--surface-scan", str(repo), "--source-repo", "o/r", "--skip-poc"])
    assert str(repo) not in passthrough
    ctx = hunt_cmd.make_context("hunt", [*passthrough, "--hypotheses", str(lib)])
    ctx.close()                                   # no UsageError == argv well-formed


# ---- arg parsing delegated to click (never hand-rolled) --------------------

@pytest.mark.parametrize("args", [
    ["--target-name", "perc"], ["-t", "perc"], ["-tperc"], ["--target-name=perc"],
])
def test_parse_hunt_args_sees_every_target_name_form(args):
    # Hand-rolled scanning missed `-t`/`-tVALUE`, silently mis-scoping snapshots into a
    # false "converged". Delegating to hunt's own parser can't drift from its options.
    assert cv._parse_hunt_args(tuple(args))["target_name"] == "perc"


def test_parse_hunt_args_rejects_bad_args_as_preflight():
    # Bad operator args must fail ONCE here, not as N mystery "round exited 2" lines.
    # click.UsageError specifically — converge catches exactly that to re-report it.
    with pytest.raises(click.UsageError):
        cv._parse_hunt_args(("--surface-scan", "/definitely/not/a/real/dir"))


# ---- DB root: must match hunt's, or converge is blind ----------------------

def test_db_root_is_workspace_without_customer_id(tmp_path):
    assert cv._resolve_db_root(tmp_path, {}) == tmp_path


def test_db_root_follows_hunt_into_the_shared_eval_db(tmp_path):
    # hunt redirects to the shared customer DB when workspace.json declares customer_id
    # (hunt.py:420 + 944-955). If converge doesn't follow, it reads a DB hunt never
    # writes -> sees no cycles -> reports a false "converged".
    cell = tmp_path / "ottersec-eval" / "workspaces" / "cell1"
    cell.mkdir(parents=True)
    assert cv._resolve_db_root(cell, {"customer_id": "ottersec"}) == (tmp_path / "ottersec-eval")


def test_db_root_matches_hunt_on_a_fresh_eval_with_no_shared_db_yet(tmp_path):
    # The divergence case: db._resolve_findings_db_path also requires shared.is_file(),
    # so on a FIRST-EVER eval run a plain open_findings_db(workspace) binds to the cell
    # while hunt writes the eval root. converge must follow hunt, not the resolver.
    cell = tmp_path / "cust-eval" / "workspaces" / "cellA"
    cell.mkdir(parents=True)
    assert not (tmp_path / "cust-eval" / "findings.db").exists()
    assert cv._resolve_db_root(cell, {"customer_id": "cust"}) == (tmp_path / "cust-eval")


def test_real_db_roundtrip_converge_reads_what_hunt_writes(tmp_path):
    """REAL FindingsDB, real eval layout: converge's resolved DB must be the same FILE
    hunt would write. A mocked DB cannot catch this — it's why the bug shipped."""
    from audit_pipeline.db import _resolve_findings_db_path, open_findings_db

    cell = tmp_path / "acme-eval" / "workspaces" / "cell1"
    cell.mkdir(parents=True)
    config = {"customer_id": "acme", "target_name": "acme-prog"}
    (cell / "workspace.json").write_text(json.dumps(config), encoding="utf-8")

    # hunt's own derivation (hunt.py:944-955), replicated here as the oracle.
    hunt_db_workspace = cell.resolve().parent.parent
    hunt_db_path = _resolve_findings_db_path(hunt_db_workspace)

    converge_root = cv._resolve_db_root(cell, config)
    converge_db = open_findings_db(converge_root)
    assert Path(converge_db.path).resolve() == Path(hunt_db_path).resolve()

    # ...and a cycle hunt writes must be visible to converge's own reads.
    tid = converge_db.upsert_target(name="acme-prog")
    converge_db.insert_cycle(target_id=tid, cycle_id="cy-1")
    assert "cy-1" in {c["cycle_id"] for c in converge_db.list_cycles(target_id=tid)}
    # ...and converge's read-only target resolution finds the row hunt's upsert created.
    assert cv._resolve_target_id(converge_db, "acme-prog") == tid


def test_sibling_roots_are_cell_local_only(tmp_path):
    """The shared eval root must NOT be read. The lifecycle hook roots itself at the
    DB file's parent (db.py:720), which under the customer-eval rollup is shared by
    every cell — reading it makes converge on cell A dispatch cell B's hypotheses
    against cell A's target, with no scoping backstop (scoping.filter_hypotheses has
    no callers, and absent `applies_to` defaults to ["*"])."""
    ws = tmp_path / "acme-eval" / "workspaces" / "cellA"
    roots = cv._sibling_roots(ws)
    assert roots == [ws / "derived"]
    assert (ws.parent.parent / "derived") not in roots        # the shared eval root


# ---- confirmed vs dispatched ------------------------------------------------

class _FakeDB:
    def __init__(self, cycles=(), findings_by_cycle=None, target=None):
        self._cycles = list(cycles)
        self._fbc = findings_by_cycle or {}
        self._target = target

    def list_cycles(self, target_id=None, limit=50):
        return [c for c in self._cycles
                if target_id is None or c.get("target_id") == target_id]

    def list_findings_by_cycle(self, cycle_id):
        return list(self._fbc.get(cycle_id, []))

    def get_target(self, name):
        return self._target


def test_cycle_hyp_ids_separates_confirmed_dispatched_and_finding_ids():
    db = _FakeDB(findings_by_cycle={"c1": [
        {"id": 1, "hypothesis_id": "H1", "status": "confirmed"},
        {"id": 2, "hypothesis_id": "H2", "status": "rejected"},   # dispatched, not confirmed
        {"id": 3, "hypothesis_id": "H3", "status": "confirmed"},
    ]})
    confirmed, dispatched, findings = cv._cycle_hyp_ids(db, "c1")
    assert confirmed == {"H1", "H3"}
    # H2 was investigated and rejected — it MUST count as dispatched, or next round
    # re-globs its YAML as "fresh" and re-pays its full pipeline cost.
    assert dispatched == {"H1", "H2", "H3"}
    assert [f["id"] for f in findings] == [1, 3]   # only confirmed findings get derived
    # Full rows, not ids: naming the sibling file the way triage-siblings expects
    # needs hypothesis_id, not just id.
    assert findings[0]["hypothesis_id"] == "H1"


def test_resolve_target_id_reads_row():
    assert cv._resolve_target_id(_FakeDB(target={"id": 7}), "perc") == 7


def test_resolve_target_id_none_when_target_row_absent():
    # init never creates the target row (only hunt's upsert does), so a first-ever run
    # resolves None. converge must re-resolve per round rather than latch this.
    assert cv._resolve_target_id(_FakeDB(target=None), "perc") is None


# ---- sibling collection: dedup, gate reporting, bounds ---------------------

def _write_siblings(path: Path, ids, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    hyps = []
    for i in ids:
        h = {"id": i, "class": "arithmetic",
             # load_hypotheses enforces a >=20-char claim — keep fixtures schema-real
             # so these tests exercise the actual loader, not a lenient stand-in.
             "claim": f"rounding truncation in the {i} settlement path loses lamports",
             "severity": "high", "bug_class": "rounding"}
        if extra:
            h.update(extra)
        hyps.append(h)
    path.write_text(yaml.safe_dump({"hypotheses": hyps}), encoding="utf-8")


def test_dispatched_sibling_is_not_recollected(tmp_path):
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", ["SIB-A", "SIB-B"])
    fresh, dropped, blocked = cv._load_new_siblings([tmp_path / "derived"], {"SIB-A"})
    assert [h["id"] for h in fresh] == ["SIB-B"]
    assert dropped == 0 and blocked == []


def test_default_dispatches_pending_and_approved(tmp_path):
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", ["SIB-P"])
    _write_siblings(tmp_path / "derived" / "approved" / "F2-siblings.yaml", ["SIB-Q"])
    fresh, _, _ = cv._load_new_siblings([tmp_path / "derived"], set())
    assert {h["id"] for h in fresh} == {"SIB-P", "SIB-Q"}       # autonomous default


def test_approved_only_ignores_the_pending_queue(tmp_path):
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", ["SIB-P"])
    _write_siblings(tmp_path / "derived" / "approved" / "F2-siblings.yaml", ["SIB-Q"])
    fresh, _, _ = cv._load_new_siblings([tmp_path / "derived"], set(), approved_only=True)
    assert {h["id"] for h in fresh} == {"SIB-Q"}                # human gate restored


def test_gate_blocked_siblings_are_reported_not_swallowed(tmp_path):
    # An audited repo can shape every sibling to carry a rejected `prior_disclosure`
    # (the field round-trips verbatim from LLM output). Swallowing the gate's skip-list
    # lets the repo empty the queue and have converge call that "converged".
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", ["SIB-X"],
                    extra={"prior_disclosure": {"decision": "rejected"}})
    fresh, _, blocked = cv._load_new_siblings([tmp_path / "derived"], set())
    assert fresh == []
    assert blocked and blocked[0][0] == "SIB-X"                 # id + reason surfaced


def test_max_batch_bounds_round_size(tmp_path):
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", [f"SIB-{i}" for i in range(10)])
    fresh, dropped, _ = cv._load_new_siblings([tmp_path / "derived"], set(), max_batch=3)
    assert len(fresh) == 3 and dropped == 7                     # over-cap NOT silent


def test_max_batch_zero_means_no_cap(tmp_path):
    _write_siblings(tmp_path / "derived" / "F1-siblings.yaml", [f"SIB-{i}" for i in range(10)])
    fresh, dropped, _ = cv._load_new_siblings([tmp_path / "derived"], set(), max_batch=0)
    assert len(fresh) == 10 and dropped == 0


def test_unreadable_derived_file_does_not_crash_the_loop(tmp_path):
    d = tmp_path / "derived"
    d.mkdir(parents=True)
    (d / "bad-siblings.yaml").write_text("{[not: valid: yaml", encoding="utf-8")
    _write_siblings(d / "F1-siblings.yaml", ["SIB-OK"])
    fresh, _, _ = cv._load_new_siblings([d], set())
    assert [h["id"] for h in fresh] == ["SIB-OK"]


# ---- CLI: dry-run + guards -------------------------------------------------

def _ws(tmp_path, config=None):
    (tmp_path / "workspace.json").write_text(json.dumps(config or {}), encoding="utf-8")
    return str(tmp_path)


def test_dry_run_prints_plan_without_running(monkeypatch, tmp_path):
    called = {"n": 0}
    monkeypatch.setattr(cv.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--dry-run", "--skip-poc"])
    assert res.exit_code == 0, res.output
    assert "converge plan" in res.output
    assert "PENDING" in res.output              # the autonomous posture is disclosed
    assert called["n"] == 0


def test_dry_run_discloses_approved_only(tmp_path):
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--dry-run", "--approved-only"])
    assert res.exit_code == 0, res.output
    assert "APPROVED only" in res.output


def test_missing_workspace_errors(tmp_path):
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "converge", "--skip-poc"])
    assert res.exit_code != 0
    assert "workspace.json" in res.output


def test_negative_sibling_cap_rejected(tmp_path):
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge",
                                    "--max-siblings-per-round", "-1"])
    assert res.exit_code != 0
    assert "--max-siblings-per-round" in res.output


def test_bad_hunt_args_fail_preflight_once(tmp_path):
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge",
                                    "--surface-scan", "/no/such/dir"])
    assert res.exit_code != 0
    assert "hunt rejected these arguments" in res.output


# ---- CLI: the loop ---------------------------------------------------------

def _patch(monkeypatch, *, returncode=0, cycles_after_run=True, findings=(),
           argvs=None, n_dispatched=5, derive_rc=0, history=()):
    """Mock the hunt subprocess + findings DB.

    `n_dispatched` defaults to a NON-zero value because that is what a healthy hunt
    round produces. A fake that omitted it (defaulting to 0) is what let the
    zero-dispatch false-"converged" bug pass the suite.

    `derive_rc` exists because the previous version hard-coded derive-siblings to ALWAYS
    succeed — which is precisely why the fail-fast path and the derivation-failed abort
    were both unexercised while looking covered.
    """
    state = {"ran": 0, "derives": 0}

    class _CP:
        pass

    def fake_run(argv, **kw):
        if argvs is not None:
            argvs.append(list(argv))
        cp = _CP()
        if "derive-siblings" in argv:
            state["derives"] += 1
            cp.returncode = derive_rc
            return cp
        state["ran"] += 1
        cp.returncode = returncode
        return cp
    monkeypatch.setattr(cv.subprocess, "run", fake_run)

    class _DB:
        path = "/tmp/findings.db"

        def list_cycles(self, target_id=None, limit=50):
            if not (state["ran"] and cycles_after_run):
                return []
            return [{"cycle_id": "cyN", "total_cost_usd": 0.0,
                     "n_dispatched": n_dispatched}]

        def list_findings_by_cycle(self, cid):
            return [dict(f) for f in findings]

        def list_findings(self, target_id=None, limit=50):
            return [dict(h) for h in history]

        def get_target(self, name):
            return {"id": 1}

    monkeypatch.setattr("audit_pipeline.db.open_findings_db", lambda ws: _DB())
    return state


def test_derivation_failure_is_diagnosed_not_called_converged(monkeypatch, tmp_path):
    # Every derivation failing (no ANTHROPIC_API_KEY, budget spent) must never read as
    # convergence, and the message must name the likely cause rather than saying
    # "produced nothing" and sending the operator to debug the wrong thing.
    _patch(monkeypatch, findings=[{"id": 1, "hypothesis_id": "H1", "status": "confirmed"}],
           derive_rc=1)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    assert res.exit_code != 0, res.output
    assert "ANTHROPIC_API_KEY" in res.output
    assert "converged —" not in res.output


def test_derivation_fails_fast_instead_of_spawning_the_doomed_tail(monkeypatch, tmp_path):
    # 50 confirmed findings + a systemic failure must not mean 50 doomed ~2s subprocesses.
    many = [{"id": i, "hypothesis_id": f"H{i}", "status": "confirmed"} for i in range(50)]
    state = _patch(monkeypatch, findings=many, derive_rc=1)
    CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    assert state["derives"] <= cv._DERIVE_FAILFAST_STREAK, state["derives"]


def test_approved_only_parks_siblings_for_review_and_does_not_abort(monkeypatch, tmp_path):
    # --approved-only is the human gate, not a failure mode: derivation succeeds, the
    # siblings sit in derived/ awaiting `triage-siblings approve`. Reporting that as
    # "aborted" made the safe mode look broken and pushed operators to the autonomous one.
    _patch(monkeypatch, findings=[{"id": 1, "hypothesis_id": "H1", "status": "confirmed"}])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--approved-only",
                                    "--max-rounds", "1"])
    assert "awaiting review" in res.output
    assert "converge incomplete" in res.output           # not "aborted"
    assert "triage-siblings list" in res.output          # tells the operator what to do


def test_budget_warning_does_not_fire_without_an_explicit_cap(monkeypatch, tmp_path):
    # make_context returns DEFAULTS, and hunt's --budget-cap-usd default is $1,000,000 —
    # so a naive truthiness check fired this warning on every single run. A warning that
    # always fires is one the operator learns to ignore.
    _patch(monkeypatch, findings=[])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--skip-poc",
                                    "--max-rounds", "1"])
    assert "PER CYCLE" not in res.output


def test_budget_warning_fires_when_the_operator_sets_a_cap(monkeypatch, tmp_path):
    _patch(monkeypatch, findings=[])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "2",
                                    "--budget-cap-usd", "100"])
    assert "PER CYCLE" in res.output


def test_max_total_usd_stops_before_paying_for_derivation(monkeypatch, tmp_path):
    # The bound must be checked BEFORE the spend, with a real number — derivation runs
    # outside a hunt cycle, so counting only cycles.total_cost_usd would let it through.
    many = [{"id": i, "hypothesis_id": f"H{i}", "status": "confirmed"} for i in range(50)]
    state = _patch(monkeypatch, findings=many)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-total-usd",
                                    "1.00", "--max-rounds", "1"])
    assert state["derives"] == 0, "spent before checking the budget"
    assert "exceeds --max-total-usd" in res.output
    assert res.exit_code != 0


def test_rejected_siblings_are_not_re_derived(monkeypatch, tmp_path):
    """A human's `triage-siblings reject` must survive the next run. converge writes into
    the dir triage manages and the slug is stable, so without this the rejected file is
    silently re-created as pending and re-dispatched — while `show` still says rejected."""
    _patch(monkeypatch, findings=[{"id": 1, "hypothesis_id": "H1", "status": "confirmed"}])
    monkeypatch.setattr("audit_pipeline.commands.triage_siblings._find_sibling_file",
                        lambda ws, fid: (Path("x/H1-siblings.yaml"), "rejected"))
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    assert "REJECTED by an operator" in res.output


def test_history_seeds_the_dispatched_set(monkeypatch, tmp_path):
    # derived/ is triage's queue so converge may never delete from it — meaning every
    # sibling ever written is still on disk. Without seeding from history, each scheduled
    # run re-dispatches the union of all prior runs, forever.
    _patch(monkeypatch, findings=[],
           history=[{"hypothesis_id": "OLD-1"}, {"hypothesis_id": "OLD-2"}])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    assert "2 hypothesis id(s) already audited" in res.output


def test_converges_when_nothing_left_to_audit(monkeypatch, tmp_path):
    # Round 1 confirms nothing, derives nothing, no siblings -> honest convergence.
    _patch(monkeypatch, findings=[{"id": 1, "hypothesis_id": "H1", "status": "rejected"}])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--skip-poc",
                                    "--max-rounds", "3"])
    assert res.exit_code == 0, res.output
    assert "converged — nothing left to audit" in res.output


def test_failed_round_exits_nonzero(monkeypatch, tmp_path):
    # A failed round must NOT report success: an unattended harness has to be able to
    # tell "converged" from "audited nothing".
    _patch(monkeypatch, returncode=2)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--skip-poc"])
    assert res.exit_code != 0, res.output
    assert "exited 2" in res.output
    assert "converge aborted" in res.output


def test_invisible_cycle_aborts_instead_of_reporting_clean(monkeypatch, tmp_path):
    """hunt exits 0 but converge sees no new cycle => converge is reading a different
    findings.db than hunt writes. Reporting "0 confirmed" here is a false clean bill of
    health, which is the worst possible failure for an autonomy claim."""
    _patch(monkeypatch, returncode=0, cycles_after_run=False)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--skip-poc"])
    assert res.exit_code != 0, res.output
    assert "no new cycle is visible" in res.output
    assert "converged" not in res.output.replace("converge aborted", "")


def test_derives_siblings_from_confirmed_findings(monkeypatch, tmp_path):
    """THE load-bearing test: hunt fires no derivation hook, so if converge doesn't
    derive siblings itself the loop can never reach round 2 unattended. Pins that
    converge invokes `derive-siblings` for each confirmed finding."""
    argvs: list[list[str]] = []
    _patch(monkeypatch, findings=[{"id": 42, "hypothesis_id": "H1", "status": "confirmed"}],
           argvs=argvs)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    derive_calls = [a for a in argvs if "derive-siblings" in a]
    assert derive_calls, f"converge never derived siblings; argvs={argvs}"
    assert "42" in derive_calls[0]               # the confirmed finding's id
    assert res.exit_code != 0                    # derivation produced nothing -> not clean


def test_no_derive_flag_skips_derivation(monkeypatch, tmp_path):
    argvs: list[list[str]] = []
    _patch(monkeypatch, findings=[{"id": 42, "hypothesis_id": "H1", "status": "confirmed"}],
           argvs=argvs)
    CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--no-derive",
                              "--max-rounds", "1"])
    assert not [a for a in argvs if "derive-siblings" in a]


def test_resume_cycle_is_warned_and_not_forwarded(monkeypatch, tmp_path):
    """Assert the ARGV, not the console string. The previous version of this test
    asserted only that the warning printed — so it passed while round 1 forwarded the
    flag anyway (round 1 used raw hunt_args; only rounds 2+ were stripped). hunt then
    reused the pinned cycle, no new cycle appeared, and converge's own safety net
    aborted blaming DB divergence for a flag converge itself forwarded. A test named
    after a behaviour must test that behaviour."""
    argvs: list[list[str]] = []
    _patch(monkeypatch, findings=[], argvs=argvs)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge",
                                    "--resume-cycle", "c-old"])
    assert "--resume-cycle is not forwarded" in res.output
    hunt_argv = [a for a in argvs if "hunt" in a][0]
    assert "--resume-cycle" not in hunt_argv, hunt_argv     # round 1 too
    assert "c-old" not in hunt_argv, hunt_argv


def test_zero_dispatch_cycle_never_claims_converged(monkeypatch, tmp_path):
    """hunt's Layer-1 failure path (recon timeout/crash/outage, hunt.py:1266-1269)
    finishes the cycle with n_dispatched=0 and RETURNS EXIT 0. rc==0 and a real cycle
    both look healthy, so the "no new cycle" net can't catch it. Without an explicit
    check converge prints "converged, 0 findings, exit 0" for an audit that never ran —
    a false clean bill of health on a repo full of bugs.

    It must NOT diagnose a cause either: a legitimate round can dispatch 0 (a
    --diff-since-sha commit touching no hypothesis's target_file), and crying "outage"
    on a README commit teaches an operator to ignore the alarm."""
    _patch(monkeypatch, returncode=0, findings=[], n_dispatched=0)
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--skip-poc"])
    assert res.exit_code != 0, res.output                 # never a clean bill of health
    assert "dispatched 0 hypotheses" in res.output
    assert "converge incomplete" in res.output            # not "aborted": nothing crashed
    assert "cannot tell which" in res.output              # no false diagnosis
    assert "converged —" not in res.output


def test_no_derive_with_confirmed_findings_is_not_a_convergence(monkeypatch, tmp_path):
    # --no-derive + confirmed findings + no siblings on disk = one round, nothing
    # expanded. Reporting "converged" there is the round-2 F1 bug behind a flag.
    _patch(monkeypatch, findings=[{"id": 7, "hypothesis_id": "H1", "status": "confirmed"}])
    res = CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--no-derive",
                                    "--max-rounds", "1"])
    assert res.exit_code != 0, res.output
    assert "never expanded into siblings" in res.output


def test_derivation_uses_triage_siblings_naming(monkeypatch, tmp_path):
    """converge's derived files must land where `triage-siblings` looks — otherwise its
    output is structurally unreviewable and --approved-only can never pass. Naming comes
    from triage's own _slug_for_finding, so the two cannot drift."""
    from audit_pipeline.commands.triage_siblings import _slug_for_finding

    argvs: list[list[str]] = []
    _patch(monkeypatch, findings=[{"id": 42, "hypothesis_id": "PERP-001",
                                   "status": "confirmed"}], argvs=argvs)
    CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    derive = [a for a in argvs if "derive-siblings" in a][0]
    out = Path(derive[derive.index("--output") + 1])
    expected = _slug_for_finding({"id": 42, "hypothesis_id": "PERP-001"})
    assert out.name == f"{expected}-siblings.yaml"
    assert out.parent == tmp_path / "derived"      # triage's dir, not a private one


def test_workspace_passed_to_hunt_is_absolute(monkeypatch, tmp_path):
    """Uses a genuinely RELATIVE -w. The previous version passed an already-absolute
    tmp_path, so it passed even with the `.resolve()` deleted — a green test guarding
    nothing. A relative -w reaches the child and gets re-resolved against the child's
    cwd (`-w myws` inside myws => myws/myws => hunt exits 1)."""
    argvs: list[list[str]] = []
    _patch(monkeypatch, findings=[], argvs=argvs)
    ws = tmp_path / "myws"
    ws.mkdir()
    (ws / "workspace.json").write_text("{}", encoding="utf-8")
    with CliRunner().isolated_filesystem(temp_dir=tmp_path):
        import os
        os.chdir(tmp_path)
        res = CliRunner().invoke(main, ["-w", "myws", "converge", "--max-rounds", "1"])
    assert res.exit_code is not None
    hunt_argv = [a for a in argvs if "hunt" in a][0]
    passed = Path(hunt_argv[hunt_argv.index("-w") + 1])
    assert passed.is_absolute(), f"relative -w leaked to hunt: {passed}"
    assert passed.name == "myws"


def test_hunt_subprocess_does_not_override_cwd(monkeypatch, tmp_path):
    """hunt must resolve the operator's relative paths (--surface-scan ./src) against
    the OPERATOR's cwd, exactly as if they'd typed the hunt command themselves."""
    seen = {}

    class _CP:
        returncode = 0

    def fake_run(argv, **kw):
        seen["cwd"] = kw.get("cwd", "UNSET")
        return _CP()
    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    monkeypatch.setattr("audit_pipeline.db.open_findings_db",
                        lambda ws: _FakeDB(cycles=[{"cycle_id": "c", "target_id": 1}],
                                           target={"id": 1}))
    CliRunner().invoke(main, ["-w", _ws(tmp_path), "converge", "--max-rounds", "1"])
    assert seen.get("cwd", "UNSET") == "UNSET"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
