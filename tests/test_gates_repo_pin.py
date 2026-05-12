"""Tests for ``audit_pipeline.gates.repo_pin`` (Gate 6).

Exercises both the SHA-extraction helper and the full ``check_repo_pin``
logic against mocked ``subprocess.run`` invocations. We do NOT hit the real
GitHub API in tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from audit_pipeline.gates.repo_pin import (
    check_repo_pin,
    extract_candidate_shas,
)


# ---------- extract_candidate_shas ---------------------------------------

class TestExtractCandidateShas:
    def test_finds_sha_after_at_anchor(self):
        body = "filed against percolator-prog @ 6cd742f25a9bcebeb9ad"
        shas = extract_candidate_shas(body)
        assert "6cd742f25a9bcebeb9ad" in shas

    def test_finds_sha_after_commit_word(self):
        body = "the commit 397be0d landed before the cycle"
        assert "397be0d" in extract_candidate_shas(body)

    def test_finds_sha_in_code_fence(self):
        body = "Engine SHA: `ba667e8c68b4dbc4ebf47740bccf59a9aa1ec6a8`"
        assert "ba667e8c68b4dbc4ebf47740bccf59a9aa1ec6a8" in extract_candidate_shas(body)

    def test_ignores_merkle_root_without_git_context(self):
        # Long hex string standalone (e.g. Merkle root) should NOT be treated
        # as a SHA, because git-context hints aren't nearby.
        body = "Cycle Merkle root: 141ffeb01d373ab2d1d8e0a24f244664eb40abda424d67ef1b66fa769ae20b1f"
        # "merkle root" is not in our hint list, and we don't want to look up
        # a 64-char hex as a commit. But the regex caps at 40 chars so this
        # would match characters 1-40 of the merkle root. The context-hint
        # check ("merkle" not in hints) excludes it. Confirm:
        assert extract_candidate_shas(body) == []

    def test_ignores_long_hex_without_context(self):
        body = "Signature: deadbeefcafebabe deadbeefcafebabe deadbeefcafebabe"
        assert extract_candidate_shas(body) == []

    def test_deduplicates_repeated_shas(self):
        body = "Engine SHA 6cd742f25a. Wrapper SHA 6cd742f25a (re-cited)."
        shas = extract_candidate_shas(body)
        assert shas.count("6cd742f25a") == 1

    def test_lowercases_shas(self):
        body = "commit 6CD742F25A"
        assert "6cd742f25a" in extract_candidate_shas(body)

    def test_word_boundary_context_hints(self):
        """Phase B self-audit Defect 08: substring matching on hints used
        to false-positive on words like ``reference``, ``preferred``,
        ``refactor`` (which contain ``ref``) and ``shabby``/``shamash``
        (which contain ``sha``)."""
        # "reference" should NOT be enough context to mark hex as a SHA
        body = "see Polymarket reference deadbeefcafe123456 below"
        assert extract_candidate_shas(body) == []

    def test_word_boundary_does_not_match_substring(self):
        body = "the preferred deadbeefcafe123456 build path"
        # "preferred" contains "ref" but with word boundaries, no match
        assert extract_candidate_shas(body) == []

    def test_email_at_does_not_trigger(self):
        # Bare `@` in an email address should NOT be a SHA-context hint
        body = "Author: kirill@jelleo.com deadbeefcafe123456 in the body"
        # The @ is not at end-of-string nor preceded by whitespace alone,
        # so context check rejects it.
        assert extract_candidate_shas(body) == []


# ---------- check_repo_pin -----------------------------------------------

def _mock_gh_response(returncode: int, stderr: str = ""):
    """Build a mock subprocess.CompletedProcess."""
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = ""
    return m


class TestCheckRepoPin:
    def test_empty_body_passes_no_op(self):
        result = check_repo_pin(body="", target_repo="owner/repo")
        assert result.passed is True
        assert "no git-context SHAs" in result.reason

    def test_body_with_no_shas_passes(self):
        body = "Findings: residual conservation broken. No commits cited."
        result = check_repo_pin(body=body, target_repo="owner/repo")
        assert result.passed is True

    def test_correct_pinned_sha_passes(self):
        body = "filed against owner/repo @ aabbccddeeff112233"
        with patch("subprocess.run", return_value=_mock_gh_response(0)):
            result = check_repo_pin(body=body, target_repo="owner/repo")
        assert result.passed is True
        assert "aabbccddeeff112233" in result.details["in_repo"]

    def test_wrong_repo_sha_fails(self):
        """The exact failure mode from issue #92: engine SHA in wrapper issue."""
        body = "filed against aeyakovenko/percolator-prog @ 6cd742f25a"
        with patch("subprocess.run", return_value=_mock_gh_response(1, stderr="HTTP 404: Not Found")):
            result = check_repo_pin(
                body=body, target_repo="aeyakovenko/percolator-prog",
            )
        assert result.passed is False
        assert "6cd742f25a" in result.details["not_in_repo"]
        assert "do NOT resolve" in result.reason
        assert "wrong-repo header" in result.reason

    def test_indeterminate_when_gh_missing(self):
        body = "filed against owner/repo @ aabbccddeeff112233"
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = check_repo_pin(body=body, target_repo="owner/repo")
        assert result.passed is None
        assert "gh missing" in result.reason or "could not verify" in result.reason

    def test_indeterminate_on_api_timeout(self):
        body = "filed against owner/repo @ aabbccddeeff112233"
        import subprocess as _sp
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="gh", timeout=10)):
            result = check_repo_pin(body=body, target_repo="owner/repo")
        assert result.passed is None

    def test_mixed_some_pass_some_fail(self):
        body = (
            "Engine SHA: aabbccddeeff112233. "
            "Wrapper SHA: 99887766554433221100."
        )
        def fake_run(*args, **kwargs):
            sha = args[0][-2]  # gh api repos/<repo>/commits/<sha> --silent
            if "aabbccddeeff112233" in sha:
                return _mock_gh_response(0)
            return _mock_gh_response(1, stderr="HTTP 404: Not Found")
        with patch("subprocess.run", side_effect=fake_run):
            result = check_repo_pin(body=body, target_repo="owner/repo")
        assert result.passed is False
        assert "99887766554433221100" in result.details["not_in_repo"]
        assert "aabbccddeeff112233" in result.details["in_repo"]

    def test_auth_error_skips_not_fails(self):
        """Auth/rate-limit errors should not be hard-failures (could be transient)."""
        body = "filed against owner/repo @ aabbccddeeff112233"
        with patch("subprocess.run", return_value=_mock_gh_response(1, stderr="GraphQL: rate limit exceeded")):
            result = check_repo_pin(body=body, target_repo="owner/repo")
        # rate-limit / auth → indeterminate, not failed
        assert result.passed is None
        assert "aabbccddeeff112233" in result.details["indeterminate"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
