"""WS6 continuous diff-scan — regression coverage for the 3-gate hardening.

Pins the security-critical fail-closed behavior of the source-mode compare-API
scoping and the local-clone rename coverage. Every one of these guards the same
invariant: a genuinely-changed file must NEVER be silently dropped from scope.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import requests
from click.testing import CliRunner

import audit_pipeline.commands.watch as _watch
import audit_pipeline.utils.github as gh
from audit_pipeline.scoping import changed_files_between

_SHA_A = "a" * 40
_SHA_B = "b" * 40


class _FakeResp:
    def __init__(self, body, *, raise_exc=None, json_exc=None):
        self._body = body
        self._raise = raise_exc
        self._json_exc = json_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._body


def _patch_get(monkeypatch, resp=None, *, exc=None, urls=None):
    def fake_get(url, **_kw):
        if urls is not None:
            urls.append(url)
        if exc is not None:
            raise exc
        return resp
    monkeypatch.setattr(gh.requests, "get", fake_get)


def _ahead(files):
    return {
        "status": "ahead",
        "base_commit": {"sha": _SHA_A},
        "merge_base_commit": {"sha": _SHA_A},
        "files": files,
    }


# ---- the CRITICAL: only a clean fast-forward is trusted -----------------

def test_ahead_returns_changed_filenames(monkeypatch):
    _patch_get(monkeypatch, _FakeResp(_ahead([{"filename": "src/pool.rs"}])))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == {"src/pool.rs"}


def test_diverged_falls_back_to_full_library(monkeypatch):
    # Rebased/force-pushed ref: compare's merge-base diff is INCOMPLETE. Must
    # return empty (=> full library) NOT the partial `files` list.
    body = {"status": "diverged", "files": [{"filename": "README.md"}]}
    _patch_get(monkeypatch, _FakeResp(body))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_behind_falls_back_to_full_library(monkeypatch):
    body = {"status": "behind", "files": [{"filename": "README.md"}]}
    _patch_get(monkeypatch, _FakeResp(body))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_ahead_but_mergebase_not_base_falls_back(monkeypatch):
    # Defense-in-depth: even if status says "ahead", a merge-base != base means
    # the list can't be trusted as a two-dot diff.
    body = {
        "status": "ahead",
        "base_commit": {"sha": _SHA_A},
        "merge_base_commit": {"sha": _SHA_B},
        "files": [{"filename": "x.rs"}],
    }
    _patch_get(monkeypatch, _FakeResp(body))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


# ---- rename coverage: old path must still count as changed ---------------

def test_rename_unions_previous_filename(monkeypatch):
    files = [{
        "filename": "src/pool_v2.rs",
        "status": "renamed",
        "previous_filename": "src/pool.rs",
    }]
    _patch_get(monkeypatch, _FakeResp(_ahead(files)))
    got = gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B)
    assert got == {"src/pool_v2.rs", "src/pool.rs"}


# ---- fail-closed on every ambiguous response ----------------------------

def test_non_dict_body_fails_closed(monkeypatch):
    _patch_get(monkeypatch, _FakeResp([]))  # 200 with a JSON array, not object
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_truncated_over_cap_fails_closed(monkeypatch):
    files = [{"filename": f"f{i}.rs"} for i in range(gh._COMPARE_FILES_CAP)]
    _patch_get(monkeypatch, _FakeResp(_ahead(files)))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_http_error_fails_closed(monkeypatch):
    _patch_get(monkeypatch, _FakeResp({}, raise_exc=requests.HTTPError("404")))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_network_error_fails_closed(monkeypatch):
    _patch_get(monkeypatch, exc=requests.ConnectionError("down"))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_malformed_json_fails_closed(monkeypatch):
    _patch_get(monkeypatch, _FakeResp(None, json_exc=ValueError("bad json")))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_abbreviated_base_sha_rejected(monkeypatch):
    # exact-40 only: an abbreviated SHA is brute-forceable into an ambiguous
    # compare ref (freshness_gate's CRITICAL 0efd25c3 class) — must be rejected.
    urls: list[str] = []
    _patch_get(monkeypatch, _FakeResp(_ahead([{"filename": "x.rs"}])), urls=urls)
    assert gh.changed_files_via_compare("o", "r", "abc1234", _SHA_B) == set()  # 7-char
    assert gh.changed_files_via_compare("o", "r", "a" * 39, _SHA_B) == set()   # 39-char
    assert urls == []


def test_missing_mergebase_fields_fails_closed(monkeypatch):
    # status=="ahead" but no base_commit/merge_base_commit keys -> must NOT be
    # trusted (goober round-2 truthiness bypass).
    body = {"status": "ahead", "files": [{"filename": "src/critical_math.rs"}]}
    _patch_get(monkeypatch, _FakeResp(body))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, _SHA_B) == set()


def test_invalid_head_ref_fails_closed(monkeypatch):
    urls: list[str] = []
    _patch_get(monkeypatch, _FakeResp(_ahead([{"filename": "x.rs"}])), urls=urls)
    assert gh.changed_files_via_compare("o", "r", _SHA_A, "main; rm -rf") == set()
    assert gh.changed_files_via_compare("o", "r", _SHA_A, "abc1234") == set()
    assert urls == []


def test_bad_owner_repo_rejected(monkeypatch):
    urls: list[str] = []
    _patch_get(monkeypatch, _FakeResp(_ahead([{"filename": "x.rs"}])), urls=urls)
    assert gh.changed_files_via_compare("o/../x", "r", _SHA_A, _SHA_B) == set()
    assert gh.changed_files_via_compare("..", "r", _SHA_A, _SHA_B) == set()   # dot-only
    assert gh.changed_files_via_compare("o", ".", _SHA_A, _SHA_B) == set()
    assert urls == []


def test_head_ref_literal_HEAD_allowed(monkeypatch):
    _patch_get(monkeypatch, _FakeResp(_ahead([{"filename": "x.rs"}])))
    assert gh.changed_files_via_compare("o", "r", _SHA_A, "HEAD") == {"x.rs"}


def test_invalid_base_sha_never_hits_network(monkeypatch):
    urls: list[str] = []
    _patch_get(monkeypatch, _FakeResp(_ahead([])), urls=urls)
    assert gh.changed_files_via_compare("o", "r", "HEAD", _SHA_B) == set()
    assert gh.changed_files_via_compare("o", "r", "../etc", _SHA_B) == set()
    assert urls == []  # bad SHA rejected before the request is built


# ---- local-clone rename coverage via --no-renames -----------------------

def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def test_changed_files_between_reports_both_rename_paths(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "old.rs").write_text("fn a() {}\n" * 20)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # rename + modify in one commit
    (repo / "old.rs").unlink()
    (repo / "new.rs").write_text("fn a() {}\n" * 20 + "fn b() {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename+modify")
    changed = changed_files_between(repo, base, "HEAD")
    # --no-renames => rename surfaces as delete(old)+add(new): BOTH paths, so a
    # hyp pinned to the pre-rename path still counts the file as changed.
    assert "old.rs" in changed
    assert "new.rs" in changed


# ---- watch orchestration: opt-in, first-run-full, success-gated baseline ----

_ENGINE_PIN = "e" * 40
_NEW_ENGINE = "f" * 40
_WRAP_SHA = "d" * 40


_NEW_WRAP = "b" * 40


def _run_watch_once(tmp_path, monkeypatch, *, on_update, extra_args=(),
                    prior_state=None, returncode=0, changed="engine"):
    (tmp_path / "workspace.json").write_text(json.dumps({
        "engine": {"repo": "https://github.com/o/engine", "sha": _ENGINE_PIN, "local": "engine"},
        "wrapper": {"repo": "https://github.com/o/wrapper", "sha": _WRAP_SHA, "local": "wrapper"},
    }))
    wdir = tmp_path / "watch"
    wdir.mkdir(exist_ok=True)
    if prior_state is not None:
        (wdir / "state.json").write_text(json.dumps(prior_state))

    # Only `changed` gets a NEW sha (fires on_update); the other stays at its
    # config pin (unchanged, no fire).
    _new = {"engine": _NEW_ENGINE, "wrapper": _NEW_WRAP}
    _pin = {"engine": _ENGINE_PIN, "wrapper": _WRAP_SHA}

    def fake_latest(owner, repo, ref="HEAD", timeout=30):
        sha = _new[repo] if repo == changed else _pin[repo]
        return {"sha": sha, "commit": {"message": "m", "author": {"date": "", "name": "x"}},
                "html_url": ""}
    monkeypatch.setattr(_watch, "get_latest_commit", fake_latest)

    captured = {}

    class _CP:
        pass

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        cp = _CP()
        cp.returncode = returncode
        return cp
    monkeypatch.setattr(_watch.subprocess, "run", fake_run)

    res = CliRunner().invoke(
        _watch.watch_cmd,
        ["--once", "--on-update", on_update, *extra_args],
        obj={"workspace": str(tmp_path)},
    )
    assert res.exit_code == 0, res.output
    state = json.loads((wdir / "state.json").read_text())
    return captured.get("argv", []), state


def test_watch_default_off_never_scopes_or_writes_baseline(tmp_path, monkeypatch):
    argv, state = _run_watch_once(tmp_path, monkeypatch, on_update="hunt {sha}")
    assert "--diff-since-sha" not in argv                       # off => no scoping
    assert "last_scoped_sha" not in state["engine"]            # off => no baseline tracked
    assert state["engine"]["last_seen_sha"] == _NEW_ENGINE


def test_watch_diffscope_first_run_full_scans_then_sets_baseline(tmp_path, monkeypatch):
    argv, state = _run_watch_once(tmp_path, monkeypatch, on_update="hunt {sha}",
                                  extra_args=["--diff-scope"], returncode=0)
    assert "--diff-since-sha" not in argv                       # first run => full library
    assert state["engine"]["last_scoped_sha"] == _NEW_ENGINE   # baseline set on success


def test_watch_diffscope_uses_prior_baseline_and_advances(tmp_path, monkeypatch):
    prior = {"engine": {"last_seen_sha": _ENGINE_PIN, "last_scoped_sha": "c" * 40}}
    argv, state = _run_watch_once(tmp_path, monkeypatch, on_update="hunt {sha}",
                                  extra_args=["--diff-scope"], prior_state=prior, returncode=0)
    assert "--diff-since-sha" in argv
    assert argv[argv.index("--diff-since-sha") + 1] == "c" * 40  # scoped from prior baseline
    assert state["engine"]["last_scoped_sha"] == _NEW_ENGINE     # advanced on success


def test_watch_failed_cycle_does_not_advance_baseline(tmp_path, monkeypatch):
    prior = {"engine": {"last_seen_sha": _ENGINE_PIN, "last_scoped_sha": "c" * 40}}
    _argv, state = _run_watch_once(tmp_path, monkeypatch, on_update="hunt {sha}",
                                   extra_args=["--diff-scope"], prior_state=prior, returncode=1)
    assert state["engine"]["last_scoped_sha"] == "c" * 40        # NOT advanced on failure
    assert state["engine"]["last_seen_sha"] == _NEW_ENGINE       # but seen advances


def test_watch_wrapper_local_mode_not_scoped_and_no_baseline(tmp_path, monkeypatch):
    # local (non-source) mode: hunt only diffs the ENGINE clone, so a wrapper
    # commit must NOT be auto-scoped AND must NOT get a last_scoped_sha baseline
    # (goober round-3: persist was gated on diff_scope+audit_ok but not can_scope).
    argv, state = _run_watch_once(tmp_path, monkeypatch, on_update="hunt {sha}",
                                  extra_args=["--diff-scope"], returncode=0,
                                  changed="wrapper")
    assert "--diff-since-sha" not in argv                            # wrapper not scopeable locally
    assert "last_scoped_sha" not in state.get("wrapper", {})        # no phantom baseline


def test_watch_prevsha_template_no_longer_supported(tmp_path, monkeypatch):
    # {prev_sha} exposure was removed (round-4 goober High: it let a hand-written
    # template bypass the first-run-full guard). A template referencing it now
    # fails SAFELY — format() raises, is caught, and no on_update subprocess runs.
    argv, state = _run_watch_once(tmp_path, monkeypatch,
                                  on_update="hunt --diff-since-sha {prev_sha}",
                                  extra_args=["--diff-scope"])
    assert argv == []                                              # subprocess never invoked
    assert state["engine"]["last_seen_sha"] == _NEW_ENGINE         # commit still marked seen
    assert "last_scoped_sha" not in state["engine"]               # no baseline established


def test_watch_operator_diffsince_respected_and_baseline_untouched(tmp_path, monkeypatch):
    # A hand-supplied --diff-since-sha is respected (not duplicated by auto-append)
    # and does NOT advance our managed baseline (operator override, guards bypassed).
    hard = "9" * 40
    argv, state = _run_watch_once(
        tmp_path, monkeypatch,
        on_update=f"hunt --diff-since-sha {hard}",
        extra_args=["--diff-scope"], returncode=0,
    )
    assert argv.count("--diff-since-sha") == 1                     # not duplicated
    assert hard in argv                                           # operator value kept
    assert "last_scoped_sha" not in state["engine"]               # baseline untouched


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
