"""A DEAD audit must report failure, never success.

THE BUG THIS PINS: when Layer 1 (recon) died — timeout, crash, missing binary, API
outage — hunt printed a red error, finished the cycle with 0 dispatched, and then did a
bare `return`, which click reports as EXIT CODE 0. Every unattended runner therefore
read a dead audit as a clean one: cron/CI saw "audit passed, nothing found", and `watch`
(which gates on `audit_ok = returncode == 0`) advanced its WS6 diff-scope baseline past
a commit that was NEVER audited — permanently excluding that commit's changes from every
future scan. A silent, permanent coverage hole.

WHY THIS BRANCH IS UNAMBIGUOUSLY A FAILURE (and not a "nothing to do" no-op): `recon`
writes recon_summary.json unconditionally at the end of its work — even when every
hypothesis is filtered out of scope, and even when every verdict is FALSE. The file is
therefore missing ONLY when recon itself died. Legitimate empty-scope runs (e.g. a
docs-only commit under --diff-since-sha) still produce a summary and still exit 0; they
never reach this branch. That separation is what keeps this from crying wolf — a red
nightly run for a README commit is one an operator learns to ignore.

These tests drive the REAL hunt command through the REAL click runner and assert the
process-level exit code, because that integer IS the product behaviour every unattended
caller keys on. Nothing else in the suite covers this line.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import audit_pipeline.commands.hunt as hunt_mod
from audit_pipeline.cli import main


def _workspace(tmp_path: Path) -> Path:
    """Minimal workspace hunt will accept."""
    # `sha` + a `wrapper` block are required — hunt reads both to stamp the cycle.
    (tmp_path / "workspace.json").write_text(json.dumps({
        "target_name": "perc",
        "engine": {"local": "engine", "repo": "acme/engine", "sha": "0" * 40},
        "wrapper": {"local": "engine", "repo": "acme/engine", "sha": "0" * 40},
    }), encoding="utf-8")
    (tmp_path / "engine" / "src").mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "hypotheses.yaml"
    lib.write_text(json.dumps({"hypotheses": [{
        "id": "H1-dead", "class": "arithmetic", "bug_class": "rounding", "severity": "high",
        "claim": "rounding truncation in the settle path loses lamports on every fill",
    }]}), encoding="utf-8")
    return lib


def _run_hunt_with_dead_recon(tmp_path: Path, monkeypatch, *, rc: int):
    """Run hunt with recon stubbed to DIE: it returns `rc` and writes no summary."""
    lib = _workspace(tmp_path)

    def fake_run(argv, **kw):
        # Recon "runs" but never writes recon_summary.json — exactly what a timeout /
        # crash / missing-binary looks like on disk.
        return rc
    monkeypatch.setattr(hunt_mod, "_run", fake_run)
    # hunt refuses to start without an API key (a real guard, firing correctly). Nothing
    # here ever calls the API — every subprocess is stubbed above — so a dummy value is
    # enough to reach the branch under test.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    return CliRunner().invoke(main, [
        "-w", str(tmp_path), "hunt",
        "--hypotheses", str(lib),
        # --ignore-freshness: the scratch workspace pins no upstream repo, so Gate
        # L0.freshness would abort BEFORE Layer 1 and the branch under test would
        # never be reached.
        "--ignore-freshness",
        "--skip-poc", "--skip-kani", "--skip-litesvm",
        "--skip-propagate", "--skip-bundle", "--skip-merkle",
    ])


def test_dead_recon_exits_nonzero(tmp_path, monkeypatch):
    """THE guard. A dead Layer 1 must NOT report success."""
    res = _run_hunt_with_dead_recon(tmp_path, monkeypatch, rc=1)
    assert res.exit_code != 0, (
        f"a dead audit reported SUCCESS (exit {res.exit_code}) — every unattended "
        f"runner would read this as a clean cycle.\n{res.output}"
    )


def test_dead_recon_says_the_audit_did_not_run(tmp_path, monkeypatch):
    # The operator must be able to tell "audit died" from "audit ran, found nothing".
    res = _run_hunt_with_dead_recon(tmp_path, monkeypatch, rc=1)
    out = " ".join(res.output.split())
    assert "did NOT run" in out, out


def test_timeout_is_diagnosed_as_a_timeout(tmp_path, monkeypatch):
    # rc=124 is the POSIX timeout convention — name the cause, don't make the operator guess.
    res = _run_hunt_with_dead_recon(tmp_path, monkeypatch, rc=hunt_mod.RC_TIMEOUT)
    assert res.exit_code != 0
    assert "timed out" in " ".join(res.output.split())


def test_missing_binary_is_diagnosed_as_missing_binary(tmp_path, monkeypatch):
    # rc=127 is the POSIX command-not-found convention.
    res = _run_hunt_with_dead_recon(tmp_path, monkeypatch, rc=hunt_mod.RC_NOT_FOUND)
    assert res.exit_code != 0
    assert "not found" in " ".join(res.output.split())


def test_cycle_is_still_finished_in_the_db(tmp_path, monkeypatch):
    """Failing loudly must not skip the bookkeeping — the cycle row must be CLOSED OUT,
    not left in-progress forever.

    Asserting `n_dispatched == 0` alone was theater: a NULL column satisfies it, so
    deleting `db.finish_cycle(...)` entirely still passed. `finished_at` is the field
    that actually proves the cycle was closed.
    """
    from audit_pipeline.db import open_findings_db

    _run_hunt_with_dead_recon(tmp_path, monkeypatch, rc=1)
    db = open_findings_db(tmp_path)
    cycles = db.list_cycles(limit=10)
    assert cycles, "no cycle row was written at all"
    assert cycles[0].get("finished_at"), (
        "cycle left in-progress — db.finish_cycle() did not run before the exit")
    assert int(cycles[0].get("n_dispatched") or 0) == 0


def test_dead_audit_branch_terminates_with_ctx_exit_not_return():
    """Belt-and-braces against a silent regression, checked STRUCTURALLY.

    A substring search for "ctx.exit(1)" over the source block was theater: that exact
    string also appears in this branch's own explanatory COMMENT, so the test passed
    even with the bug restored (mutation-proven). Parse the AST instead and assert the
    branch's terminal statement is a real `ctx.exit(...)` CALL — comments can't fake it,
    and a bare `return` fails immediately.
    """
    import ast

    tree = ast.parse(Path(hunt_mod.__file__).read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        # the branch under test: `if not summary_path.exists():`
        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) \
                and isinstance(node.test.op, ast.Not):
            call = node.test.operand
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "exists"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "summary_path"):
                target = node
                break
    assert target is not None, "could not locate the `if not summary_path.exists():` branch"

    last = target.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), (
        f"dead-audit branch ends with {type(last).__name__}, not a call — a bare "
        f"`return` here reports a DEAD audit as success")
    func = last.value.func
    assert isinstance(func, ast.Attribute) and func.attr == "exit", (
        "dead-audit branch no longer terminates with ctx.exit(...)")
    assert last.value.args and getattr(last.value.args[0], "value", None) == 1, (
        "dead-audit branch must exit with code 1")
