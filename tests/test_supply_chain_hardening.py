"""Regression tests for supply-chain hardening (audit fix/audit-001).

These are source-grep tests, not behavioral. They protect the supply-chain
defenses from being accidentally removed in future refactors. The defenses
themselves are bash + systemd-unit constructs that can't be unit-tested
in pytest — but the presence of the guard lines is easy to enforce.

Closes audit findings:
  - 5c4e072e / 500607134 (autoupdate RCE via GitHub account compromise)
  - R2-3 chain (path-hijack via pip --user install)
  - R2-2 / 5819e8b5 (bootstrap curl-pipe-bash with no integrity check)
  - R2-2 corpus (git submodule hook execution from compromised third-party repo)
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"


# ----------------------------------------------------------------------------
# jelleo-autoupdate.sh — GPG verify gate
# ----------------------------------------------------------------------------


class TestAutoupdateGpgGate:
    """The autoupdate script must verify the new HEAD's signature BEFORE
    running pip install or restart. A compromised GitHub account otherwise
    achieves root RCE on the VPS within 5 minutes."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_contains_verify_commit_call(self):
        src = self._script()
        assert "git verify-commit HEAD" in src, (
            "jelleo-autoupdate.sh must call `git verify-commit HEAD` after pull. "
            "Without it, any GitHub push installs as root with no integrity check."
        )

    def test_contains_rollback_on_verify_failure(self):
        src = self._script()
        # Look for the rollback pattern: git reset --hard "$LOCAL"
        assert 'git reset --hard "$LOCAL"' in src, (
            "jelleo-autoupdate.sh must roll back to the pre-pull HEAD when "
            "signature verification fails (otherwise an unsigned malicious "
            "commit stays in working tree even if install is blocked)."
        )

    def test_verify_runs_before_pip_install(self):
        src = self._script()
        # Verify-commit must appear BEFORE pip install in the file
        verify_idx = src.find("git verify-commit HEAD")
        # Match the actual install line (pip install -e .) NOT the rollback hint
        # The real install uses PIP_NO_USER=1 prefix
        pip_idx = src.find("PIP_NO_USER=1 pip install")
        assert verify_idx > 0, "verify-commit call missing"
        assert pip_idx > 0, "pip install call missing"
        assert verify_idx < pip_idx, (
            "verify-commit must come BEFORE pip install — otherwise the "
            "malicious package is installed before we check its signature"
        )

    def test_pip_install_uses_no_user(self):
        src = self._script()
        assert "PIP_NO_USER=1 pip install" in src, (
            "pip install must use PIP_NO_USER=1 to block path-hijack via "
            "/root/.local/lib/pythonX.Y/site-packages (audit R2-3)"
        )

    def test_bypass_env_var_logged_loudly(self):
        """If the operator sets JELLEO_ALLOW_UNSIGNED=1, every tick should
        log a WARN — otherwise the bypass becomes a silent default."""
        src = self._script()
        assert "JELLEO_ALLOW_UNSIGNED" in src
        # The WARN line must appear in the bypass branch
        bypass_block_start = src.find("JELLEO_ALLOW_UNSIGNED")
        # Find the next 'log "' call after the bypass check
        snippet = src[bypass_block_start:bypass_block_start + 500]
        assert 'WARN: JELLEO_ALLOW_UNSIGNED' in snippet, (
            "Bypass must log WARN every tick so it can't become a silent default"
        )

    def test_signature_failure_exits_non_zero(self):
        """Distinct exit code so journalctl shows security event vs no-op tick.
        The unit declares SuccessExitStatus=3 so the timer keeps firing."""
        src = self._script()
        verify_idx = src.find("BLOCKED: HEAD signature verification failed")
        assert verify_idx > 0
        # Look at the full failure-handling block (next ~2KB) for `exit 3`
        snippet = src[verify_idx:verify_idx + 2200]
        assert "exit 3" in snippet, (
            "Signature failure must exit 3 (security event), not 0 (silent success)"
        )

    def test_rollback_syncs_submodules(self):
        """git reset --hard updates submodule POINTERS but not submodule
        working trees — they keep rejected code. Must sync explicitly."""
        src = self._script()
        verify_idx = src.find("BLOCKED: HEAD signature verification failed")
        snippet = src[verify_idx:verify_idx + 1500]
        assert "git submodule update --init --recursive" in snippet, (
            "Rollback must sync submodules to the rolled-back pointer"
        )


# ----------------------------------------------------------------------------
# bootstrap.sh — integrity gate
# ----------------------------------------------------------------------------


class TestBootstrapIntegrityGate:
    """bootstrap.sh used to be delivered via curl-pipe-bash with no
    integrity check. A MITM of raw.githubusercontent.com or repo compromise
    would land arbitrary root code on a fresh VPS provision."""

    def _script(self) -> str:
        return (DEPLOY / "bootstrap.sh").read_text(encoding="utf-8")

    def test_requires_verified_env_var(self):
        src = self._script()
        assert "JELLEO_BOOTSTRAP_VERIFIED" in src
        # Must exit non-zero if unset
        assert 'JELLEO_BOOTSTRAP_VERIFIED:-' in src, (
            "Must check JELLEO_BOOTSTRAP_VERIFIED is set (with :- default to empty)"
        )

    def test_rejects_short_sha(self):
        """A non-empty value that's not 64 chars (SHA-256 hex) or 'skip'
        must be rejected — otherwise an empty quoted string passes."""
        src = self._script()
        assert "${#JELLEO_BOOTSTRAP_VERIFIED} -ne 64" in src, (
            "Must validate SHA-256 length"
        )

    def test_actually_verifies_sha_match(self):
        """The original gate only validated FORMAT of the supplied hash.
        A MITM that served a modified script + plausible 64-char hex value
        would bypass the gate. Must now compute sha256sum and compare."""
        src = self._script()
        assert "sha256sum" in src, "Must call sha256sum on the running script"
        assert 'sha256sum "$0"' in src, (
            "Must hash the running script ($0) — not a different file"
        )
        assert 'JELLEO_BOOTSTRAP_VERIFIED" != "$ACTUAL_SHA' in src, (
            "Must compare supplied hash against computed hash"
        )

    def test_uses_no_user_install(self):
        """bootstrap.sh used to pip install --user, which is the path-hijack
        attack surface. Must use PIP_NO_USER=1 instead."""
        src = self._script()
        # The pip install line must be guarded by PIP_NO_USER
        assert "PIP_NO_USER=1 python3 -m pip install" in src, (
            "bootstrap.sh must use PIP_NO_USER=1 to avoid /root/.local/lib hijack"
        )
        # Sanity: the old vulnerable --user form should NOT appear in an active install line
        # (the doc strings are allowed to reference --user explanatorily)
        lines = [
            line for line in src.splitlines()
            if line.strip().startswith("python3 -m pip install --user")
            or line.strip().startswith("/root/.local/bin/python3 -m pip install --user")
        ]
        assert not lines, f"--user pip install still present: {lines}"

    def test_skip_value_logs_warning(self):
        src = self._script()
        assert "WARN: JELLEO_BOOTSTRAP_VERIFIED=skip" in src, (
            "skip bypass must log a loud WARN, not silently allow"
        )

    def test_old_curl_pipe_pattern_documented_as_blocked(self):
        src = self._script()
        # The header comment must explicitly say curl-pipe-bash is no longer allowed
        assert "Do NOT use:" in src or "blocked" in src.lower()


# ----------------------------------------------------------------------------
# refresh_corpus.sh — hook execution prevention
# ----------------------------------------------------------------------------


class TestCorpusRefreshHooksDisabled:
    """refresh_corpus.sh runs git submodule update against third-party repos
    (anchor, drift, mango etc.). If any one of them is compromised and ships
    a malicious post-checkout hook, that hook would execute as root on the
    Jelleo VPS without these defenses."""

    def _script(self) -> str:
        return (DEPLOY / "refresh_corpus.sh").read_text(encoding="utf-8")

    def test_hooks_path_disabled(self):
        src = self._script()
        assert "core.hooksPath=/dev/null" in src, (
            "Every git invocation against a corpus repo must set "
            "core.hooksPath=/dev/null to block hook execution"
        )

    def test_file_protocol_blocked(self):
        src = self._script()
        assert "protocol.file.allow=never" in src, (
            "file:// submodule URLs are an SSRF vector — must be blocked"
        )

    def test_safe_flags_applied_to_pull_and_submodule(self):
        """Both git pull AND git submodule update must use the safe flags —
        post-rewrite hooks can fire on pull too. Safe form is the bash array
        `"${GIT_SAFE[@]}"` to avoid word-split fragility (code-reviewer
        feedback on initial draft)."""
        src = self._script()
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("git ") and "pull" in stripped:
                assert '"${GIT_SAFE[@]}"' in stripped, (
                    f"git pull missing safe array expansion: {stripped}"
                )
            if stripped.startswith("git ") and "submodule" in stripped:
                assert '"${GIT_SAFE[@]}"' in stripped, (
                    f"git submodule missing safe array expansion: {stripped}"
                )
            if stripped.startswith("git ") and "fetch" in stripped:
                assert '"${GIT_SAFE[@]}"' in stripped, (
                    f"git fetch missing safe array expansion: {stripped}"
                )

    def test_git_safe_is_array_not_string(self):
        """Regression guard: GIT_SAFE must be a bash array. The string form
        relies on unquoted word-splitting which breaks if any value contains
        a space (code-reviewer feedback)."""
        src = self._script()
        assert "GIT_SAFE=(-c" in src, (
            "GIT_SAFE must be declared as a bash array: GIT_SAFE=(-c ... -c ...)"
        )
        # The old string form must NOT be present
        assert 'GIT_SAFE="-c' not in src, (
            "Old string-form GIT_SAFE='-c ...' is fragile — convert to array"
        )


# ----------------------------------------------------------------------------
# jelleo-token-auth.service — PYTHONNOUSERSITE
# ----------------------------------------------------------------------------


class TestTokenAuthServiceHardening:
    """jelleo-token-auth.service handles all customer manifest URL HMAC
    verification. A path-hijack shim in /root/.local/lib/.../site-packages
    intercepts every sign() and verify() call — a backdoor that survives
    git revert. PYTHONNOUSERSITE=1 disables the user-site path entirely."""

    def _unit(self) -> str:
        return (DEPLOY / "jelleo-token-auth.service").read_text(encoding="utf-8")

    def test_python_no_user_site(self):
        src = self._unit()
        assert "PYTHONNOUSERSITE=1" in src, (
            "Token-auth service must set PYTHONNOUSERSITE=1 to prevent "
            "path-hijack via /root/.local/lib/.../site-packages"
        )

    def test_no_new_privileges_still_set(self):
        """Regression guard — make sure the new env line didn't break the
        existing hardening directives that were already correct."""
        src = self._unit()
        assert "NoNewPrivileges=true" in src
        assert "PrivateTmp=true" in src
        assert "ProtectSystem=full" in src


# ----------------------------------------------------------------------------
# All Python-running services must have PYTHONNOUSERSITE=1 — completes the
# R2-3 fix (path-hijack defense was incomplete with token-auth alone, since
# the other Python daemons also run as root and would load shims from the
# user-site path).
# ----------------------------------------------------------------------------


class TestAllPythonServicesNoUserSite:
    """Every systemd unit that launches Python as root must set
    PYTHONNOUSERSITE=1. Coverage gap = path-hijack persistence."""

    SERVICES_WITH_PYTHON = (
        "jelleo-token-auth.service",
        "jelleo-watch.service",
        "jelleo-shadow.service",
        "jelleo-sse.service",
        "jelleo-autoupdate.service",
    )

    def test_all_python_services_set_no_user_site(self):
        missing = []
        for svc in self.SERVICES_WITH_PYTHON:
            src = (DEPLOY / svc).read_text(encoding="utf-8")
            if "PYTHONNOUSERSITE=1" not in src:
                missing.append(svc)
        assert not missing, (
            f"These Python services must set Environment=PYTHONNOUSERSITE=1: "
            f"{missing}. Without it, /root/.local/lib/.../site-packages shims "
            f"persist as a backdoor surviving git revert."
        )

    def test_autoupdate_unit_declares_signature_exit_code(self):
        """jelleo-autoupdate.sh exits 3 on signature failure. The unit must
        declare SuccessExitStatus=3 so systemd keeps the timer firing (the
        rejection is a security event, but not a unit failure)."""
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "SuccessExitStatus=3" in src, (
            "jelleo-autoupdate.service must declare SuccessExitStatus=3 to "
            "tolerate the signature-failure exit code without alerting"
        )


# ----------------------------------------------------------------------------
# install_systemd.sh must use PIP_NO_USER=1 too (R2-3 chain)
# ----------------------------------------------------------------------------


class TestInstallSystemdNoUserInstall:
    """install_systemd.sh runs as root and previously did
    pip install --user cryptography. That writes to /root/.local/lib/...
    — the exact path the runtime PYTHONNOUSERSITE flag is supposed to block.
    Closing the loop requires NOT installing there in the first place."""

    def test_pip_install_uses_no_user(self):
        src = (DEPLOY / "install_systemd.sh").read_text(encoding="utf-8")
        # Active install lines (not in echoed help text) must have PIP_NO_USER
        active_install_lines = [
            line for line in src.splitlines()
            if "pip install" in line
            and "--user" not in line
            and not line.strip().startswith("#")
            and not line.strip().startswith('echo "')
            and not line.strip().startswith("echo \"")
        ]
        # At least one active install line should exist
        assert active_install_lines, "no pip install command found"
        # The active install should be guarded by PIP_NO_USER
        non_pip_no_user = [
            line for line in active_install_lines if "PIP_NO_USER=1" not in line
        ]
        assert not non_pip_no_user, (
            f"these install lines lack PIP_NO_USER=1: {non_pip_no_user}"
        )
