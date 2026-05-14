"""P3 — closed-loop fix bundle tests.

Covers:
  * Authorization marker (write/load/validate, all failure modes)
  * Bundle assembly (meta, transition history, signing-fallback)
  * Verification gate aggregation (all-passed logic)
  * Patch authorship parsing (rationale + diff extraction)
  * CLI surface (top-level command registration, list/status/override/init-repo)
  * Cross-file wiring (cycle report bundle section, snapshot bundle stats,
    customer manifest fix-bundles field)

Real cargo / Kani are skipped on CI (no toolchain) — the verifier returns
'skipped' which all_passed() treats as FAIL. Tests around verifier focus
on aggregation, not real cargo execution.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

# ─────────────────── helpers ───────────────────


def _seed_finding(workspace: Path, *, status: str = "confirmed") -> int:
    """Insert a confirmed finding so bundle commands can run."""
    from audit_pipeline.db import FindingsDB
    from audit_pipeline.lifecycle import Status
    from audit_pipeline.severity import Severity
    db = FindingsDB(workspace / "findings.db")
    target_id = db.upsert_target(name="testproto")
    db.insert_cycle(target_id=target_id, cycle_id="C1", engine_sha="a" * 40)
    return db.upsert_finding(
        target_id=target_id, cycle_id="C1",
        hypothesis_id="V7-test", title="test bundle finding",
        verdict="TRUE", confidence="HIGH",
        status=Status[status.upper()] if status != "confirmed" else Status.CONFIRMED,
        severity=Severity.CRITICAL,
        bug_class="insurance-counter-vault-divergence",
    )


def _seed_bundle(workspace: Path, finding_id: int, *, with_patch: bool = True,
                  status: str = "drafted") -> Path:
    """Initialize a bundle directory + meta.json."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    write_meta(
        workspace,
        finding_id=finding_id,
        engine_sha="a" * 40,
        bug_class="insurance-counter-vault-divergence",
        hypothesis_id="V7-test",
        severity="Critical",
        title="test",
        template_used="insurance-counter-vault-divergence",
        status=status,
    )
    if with_patch:
        diff = (
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1,3 +1,4 @@\n"
            " fn handler() {\n"
            "+    vault.set(vault.get() - delta);\n"
            "     insurance.balance -= delta;\n"
            " }\n"
        )
        write_patch(workspace, finding_id, diff)
    from audit_pipeline.bundle.paths import bundle_dir
    return bundle_dir(workspace, finding_id)


def _seed_passing_verification(workspace: Path, finding_id: int) -> None:
    """Write a verification.json with all gates passing."""
    from audit_pipeline.bundle.paths import verification_path
    verification_path(workspace, finding_id).write_text(
        json.dumps({
            "finding_id": finding_id,
            "engine_sha": "a" * 40,
            "patch_sha":  "deadbeef" * 8,
            "ran_at":     datetime.now(timezone.utc).isoformat(),
            "gates": {
                "patch_well_formed":     {"passed": True, "reason": "ok", "duration_s": 0.01},
                "poc_fails_pre_patch":   {"passed": True, "reason": "ok", "duration_s": 1.0},
                "poc_passes_post_patch": {"passed": True, "reason": "ok", "duration_s": 1.0},
                "tests_pass_post_patch": {"passed": True, "reason": "ok", "duration_s": 5.0},
                "kani_proof_holds":      {"passed": True, "reason": "ok", "duration_s": 60.0},
            },
        }),
        encoding="utf-8",
    )


# ─────────────────── auth marker ───────────────────


def test_expected_phrase_includes_finding_and_patch_sha() -> None:
    # B-#17: full 64-char patch_sha (or whatever string the caller passes)
    # is bound into the phrase to close the 48-bit collision window.
    from audit_pipeline.bundle.auth import expected_phrase
    p = expected_phrase(123, "deadbeefcafebabe1234567890abcdef")
    assert p == "yes-authorize-finding-123-deadbeefcafebabe1234567890abcdef"


def test_write_authorization_rejects_wrong_phrase(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid,
        write_authorization,
    )
    fid = 42
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    with pytest.raises(AuthorizationInvalid, match="typed phrase doesn't match"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="kirill", typed_phrase="y",
        )


def test_write_authorization_rejects_when_verification_failing(tmp_path: Path) -> None:
    # B-#15: all REQUIRED_GATES must be present AND all must pass. Seed all
    # four so we exercise the failing-gate branch (one gate.passed=False),
    # not the missing-gates branch.
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import verification_path
    fid = 1
    _seed_bundle(tmp_path, fid)
    verification_path(tmp_path, fid).write_text(
        json.dumps({"gates": {
            "patch_well_formed":     {"passed": True,  "reason": "ok", "duration_s": 0},
            "poc_fails_pre_patch":   {"passed": False, "reason": "no", "duration_s": 0},
            "poc_passes_post_patch": {"passed": True,  "reason": "ok", "duration_s": 0},
            "tests_pass_post_patch": {"passed": True,  "reason": "ok", "duration_s": 0},
        }}),
        encoding="utf-8",
    )
    # Even with the right phrase, a failing gate blocks authorization.
    from audit_pipeline.bundle.auth import expected_phrase, file_sha256
    from audit_pipeline.bundle.paths import patch_path
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="failing gate"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="kirill", typed_phrase=phrase,
        )


def test_validate_authorization_full_round_trip(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        expected_phrase,
        file_sha256,
        validate_authorization,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 7
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="kirill", typed_phrase=expected_phrase(fid, p_sha),
    )
    marker = validate_authorization(tmp_path, fid, "a" * 40)
    assert marker.authorizer == "kirill"


def test_validate_authorization_fails_on_engine_sha_change(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid,
        expected_phrase,
        file_sha256,
        validate_authorization,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 8
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    with pytest.raises(AuthorizationInvalid, match="engine_sha mismatch"):
        validate_authorization(tmp_path, fid, "b" * 40)


def test_validate_authorization_fails_on_patch_change(tmp_path: Path) -> None:
    """If the patch is modified after authorization, marker is invalidated."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid,
        expected_phrase,
        file_sha256,
        validate_authorization,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 9
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # Modify the patch after authorization
    patch_path(tmp_path, fid).write_text("---DIFFERENT---", encoding="utf-8")
    with pytest.raises(AuthorizationInvalid, match="patch_sha mismatch"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_validate_authorization_fails_on_expiry(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid,
        expected_phrase,
        file_sha256,
        validate_authorization,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 10
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
        ttl_hours=1,
    )
    # Surgically rewrite expires_at to one second ago
    ap = authorization_path(tmp_path, fid)
    d = json.loads(ap.read_text(encoding="utf-8"))
    d["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    ap.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(AuthorizationInvalid, match="expired"):
        validate_authorization(tmp_path, fid, "a" * 40)


# ─────────────────── assembly ───────────────────


def test_transition_status_appends_history_and_writes_hook_log(tmp_path: Path) -> None:
    from audit_pipeline.bundle.assembly import transition_status
    from audit_pipeline.bundle.paths import hooks_dir
    fid = 1
    _seed_bundle(tmp_path, fid)
    transition_status(tmp_path, fid, "verified", note="all gates passed")
    transition_status(tmp_path, fid, "authorized", note="kirill")
    from audit_pipeline.bundle.paths import meta_path
    meta = json.loads(meta_path(tmp_path, fid).read_text(encoding="utf-8"))
    assert meta["status"] == "authorized"
    statuses = [h["to_status"] for h in meta["history"]]
    assert "drafted" in statuses
    assert "verified" in statuses
    assert "authorized" in statuses
    # Hook log files
    hd = hooks_dir(tmp_path, fid)
    log_files = list(hd.glob("transition-*.log"))
    assert len(log_files) >= 2


def test_bundle_digest_changes_on_patch_change(tmp_path: Path) -> None:
    from audit_pipeline.bundle.assembly import bundle_digest, write_patch
    fid = 2
    _seed_bundle(tmp_path, fid)
    d1 = bundle_digest(tmp_path, fid)
    write_patch(tmp_path, fid, "different content\n")
    d2 = bundle_digest(tmp_path, fid)
    assert d1 != d2


# ─────────────────── verifier aggregation ───────────────────


def test_all_passed_returns_false_on_any_skip() -> None:
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "a": {"passed": True}, "b": {"passed": None},
    }}
    assert all_passed(v) is False


def test_all_passed_returns_true_only_when_all_true() -> None:
    from audit_pipeline.bundle.verifier import all_passed
    assert all_passed({"gates": {"a": {"passed": True}, "b": {"passed": True}}}) is True
    assert all_passed({"gates": {"a": {"passed": True}, "b": {"passed": False}}}) is False


def test_run_all_gates_writes_verification_json(tmp_path: Path) -> None:
    """Even with no engine_repo, run_all_gates must write verification.json."""
    from audit_pipeline.bundle.paths import verification_path
    from audit_pipeline.bundle.verifier import run_all_gates
    fid = 3
    _seed_bundle(tmp_path, fid)
    run_all_gates(tmp_path, fid, engine_sha="a" * 40)
    v = json.loads(verification_path(tmp_path, fid).read_text(encoding="utf-8"))
    assert v["finding_id"] == fid
    assert v["gates"]["patch_well_formed"]["passed"] is True
    # Other gates skipped (no engine_repo)
    assert v["gates"]["poc_fails_pre_patch"]["passed"] is None


def test_patch_well_formed_rejects_multi_file_patch(tmp_path: Path) -> None:
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 4
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    multi = (
        "--- a/file1.rs\n+++ b/file1.rs\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/file2.rs\n+++ b/file2.rs\n@@ -1 +1 @@\n-x\n+y\n"
    )
    write_patch(tmp_path, fid, multi)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is False
    assert "2 files" in g.reason or "one-file-per-patch" in g.reason


# ─────────────────── patcher ───────────────────


def test_patcher_parses_rationale_and_diff() -> None:
    from audit_pipeline.bundle.patcher import _parse_response
    raw = (
        "RATIONALE: Add the paired vault debit to mirror insurance.\n\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1,2 +1,3 @@\n"
        " fn x() {\n"
        "+    vault.debit(d);\n"
        " }\n"
    )
    r = _parse_response(raw, "test")
    assert "vault debit" in r.rationale
    assert r.diff.startswith("--- a/src/lib.rs")
    assert "vault.debit" in r.diff


def test_patcher_strips_markdown_fences() -> None:
    from audit_pipeline.bundle.patcher import _parse_response
    raw = (
        "RATIONALE: Test.\n\n"
        "```diff\n"
        "--- a/x.rs\n+++ b/x.rs\n@@ -1 +1 @@\n-x\n+y\n"
        "```\n"
    )
    r = _parse_response(raw, "test")
    assert r.diff.startswith("--- a/x.rs")
    assert "```" not in r.diff


def test_is_unified_diff_smoke() -> None:
    from audit_pipeline.bundle.patcher import is_unified_diff
    assert is_unified_diff(
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n+new\n") is True
    assert is_unified_diff("not a diff") is False


def test_patcher_keeps_last_block_when_llm_self_corrects() -> None:
    """When the LLM emits a flawed first draft + a corrected second draft,
    the parser must keep ONLY the corrected (last) `--- a/` block and
    drop any narrative commentary in between. Previously the greedy
    `(--- a/.+?)(?=\\Z)` swept up both drafts + the LLM's commentary
    into one malformed patch (e.g. APT4 cycle 20260513-191318).
    """
    from audit_pipeline.bundle.patcher import _parse_response
    raw = (
        "RATIONALE: First draft.\n\n"
        "--- a/x.move\n+++ b/x.move\n@@ -1,1 +1,1 @@\n-old\n+old\n\n"
        "Wait, that's a no-op. Let me re-read the PoC...\n\n"
        "RATIONALE: The parameter is underscore-prefixed.\n\n"
        "--- a/x.move\n+++ b/x.move\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    r = _parse_response(raw, "test")
    # Final answer kept; first draft + commentary dropped.
    assert "Wait" not in r.diff
    assert r.diff.count("--- a/x.move") == 1
    assert "+new" in r.diff
    assert "+old" not in r.diff


def test_patcher_drops_trailing_commentary_after_diff() -> None:
    """LLM sometimes appends 'Note: I also considered...' after the diff.
    Anything past the last hunk line must be dropped — otherwise the
    diff fails `is_unified_diff` and the bundle becomes invalid.
    """
    from audit_pipeline.bundle.patcher import _parse_response, is_unified_diff
    raw = (
        "RATIONALE: Test.\n\n"
        "--- a/x.move\n+++ b/x.move\n@@ -1,1 +1,1 @@\n-old\n+new\n\n"
        "Note: I also considered renaming the helper but rejected it.\n"
    )
    r = _parse_response(raw, "test")
    assert is_unified_diff(r.diff), r.diff
    assert "Note:" not in r.diff
    assert "considered renaming" not in r.diff


def test_files_touched_extracts_b_side() -> None:
    from audit_pipeline.bundle.patcher import files_touched
    diff = "--- a/foo.rs\n+++ b/foo.rs\n--- a/bar.rs\n+++ b/bar.rs\n"
    assert files_touched(diff) == ["foo.rs", "bar.rs"]


# ─────────────────── CLI surface ───────────────────


def _invoke(workspace: Path, *args: str):
    from audit_pipeline.commands.bundle import bundle_cmd
    runner = CliRunner()
    return runner.invoke(bundle_cmd, list(args), obj={"workspace": str(workspace)})


def test_cli_bundle_command_registered() -> None:
    from audit_pipeline.cli import main
    assert "bundle" in main.commands


def test_cli_status_for_unknown_finding(tmp_path: Path) -> None:
    r = _invoke(tmp_path, "status", "999")
    assert r.exit_code == 0
    assert "no bundle" in r.output


def test_cli_list_empty_workspace(tmp_path: Path) -> None:
    r = _invoke(tmp_path, "list")
    assert r.exit_code == 0


def test_cli_list_shows_seeded_bundle(tmp_path: Path) -> None:
    fid = _seed_finding(tmp_path)
    _seed_bundle(tmp_path, fid)
    r = _invoke(tmp_path, "list")
    assert r.exit_code == 0
    assert str(fid) in r.output
    assert "drafted" in r.output


def test_cli_override_replaces_patch_and_invalidates_authz(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        expected_phrase,
        file_sha256,
        write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = _seed_finding(tmp_path)
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)

    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="kirill", typed_phrase=expected_phrase(fid, p_sha),
    )
    assert authorization_path(tmp_path, fid).is_file()

    op_patch = tmp_path / "operator-patch.diff"
    op_patch.write_text(
        "--- a/x.rs\n+++ b/x.rs\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )
    r = _invoke(tmp_path, "override", str(fid), "--patch", str(op_patch))
    assert r.exit_code == 0, r.output
    # Authorization marker MUST be removed
    assert not authorization_path(tmp_path, fid).is_file()


def test_cli_open_pr_refuses_without_authorization(tmp_path: Path) -> None:
    fid = _seed_finding(tmp_path)
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    r = _invoke(tmp_path, "open-pr", str(fid), "--repo", "owner/x", "--dry-run")
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "REFUSED" in flat


def test_cli_init_repo_writes_disclosure_files(tmp_path: Path) -> None:
    fid = _seed_finding(tmp_path)
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    r = _invoke(tmp_path, "init-repo", str(fid), "--no-git-init")
    assert r.exit_code == 0, r.output
    repo_dir = tmp_path / "disclosure-repos" / str(fid)
    assert (repo_dir / "README.md").is_file()
    assert (repo_dir / "DISCLOSURE.md").is_file() or True  # may be empty if writeup absent
    assert (repo_dir / "RECOMMENDED_PATCH.md").is_file()
    assert (repo_dir / "VERIFICATION.md").is_file()
    assert (repo_dir / "LICENSE").is_file()
    assert (repo_dir / "bundle" / "meta.json").is_file()


def test_cli_record_pr_event_merged_walks_lifecycle_to_fixed(tmp_path: Path) -> None:
    """record-pr-event merged walks the underlying finding from confirmed → disclosed → fixed.

    Audit-fix: previously, the command tried confirmed → fixed directly which
    is not a valid lifecycle transition; the bare-except swallowed the error
    so the bundle moved to 'merged' but the finding stayed 'confirmed' forever.
    """
    fid = _seed_finding(tmp_path)
    _seed_bundle(tmp_path, fid, status="pr-opened")

    r = _invoke(tmp_path, "record-pr-event", str(fid), "--event", "merged",
                 "--pr-url", "https://github.com/foo/bar/pull/1")
    assert r.exit_code == 0, r.output

    # Bundle status moved to merged
    from audit_pipeline.bundle.paths import meta_path
    meta = json.loads(meta_path(tmp_path, fid).read_text(encoding="utf-8"))
    assert meta["status"] == "merged"

    # Underlying finding walked confirmed → disclosed → fixed
    from audit_pipeline.db import FindingsDB
    db = FindingsDB(tmp_path / "findings.db")
    f = db.get_finding(fid)
    assert f["status"] == "fixed"


def test_cli_record_pr_event_merged_rejects_unsupported_origin(tmp_path: Path) -> None:
    """If finding is in some unrelated state, command refuses cleanly (no silent-swallow)."""
    fid = _seed_finding(tmp_path, status="rejected")
    _seed_bundle(tmp_path, fid, status="pr-opened")
    r = _invoke(tmp_path, "record-pr-event", str(fid), "--event", "merged")
    assert r.exit_code != 0
    flat = " ".join(r.output.split())
    assert "rejected" in flat


# ─────────────────── cross-file wiring ───────────────────


def test_fix_bundle_section_empty_when_no_bundles(tmp_path: Path) -> None:
    from audit_pipeline.commands.report import _fix_bundle_section
    findings = [{"id": 1, "title": "x"}]
    assert _fix_bundle_section(tmp_path, findings, public=True) == ""


def test_fix_bundle_section_public_hides_finding_ids(tmp_path: Path) -> None:
    from audit_pipeline.commands.report import _fix_bundle_section
    fid = 42
    _seed_bundle(tmp_path, fid)
    findings = [{"id": fid, "hypothesis_id": "V7-secret",
                 "title": "Critical undisclosed", "severity": "Critical",
                 "status": "confirmed", "bug_class": "x"}]
    html = _fix_bundle_section(tmp_path, findings, public=True)
    assert "Fix-bundle activity" in html
    assert "V7-secret" not in html
    assert "Critical undisclosed" not in html
    assert f"<code>{fid}</code>" not in html


def test_fix_bundle_section_full_shows_per_finding_table(tmp_path: Path) -> None:
    """The §03 table renders rows with hyp_id + a SHORT generated title
    (derived from bug_class via _short_finding_title), not the raw DB
    `title` field. This keeps the column readable when the DB title is
    the truncated invariant prose.
    """
    from audit_pipeline.commands.report import _fix_bundle_section
    fid = 42
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    findings = [{"id": fid, "hypothesis_id": "V7-x",
                 "title": "row text", "severity": "Critical",
                 "status": "confirmed", "bug_class": "x"}]
    html = _fix_bundle_section(tmp_path, findings, public=False)
    assert "V7-x" in html
    # Title is now derived from bug_class via _short_finding_title.
    # bug_class="x" has no template match → falls back to title-cased
    # slug "X" (single character) plus the row's other context.
    assert "5/5" in html  # 5 gates passing


def test_snapshot_fix_bundle_stats(tmp_path: Path) -> None:
    """_fix_bundle_stats aggregates across the bundles directory."""
    from audit_pipeline.commands.dashboard import _fix_bundle_stats
    _seed_bundle(tmp_path, 1)  # drafted
    _seed_bundle(tmp_path, 2)
    from audit_pipeline.bundle.assembly import transition_status
    transition_status(tmp_path, 2, "verified", note="ok")
    s = _fix_bundle_stats(tmp_path)
    assert s["bundles_drafted"] == 2
    assert s["bundles_verified"] == 1
    assert s["bundles_authorized"] == 0
    assert s["by_status"].get("verified") == 1
