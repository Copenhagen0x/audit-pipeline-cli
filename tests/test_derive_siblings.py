"""Tier 2 #8 — derive-siblings command tests.

Pure-function tests for the logic that doesn't need network access:
  - _strip_md_fence: handle ``` and ```yaml fences (and bare text)
  - _infer_protocol_class: best-effort target -> class mapping
  - _append_siblings: idempotent merge into existing libraries
  - derive_siblings_async: never raises (network-down case)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from audit_pipeline.commands.derive_siblings import (
    _append_siblings,
    _infer_protocol_class,
    _strip_md_fence,
    derive_siblings_async,
)
from audit_pipeline.db import FindingsDB
from audit_pipeline.lifecycle import Status
from audit_pipeline.severity import Severity

# ─────────────────────────── _strip_md_fence ───────────────────────────────


def test_strip_md_fence_yaml_marker() -> None:
    raw = "```yaml\nhypotheses:\n  - id: TEST1-foo\n```"
    assert _strip_md_fence(raw) == "hypotheses:\n  - id: TEST1-foo"


def test_strip_md_fence_yml_marker() -> None:
    raw = "```yml\nhypotheses: []\n```"
    assert _strip_md_fence(raw) == "hypotheses: []"


def test_strip_md_fence_bare_marker() -> None:
    raw = "```\nhypotheses: []\n```"
    assert _strip_md_fence(raw) == "hypotheses: []"


def test_strip_md_fence_no_marker_passes_through() -> None:
    raw = "hypotheses:\n  - id: TEST1-foo"
    assert _strip_md_fence(raw) == "hypotheses:\n  - id: TEST1-foo"


def test_strip_md_fence_strips_leading_trailing_whitespace() -> None:
    raw = "  \n  ```yaml\nhypotheses: []\n```\n  "
    assert _strip_md_fence(raw) == "hypotheses: []"


# ─────────────────────────── _infer_protocol_class ─────────────────────────


def test_infer_protocol_class_percolator(tmp_path: Path) -> None:
    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="percolator-live",
                                 github_url="https://github.com/x/p")
    db.upsert_finding(
        target_id=target_id, cycle_id="c-1", hypothesis_id="H1-test",
        verdict="TRUE", confidence=None, severity=Severity.HIGH, status=Status.NEW,
        engine_sha="abc", wrapper_sha=None, debate_promoted=False,
        poc_fired=False, title="t", poc_path=None,
    )
    finding = db.list_findings()[0]
    assert _infer_protocol_class(db, finding) == "perp_dex"


def test_infer_protocol_class_unknown_target(tmp_path: Path) -> None:
    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="some-random-thing",
                                 github_url="https://github.com/x/y")
    db.upsert_finding(
        target_id=target_id, cycle_id="c-1", hypothesis_id="H1-test",
        verdict="TRUE", confidence=None, severity=Severity.HIGH, status=Status.NEW,
        engine_sha="abc", wrapper_sha=None, debate_promoted=False,
        poc_fired=False, title="t", poc_path=None,
    )
    finding = db.list_findings()[0]
    assert _infer_protocol_class(db, finding) is None


def test_infer_protocol_class_orca_whirlpools(tmp_path: Path) -> None:
    db = FindingsDB(tmp_path / "findings.db")
    target_id = db.upsert_target(name="orca-whirlpools",
                                 github_url="https://github.com/orca-so/whirlpools")
    db.upsert_finding(
        target_id=target_id, cycle_id="c-1", hypothesis_id="H1-test",
        verdict="TRUE", confidence=None, severity=Severity.HIGH, status=Status.NEW,
        engine_sha="abc", wrapper_sha=None, debate_promoted=False,
        poc_fired=False, title="t", poc_path=None,
    )
    finding = db.list_findings()[0]
    assert _infer_protocol_class(db, finding) == "clmm"


# ─────────────────────────── _append_siblings ──────────────────────────────


def test_append_siblings_to_new_file(tmp_path: Path) -> None:
    target = tmp_path / "lib.yaml"
    siblings = [
        {"id": "SIB1-a", "class": "invariant_property", "severity": "High",
         "claim": "x" * 30},
        {"id": "SIB2-b", "class": "state_transition", "severity": "Medium",
         "claim": "y" * 30},
    ]
    n = _append_siblings(target, siblings)
    assert n == 2
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert len(loaded["hypotheses"]) == 2


def test_append_siblings_dedupes_by_id(tmp_path: Path) -> None:
    target = tmp_path / "lib.yaml"
    target.write_text(
        "hypotheses:\n"
        "  - id: SIB1-a\n"
        "    class: invariant_property\n"
        "    severity: High\n"
        "    claim: pre-existing entry, must not be duplicated\n",
        encoding="utf-8",
    )
    incoming = [
        {"id": "SIB1-a", "class": "invariant_property", "severity": "High",
         "claim": "z" * 30},
        {"id": "SIB2-b-new", "class": "invariant_property", "severity": "High",
         "claim": "y" * 30},
    ]
    n = _append_siblings(target, incoming)
    assert n == 1
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    ids = sorted(h["id"] for h in loaded["hypotheses"])
    assert ids == ["SIB1-a", "SIB2-b-new"]


def test_append_siblings_skips_non_dict_entries(tmp_path: Path) -> None:
    target = tmp_path / "lib.yaml"
    incoming = [
        {"id": "SIB1-a", "class": "invariant_property", "severity": "High",
         "claim": "x" * 30},
        "not a dict",
        42,
        None,
    ]
    n = _append_siblings(target, incoming)
    assert n == 1


# ─────────────────────────── derive_siblings_async ─────────────────────────


def test_derive_siblings_async_silences_missing_db(tmp_path: Path) -> None:
    """Hook must never raise when DB is missing or finding doesn't exist."""
    # No findings.db at all in tmp_path
    derive_siblings_async(tmp_path, finding_id=999)


def test_derive_siblings_async_silences_missing_finding(tmp_path: Path) -> None:
    """Hook must never raise when finding_id doesn't exist in the DB."""
    FindingsDB(tmp_path / "findings.db")
    derive_siblings_async(tmp_path, finding_id=99999)
