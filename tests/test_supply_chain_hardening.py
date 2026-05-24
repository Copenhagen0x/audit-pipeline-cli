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

import re
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
        # Round-2 update: regex match — the bare invocation was replaced by
        # `git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=... verify-commit HEAD`.
        import re
        assert re.search(r"verify-commit\s+HEAD", src), (
            "jelleo-autoupdate.sh must call `verify-commit HEAD` after pull. "
            "Without it, any GitHub push installs as root with no integrity check."
        )

    def test_contains_rollback_on_verify_failure(self):
        src = self._script()
        # Round-4 update: invocation is now `git "${GIT_SAFE[@]}" reset --hard "$LOCAL"`
        # (GIT_SAFE expansion between git and reset). Use substring `reset --hard "$LOCAL"`
        # which is stable across both forms.
        assert 'reset --hard "$LOCAL"' in src, (
            "jelleo-autoupdate.sh must roll back to the pre-pull HEAD when "
            "signature verification fails (otherwise an unsigned malicious "
            "commit stays in working tree even if install is blocked)."
        )

    def test_verify_runs_before_pip_install(self):
        src = self._script()
        # Round-2: `verify-commit HEAD` now follows several -c args; use the
        # substring `verify-commit HEAD` instead of `git verify-commit HEAD`.
        verify_idx = src.find("verify-commit HEAD")
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
        The unit declares SuccessExitStatus=3 so the timer keeps firing.

        Round-7 fix (devils-advocate ROUND-6 HIGH #1): use ANCHORED line-start
        regex, not bare substring. Round-6 added comments containing the text
        `exit 3 = ...` inside this snippet window — bare `in` would match the
        comment even if the actual `exit 3` line on the rollback path was
        changed (e.g., to `exit 4`), silently breaking the test's invariant.
        Anchored regex `^\\s+exit 3\\s*$` matches ONLY a real statement line.
        """
        src = self._script()
        verify_idx = src.find("BLOCKED: HEAD signature verification failed")
        assert verify_idx > 0
        # Look at the full failure-handling block (next ~5KB) for the actual
        # `exit 3` STATEMENT (whitespace, then `exit 3`, then end-of-line —
        # not inside a comment, not inside a string).
        snippet = src[verify_idx:verify_idx + 5500]
        assert re.search(r"^\s+exit 3\s*$", snippet, re.MULTILINE), (
            "Signature failure must exit 3 (security event), not 0 (silent success). "
            "Test uses anchored regex to avoid matching comment-text `exit 3` mentions."
        )

    def test_rollback_syncs_submodules(self):
        """git reset --hard updates submodule POINTERS but not submodule
        working trees — they keep rejected code. Must sync explicitly.
        Round-2: now invoked as `git "${GIT_SAFE[@]}" submodule update --init --recursive`.
        Round-4: snippet window widened (rollback block grew with submodule-
        existence check + GIT_SAFE on reset)."""
        src = self._script()
        verify_idx = src.find("BLOCKED: HEAD signature verification failed")
        snippet = src[verify_idx:verify_idx + 4000]
        assert "submodule update --init --recursive" in snippet, (
            "Rollback must sync submodules to the rolled-back pointer"
        )
        # Round-2 (devils-advocate CRITICAL #3): the sync must also use GIT_SAFE
        assert '"${GIT_SAFE[@]}" submodule update' in snippet, (
            "Rollback submodule sync must apply GIT_SAFE flags"
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
        feedback on initial draft).

        ROUND-2 FIX (code-reviewer LOW #10): startswith("git ") did NOT match
        the `if ( cd && git ...)` shape. Replaced with substring scan.

        ROUND-3 FIX (code-reviewer round-2 LOW #1): even the round-2 fix was
        still vacuously green for refresh_corpus.sh because the actual line is
        `git "${GIT_SAFE[@]}" pull` (not `git pull` — GIT_SAFE between them).
        Substring `git pull` never matches. Use regex `\\bgit\\s+\\S*\\s*<verb>\\b`
        which matches both bare AND array-expanded forms, plus found_one assert
        so a deleted line is detected as a regression."""
        import re
        src = self._script()
        found_pull = False
        found_submodule = False
        for line in src.splitlines():
            stripped = line.split("#", 1)[0]
            if re.search(r"\bgit\s+\S*\s*pull\b", stripped):
                found_pull = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git pull missing safe array expansion: {line!r}"
                )
            if re.search(r"\bgit\s+\S*\s*submodule\b", stripped):
                found_submodule = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git submodule missing safe array expansion: {line!r}"
                )
            if re.search(r"\bgit\s+\S*\s*fetch\b", stripped):
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git fetch missing safe array expansion: {line!r}"
                )
        assert found_pull, "no executable `git pull` line found in refresh_corpus.sh"
        assert found_submodule, "no executable `git submodule` line found in refresh_corpus.sh"

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
        # ROUND-2 FIX (devils-advocate HIGH #5): six more Python services
        # were originally missed. Each runs `audit-pipeline` (Python CLI) as
        # root and would otherwise load shims from /root/.local/lib/.../site-packages.
        "jelleo-heartbeat.service",
        "jelleo-scheduler-24h.service",
        "jelleo-scheduler-weekly.service",
        "jelleo-scheduler-monthly.service",
        "jelleo-snapshot.service",
        "jelleo-health.service",
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

    def test_autoupdate_unit_excludes_exit_4_from_success(self):
        """Round-7 fix (devils-advocate ROUND-6 MED #2): exit 4 paths
        (LOCAL='?' corrupt repo, interrupt-rollback, bash version guard) MUST
        trigger OnFailure=jelleo-alert-failure@. The entire exit-code split
        depends on `SuccessExitStatus` NOT containing 4. A future edit that
        adds `SuccessExitStatus=3 4` (perhaps to silence bash-version-guard
        spam during a migration) silently disables operator alerts for
        corruption + interrupt cases. Lock the invariant here."""
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        # Find every SuccessExitStatus= line, verify no `4` token in the values.
        # `SuccessExitStatus=3` should match; `SuccessExitStatus=3 4` should fail;
        # `SuccessExitStatus=3,4` should fail; `SuccessExitStatus=40` is a
        # different code but `4` would still be there as a digit — be strict:
        # tokenize on whitespace + comma, exact match against "4".
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped.startswith("SuccessExitStatus="):
                continue
            values = re.split(r"[\s,]+", stripped.split("=", 1)[1])
            assert "4" not in values, (
                f"SuccessExitStatus must NOT contain 4 — that would suppress "
                f"OnFailure alerts for corrupt-repo + interrupt-rollback + "
                f"bash-version-guard cases that need operator attention. "
                f"Offending line: {stripped!r}"
            )

    def test_autoupdate_unit_has_timeout_stop_sec(self):
        """Round-7 fix (devils-advocate ROUND-5 HIGH #2 regression check):
        TimeoutStopSec must be set EXPLICITLY (not relying on systemd default
        90s) so SIGKILL doesn't arrive mid-rollback during a `git reset --hard`
        on a large repo. Locks the round-5 fix against accidental removal."""
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        m = re.search(r"^TimeoutStopSec=(\d+)", src, re.MULTILINE)
        assert m, "jelleo-autoupdate.service must set TimeoutStopSec=<seconds> explicitly"
        secs = int(m.group(1))
        assert secs >= 120, (
            f"TimeoutStopSec={secs} is too short — git reset --hard on a large "
            f"repo can take >90s, the systemd default. Use ≥120 (round-5 fix used 300)."
        )

    def test_autoupdate_script_requires_bash_4_4(self):
        """Round-7 regression check: bash version guard at top must check for
        4.4+ (closes round-5 MED #4 trap re-entry on bash 3.x / 4.0-4.3).
        Locks against accidental removal of the guard."""
        src = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
        # Look for the BASH_VERSINFO check in the top ~50 lines
        head = "\n".join(src.splitlines()[:50])
        assert "BASH_VERSINFO" in head, (
            "jelleo-autoupdate.sh must check BASH_VERSINFO at top to refuse "
            "bash < 4.4 (trap-exit re-entry bug). See round-5 MED #4."
        )
        # Also verify it refuses with an alerting exit code (4), not silent 0
        guard_match = re.search(r"BASH_VERSINFO.*?exit\s+(\d+)", head, re.DOTALL)
        assert guard_match, "bash version guard must `exit <code>` on failure"
        assert int(guard_match.group(1)) >= 1, (
            "bash version guard must exit non-zero (preferably 4 = alert) so "
            "OnFailure fires when the operator's VPS has bash too old"
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


# ----------------------------------------------------------------------------
# ROUND-2 HARDENING TESTS — devils-advocate + code-reviewer findings on the
# original Patch #1. Each test below corresponds to a specific finding from
# the iterate-until-clean round-1 review. Locks the fix as a regression bar.
# ----------------------------------------------------------------------------


class TestRound2AutoupdateEnvShadow:
    """Devils-advocate CRITICAL #1: JELLEO_ALLOW_UNSIGNED=1 was injectable via
    EnvironmentFile=-/root/.audit-env. Attacker who writes that file silently
    bypasses the GPG gate. Round-2 fix: hardcode Environment=JELLEO_ALLOW_UNSIGNED=0
    in the service unit so the env-file value is SHADOWED."""

    def test_autoupdate_service_hardcodes_allow_unsigned_zero(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_ALLOW_UNSIGNED=0" in src, (
            "jelleo-autoupdate.service must hardcode Environment=JELLEO_ALLOW_UNSIGNED=0 "
            "to shadow any value injected via EnvironmentFile=-/root/.audit-env. "
            "Without this an attacker who writes /root/.audit-env silently bypasses "
            "the GPG verification gate. (round-2 devils-advocate CRITICAL #1)"
        )

    def test_shadow_appears_after_envfile_directive(self):
        """systemd processes directives in order; the Environment= line must
        appear AFTER EnvironmentFile= so it actually shadows the file value."""
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_ALLOW_UNSIGNED=0")
        assert envfile_idx >= 0, "EnvironmentFile= directive missing"
        assert shadow_idx >= 0, "shadow directive missing"
        assert shadow_idx > envfile_idx, (
            "Environment=JELLEO_ALLOW_UNSIGNED=0 must appear AFTER "
            "EnvironmentFile= so it shadows the file-injected value"
        )


class TestRound2VerifyCommitPinsSigners:
    """Devils-advocate CRITICAL #2: bare `git verify-commit HEAD` trusted
    whatever `gpg.ssh.allowedSignersFile` global config pointed to. Attacker
    who writes /root/.gitconfig redirects to their own signers file.
    Round-2 fix: pin allowedSignersFile at invocation time."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_verify_commit_pins_allowed_signers_file(self):
        src = self._script()
        # The verify-commit invocation must carry an inline -c override
        # for gpg.ssh.allowedSignersFile (and gpg.format=ssh for safety).
        assert "gpg.ssh.allowedSignersFile=" in src, (
            "verify-commit must pin allowedSignersFile inline via -c so a write "
            "to /root/.gitconfig cannot redirect verification"
        )
        assert "gpg.format=ssh" in src, (
            "verify-commit should also pin gpg.format=ssh inline"
        )

    def test_pinned_path_is_absolute(self):
        src = self._script()
        # The path should be an absolute /root/.ssh/... path, not relative
        assert "/root/.ssh/" in src, (
            "allowedSignersFile path must be absolute; relative would be operator-CWD-dependent"
        )

    def test_pin_uses_env_var_for_override(self):
        """Operator can override via JELLEO_ALLOWED_SIGNERS for test/staging."""
        src = self._script()
        assert "JELLEO_ALLOWED_SIGNERS" in src, (
            "operator override via env var should be available"
        )


class TestRound2RollbackUsesGitSafe:
    """Devils-advocate CRITICAL #3: rollback `git submodule update` ran
    without core.hooksPath=/dev/null. The defensive rollback IS the hook-
    execution vector. Round-2 fix: GIT_SAFE applied to rollback ops too."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_rollback_submodule_update_uses_git_safe(self):
        src = self._script()
        # Find the submodule update line in the rollback context
        in_rollback = False
        for line in src.splitlines():
            if "git verify-commit" in line or "BLOCKED: HEAD signature" in line:
                in_rollback = True
            if "git" in line and "submodule update" in line and in_rollback:
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"rollback submodule update line missing GIT_SAFE: {line!r}"
                )
                return
        raise AssertionError("could not find rollback submodule update line")

    def test_git_safe_array_declared_in_autoupdate(self):
        src = self._script()
        assert "GIT_SAFE=(-c" in src, (
            "jelleo-autoupdate.sh must declare GIT_SAFE bash array (same as "
            "refresh_corpus.sh) for use in rollback + fetch + pull"
        )

    def test_git_safe_contains_hooks_path_dev_null(self):
        src = self._script()
        assert "core.hooksPath=/dev/null" in src, (
            "GIT_SAFE must include `-c core.hooksPath=/dev/null` to block git hooks"
        )


class TestRound2BootstrapBashSDetection:
    """Devils-advocate HIGH #4: sha256sum "$0" produced misleading mismatch
    error under `bash -s` / `bash <(curl ...)` because $0 was the shell
    binary, not the script. Operator fell back to skip=. Round-2 fix:
    refuse upfront with precise error."""

    def _script(self) -> str:
        return (DEPLOY / "bootstrap.sh").read_text(encoding="utf-8")

    def test_detects_bash_s_invocation(self):
        src = self._script()
        # The script must have an explicit case for $0 being bash or sh
        assert "case \"$0\"" in src or 'case "$0" in' in src, (
            "bootstrap.sh must explicitly inspect $0 to detect non-file invocation"
        )
        # The case patterns must catch bash forms
        assert "bash|" in src or "*/bash" in src, (
            "case must catch bash / /bin/bash / */bash forms"
        )

    def test_detects_process_substitution(self):
        src = self._script()
        # /dev/fd/* or /proc/self/fd/* indicates process substitution
        assert "/dev/fd/" in src or "/proc/self/fd/" in src, (
            "bootstrap.sh must detect process-substitution forms"
        )

    def test_refuses_non_file_invocation_explicitly(self):
        src = self._script()
        # The refusal must come BEFORE sha256sum to avoid the misleading
        # "mismatch" error path.
        non_file_idx = src.find('test -f "$0"')
        if non_file_idx == -1:
            non_file_idx = src.find('[[ ! -f "$0" ]]')
        sha_idx = src.find('sha256sum "$0"')
        assert non_file_idx >= 0, "must have a not-a-file check"
        assert sha_idx >= 0, "sha256sum invocation should still exist"
        assert non_file_idx < sha_idx, (
            "not-a-file check must come BEFORE sha256sum to avoid the "
            "misleading mismatch error path"
        )


class TestRound2FetchAndPullUseGitSafe:
    """Devils-advocate HIGH #7: git fetch and git pull in jelleo-autoupdate.sh
    lacked core.hooksPath=/dev/null. post-merge / post-rewrite hooks fire on
    pull BEFORE the verify-commit gate. Round-2 fix: GIT_SAFE on both."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def _is_executable_git_line(self, line: str) -> bool:
        """A line is an actual git invocation (not a log message / comment)
        if `git ` appears OUTSIDE of any quoted string. Crude check: if the
        line is inside a `log "..."` call or starts with `echo`, skip."""
        stripped = line.strip()
        if stripped.startswith("#"):
            return False
        if "log \"" in line and "git " in line.split("log \"", 1)[1]:
            # The "git " token appears inside a log string — skip
            return False
        if stripped.startswith("echo "):
            return False
        return True

    def test_fetch_uses_git_safe(self):
        """Round-2: the executable fetch line is `git "${GIT_SAFE[@]}" fetch`
        (with GIT_SAFE expansion between git and fetch). Match that shape;
        skip lines where `git fetch` substring appears inside a log message."""
        src = self._script()
        import re
        found_one = False
        for line in src.splitlines():
            if not self._is_executable_git_line(line):
                continue
            # Either canonical `git fetch` (must have GIT_SAFE) OR the
            # round-2 form `git "${GIT_SAFE[@]}" fetch` (already safe).
            if re.search(r"\bgit\s+\S*\s*fetch\b", line) or "git fetch" in line.split("#", 1)[0]:
                # If the line invokes git with fetch, GIT_SAFE expansion must
                # appear between git and fetch (or be the safe array form).
                if "fetch" in line.split("#", 1)[0]:
                    found_one = True
                    assert '"${GIT_SAFE[@]}"' in line, (
                        f"git fetch missing GIT_SAFE: {line!r}"
                    )
        assert found_one, "no executable git fetch line found"

    def test_pull_uses_git_safe(self):
        src = self._script()
        import re
        found_one = False
        for line in src.splitlines():
            if not self._is_executable_git_line(line):
                continue
            stripped = line.split("#", 1)[0]
            # Match either bare `git pull` (which must have GIT_SAFE) or the
            # round-2 form `git "${GIT_SAFE[@]}" pull` (already safe).
            if re.search(r"\bgit\s+\S*\s*pull\b", stripped):
                found_one = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git pull missing GIT_SAFE: {line!r}"
                )
        assert found_one, "no executable git pull line found"


class TestRound2DirtyStateSentinel:
    """Code-reviewer LOW #11: if rollback fails bash-side, subsequent ticks
    went silent (LOCAL/REMOTE converge on the unsigned commit, early-exit
    fires). Round-2 fix: persistent sentinel file blocks ALL subsequent
    ticks until operator manually clears it."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_sentinel_path_declared(self):
        src = self._script()
        assert "DIRTY_SENTINEL=" in src or "JELLEO_AUTOUPDATE_DIRTY" in src, (
            "autoupdate must declare a dirty-state sentinel path"
        )

    def test_sentinel_check_blocks_run(self):
        src = self._script()
        # Must have an early-exit check that fires when the sentinel exists
        assert "-f \"$DIRTY_SENTINEL\"" in src or '-f "$DIRTY_SENTINEL"' in src, (
            "autoupdate must have an early-exit check on the sentinel file"
        )

    def test_sentinel_written_on_failed_rollback(self):
        """Round-6 update (code-reviewer ROUND-5 MED #2): use regex anchored
        to the FULL append-token. Substring `> $X` is satisfied by `>> $X`
        (because > is a prefix of >>), so the round-2 form was vacuously
        green after round-4 switched to `>>`. Now require `>>` explicitly."""
        import re
        src = self._script()
        # The append form must appear at least once after a rollback failure.
        assert re.search(r'>>\s*"\$DIRTY_SENTINEL"', src), (
            "autoupdate must write the sentinel via >> (append) when "
            "rollback fails"
        )


class TestRound2SupplyChainMdWarnTimingCorrect:
    """Code-reviewer MED #9: SUPPLY_CHAIN.md claimed the WARN fires every 5
    minutes when JELLEO_ALLOW_UNSIGNED=1. Actually it fires only when a new
    commit is pending. Round-2 fix: docs corrected."""

    def _md(self) -> str:
        return (DEPLOY / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")

    def test_docs_do_not_claim_every_5_minutes_warn(self):
        md = self._md()
        # The misleading phrase "every 5 minutes" should NOT appear in the
        # emergency-bypass section adjacent to the WARN claim. (The phrase
        # may appear elsewhere referring to the timer cadence — only the
        # bypass-log claim is the bug.)
        bypass_section = md.split("Emergency bypass", 1)
        if len(bypass_section) < 2:
            raise AssertionError("Emergency bypass section missing from docs")
        bypass_body = bypass_section[1].split("---", 1)[0]
        # Either remove the claim entirely or qualify it
        if "every 5 minutes" in bypass_body:
            assert "only when" in bypass_body or "only if" in bypass_body, (
                "bypass docs must NOT claim WARN fires every 5 minutes "
                "without the 'only when a new commit is available' qualifier"
            )

    def test_docs_mention_envfile_shadowed(self):
        """Round-2 hardening note: operator must use systemctl edit, NOT
        /root/.audit-env, because the latter is shadowed by the unit."""
        md = self._md()
        bypass_section = md.split("Emergency bypass", 1)[1].split("---", 1)[0]
        # The bypass section must mention that env-file injection is shadowed
        # / blocked, so operator uses systemctl edit
        assert "shadow" in bypass_section.lower() or "systemctl edit" in bypass_section, (
            "bypass docs must explain that EnvironmentFile is shadowed; "
            "operator must use systemctl edit"
        )


class TestRound2AutoupdateCommentSaysExit3:
    """Code-reviewer MED #8: stale comment said "(exit 0)" while code did
    `exit 3`. Misleading for operators reading the failure-mode comment.
    Round-2 fix: comment updated."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_comment_matches_exit_code(self):
        src = self._script()
        # The verification-failure block must describe exit 3, not exit 0.
        # Round-2: anchor on `verify-commit HEAD` (the bare substring without
        # `git ` prefix, since the new invocation has -c args between them).
        idx = src.find("verify-commit HEAD")
        assert idx > 0, "verify-commit invocation not found"
        # Look at the 1500 chars BEFORE the verify-commit line
        comment_block = src[max(0, idx - 1500):idx]
        # The specific misleading phrase from the original draft was
        # `(exit 0 — same pattern as a network blip)`. Round-2 fix
        # replaced it with `(exit 3 — distinct security event...)`.
        # Allow `(exit 0)` only inside an EXPLANATORY round-2-fix note,
        # not as a description of the actual behavior. Strict check: the
        # exact original misleading phrase MUST NOT appear.
        assert "(exit 0 — same pattern as a network blip)" not in comment_block, (
            "verify-commit comment must not describe behavior as `(exit 0)`. "
            "The actual code does `exit 3`."
        )
        # Must mention exit 3 (the actual behavior)
        assert "exit 3" in comment_block, (
            "verify-commit comment block must mention exit 3 (the actual code)"
        )


class TestRound2AutoupdateSetEDocumented:
    """Devils-advocate HIGH #6: jelleo-autoupdate.sh uses `set -uo pipefail`
    (no -e). Each non-zero command path IS explicitly guarded by `if !` /
    `|| true` — this is intentional design. Round-2 fix: document the
    design choice explicitly so a future refactor doesn't add `set -e`
    and break the deliberate non-zero returns."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_design_note_present(self):
        src = self._script()
        # The script must explain WHY -e is omitted
        assert "intentional" in src.lower() and "-e" in src, (
            "autoupdate.sh must explicitly document the -e omission as "
            "intentional design (round-2 hardening)"
        )


# ----------------------------------------------------------------------------
# ROUND-3 FIXES — devils-advocate ROUND-2 (3 new CRITICAL/HIGH/MED) + code-
# reviewer ROUND-2 (3 LOW). Same self-audit checklist pattern as round-2
# tests above. The first 2 tests catch the EXACT bypass class that the
# round-1 fix introduced — env-var injection redirect via /root/.audit-env
# affecting variables OTHER than the originally-shadowed one. Self-audit
# checklist (vault/feedback/patch-checklist-supply-chain.md) lists this as
# the #1 item to check before reviewer spawn.
# ----------------------------------------------------------------------------


class TestRound3AllowedSignersShadowed:
    """Devils-advocate ROUND-2 CRITICAL #1 NEW: round-1 fix shadowed
    JELLEO_ALLOW_UNSIGNED but introduced JELLEO_ALLOWED_SIGNERS which was
    NOT shadowed. Attacker writing /root/.audit-env redirects the trust root
    to a file under their control. Round-3 fix: shadow it too."""

    def test_allowed_signers_env_shadowed_in_unit(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_ALLOWED_SIGNERS=" in src, (
            "jelleo-autoupdate.service must hardcode "
            "Environment=JELLEO_ALLOWED_SIGNERS=<absolute-path> to shadow "
            "any value injected via EnvironmentFile=-/root/.audit-env. "
            "Otherwise an attacker who writes /root/.audit-env redirects "
            "the GPG trust root to their own signers file."
        )

    def test_shadow_value_is_absolute_path(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        m = re.search(r"Environment=JELLEO_ALLOWED_SIGNERS=(\S+)", src)
        assert m is not None
        assert m.group(1).startswith("/"), (
            f"Shadow value must be an absolute path; got: {m.group(1)!r}"
        )

    def test_shadow_appears_after_envfile(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_ALLOWED_SIGNERS=")
        assert envfile_idx >= 0 and shadow_idx >= 0
        assert shadow_idx > envfile_idx, (
            "Environment= shadow must come AFTER EnvironmentFile= "
            "for systemd ordering"
        )


class TestRound3DirtySentinelShadowed:
    """Devils-advocate ROUND-2 CRITICAL #2 NEW: same shadow pattern for
    JELLEO_AUTOUPDATE_DIRTY. Without shadow, attacker sets path to
    /dev/null → sentinel writes succeed silently → check returns False
    → mechanism neutralized indefinitely.

    ALSO covers ROUND-2 MEDIUM: sentinel must default to /var/lib/jelleo/
    (persistent across reboots), NOT /var/run/ (tmpfs, lost on reboot)."""

    def test_dirty_sentinel_env_shadowed_in_unit(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_AUTOUPDATE_DIRTY=" in src, (
            "jelleo-autoupdate.service must hardcode "
            "Environment=JELLEO_AUTOUPDATE_DIRTY=<absolute-path> to shadow "
            "any value injected via EnvironmentFile. Otherwise an attacker "
            "redirects the sentinel to /dev/null and silences the mechanism."
        )

    def test_dirty_sentinel_in_persistent_storage(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        m = re.search(r"Environment=JELLEO_AUTOUPDATE_DIRTY=(\S+)", src)
        assert m is not None
        path = m.group(1)
        # Must be persistent across reboots — NOT /var/run, /run, /tmp, /dev/shm
        assert not path.startswith("/var/run/"), f"sentinel in tmpfs: {path}"
        assert not path.startswith("/run/"), f"sentinel in tmpfs: {path}"
        assert not path.startswith("/tmp/"), f"sentinel in tmpfs: {path}"
        assert not path.startswith("/dev/shm/"), f"sentinel in tmpfs: {path}"
        # /var/lib is the canonical persistent state path
        assert path.startswith("/var/lib/") or path.startswith("/var/log/"), (
            f"sentinel should be in /var/lib/<service>/ for persistence; got: {path}"
        )

    def test_script_default_matches_unit_shadow(self):
        """The script's DIRTY_SENTINEL default and the unit's shadow value
        must agree, so a misconfigured operator (deleted shadow) doesn't
        get a different path than a properly-configured one."""
        script_src = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
        unit_src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        m_script = re.search(r'DIRTY_SENTINEL="\$\{JELLEO_AUTOUPDATE_DIRTY:-([^}]+)\}"', script_src)
        m_unit = re.search(r"Environment=JELLEO_AUTOUPDATE_DIRTY=(\S+)", unit_src)
        assert m_script is not None, "script must declare DIRTY_SENTINEL with default"
        assert m_unit is not None, "unit must shadow JELLEO_AUTOUPDATE_DIRTY"
        assert m_script.group(1) == m_unit.group(1), (
            f"script default {m_script.group(1)!r} must equal unit shadow "
            f"{m_unit.group(1)!r}"
        )


class TestRound3SubmoduleFailureSentinel:
    """Code-reviewer ROUND-2 LOW #2: round-1 sentinel was written only on
    `git reset --hard` failure, NOT on `git submodule update` failure.
    Round-3 fix: also write sentinel on submodule sync failure so the
    operator doesn't get silent ticks after an attacker's submodule code
    is left checked out."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_sentinel_written_on_submodule_failure(self):
        src = self._script()
        # Find the submodule-update failure handler
        idx = src.find("submodule sync after rollback non-zero")
        assert idx >= 0, "submodule-failure handler not found"
        # The next ~2000 chars should contain a sentinel write. Round-4
        # widened the handler block (added submodule-existence check + the
        # "no submodules — not writing sentinel" branch).
        handler_block = src[idx:idx + 2000]
        assert "$DIRTY_SENTINEL" in handler_block, (
            "submodule-update failure must also write to the dirty sentinel"
        )


class TestRound3SupplyChainMdRound2Notes:
    """Code-reviewer ROUND-2 LOW #3: round-1 gitconfig step is now
    redundant after the round-2 inline-pin fix. Documentation must
    explain. Also: GPG-format=ssh breaking change must be documented."""

    def _md(self) -> str:
        return (DEPLOY / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")

    def test_documents_gitconfig_redundancy(self):
        md = self._md()
        assert "redundant" in md.lower(), (
            "SUPPLY_CHAIN.md must explain the gitconfig step is now redundant "
            "(round-2 hardening pins inline at invocation time)"
        )

    def test_documents_gpg_format_breaking_change(self):
        md = self._md()
        # Must warn that GPG-signed commits will be REJECTED
        assert "REJECT" in md or "rejecting" in md.lower(), (
            "SUPPLY_CHAIN.md must warn that GPG-signed commits will be "
            "rejected by the gate (gpg.format=ssh pin)"
        )


# ----------------------------------------------------------------------------
# Anti-regression: the self-audit checklist from round-1 → round-2 →
# round-3 has a meta-pattern: any new env var introduced by a round-N fix
# becomes the next round's CRITICAL bypass if not shadowed. This test
# scans the autoupdate.sh script for ALL ${VAR:-default} reads and
# requires EACH to have a matching Environment= shadow in the unit.
# ----------------------------------------------------------------------------


class TestRound3AllEnvVarsShadowed:
    """Self-audit anti-regression: any future fix that adds a new
    ${JELLEO_FOO:-default} read in the script MUST also add a matching
    Environment=JELLEO_FOO=safe-default in the service unit. This test
    enforces that invariant across the entire script."""

    # Round-6 update: EXEMPT now EMPTY. Round-5 devils-advocate flagged
    # JELLEO_AUTOUPDATE_LOG as a write-path var = SECURITY-CRITICAL not
    # cosmetic (tee -a $LOG = SSH key injection if attacker sets
    # JELLEO_AUTOUPDATE_LOG=/root/.ssh/authorized_keys). Round-6 shadows
    # it. Round-5 pattern-class rule: write-path vars are NEVER safe to
    # exempt. Only read-path or pure cosmetic flags are exemptable.
    EXEMPT = frozenset()

    def test_exempt_list_is_exact_set(self):
        """Round-4 (devils-advocate ROUND-3 LOW #5) + ROUND-6 (devils-advocate
        ROUND-5 CRITICAL #1): EXEMPT must be checked for EXACT equality.
        Round-6 EXEMPT is empty — all env vars in the script are now
        shadowed. Future additions to EXEMPT require PR justification +
        update to vault/feedback/patch-checklist-supply-chain.md."""
        assert self.EXEMPT == frozenset(), (
            f"EXEMPT must be empty. Round-5 pattern-class: write-path vars "
            f"are SECURITY-CRITICAL not cosmetic. Got: {sorted(self.EXEMPT)}"
        )

    def test_all_jelleo_env_vars_in_script_are_shadowed_in_unit(self):
        import re
        script_src = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
        unit_src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        # Find all ${JELLEO_FOO:-...} reads (security-critical defaults)
        script_vars = set(re.findall(r"\$\{(JELLEO_[A-Z0-9_]+):-", script_src))
        security_critical = script_vars - self.EXEMPT
        # Find all shadowed env vars in the unit
        unit_vars = set(re.findall(r"Environment=(JELLEO_[A-Z0-9_]+)=", unit_src))
        missing = security_critical - unit_vars
        assert not missing, (
            f"Security-critical env vars read by the script with ${{X:-default}} "
            f"MUST have a matching Environment=X=safe-default shadow in the unit "
            f"file. Missing: {sorted(missing)}. This is the env-var-injection "
            f"bypass class — see vault/feedback/patch-checklist-supply-chain.md."
        )


# ----------------------------------------------------------------------------
# ROUND-4 FIXES — devils-advocate ROUND-3 (2 new CRITICAL + 1 HIGH + 1 MED) +
# code-reviewer ROUND-3 (1 MED + 2 LOW). The CRITICALs and HIGH below are all
# CLASSES the round-3 self-audit checklist should now learn (will be added to
# vault/feedback/patch-checklist-supply-chain.md at patch end):
#   - any git verb that touches the working tree (reset --hard included)
#     fires hooks and needs GIT_SAFE
#   - PATH-vars (env vars naming directories where code runs) are SECURITY-
#     CRITICAL not cosmetic; never exempt them
#   - sentinel writes for forensic preservation must APPEND (>>) not OVERWRITE
#   - sentinel-on-failure must check whether the failure is actually destructive
#   - comments must MATCH reality (no aspirational ExecStartPre claims)
# ----------------------------------------------------------------------------


class TestRound4GitResetUsesGitSafe:
    """Devils-advocate ROUND-3 CRITICAL #1: `git reset --hard` FIRES the
    post-checkout hook. Round-3 fix applied GIT_SAFE to fetch/pull/submodule
    but missed reset itself. An attacker-pushed malicious .git/hooks/
    post-checkout file would execute as root at rollback time. Round-4 fix:
    `git "${GIT_SAFE[@]}" reset --hard "$LOCAL"`."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_reset_hard_uses_git_safe(self):
        src = self._script()
        import re
        # Find every `git ... reset --hard` invocation
        found = False
        for line in src.splitlines():
            stripped = line.split("#", 1)[0]
            if re.search(r"\bgit\s+\S*\s*reset\s+--hard\b", stripped):
                found = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git reset --hard missing GIT_SAFE: {line!r}"
                )
        assert found, "no `git reset --hard` invocation found"


class TestRound4RepoWorkspaceShadowed:
    """Devils-advocate ROUND-3 CRITICAL #2: JELLEO_REPO and JELLEO_WORKSPACE
    were exempted as 'cosmetic' but they actually CONTROL PATHS. Attacker
    with /root/.audit-env write + signing-key compromise can redirect the
    entire install to /tmp/attacker-repo via JELLEO_REPO. Round-4 fix:
    shadow both in the unit."""

    def test_jelleo_repo_shadowed_in_unit(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_REPO=" in src, (
            "JELLEO_REPO must be shadowed in jelleo-autoupdate.service "
            "to prevent /root/.audit-env injection from redirecting the "
            "install path to an attacker-controlled repo."
        )

    def test_jelleo_repo_shadow_value_matches_script_default(self):
        unit_src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        script_src = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
        import re
        m_unit = re.search(r"Environment=JELLEO_REPO=(\S+)", unit_src)
        m_script = re.search(r'REPO="\$\{JELLEO_REPO:-([^}]+)\}"', script_src)
        assert m_unit and m_script
        assert m_unit.group(1) == m_script.group(1), (
            f"Unit shadow {m_unit.group(1)!r} must equal script default "
            f"{m_script.group(1)!r}"
        )

    def test_jelleo_workspace_shadowed_in_unit(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_WORKSPACE=" in src, (
            "JELLEO_WORKSPACE must be shadowed in jelleo-autoupdate.service "
            "(controls log/hunts directories — attacker redirect = forensic "
            "evidence redirect + control of hunt-event stream)."
        )


class TestRound4SentinelUsesAppend:
    """Devils-advocate ROUND-3 HIGH #3: sentinel writes used `>` (truncate).
    When both reset-fail AND submodule-fail occur on the same tick, the
    second write LOST the first message. Operator sees only the latest
    failure, may clear sentinel prematurely. Round-4 fix: use `>>` (append)
    so all forensic messages are preserved."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_sentinel_writes_use_append(self):
        src = self._script()
        import re
        # Find lines that REDIRECT into $DIRTY_SENTINEL (the actual writes).
        # Specifically look for the redirect pattern `> "$DIRTY_SENTINEL"` or
        # `>> "$DIRTY_SENTINEL"`. Don't false-positive on:
        #   - `2>/dev/null` (stderr redirect, unrelated)
        #   - `[[ -f "$DIRTY_SENTINEL" ]]` (file-existence test)
        #   - `mkdir -p "$(dirname "$DIRTY_SENTINEL")"` (no redirect to sentinel)
        write_pattern = re.compile(r'(>+)\s*"\$DIRTY_SENTINEL"')
        found_writes = False
        for line in src.splitlines():
            stripped = line.split("#", 1)[0]
            for m in write_pattern.finditer(stripped):
                redirect = m.group(1)
                found_writes = True
                assert redirect == ">>", (
                    f"sentinel write must use >> (append), not {redirect!r} "
                    f"(overwrite). Forensic preservation requires append so "
                    f"multiple failure messages survive same-tick collisions. "
                    f"Line: {line!r}"
                )
        assert found_writes, "no `> $DIRTY_SENTINEL` redirect lines found"


class TestRound4UnitHasExecStartPreMkdir:
    """Devils-advocate ROUND-3 MED #4: script claimed ExecStartPre handles
    /var/lib/jelleo creation but unit had no such directive (lying comment).
    Round-4 fix: actually add ExecStartPre=-/bin/mkdir -p /var/lib/jelleo
    to the unit. Comment is now truthful."""

    def test_unit_has_execstartpre_mkdir_var_lib_jelleo(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        # Match `ExecStartPre=...mkdir...var/lib/jelleo` (any variant: leading
        # `-` for tolerance, `/bin/mkdir` or `mkdir`, `-p` flag, etc.)
        assert re.search(r"ExecStartPre=.*mkdir.*var/lib/jelleo", src), (
            "jelleo-autoupdate.service must declare ExecStartPre to "
            "create /var/lib/jelleo before the script runs. Otherwise "
            "the dirty-sentinel mechanism silently fails on hardened "
            "hosts where the script's belt-and-suspenders mkdir can't "
            "create the dir."
        )


class TestRound4InstallSystemdCreatesVarLibJelleo:
    """Same finding (devils-advocate ROUND-3 MED #4): install_systemd.sh
    must create the persistent state dir during install so a fresh deploy
    has the dir + correct ownership BEFORE the first timer fire."""

    def test_install_systemd_creates_var_lib_jelleo(self):
        src = (DEPLOY / "install_systemd.sh").read_text(encoding="utf-8")
        assert "mkdir -p /var/lib/jelleo" in src, (
            "install_systemd.sh must create /var/lib/jelleo during install"
        )


class TestRound4SubmoduleFailureOnlyIfSubmodulesExist:
    """Code-reviewer ROUND-3 MED #1: round-3 fix wrote sentinel on EVERY
    submodule-update non-zero. On a repo with NO submodules (current
    audit-pipeline-cli reality), this turns benign non-zero into indefinite
    operator-intervention block. Round-4 fix: check `git submodule status`
    has output before writing sentinel."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_submodule_check_before_sentinel_write(self):
        src = self._script()
        # The submodule-failure handler must contain a `git submodule status`
        # check (or equivalent) before writing the sentinel
        idx = src.find("submodule sync after rollback non-zero")
        assert idx >= 0
        # Look at the next ~800 chars (the failure-handler block)
        block = src[idx:idx + 800]
        assert "git submodule status" in block or "submodule status" in block, (
            "submodule-failure handler must check whether submodules exist "
            "before treating the non-zero as a destructive failure"
        )


class TestRound4DirtySentinelShadowOrdering:
    """Code-reviewer ROUND-3 LOW #4: TestRound3DirtySentinelShadowed was
    missing the ordering test (Environment= must come AFTER EnvironmentFile=).
    Round-4 fix: add it."""

    def test_dirty_sentinel_shadow_after_envfile(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_AUTOUPDATE_DIRTY=")
        assert envfile_idx >= 0 and shadow_idx >= 0
        assert shadow_idx > envfile_idx, (
            "JELLEO_AUTOUPDATE_DIRTY shadow must appear AFTER EnvironmentFile= "
            "in the unit file for systemd to apply it correctly"
        )

    def test_jelleo_repo_shadow_after_envfile(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_REPO=")
        assert envfile_idx >= 0 and shadow_idx >= 0
        assert shadow_idx > envfile_idx

    def test_jelleo_workspace_shadow_after_envfile(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_WORKSPACE=")
        assert envfile_idx >= 0 and shadow_idx >= 0
        assert shadow_idx > envfile_idx


# ----------------------------------------------------------------------------
# ROUND-6 FIXES — devils-advocate ROUND-5 (2 CRITICAL + 2 HIGH + 2 MED + 2 LOW)
# + code-reviewer ROUND-5 (1 MED + 1 MED + others). New pattern classes added
# to vault/feedback/patch-checklist-supply-chain.md.
# ----------------------------------------------------------------------------


class TestRound6AutoupdateLogShadowed:
    """Devils-advocate ROUND-5 CRITICAL #1: JELLEO_AUTOUPDATE_LOG was
    exempted as 'cosmetic' but it's a WRITE PATH. Attacker writes
    /root/.audit-env with JELLEO_AUTOUPDATE_LOG=/root/.ssh/authorized_keys
    → script's log() function (tee -a $LOG) injects SSH key as root."""

    def test_log_var_shadowed_in_unit(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=JELLEO_AUTOUPDATE_LOG=" in src, (
            "JELLEO_AUTOUPDATE_LOG must be shadowed in the unit. Otherwise "
            "attacker writing /root/.audit-env redirects the log() function "
            "to overwrite /root/.ssh/authorized_keys (or any other root-"
            "writable path) via tee -a. NEW pattern-class: any env var "
            "naming a WRITE PATH is security-critical."
        )

    def test_log_shadow_value_is_canonical_path(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        m = re.search(r"Environment=JELLEO_AUTOUPDATE_LOG=(\S+)", src)
        assert m is not None
        path = m.group(1)
        # Must point inside the workspace, not at a system file
        assert path.startswith("/root/audit_runs/"), (
            f"Log shadow value must point into the workspace dir, not at "
            f"a system file. Got: {path!r}"
        )

    def test_log_shadow_after_envfile(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        envfile_idx = src.find("EnvironmentFile=")
        shadow_idx = src.find("Environment=JELLEO_AUTOUPDATE_LOG=")
        assert envfile_idx >= 0 and shadow_idx >= 0
        assert shadow_idx > envfile_idx


class TestRound6PathExplicitlySet:
    """Devils-advocate ROUND-5 CRITICAL #2: unit had NO Environment=PATH=
    line. Default systemd PATH varies by distro. If operator habits or
    /etc/environment put /root/.local/bin first (common), attacker drops
    /root/.local/bin/git that lies about verify-commit. Round-6 fix:
    pin PATH explicitly to a hardened list."""

    def test_unit_sets_path_explicitly(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        assert "Environment=PATH=" in src, (
            "jelleo-autoupdate.service must set Environment=PATH= explicitly. "
            "Without it, the service inherits systemd DefaultEnvironment "
            "which may include /root/.local/bin — attacker hijacks git "
            "binary entirely."
        )

    def test_path_excludes_user_local_bin(self):
        src = (DEPLOY / "jelleo-autoupdate.service").read_text(encoding="utf-8")
        import re
        m = re.search(r"Environment=PATH=(\S+)", src)
        assert m is not None
        path_val = m.group(1)
        assert "/root/.local/bin" not in path_val, (
            f"jelleo-autoupdate.service PATH must NOT include /root/.local/bin "
            f"(attacker drops malicious git binary there). Got: {path_val!r}"
        )
        # Hardened PATH must include the standard system dirs
        assert "/usr/bin" in path_val, (
            "PATH must include /usr/bin to find system git"
        )


class TestRound6GitReadOpsUseGitSafe:
    """Devils-advocate ROUND-5 HIGH #3+#4 + code-reviewer #1: bare git
    invocations after fetch (git log, git submodule status) can trigger
    external commands via core.pager / log.showSignature / attribute-
    driven hooks. Round-6 fix: GIT_SAFE on ALL git ops after fetch."""

    def _script(self) -> str:
        return (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")

    def test_git_log_uses_git_safe(self):
        src = self._script()
        import re
        # Find every `git log` invocation (executable, not in a comment/string)
        found = False
        for line in src.splitlines():
            stripped = line.split("#", 1)[0]
            if re.search(r"\bgit\s+\S*\s*log\b", stripped):
                # Skip log() function-call lines (the bash function)
                if "log(" in line or 'log "' in line:
                    continue
                found = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git log must use GIT_SAFE: {line!r}"
                )
        assert found, "no `git log` invocation found"

    def test_git_submodule_status_uses_git_safe(self):
        src = self._script()
        import re
        found = False
        for line in src.splitlines():
            stripped = line.split("#", 1)[0]
            if re.search(r"\bgit\s+\S*\s*submodule\s+status\b", stripped):
                found = True
                assert '"${GIT_SAFE[@]}"' in line, (
                    f"git submodule status must use GIT_SAFE: {line!r}"
                )
        assert found, "no `git submodule status` invocation found"


class TestRound6AutoUpdateLogInLogrotate:
    """Devils-advocate ROUND-5 MED #5: auto-update.log was the only tick-
    driven log path NOT covered by logrotate. Disk exhaustion attack +
    when ENOSPC, sentinel writes fail silently → gate failure."""

    def test_auto_update_log_in_logrotate(self):
        src = (DEPLOY / "logrotate-jelleo").read_text(encoding="utf-8")
        assert "auto-update.log" in src, (
            "logrotate-jelleo must include /root/audit_runs/percolator-live/"
            "auto-update.log to prevent unbounded log growth"
        )

    def test_auto_update_log_has_rotation_policy(self):
        src = (DEPLOY / "logrotate-jelleo").read_text(encoding="utf-8")
        # Find the auto-update.log stanza
        idx = src.find("auto-update.log")
        assert idx >= 0
        stanza = src[idx:idx + 500]
        # Must have rotation directives
        assert "weekly" in stanza or "daily" in stanza
        assert "rotate" in stanza
        assert "compress" in stanza


class TestRound6InstallSymlinkCheck:
    """Devils-advocate ROUND-5 MED #6: ExecStartPre=-/bin/mkdir tolerates
    existing symlink silently. Sentinel writes follow attacker symlink.
    Round-6 fix: install_systemd.sh refuses install if /var/lib/jelleo
    is a symlink."""

    def test_install_checks_for_symlink(self):
        src = (DEPLOY / "install_systemd.sh").read_text(encoding="utf-8")
        # Must have the -L check on the path
        assert "-L /var/lib/jelleo" in src, (
            "install_systemd.sh must check `[[ -L /var/lib/jelleo ]]` "
            "and refuse install if true (attacker symlink-substitution)"
        )

    def test_install_chmods_jelleo_dir(self):
        src = (DEPLOY / "install_systemd.sh").read_text(encoding="utf-8")
        # /var/lib/jelleo should be chmod 0700 (operator-only)
        assert "chmod 0700 /var/lib/jelleo" in src, (
            "install_systemd.sh must chmod 0700 /var/lib/jelleo to "
            "restrict to root-only access (sentinel contains forensic data)"
        )
