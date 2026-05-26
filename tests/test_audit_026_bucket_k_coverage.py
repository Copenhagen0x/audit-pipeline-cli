"""Regression tests for audit-026 (Bucket K coverage sweep + L11 lock).

Bucket K — 9 functions flagged as critical-path-with-no-test-coverage in
``REPORT.md``. Coverage audit at audit-026 time:

| K  | Function                                  | Status                       |
|----|-------------------------------------------|------------------------------|
| K1 | ``_inline_md()`` XSS path                 | covered: test_inline_md_xss  |
| K2 | ``all_passed()`` gate-bypass condition    | GAP — added below            |
| K3 | ``upsert_hypothesis()`` Postgres path     | function removed (stale)     |
| K4 | ``scoping.py`` dedup logic                | covered: test_scoping_patch5 |
| K5 | autoupdate integrity check                | covered: test_supply_chain   |
| K6 | ``validate_authorization()`` replay       | covered: test_verif_gates    |
| K7 | L4 coverage threshold enforcement         | covered: test_l4_false_pass  |
| K8 | L3 timeout-vs-failure distinction         | covered: test_l4_false_pass  |
| K9 | ``tool_read_file()`` path traversal guard | GAP — added below            |

L11 — ``L6 sign step silently skipped when JELLEO_SKIP_SIGN=1 with no
audit log entry``. Verified at audit-026 time: no such env var exists
in ``sign.py`` or ``bundle.py`` (P2 refactored). Regression lock below.
"""

from __future__ import annotations

from pathlib import Path

# --- K2: all_passed() gate-bypass --------------------------------------------


def test_all_passed_blocks_skipped_gate_without_na_code() -> None:
    """K2: ``all_passed`` must return False when a gate is ``passed=None``
    (skipped) without a structured ``skip_reason_code`` in the N/A
    allowlist. Closes the historical bypass where free-text reasons
    could match allowlisted phrases (audit CRITICAL 41b26e30).
    """
    from audit_pipeline.bundle.verifier import all_passed

    verification = {
        "engine_lang": "rust",
        "gates": {
            "kani": {
                "passed": None,
                "reason": "skipped — no kani_harness registered for hypothesis 41",
                # NO skip_reason_code — must BLOCK
            },
            "tests_pass_post_patch": {"passed": True},
        },
    }
    assert all_passed(verification) is False, (
        "K2 regression: skipped gate without structured N/A code must "
        "block authorization (audit CRITICAL 41b26e30)"
    )


def test_all_passed_accepts_all_true_gates() -> None:
    """Sanity: when every gate is passed=True, the bundle is authorized."""
    from audit_pipeline.bundle.verifier import all_passed

    verification = {
        "engine_lang": "rust",
        "gates": {
            "kani": {"passed": True},
            "tests_pass_post_patch": {"passed": True},
            "sig_verify": {"passed": True},
        },
    }
    assert all_passed(verification) is True


def test_all_passed_blocks_failed_gate() -> None:
    """Sanity: any passed=False gate blocks authorization."""
    from audit_pipeline.bundle.verifier import all_passed

    verification = {
        "engine_lang": "rust",
        "gates": {
            "kani": {"passed": True},
            "tests_pass_post_patch": {"passed": False},
        },
    }
    assert all_passed(verification) is False


# --- K9: tool_read_file path traversal --------------------------------------


def test_tool_read_file_blocks_posix_traversal(tmp_path: Path) -> None:
    """K9: ``tool_read_file`` must refuse to read a path that resolves
    outside the workspace (``../../etc/passwd``-style).
    """
    from audit_pipeline.utils.llm_tools import tool_read_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The traversal target — we create it so the test would fail
    # OBVIOUSLY if the guard let the read through (it would return
    # the file content; we assert it does NOT).
    outside = tmp_path / "secrets.txt"
    outside.write_text("SECRET=hunter2\n", encoding="utf-8")

    out = tool_read_file(workspace, "../secrets.txt")
    assert "SECRET" not in out, (
        f"K9 regression: tool_read_file leaked outside-workspace content. "
        f"Got: {out!r}"
    )
    # The expected behavior is an ERROR string return.
    assert out.startswith("ERROR"), (
        f"K9 regression: expected ERROR return on traversal attempt; "
        f"got: {out!r}"
    )


def test_tool_read_file_reads_inside_workspace(tmp_path: Path) -> None:
    """Sanity: a workspace-relative read must work."""
    from audit_pipeline.utils.llm_tools import tool_read_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "ok.txt"
    target.write_text("inside content\n", encoding="utf-8")

    out = tool_read_file(workspace, "ok.txt")
    assert "inside content" in out
    assert not out.startswith("ERROR")


# --- L11: no skip-sign env var ----------------------------------------------


def test_no_skip_sign_env_var_in_signing_pipeline() -> None:
    """L11 verified-clean lock: no ``JELLEO_SKIP_SIGN`` (or similar)
    env-var-based sign-skip mechanism exists in the signing pipeline.
    The L11 finding was stale; P2 refactored away the surface.

    Source-level pin: any reintroduction of ``JELLEO_SKIP_SIGN`` or
    ``SKIP_SIGN`` env-var consultation in sign.py / bundle.py fails
    this test.
    """
    for rel in (
        "src/audit_pipeline/commands/sign.py",
        "src/audit_pipeline/commands/bundle.py",
    ):
        src = Path(rel).read_text(encoding="utf-8")
        forbidden = ["JELLEO_SKIP_SIGN", "SKIP_SIGN"]
        for needle in forbidden:
            assert needle not in src, (
                f"L11 regression: {needle!r} found in {rel}. The signing "
                f"pipeline must not consult an env-var-based sign-skip "
                f"toggle — that would silently bypass authorization "
                f"with no audit trail."
            )
