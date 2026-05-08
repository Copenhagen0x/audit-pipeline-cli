"""Tier 2 #10 — PoC test cache (DB layer) tests.

Covers:
  - put / get round-trip
  - get hit returns the cached row
  - n_hits increments across consecutive gets (analytics-friendly)
  - get with wrong key returns None
  - list returns recent entries first
  - flush by hyp / by sha / by both / by all
  - schema migrations are idempotent (table already exists)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline.db import FindingsDB


@pytest.fixture()
def db(tmp_path: Path) -> FindingsDB:
    return FindingsDB(tmp_path / "findings.db")


def test_put_then_get_round_trip(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired", cargo_rc=1, elapsed_s=12.5)
    row = db.get_poc_cache("sha1", "H1-foo", "h1")
    assert row is not None
    assert row["outcome"] == "fired"
    assert row["cargo_rc"] == 1
    assert row["elapsed_s"] == 12.5


def test_get_with_wrong_key_returns_none(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    assert db.get_poc_cache("wrong-sha", "H1-foo", "h1") is None
    assert db.get_poc_cache("sha1", "WRONG-hyp", "h1") is None
    assert db.get_poc_cache("sha1", "H1-foo", "wrong-hash") is None


def test_n_hits_increments_across_gets(db: FindingsDB) -> None:
    """Each cache hit bumps n_hits so we can spot hot entries."""
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    first = db.get_poc_cache("sha1", "H1-foo", "h1")
    second = db.get_poc_cache("sha1", "H1-foo", "h1")
    third = db.get_poc_cache("sha1", "H1-foo", "h1")
    assert first["n_hits"] == 1   # pre-bump (the SELECT happens before the UPDATE)
    assert second["n_hits"] == 2
    assert third["n_hits"] == 3


def test_put_is_idempotent_upsert(db: FindingsDB) -> None:
    """Re-putting the same key updates the outcome but keeps n_hits column."""
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired", cargo_rc=1)
    db.put_poc_cache("sha1", "H1-foo", "h1", "safety_attestation", cargo_rc=0)
    row = db.get_poc_cache("sha1", "H1-foo", "h1")
    assert row["outcome"] == "safety_attestation"
    assert row["cargo_rc"] == 0


def test_list_poc_cache_returns_all_entries(db: FindingsDB) -> None:
    """list returns every cached entry up to the limit. Ordering between
    same-second writes is undefined (timestamp precision is 1s), so we
    assert membership, not order."""
    db.put_poc_cache("sha1", "H1-a", "h-a", "fired")
    db.put_poc_cache("sha1", "H2-b", "h-b", "fired")
    rows = db.list_poc_cache(limit=10)
    assert len(rows) == 2
    ids = {r["hypothesis_id"] for r in rows}
    assert ids == {"H1-a", "H2-b"}


def test_flush_by_hyp(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    db.put_poc_cache("sha1", "H2-bar", "h2", "fired")
    deleted = db.flush_poc_cache(hypothesis_id="H1-foo")
    assert deleted == 1
    assert db.get_poc_cache("sha1", "H1-foo", "h1") is None
    assert db.get_poc_cache("sha1", "H2-bar", "h2") is not None


def test_flush_by_sha(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    db.put_poc_cache("sha2", "H1-foo", "h1", "fired")
    deleted = db.flush_poc_cache(engine_sha="sha1")
    assert deleted == 1
    assert db.get_poc_cache("sha1", "H1-foo", "h1") is None
    assert db.get_poc_cache("sha2", "H1-foo", "h1") is not None


def test_flush_by_sha_and_hyp_combined(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    db.put_poc_cache("sha1", "H2-bar", "h2", "fired")
    db.put_poc_cache("sha2", "H1-foo", "h3", "fired")
    deleted = db.flush_poc_cache(engine_sha="sha1", hypothesis_id="H1-foo")
    assert deleted == 1
    # sha1+H2 and sha2+H1 should remain
    assert db.get_poc_cache("sha1", "H2-bar", "h2") is not None
    assert db.get_poc_cache("sha2", "H1-foo", "h3") is not None


def test_flush_all_with_no_args_clears_table(db: FindingsDB) -> None:
    db.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    db.put_poc_cache("sha1", "H2-bar", "h2", "fired")
    deleted = db.flush_poc_cache()
    assert deleted == 2
    assert db.list_poc_cache() == []


def test_schema_init_idempotent(tmp_path: Path) -> None:
    """Initializing the DB twice on the same file does not raise."""
    path = tmp_path / "findings.db"
    db1 = FindingsDB(path)
    db1.put_poc_cache("sha1", "H1-foo", "h1", "fired")
    # Re-open: should pick up existing data, not drop tables.
    db2 = FindingsDB(path)
    assert db2.get_poc_cache("sha1", "H1-foo", "h1") is not None
