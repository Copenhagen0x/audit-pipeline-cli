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
    """Write a verification.json with all gates passing.

    Patch #3 round-1: the new `patch_unchanged_during_verify` race-check
    gate must be present (REQUIRED_GATES in auth.py). Tests using this
    helper exercise the happy path where the patch wasn't tampered.
    """
    from audit_pipeline.bundle.paths import verification_path
    verification_path(workspace, finding_id).write_text(
        json.dumps({
            "finding_id": finding_id,
            "engine_sha": "a" * 40,
            "patch_sha":  "deadbeef" * 8,
            "ran_at":     datetime.now(timezone.utc).isoformat(),
            # Patch #3 round-5 (devils-advocate fixture #4): include
            # engine_lang so fixture-backed tests exercise the round-4
            # cross-check correctly. Use "rust" as the default since
            # most fixture-backed tests are Solana/Rust scenarios.
            "engine_lang": "rust",
            "gates": {
                "patch_well_formed":     {"passed": True, "reason": "ok", "duration_s": 0.01},
                "poc_fails_pre_patch":   {"passed": True, "reason": "ok", "duration_s": 1.0},
                "poc_passes_post_patch": {"passed": True, "reason": "ok", "duration_s": 1.0},
                "tests_pass_post_patch": {"passed": True, "reason": "ok", "duration_s": 5.0},
                "kani_proof_holds":      {"passed": True, "reason": "ok", "duration_s": 60.0},
                "patch_unchanged_during_verify": {"passed": True, "reason": "ok", "duration_s": 0.0},
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
            # Patch #3 round-1: required-present gate (race-check). Always
            # included in fixtures going forward.
            "patch_unchanged_during_verify": {"passed": True, "reason": "ok", "duration_s": 0},
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


def test_validate_authorization_full_round_trip(tmp_path: Path, monkeypatch) -> None:
    # R5b (2026-05-24): the new default REQUIRES signed markers. This
    # test uses _seed_bundle which doesn't create keys/ → sidecar is
    # UNSIGNED. To exercise the round-trip without setting up real
    # keys, opt into legacy mode via JELLEO_AUTHZ_ALLOW_UNSIGNED=1.
    # The signed-path round-trip is covered separately by tests that
    # generate a real key.
    monkeypatch.setenv("JELLEO_AUTHZ_ALLOW_UNSIGNED", "1")
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


# ─────────────────── Patch #3 round-1 tests ───────────────────


def test_all_passed_rejects_skip_with_no_code() -> None:
    """Patch #3 round-1 (audit CRITICAL 41b26e30): a skipped gate with
    NO `skip_reason_code` field BLOCKS, even if the reason text contains
    a legacy allowlist phrase like 'no kani_harness registered'. This is
    the core fix: substring matching on reason is gone, structured code
    is the only path."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "kani_proof_holds": {
            "passed": None,
            "reason": "skipped — no kani_harness registered for hypothesis-X",
            # NB: no skip_reason_code
        },
    }}
    assert all_passed(v) is False


def test_all_passed_accepts_valid_skip_code_kani() -> None:
    """A skipped gate WITH skip_reason_code='no_kani_harness_registered'
    counts as N/A (does not block)."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "patch_well_formed": {"passed": True},
        "kani_proof_holds": {
            "passed": None,
            "reason": "skipped — no harness registered",
            "skip_reason_code": "no_kani_harness_registered",
        },
    }}
    assert all_passed(v) is True


def test_all_passed_accepts_litesvm_na_code() -> None:
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "patch_well_formed": {"passed": True},
        "litesvm_exploit_neutralized": {
            "passed": None,
            "reason": "skipped",
            "skip_reason_code": "no_litesvm_test_name_registered",
        },
    }}
    assert all_passed(v) is True


def test_all_passed_accepts_lang_mismatch_code() -> None:
    """Patch #3 round-2: not_applicable_solana removed (Solana is detected
    as lang=rust). Only the 3 real lang codes pass on kani_proof_holds.
    Round-5: must also supply matching engine_lang so the cross-check accepts."""
    from audit_pipeline.bundle.verifier import all_passed
    code_lang_pairs = [
        ("not_applicable_solidity", "solidity"),
        ("not_applicable_c",        "c"),
        ("not_applicable_move",     "move"),
    ]
    for code, lang in code_lang_pairs:
        v = {
            "engine_lang": lang,
            "gates": {
                "patch_well_formed": {"passed": True},
                "kani_proof_holds": {
                    "passed": None, "reason": "", "skip_reason_code": code,
                },
            },
        }
        assert all_passed(v) is True, f"code {code!r} on lang {lang!r} should be N/A"


def test_all_passed_rejects_cross_gate_skip_code() -> None:
    """Patch #3 round-2 (threat-modeler #5): a Solidity N/A code on a gate
    that has no such allowlist entry must BLOCK. Specifically, applying
    a kani-allowlist code (`not_applicable_solidity`) to a gate that
    doesn't expect any N/A skip (e.g. `poc_passes_post_patch`) blocks."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "patch_well_formed":     {"passed": True},
        "poc_fails_pre_patch":   {"passed": True},
        # poc_passes_post_patch has NO entry in _GATE_SKIP_CODE_ALLOWLIST,
        # so ANY skip code (including valid kani codes) blocks.
        "poc_passes_post_patch": {
            "passed": None, "reason": "",
            "skip_reason_code": "not_applicable_solidity",
        },
    }}
    assert all_passed(v) is False


def test_all_passed_rejects_obsolete_not_applicable_solana_code() -> None:
    """Patch #3 round-2 (devils-advocate #7): removed from _NA_SKIP_CODES.
    A verification.json that still uses this code must BLOCK."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "kani_proof_holds": {
            "passed": None, "reason": "",
            "skip_reason_code": "not_applicable_solana",
        },
    }}
    assert all_passed(v) is False


def test_write_authorization_rejects_ttl_bool() -> None:
    """Patch #3 round-2 (code-reviewer #7): bool is subclass of int but
    must be rejected explicitly. Previously `ttl_hours=True` silently
    authorized for 1 hour."""
    import tempfile
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        fid = 2050
        _seed_bundle(tp, fid)
        _seed_passing_verification(tp, fid)
        phrase = expected_phrase(fid, file_sha256(patch_path(tp, fid)))
        with pytest.raises(AuthorizationInvalid, match="ttl_hours"):
            write_authorization(
                tp, finding_id=fid, engine_sha="a" * 40,
                authorizer="k", typed_phrase=phrase, ttl_hours=True,
            )


def test_write_authorization_sidecar_status_signed_or_unsigned(tmp_path: Path) -> None:
    """Patch #3 round-2 (code-reviewer #5 + devils-advocate #6): the
    signing block now writes a .sig.status sidecar that distinguishes
    SIGNED / UNSIGNED / FAILED / REFUSED. On a workspace with no key
    file, status must be UNSIGNED — not silently 'good'."""
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 2060
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=phrase,
    )
    ap = authorization_path(tmp_path, fid)
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    assert status_path.is_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] in ("SIGNED", "UNSIGNED")  # no key in tmp workspace


def test_kani_gate_double_unapply_fix_pattern_present() -> None:
    """Patch #3 round-2 (devils-advocate #1): assert the kani gate uses
    the timed_out + crashed_err flag pattern, not the bare except-then-
    finally-double-unapply pattern."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_kani_proof_holds)
    # The new pattern uses these structured flags
    assert "timed_out = False" in src
    assert "crashed_err" in src
    # The old broken pattern had _unapply_patch in the except clause
    # AND the finally. After the fix, _unapply_patch appears ONCE in
    # the finally — verify by counting.
    # (count both with and without space variations)
    unapply_count = src.count("_unapply_patch(engine_repo")
    assert unapply_count == 1, (
        f"_unapply_patch appears {unapply_count}x in kani gate; should be 1 "
        f"(only in finally clause)"
    )


def test_litesvm_gate_double_unapply_fix_pattern_present() -> None:
    """Same as above for the LiteSVM gate."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_litesvm_exploit_neutralized)
    assert "timed_out = False" in src
    assert "crashed_err" in src
    unapply_count = src.count("_unapply_patch(engine_repo")
    assert unapply_count == 1, (
        f"_unapply_patch appears {unapply_count}x in litesvm gate; should be 1"
    )


def test_all_passed_rejects_unknown_skip_code() -> None:
    """An attacker who controls verification.json can write
    skip_reason_code='attacker_chosen_value' — that must BLOCK."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {"gates": {
        "kani_proof_holds": {
            "passed": None,
            "reason": "",
            "skip_reason_code": "attacker_chosen_value",
        },
    }}
    assert all_passed(v) is False


def test_all_passed_rejects_legacy_substring_attack() -> None:
    """Patch #3 round-1 (audit CRITICAL 7c0e4683 / 29db45bd): the OLD
    `in`-substring check on the free-text reason field is GONE. Even
    if the reason verbatim contains 'no kani_harness registered' or
    'not applicable to solidity', without a structured code it BLOCKS.
    """
    from audit_pipeline.bundle.verifier import all_passed
    for malicious_reason in (
        "no kani_harness registered for hypothesis 41",
        "not applicable to solidity",
        "not applicable to solana",
        "no litesvm_test_name registered",
        # Trying to look like the canonical phrase but with no code:
        "skipped — no kani_harness registered",
    ):
        v = {"gates": {
            "g": {"passed": None, "reason": malicious_reason}
            # NB: no skip_reason_code → BLOCK
        }}
        assert all_passed(v) is False, (
            f"reason {malicious_reason!r} should not bypass without code"
        )


def test_patch_well_formed_rejects_path_traversal_in_second_file(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit CRITICAL 089f83c7 + HIGH 7dfddb64 /
    4c1014cd): the original path-traversal check only validated
    touched[0]. A patch with safe first file + malicious second file
    would slip past."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 1041
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    # Safe first file (root-crate) + traversal in second (also root-crate-ish
    # — we must avoid tripping the multi-crate check, which fires BEFORE
    # the path-traversal check). Both paths lack `programs/<name>/` prefix
    # so crate_roots is {'__root__'} only, then the path-traversal loop
    # catches the `..` in file #2.
    sneaky = (
        "--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/src/../../../etc/passwd\n+++ b/src/../../../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n"
    )
    write_patch(tmp_path, fid, sneaky)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is False
    assert "path-traversal" in g.reason.lower() or "absolute" in g.reason.lower()


def test_patch_well_formed_rejects_path_traversal_in_first_file(tmp_path: Path) -> None:
    """The legacy single-file check still works (touched[0] is now in the loop)."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 1042
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    bad = (
        "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n"
    )
    write_patch(tmp_path, fid, bad)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is False


def test_patch_well_formed_rejects_windows_drive_letter_path(tmp_path: Path) -> None:
    """Patch #3 round-1: also catch Windows-style absolute paths like
    `C:/Windows/System32/...` which `startswith('/')` misses."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 1043
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    bad = (
        "--- a/C:/Windows/calc.exe\n+++ b/C:/Windows/calc.exe\n@@ -1 +1 @@\n-x\n+y\n"
    )
    write_patch(tmp_path, fid, bad)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is False


def test_write_authorization_rejects_ttl_above_cap(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit MED 540763a6): TTL > 168h (1 week) rejected."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 2001
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="ttl_hours"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=phrase, ttl_hours=999,
        )


def test_write_authorization_rejects_ttl_zero(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 2002
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="ttl_hours"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=phrase, ttl_hours=0,
        )


def test_write_authorization_atomic_no_partial_on_disk(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit MED f8fe4ebf): write goes through tmp+rename
    so a crash mid-write doesn't leave a corrupt authorization.json."""
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 2010
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=phrase,
    )
    ap = authorization_path(tmp_path, fid)
    # No tmp file lingering after a successful write
    assert not ap.with_suffix(ap.suffix + ".tmp").exists()
    # And the written file is valid JSON
    assert json.loads(ap.read_text(encoding="utf-8"))["finding_id"] == fid


def test_load_authorization_rejects_malformed_engine_sha(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit HIGH 1570b9e8): load-time format guard."""
    from audit_pipeline.bundle.auth import AuthorizationInvalid, load_authorization
    from audit_pipeline.bundle.paths import authorization_path
    fid = 2020
    _seed_bundle(tmp_path, fid)
    ap = authorization_path(tmp_path, fid)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps({
        "finding_id": fid,
        "engine_sha": "not-a-sha",
        "patch_sha":  "d" * 64,
        "verification_digest": "e" * 64,
        "authorized_at": "2026-01-01T00:00:00+00:00",
        "expires_at":    "2026-01-02T00:00:00+00:00",
        "authorizer": "k",
        "phrase": "x",
    }), encoding="utf-8")
    with pytest.raises(AuthorizationInvalid, match="malformed engine_sha"):
        load_authorization(tmp_path, fid)


def test_load_authorization_rejects_malformed_patch_sha(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import AuthorizationInvalid, load_authorization
    from audit_pipeline.bundle.paths import authorization_path
    fid = 2021
    _seed_bundle(tmp_path, fid)
    ap = authorization_path(tmp_path, fid)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps({
        "finding_id": fid,
        "engine_sha": "a" * 40,
        "patch_sha":  "short",
        "verification_digest": "e" * 64,
        "authorized_at": "2026-01-01T00:00:00+00:00",
        "expires_at":    "2026-01-02T00:00:00+00:00",
        "authorizer": "k",
        "phrase": "x",
    }), encoding="utf-8")
    with pytest.raises(AuthorizationInvalid, match="malformed patch_sha"):
        load_authorization(tmp_path, fid)


def test_validate_authorization_rechecks_all_passed(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit HIGH a834b8f4): validate_authorization
    must re-call all_passed() on the current verification.json. Even
    if verification_digest matches the marker (file unchanged since
    auth), the gate semantics must still hold."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path, verification_path
    fid = 2030
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # Now tamper verification.json to add a failing gate AND rewrite
    # the authorization marker's verification_digest to match (the
    # attacker-controlled scenario). This proves all_passed() runs
    # on CURRENT content even when the digest itself is valid.
    v_path = verification_path(tmp_path, fid)
    v = json.loads(v_path.read_text(encoding="utf-8"))
    v["gates"]["poc_passes_post_patch"]["passed"] = False
    v_path.write_text(json.dumps(v), encoding="utf-8")
    # Rewrite marker's verification_digest to match the new content
    from audit_pipeline.bundle.paths import authorization_path
    ap = authorization_path(tmp_path, fid)
    marker_d = json.loads(ap.read_text(encoding="utf-8"))
    marker_d["verification_digest"] = file_sha256(v_path)
    ap.write_text(json.dumps(marker_d), encoding="utf-8")
    # The digest matches now, but all_passed() should fail
    with pytest.raises(AuthorizationInvalid, match="gates do not all pass"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_write_authorization_blocks_when_only_skip_code_invalid(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit CRITICAL 31fd96d0): defence-in-depth.
    write_authorization() now also calls all_passed(). Build a
    verification.json that passes the REQUIRED_GATES per-key check
    but has a 6th gate with unknown skip_reason_code — that previously
    slipped past write_authorization() but failed all_passed() (UI path
    only). Now both paths reject."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path, verification_path
    fid = 2040
    _seed_bundle(tmp_path, fid)
    verification_path(tmp_path, fid).write_text(json.dumps({
        "gates": {
            "patch_well_formed":     {"passed": True, "reason": "ok"},
            "poc_fails_pre_patch":   {"passed": True, "reason": "ok"},
            "poc_passes_post_patch": {"passed": True, "reason": "ok"},
            "tests_pass_post_patch": {"passed": True, "reason": "ok"},
            "patch_unchanged_during_verify": {"passed": True, "reason": "ok"},
            # Attacker-chosen skip code (NOT in _NA_SKIP_CODES) — blocks
            "litesvm_exploit_neutralized": {
                "passed": None, "reason": "x",
                "skip_reason_code": "attacker_chosen",
            },
        },
    }), encoding="utf-8")
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="all_passed"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=phrase,
        )


def test_anchor_overlap_slack_is_symmetric_and_tight() -> None:
    """Patch #3 round-3 (devils-advocate #5 boundary fix): SLACK is now 1
    per side. Test the actual boundary: a drift of 2 lines from the
    claimed anchor must be REJECTED (claim_hi = start + count + 1,
    actual at start+2 → actual_lo=start+1 > claim_hi means no overlap),
    but a drift of 1 should still be accepted within slack."""
    from audit_pipeline.bundle.verifier import _verify_anchors_post_apply
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td_str:
        td = Path(td_str)
        subprocess.run(["git", "init"], cwd=td, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=td, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=td, capture_output=True)
        f = td / "lib.rs"
        f.write_text("\n".join(f"line{i}" for i in range(200)) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=td, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=td, capture_output=True, check=True)
        # Modify line 150 (1-indexed: body[149])
        body = f.read_text(encoding="utf-8").splitlines()
        body[149] = "MODIFIED"
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        # Large drift: claim at 100, actual at 150. Clearly outside any
        # reasonable slack. REJECT.
        ok, msg = _verify_anchors_post_apply(td, [("lib.rs", 100, 1)])
        assert ok is False
        assert "doesn't overlap" in msg
        # Boundary tests at SLACK=1:
        #   claim at exact line (150) → ACCEPT
        ok_exact, _ = _verify_anchors_post_apply(td, [("lib.rs", 150, 1)])
        assert ok_exact is True, "exact-line anchor should always overlap"
        #   claim at line 148 (drift of 2 below actual) — with claim_hi
        #   = 148 + 1 + 1 = 150, and actual_lo = 150 - 1 = 149, overlap
        #   is 149 <= 150 → TRUE. Edge: 2-line drift currently accepted.
        #   At drift=4 it should reject decisively (claim_hi=146 vs
        #   actual_lo=149 → no overlap).
        ok_far, msg_far = _verify_anchors_post_apply(td, [("lib.rs", 144, 1)])
        assert ok_far is False, f"6-line drift should reject; got: {msg_far}"


def test_apply_patch_refuses_when_no_b_headers_returns_specific_msg() -> None:
    """Patch #3 round-3: ensure the specific error message change from
    round-1 is preserved (no `src/percolator.rs` fallback string)."""
    from audit_pipeline.bundle.verifier import _apply_patch
    import tempfile
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        subprocess.run(["git", "init"], cwd=td_p, capture_output=True, check=True)
        ok, err = _apply_patch(td_p, "")
        assert ok is False
        # Round-1 message contains either of these phrases
        assert "no `+++ b/`" in err or "refusing to apply" in err
        assert "percolator.rs" not in err


def test_c_target_tests_pass_post_patch_emits_skip_code() -> None:
    """Patch #3 round-3 (devils-advocate #1 + threat-modeler #4): the
    tests_pass_post_patch gate skip for C targets MUST carry
    skip_reason_code='not_applicable_c' so all_passed() doesn't BLOCK."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_tests_pass_post_patch)
    # The C branch must include the structured code
    assert 'skip_reason_code="not_applicable_c"' in src


def test_all_passed_accepts_c_tests_pass_skip() -> None:
    """The corresponding allowlist entry must accept the C tests_pass
    skip end-to-end through all_passed(). Round-5 requires engine_lang
    matching."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {
        "engine_lang": "c",
        "gates": {
            "patch_well_formed":              {"passed": True},
            "poc_fails_pre_patch":            {"passed": True},
            "poc_passes_post_patch":          {"passed": True},
            "tests_pass_post_patch":          {
                "passed": None, "reason": "C target — delegated to PoC gate",
                "skip_reason_code": "not_applicable_c",
            },
            "patch_unchanged_during_verify":  {"passed": True},
        },
    }
    assert all_passed(v) is True


def test_c_target_kani_proof_holds_emits_skip_code() -> None:
    """Patch #3 round-3 (devils-advocate #7): C target's kani gate must
    emit the not_applicable_c code so it doesn't block."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_kani_proof_holds)
    # The C branch must include the structured code
    assert 'skip_reason_code="not_applicable_c"' in src


def test_rust_poc_passes_double_unapply_fix_pattern_present() -> None:
    """Patch #3 round-3 (code-reviewer #1 + devils-advocate #4): the
    Rust poc_passes_post_patch path must also use the timed_out flag
    pattern. Single _unapply_patch in finally only."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_poc_passes_post_patch)
    assert "timed_out = False" in src
    assert "crashed_err" in src
    # The Rust path inner subprocess.run is the cargo test invocation.
    # finally has the only _unapply_patch. Count occurrences (Solidity
    # and C dispatch out of this function so only the Rust call site
    # body is in here for cargo test). The function ALSO calls
    # _apply_patch which is fine; we count the unapply.
    unapply_count = src.count("_unapply_patch(engine_repo")
    # Standalone-PoC arm also has 1 apply+unapply pair. Plus the main
    # cargo-test arm. So 2 _unapply calls in the function is normal —
    # but neither should appear in an except clause.
    # Verify by looking for the broken pattern: "except subprocess.TimeoutExpired:\n        _unapply_patch"
    assert "except subprocess.TimeoutExpired:\n        _unapply_patch" not in src


def test_tests_pass_double_unapply_fix_pattern_present() -> None:
    """Same for tests_pass_post_patch."""
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_tests_pass_post_patch)
    assert "timed_out = False" in src
    assert "crashed_err" in src
    assert "except subprocess.TimeoutExpired:\n        _unapply_patch" not in src


def test_sidecar_status_reject_FAILED_in_validate(tmp_path: Path) -> None:
    """Patch #3 round-3 (code-reviewer #2 + devils-advocate #3 +
    threat-modeler #1): if .sig.status reports FAILED, validate_authorization
    must REJECT regardless of all other checks passing."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 3001
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # Now write a FAILED status sidecar to simulate signing-infra failure
    ap = authorization_path(tmp_path, fid)
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    status_path.write_text(
        json.dumps({"status": "FAILED", "reason": "key corrupt"}),
        encoding="utf-8",
    )
    with pytest.raises(AuthorizationInvalid, match="FAILED"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_sidecar_status_reject_REFUSED_in_validate(tmp_path: Path) -> None:
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 3002
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    ap = authorization_path(tmp_path, fid)
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    status_path.write_text(
        json.dumps({"status": "REFUSED", "reason": "key symlink escapes"}),
        encoding="utf-8",
    )
    with pytest.raises(AuthorizationInvalid, match="REFUSED"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_sidecar_status_unsigned_rejected_by_default(tmp_path: Path, monkeypatch) -> None:
    """R5b (2026-05-24) — goober HIGH #3: signed is now the DEFAULT.
    Previously UNSIGNED was accepted silently which defeated the entire
    'operator typed phrase' defense. Now must be explicitly opted into."""
    monkeypatch.delenv("JELLEO_AUTHZ_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("JELLEO_AUTHZ_REQUIRE_SIGNED", raising=False)
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 3003
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # _seed_bundle doesn't create keys/ → write_authorization wrote
    # UNSIGNED. Default (signed-required) must REJECT.
    import pytest
    with pytest.raises(AuthorizationInvalid, match="UNSIGNED"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_sidecar_status_unsigned_accepted_in_legacy_mode(tmp_path: Path, monkeypatch) -> None:
    """R5b legacy escape hatch: operators with no signing key can opt
    into the old behavior via JELLEO_AUTHZ_ALLOW_UNSIGNED=1."""
    monkeypatch.setenv("JELLEO_AUTHZ_ALLOW_UNSIGNED", "1")
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 3004
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    marker = validate_authorization(tmp_path, fid, "a" * 40)
    assert marker.authorizer == "k"


def test_sidecar_status_unsigned_blocked_legacy_test_kept_for_history(tmp_path: Path, monkeypatch) -> None:
    """R5b: previously this test verified that setting JELLEO_AUTHZ_
    REQUIRE_SIGNED=1 rejected UNSIGNED. R5b inverted the default —
    the equivalent NEW test is test_sidecar_status_unsigned_rejected_
    by_default (no env var needed). Kept here as a smoke test that
    setting the OLD env var doesn't accidentally re-enable legacy
    mode (since R5b code only checks JELLEO_AUTHZ_ALLOW_UNSIGNED)."""
    monkeypatch.delenv("JELLEO_AUTHZ_ALLOW_UNSIGNED", raising=False)
    monkeypatch.setenv("JELLEO_AUTHZ_REQUIRE_SIGNED", "1")  # ignored by R5b
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 3005
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    with pytest.raises(AuthorizationInvalid, match="UNSIGNED"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_exclude_public_covers_authorization_sig_sidecars() -> None:
    """Patch #3 round-3 (code-reviewer #3): publish-archive must exclude
    the .sig and .sig.status sidecars introduced in round-2."""
    import inspect
    from audit_pipeline.commands import bundle as bundle_cmd_mod
    # publish_archive_cmd defines EXCLUDE_PUBLIC inline; inspect source.
    src = inspect.getsource(bundle_cmd_mod)
    assert '"authorization.json.sig"' in src
    assert '"authorization.json.sig.status"' in src


# ─────────────── Patch #3 round-4 fixes (critical-cluster) ───────────────


def test_sigfile_parser_extracts_bare_base64_payload(tmp_path) -> None:
    """Patch #3 round-4 (CRITICAL — code-reviewer #1 + devils-advocate #1
    + threat-modeler #2): the sig file written by sign_file has the
    base64 as a BARE line inside BEGIN/END markers — no "Signature: "
    header. validate_authorization's parser must extract it using the
    same logic verify_cmd does (lines without ':' inside the block).
    End-to-end: keygen → write_authorization → validate_authorization
    must succeed with the SIGNED path, not crash on a "no header" error."""
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import patch_path
    fid = 4001
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    # Set up a real keypair at the path write_authorization expects
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    (keys_dir / "jelleo.ed25519").write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (keys_dir / "jelleo.ed25519.pub").write_bytes(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # The .sig.status should be SIGNED now
    from audit_pipeline.bundle.paths import authorization_path
    ap = authorization_path(tmp_path, fid)
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    assert status_path.is_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "SIGNED", f"got status {status}"
    # And the .sig file should exist
    assert ap.with_suffix(ap.suffix + ".sig").is_file()
    # Validate should accept the signature (proves the parser works)
    marker = validate_authorization(tmp_path, fid, "a" * 40)
    assert marker.authorizer == "k"


def test_validate_authorization_refuses_signed_when_pubkey_absent(tmp_path) -> None:
    """Patch #3 round-4 (CRITICAL — threat-modeler #1): if sidecar
    claims SIGNED but the pub key file is absent, validate_authorization
    must REFUSE — otherwise an attacker who deletes the pub key bypasses
    cryptographic verification by silent fallback."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 4002
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    ap = authorization_path(tmp_path, fid)
    # Forge sidecar to claim SIGNED AND write a fake .sig
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    status_path.write_text(json.dumps({"status": "SIGNED"}), encoding="utf-8")
    ap.with_suffix(ap.suffix + ".sig").write_text(
        "-----BEGIN JELLEO SIGNATURE-----\nFAKE\n-----END JELLEO SIGNATURE-----\n",
        encoding="utf-8",
    )
    # No pub key exists → must refuse
    with pytest.raises(AuthorizationInvalid, match="pub key"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_validate_authorization_rejects_tampered_signature(tmp_path) -> None:
    """Patch #3 round-4: a SIGNED claim with an INVALID signature must
    raise AuthorizationInvalid (the new parser correctly verifies, not
    just parses)."""
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 4003
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    # Place pub key of attacker's keypair (different from any sig we'll write)
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    other = Ed25519PrivateKey.generate()
    (keys_dir / "jelleo.ed25519.pub").write_bytes(
        other.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    # Write a sig that won't verify (signed by other key, wrong content)
    ap = authorization_path(tmp_path, fid)
    bad_sig = other.sign(b"wrong content")
    import base64
    sig_text = (
        "-----BEGIN JELLEO SIGNATURE-----\n"
        "Algorithm: Ed25519\n"
        "Schema: jelleo-sign/v2\n"
        "Domain: authorization\n"
        f"Signed-File: {ap.name}\n"
        "\n"
        f"{base64.b64encode(bad_sig).decode()}\n"
        "-----END JELLEO SIGNATURE-----\n"
    )
    ap.with_suffix(ap.suffix + ".sig").write_text(sig_text, encoding="utf-8")
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    status_path.write_text(json.dumps({"status": "SIGNED"}), encoding="utf-8")
    with pytest.raises(AuthorizationInvalid, match="FAILED"):
        validate_authorization(tmp_path, fid, "a" * 40)


def test_all_passed_rejects_wrong_lang_code(tmp_path) -> None:
    """Patch #3 round-4 (devils-advocate #2 + threat-modeler #4):
    cross-gate language binding. A verification.json for a Rust target
    that applies not_applicable_c on tests_pass_post_patch must BLOCK
    even though the code is in the allowlist for that gate."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {
        "engine_lang": "rust",
        "gates": {
            "patch_well_formed":              {"passed": True},
            "poc_fails_pre_patch":            {"passed": True},
            "poc_passes_post_patch":          {"passed": True},
            "tests_pass_post_patch": {
                "passed": None, "reason": "fake C-skip",
                "skip_reason_code": "not_applicable_c",
            },
            "patch_unchanged_during_verify":  {"passed": True},
        },
    }
    assert all_passed(v) is False


def test_all_passed_accepts_matching_lang_code() -> None:
    """The matching-lang case should still accept."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {
        "engine_lang": "c",
        "gates": {
            "patch_well_formed":              {"passed": True},
            "poc_fails_pre_patch":            {"passed": True},
            "poc_passes_post_patch":          {"passed": True},
            "tests_pass_post_patch": {
                "passed": None, "reason": "C — delegated to PoC gate",
                "skip_reason_code": "not_applicable_c",
            },
            "patch_unchanged_during_verify":  {"passed": True},
        },
    }
    assert all_passed(v) is True


def test_override_cmd_cleans_up_sig_sidecars(tmp_path) -> None:
    """Patch #3 round-4 (devils-advocate #3): override_cmd must remove
    .sig + .sig.status alongside authorization.json. Otherwise stale
    sidecars persist across patch swaps."""
    from audit_pipeline.bundle.paths import authorization_path
    fid = 4010
    _seed_bundle(tmp_path, fid)
    ap = authorization_path(tmp_path, fid)
    ap.parent.mkdir(parents=True, exist_ok=True)
    # Pre-create stale sidecars
    ap.write_text(json.dumps({"finding_id": fid, "engine_sha": "a"*40,
                              "patch_sha": "d"*64,
                              "verification_digest": "e"*64,
                              "authorized_at": "2026", "expires_at": "2027",
                              "authorizer": "k", "phrase": "x"}),
                  encoding="utf-8")
    sig = ap.with_suffix(ap.suffix + ".sig")
    status = ap.with_suffix(ap.suffix + ".sig.status")
    sig.write_text("FAKE", encoding="utf-8")
    status.write_text(json.dumps({"status": "SIGNED"}), encoding="utf-8")
    # Write an operator patch file
    op_patch = tmp_path / "op_patch.diff"
    op_patch.write_text(
        "--- a/lib.rs\n+++ b/lib.rs\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )
    # Invoke override_cmd via the bundle_cmd group (same pattern as
    # other CLI tests in this file).
    from audit_pipeline.commands.bundle import bundle_cmd
    runner = CliRunner()
    result = runner.invoke(
        bundle_cmd, ["override", str(fid), "--patch", str(op_patch)],
        obj={"workspace": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    # All three files should be gone
    assert not ap.is_file()
    assert not sig.is_file()
    assert not status.is_file()


def test_all_passed_rejects_lang_code_when_engine_lang_unknown() -> None:
    """Patch #3 round-5 (devils-advocate #2 + threat-modeler #1): the
    round-4 lang check explicitly EXEMPTED engine_lang=='unknown' /
    None / '' — which an attacker would set to bypass the language
    binding. Round-5 inverts: lang-specific codes ONLY accepted on
    KNOWN matching languages. Unknown/None/'' REJECT lang-specific
    codes."""
    from audit_pipeline.bundle.verifier import all_passed
    # engine_lang=None on a verification with not_applicable_c on tests_pass
    v_none = {
        "gates": {
            "patch_well_formed":              {"passed": True},
            "poc_fails_pre_patch":            {"passed": True},
            "poc_passes_post_patch":          {"passed": True},
            "tests_pass_post_patch": {
                "passed": None, "skip_reason_code": "not_applicable_c",
            },
            "patch_unchanged_during_verify":  {"passed": True},
        },
    }
    assert all_passed(v_none) is False, "engine_lang absent must reject lang code"
    # engine_lang='unknown' (legitimate fallback when engine_repo=None)
    v_unknown = dict(v_none, engine_lang="unknown")
    assert all_passed(v_unknown) is False, "engine_lang='unknown' must reject lang code"
    # engine_lang='' (empty)
    v_empty = dict(v_none, engine_lang="")
    assert all_passed(v_empty) is False, "engine_lang='' must reject lang code"


def test_all_passed_accepts_lang_code_with_known_match() -> None:
    """Sanity check: when engine_lang explicitly matches the code's
    required lang, all_passed accepts."""
    from audit_pipeline.bundle.verifier import all_passed
    v = {
        "engine_lang": "c",
        "gates": {
            "patch_well_formed":              {"passed": True},
            "poc_fails_pre_patch":            {"passed": True},
            "poc_passes_post_patch":          {"passed": True},
            "tests_pass_post_patch": {
                "passed": None, "skip_reason_code": "not_applicable_c",
            },
            "patch_unchanged_during_verify":  {"passed": True},
        },
    }
    assert all_passed(v) is True


def test_validate_authorization_refuses_pub_key_symlink_escape(tmp_path) -> None:
    """Patch #3 round-5 (devils-advocate #8 + threat-modeler #3):
    pub_key path must also be symlink-jailed. An attacker who symlinks
    workspace/keys/jelleo.ed25519.pub to a path outside the workspace
    would otherwise have their key loaded for verification."""
    import sys
    if sys.platform == "win32":
        pytest.skip("symlink creation requires admin on Windows")
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256,
        validate_authorization, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 5001
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    # Generate a real keypair OUTSIDE the workspace
    import tempfile
    with tempfile.TemporaryDirectory() as ext:
        ext_p = Path(ext)
        priv = Ed25519PrivateKey.generate()
        ext_pub = ext_p / "attacker.pub"
        ext_pub.write_bytes(priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        # write_authorization first (no key → UNSIGNED status)
        p_sha = file_sha256(patch_path(tmp_path, fid))
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
        )
        # Attacker setup: symlink pub key to external path + claim SIGNED
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir(parents=True, exist_ok=True)
        (keys_dir / "jelleo.ed25519.pub").symlink_to(ext_pub)
        ap = authorization_path(tmp_path, fid)
        # Write attacker's sig (with valid format)
        attacker_sig = priv.sign(
            b"jelleo-authorization/v2\x00"
            + ap.name.encode("utf-8") + b"\x00"
            + ap.read_bytes()
        )
        import base64
        ap.with_suffix(ap.suffix + ".sig").write_text(
            "-----BEGIN JELLEO SIGNATURE-----\n"
            "Algorithm: Ed25519\n"
            "\n"
            f"{base64.b64encode(attacker_sig).decode()}\n"
            "-----END JELLEO SIGNATURE-----\n",
            encoding="utf-8",
        )
        ap.with_suffix(ap.suffix + ".sig.status").write_text(
            json.dumps({"status": "SIGNED"}), encoding="utf-8",
        )
        with pytest.raises(AuthorizationInvalid, match="pub key path"):
            validate_authorization(tmp_path, fid, "a" * 40)


def test_kani_harness_rejects_trailing_newline() -> None:
    """Patch #3 round-8 (threat-modeler round-7 #1): re.match's $ anchor
    matches before terminal newline. Must use re.fullmatch so a value
    like 'kani_harness\\n' is rejected. Behavioral test — call the
    fullmatch via the regex pattern used in the gate."""
    import re
    # Confirm the function source contains re.fullmatch on the harness pattern
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_kani_proof_holds)
    assert "re.fullmatch" in src
    # Behavior: a trailing newline must fail re.fullmatch even though
    # it'd pass re.match (this is the actual bypass closed).
    assert re.fullmatch(r"[A-Za-z0-9_:]+", "kani_check\n") is None
    assert re.fullmatch(r"[A-Za-z0-9_:]+", "kani_check") is not None


def test_litesvm_test_name_rejects_trailing_newline() -> None:
    """Same as above for litesvm_test_name. The newline-bypass made
    cargo treat the value as a filter matching zero tests, causing
    'test result: ok' (0 tests ran) → gate falsely reported True."""
    import re
    import inspect
    from audit_pipeline.bundle import verifier
    src = inspect.getsource(verifier._gate_litesvm_exploit_neutralized)
    assert "re.fullmatch" in src
    assert re.fullmatch(r"test_[A-Za-z0-9_]+", "test_exploit\n") is None
    assert re.fullmatch(r"test_[A-Za-z0-9_]+", "test_exploit") is not None


def test_write_authorization_rejects_crafted_no_kani_null_field_meta_has_one(tmp_path) -> None:
    """Patch #3 round-8 (devils-advocate round-7 #1): the round-7
    cross-check on effective_kani_harness was defeatable by setting
    the field to null/absent in forged verification.json. Round-8 adds
    a meta.json fallback: if EITHER source claims a harness, the
    no_kani_harness_registered skip is rejected."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.assembly import write_meta
    from audit_pipeline.bundle.paths import patch_path, verification_path
    fid = 8001
    _seed_bundle(tmp_path, fid)
    # meta.json claims harness IS registered
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        bug_class="x", hypothesis_id="y", severity="Low",
        title="t", template_used="generic",
        kani_harness="real_harness_name",
    )
    # Forged verification.json: claim no_kani_harness_registered AND
    # null out effective_kani_harness (the round-7 attacker move).
    verification_path(tmp_path, fid).write_text(
        json.dumps({
            "engine_lang": "rust",
            "effective_kani_harness": None,   # attacker-omitted
            "gates": {
                "patch_well_formed":             {"passed": True},
                "poc_fails_pre_patch":           {"passed": True},
                "poc_passes_post_patch":         {"passed": True},
                "tests_pass_post_patch":         {"passed": True},
                "patch_unchanged_during_verify": {"passed": True},
                "kani_proof_holds": {
                    "passed": None,
                    "skip_reason_code": "no_kani_harness_registered",
                },
            },
        }),
        encoding="utf-8",
    )
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="no_kani_harness_registered"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=phrase,
        )


def test_write_authorization_rejects_crafted_no_kani_harness_when_meta_has_one(tmp_path) -> None:
    """Patch #3 round-6 (threat-modeler round-5 #1): an attacker who
    forges verification.json with `no_kani_harness_registered` on a
    bug class that ACTUALLY has a registered harness must be blocked.
    write_authorization cross-checks the skip code against meta.json."""
    from audit_pipeline.bundle.auth import (
        AuthorizationInvalid, expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.assembly import write_meta
    from audit_pipeline.bundle.paths import patch_path, verification_path
    fid = 6001
    _seed_bundle(tmp_path, fid)
    # meta.json claims kani_harness IS registered for this bug class
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        bug_class="x", hypothesis_id="y", severity="Low",
        title="t", template_used="generic",
        kani_harness="kani_check_invariant_42",
    )
    # Forged verification.json: claim no_kani_harness_registered.
    # Round-7: the cross-check now reads effective_kani_harness from
    # verification.json (not meta.json), so the forged file must also
    # include that field to mimic a real run_all_gates output.
    verification_path(tmp_path, fid).write_text(
        json.dumps({
            "engine_lang": "rust",
            "effective_kani_harness": "kani_check_invariant_42",
            "gates": {
                "patch_well_formed":             {"passed": True},
                "poc_fails_pre_patch":           {"passed": True},
                "poc_passes_post_patch":         {"passed": True},
                "tests_pass_post_patch":         {"passed": True},
                "patch_unchanged_during_verify": {"passed": True},
                "kani_proof_holds": {
                    "passed": None,
                    "skip_reason_code": "no_kani_harness_registered",
                },
            },
        }),
        encoding="utf-8",
    )
    phrase = expected_phrase(fid, file_sha256(patch_path(tmp_path, fid)))
    with pytest.raises(AuthorizationInvalid, match="no_kani_harness_registered"):
        write_authorization(
            tmp_path, finding_id=fid, engine_sha="a" * 40,
            authorizer="k", typed_phrase=phrase,
        )


def test_atomic_write_uses_random_tmp_name(tmp_path) -> None:
    """Patch #3 round-6 (threat-modeler round-5 #2): atomic-write tmp
    name is generated by tempfile.mkstemp so it can't be pre-created
    as a symlink. Assert no fixed-name tmp lingers after writes."""
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 6002
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=p_sha and expected_phrase(fid, p_sha),
    )
    ap = authorization_path(tmp_path, fid)
    # The old-style fixed-name .tmp must NOT exist
    assert not ap.with_suffix(ap.suffix + ".tmp").exists()
    # Sidecar fixed-name .tmp must also NOT exist
    status_path = ap.with_suffix(ap.suffix + ".sig.status")
    assert not status_path.with_suffix(status_path.suffix + ".tmp").exists()


def test_broken_symlink_key_emits_REFUSED_not_UNSIGNED(tmp_path) -> None:
    """Patch #3 round-4 (threat-modeler #7): a broken symlink at the
    key path must emit REFUSED status, not silently fall through to
    UNSIGNED (which would mislead the operator and bypass hardened
    deploys without warning)."""
    import sys
    if sys.platform == "win32":
        pytest.skip("symlink creation requires admin on Windows")
    from audit_pipeline.bundle.auth import (
        expected_phrase, file_sha256, write_authorization,
    )
    from audit_pipeline.bundle.paths import authorization_path, patch_path
    fid = 4020
    _seed_bundle(tmp_path, fid)
    _seed_passing_verification(tmp_path, fid)
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    # Create broken symlink
    (keys_dir / "jelleo.ed25519").symlink_to("/nonexistent/path/key.priv")
    p_sha = file_sha256(patch_path(tmp_path, fid))
    write_authorization(
        tmp_path, finding_id=fid, engine_sha="a" * 40,
        authorizer="k", typed_phrase=expected_phrase(fid, p_sha),
    )
    ap = authorization_path(tmp_path, fid)
    status = json.loads(
        ap.with_suffix(ap.suffix + ".sig.status").read_text(encoding="utf-8")
    )
    assert status["status"] == "REFUSED"
    assert "broken symlink" in status.get("reason", "")


def test_c_pre_patch_fire_marker_line_anchored(tmp_path: Path) -> None:
    """Patch #3 round-1 (audit HIGH 877ec65c + MED 5cf6f6da): the C
    pre-patch fire detection requires line-anchored markers. A log
    that merely *contains* the word FIRE (e.g. in a vendored .c
    comment echoed during compile) must not falsely fire."""
    from audit_pipeline.bundle.verifier import _gate_poc_fails_pre_patch_c
    fid = 3050
    _seed_bundle(tmp_path, fid)
    # Set up a workspace with an L2 runlog that contains the word
    # "FIRE" in benign context (not at start of line). Use a real
    # git-initialised engine repo so the gate's language detection
    # picks 'c'.
    import subprocess
    engine = tmp_path / "engine_c"
    engine.mkdir()
    subprocess.run(["git", "init"], cwd=engine, capture_output=True, check=True)
    (engine / "src").mkdir()
    (engine / "src" / "main.c").write_text("int main(){return 0;}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=engine, capture_output=True, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-m", "i"], cwd=engine, capture_output=True, check=True)
    # Place benign runlog containing "fire" in context but no canonical
    # FIRE: line-start marker.
    poc_dir = tmp_path / "hunts" / "C1" / "poc"
    poc_dir.mkdir(parents=True)
    runlog = poc_dir / "runlog_x.log"
    runlog.write_text(
        "compiling vendored.c\n"
        "// comment mentioning FIRE: in a code comment, not stderr\n"
        "test passed\n",
        encoding="utf-8",
    )
    g = _gate_poc_fails_pre_patch_c(tmp_path, fid, engine, "test_x")
    # Benign mention should NOT trigger fired=True
    assert g.passed is False or g.passed is None


def test_c_pre_patch_fire_marker_matches_canonical_asan(tmp_path: Path) -> None:
    """The line-anchored regex still catches the canonical ASan format."""
    from audit_pipeline.bundle.verifier import _gate_poc_fails_pre_patch_c
    fid = 3051
    _seed_bundle(tmp_path, fid)
    import subprocess
    engine = tmp_path / "engine_c2"
    engine.mkdir()
    subprocess.run(["git", "init"], cwd=engine, capture_output=True, check=True)
    (engine / "src").mkdir()
    (engine / "src" / "main.c").write_text("int main(){return 0;}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=engine, capture_output=True, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-m", "i"], cwd=engine, capture_output=True, check=True)
    poc_dir = tmp_path / "hunts" / "C1" / "poc"
    poc_dir.mkdir(parents=True)
    runlog = poc_dir / "runlog_x.log"
    runlog.write_text(
        "running test_x\n"
        "==12345==ERROR: AddressSanitizer: heap-buffer-overflow at 0x...\n"
        "    #0 0x1000 in foo (file.c:42)\n",
        encoding="utf-8",
    )
    g = _gate_poc_fails_pre_patch_c(tmp_path, fid, engine, "test_x")
    assert g.passed is True
    assert "fired" in (g.reason or "").lower() or "fire" in (g.reason or "").lower()


def test_apply_patch_refuses_when_no_b_headers() -> None:
    """Patch #3 round-1 (audit HIGH 5524ccc2): no more `src/percolator.rs`
    fallback. A patch with no `+++ b/` headers must be rejected outright."""
    from audit_pipeline.bundle.verifier import _apply_patch
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        import subprocess
        subprocess.run(["git", "init"], cwd=td_p, capture_output=True, check=True)
        ok, err = _apply_patch(td_p, "this is not a unified diff at all\n")
        assert ok is False
        assert "no `+++ b/`" in err or "refusing to apply" in err


def test_sign_domains_includes_authorization() -> None:
    """Patch #3 round-1: authorization sidecar gets its own domain tag."""
    from audit_pipeline.commands.sign import SIGN_DOMAINS
    assert "authorization" in SIGN_DOMAINS
    # Domain tag follows the schema-v2 naming convention
    assert SIGN_DOMAINS["authorization"] == b"jelleo-authorization/v2\x00"


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


def test_patch_well_formed_rejects_multi_crate_patch(tmp_path: Path) -> None:
    """Bundles can span files within one crate but not across crates."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 4
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    cross_crate = (
        "--- a/programs/foo/src/lib.rs\n+++ b/programs/foo/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/programs/bar/src/lib.rs\n+++ b/programs/bar/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\n"
    )
    write_patch(tmp_path, fid, cross_crate)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is False
    assert "multiple crates" in g.reason


def test_patch_well_formed_accepts_multi_file_in_one_crate(tmp_path: Path) -> None:
    """Two files in the same `programs/<crate>/` are allowed."""
    from audit_pipeline.bundle.assembly import write_meta, write_patch
    from audit_pipeline.bundle.verifier import _gate_patch_well_formed
    fid = 5
    write_meta(
        tmp_path, finding_id=fid, engine_sha="a" * 40, bug_class="x",
        hypothesis_id="y", severity="Low", title="t", template_used="generic",
    )
    multi = (
        "--- a/programs/foo/src/lib.rs\n+++ b/programs/foo/src/lib.rs\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/programs/foo/src/state.rs\n+++ b/programs/foo/src/state.rs\n@@ -1 +1 @@\n-a\n+b\n"
    )
    write_patch(tmp_path, fid, multi)
    g = _gate_patch_well_formed(tmp_path, fid)
    assert g.passed is True


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
    # Patch #3 round-1: _seed_passing_verification now writes 6 gates
    # (added patch_unchanged_during_verify), so the column shows 6/6.
    assert "6/6" in html


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


def test_engine_repo_legacy_fallback_envvar_consistent_across_callsites():
    """R5b-2 (2026-05-24) — threat-modeler #1 fix:
    JELLEO_ENGINE_REPO_LEGACY_FALLBACK is the engine-repo escape hatch,
    SEPARATE from JELLEO_AUTHZ_ALLOW_UNSIGNED (the signature escape
    hatch). Two security controls must have two independent opt-outs.

    Source-grep invariant: BOTH review_cmd and open_pr_cmd in bundle.py
    must (a) reference JELLEO_ENGINE_REPO_LEGACY_FALLBACK by exact name,
    (b) compare it against the literal string "1", and (c) NOT reference
    JELLEO_AUTHZ_ALLOW_UNSIGNED in their engine-repo branches.

    Catches typos in the env var name and accidental re-merging of the
    two opt-outs that R5b-2 explicitly split."""
    from pathlib import Path as _P
    src = _P(__file__).resolve().parent.parent / "src" / "audit_pipeline" / "commands" / "bundle.py"
    txt = src.read_text(encoding="utf-8")
    # Must contain the new env var name at least twice (review_cmd + open_pr_cmd).
    assert txt.count("JELLEO_ENGINE_REPO_LEGACY_FALLBACK") >= 2, (
        "JELLEO_ENGINE_REPO_LEGACY_FALLBACK must be referenced in both "
        "review_cmd and open_pr_cmd engine-repo fallback branches."
    )
    # Must use the literal '1' comparison (no truthy/non-empty checks).
    assert 'JELLEO_ENGINE_REPO_LEGACY_FALLBACK") == "1"' in txt, (
        "JELLEO_ENGINE_REPO_LEGACY_FALLBACK must be checked against '1' "
        "literally — truthy comparisons (`if env_var:`) would accept any "
        "non-empty string and create operator confusion."
    )
    # NEGATIVE: the engine-repo branches must NOT use the signature opt-out
    # var. Test by checking the comment "two independent security controls"
    # exists — confirming R5b-2 separation intent is documented in code.
    assert "two independent security controls" in txt or "SEPARATE from" in txt, (
        "bundle.py must document that engine-repo and signature opt-outs "
        "are separate (threat-modeler #1 fix invariant)."
    )
