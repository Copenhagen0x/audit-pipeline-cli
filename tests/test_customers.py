"""Tier 5 #26 + #27 + #28 — customer registry + per-customer paths + derived keys.

Covers:
  - load/save registry (empty, malformed, round-trip)
  - add/remove/get_customer + duplicate / not-found errors
  - validate_customer_id (good + 4 bad shapes)
  - per-customer path helpers
  - derive_customer_seed determinism + distinct customer ids → distinct seeds
  - derive_customer_seed salt isolation (same id, different salts → different seeds)
  - derive_and_persist_customer_keypair: writes priv (mode 600) + pub, refuses overwrite
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_pipeline import customers as customers_mod

# ---------------------------------------------------------------------------
# Registry round-trip
# ---------------------------------------------------------------------------


def test_load_registry_empty(tmp_path: Path) -> None:
    assert customers_mod.load_registry(tmp_path) == []


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    payload = [{"id": "ottersec", "name": "OtterSec", "tier": "Production"}]
    customers_mod.save_registry(tmp_path, payload)
    loaded = customers_mod.load_registry(tmp_path)
    assert loaded == payload


def test_load_registry_skips_entries_without_id(tmp_path: Path) -> None:
    customers_mod.save_registry(
        tmp_path,
        [{"id": "a", "name": "A"}, {"name": "no-id"}, {"id": "b", "name": "B"}],
    )
    ids = [c["id"] for c in customers_mod.load_registry(tmp_path)]
    assert ids == ["a", "b"]


def test_load_registry_rejects_non_list(tmp_path: Path) -> None:
    p = customers_mod.registry_path(tmp_path)
    p.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(customers_mod.CustomerError, match="must contain a JSON array"):
        customers_mod.load_registry(tmp_path)


def test_load_registry_rejects_malformed_json(tmp_path: Path) -> None:
    p = customers_mod.registry_path(tmp_path)
    p.write_text("{not: valid json", encoding="utf-8")
    with pytest.raises(customers_mod.CustomerError, match="Could not parse"):
        customers_mod.load_registry(tmp_path)


# ---------------------------------------------------------------------------
# add / remove / get
# ---------------------------------------------------------------------------


def test_add_customer_persists_and_creates_dir(tmp_path: Path) -> None:
    entry = customers_mod.add_customer(
        tmp_path, "ottersec", name="OtterSec · Whirlpools", protocol_name="Orca Whirlpools",
    )
    assert entry["id"] == "ottersec"
    assert entry["target_match"] == "ottersec"  # default falls back to id
    assert customers_mod.customer_dir(tmp_path, "ottersec").is_dir()
    assert customers_mod.get_customer(tmp_path, "ottersec") is not None


def test_add_customer_rejects_duplicate(tmp_path: Path) -> None:
    customers_mod.add_customer(tmp_path, "xa", name="X", protocol_name="P")
    with pytest.raises(customers_mod.CustomerError, match="already registered"):
        customers_mod.add_customer(tmp_path, "xa", name="X again", protocol_name="P")


def test_remove_customer_returns_entry(tmp_path: Path) -> None:
    customers_mod.add_customer(tmp_path, "xa", name="X", protocol_name="P")
    removed = customers_mod.remove_customer(tmp_path, "xa")
    assert removed["id"] == "xa"
    assert customers_mod.get_customer(tmp_path, "xa") is None


def test_remove_customer_keeps_dir_on_disk(tmp_path: Path) -> None:
    customers_mod.add_customer(tmp_path, "xa", name="X", protocol_name="P")
    cdir = customers_mod.customer_dir(tmp_path, "xa")
    customers_mod.remove_customer(tmp_path, "xa")
    assert cdir.is_dir(), "remove_customer must NOT delete the per-customer dir"


def test_remove_customer_not_found(tmp_path: Path) -> None:
    with pytest.raises(customers_mod.CustomerError, match="not found"):
        customers_mod.remove_customer(tmp_path, "ghost")


# ---------------------------------------------------------------------------
# id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "good_id",
    [
        "x1", "ottersec", "a-b-c-d-e", "drift-v2", "01",
        # Cross-cutting audit Defect 06: the library's regex was previously
        # narrower than the CLI's (lowercase + hyphen only, max 32). It
        # rejected every CLI-valid ``cus_<hex>`` style ID. Unified format
        # now permits uppercase, underscore, and 16-char hex tokens that
        # the CLI generates for URL-enumeration resistance.
        "Has-Caps",
        "has_underscore",
        "cus_AbC123dEf456gH",   # mixed case + underscore + 16 chars
        "x" * 33,               # 33 chars (max is now 64)
    ],
)
def test_validate_customer_id_accepts(good_id: str) -> None:
    customers_mod.validate_customer_id(good_id)  # no raise = accepted


@pytest.mark.parametrize(
    "bad_id",
    [
        "",                # too short
        "a",               # 1 char (regex requires >=2)
        "-leading",        # starts with hyphen
        "trailing-",       # ends with hyphen
        "has space",       # spaces
        "x" * 65,          # > 64 chars
        "with/slash",      # slash
        "with.dot",        # dot
    ],
)
def test_validate_customer_id_rejects(bad_id: str) -> None:
    with pytest.raises(customers_mod.CustomerError):
        customers_mod.validate_customer_id(bad_id)


# ---------------------------------------------------------------------------
# Per-customer paths
# ---------------------------------------------------------------------------


def test_per_customer_paths_layout(tmp_path: Path) -> None:
    cid = "x"
    assert customers_mod.customer_dir(tmp_path, cid) == tmp_path / "customers" / cid
    assert customers_mod.customer_keys_dir(tmp_path, cid) == tmp_path / "customers" / cid / "keys"
    assert (
        customers_mod.customer_priv_key_path(tmp_path, cid)
        == tmp_path / "customers" / cid / "keys" / "x.ed25519"
    )
    assert (
        customers_mod.customer_pub_key_path(tmp_path, cid)
        == tmp_path / "customers" / cid / "keys" / "x.ed25519.pub"
    )


# ---------------------------------------------------------------------------
# Derived keys (Tier 5 #28)
# ---------------------------------------------------------------------------

cryptography = pytest.importorskip("cryptography")


@pytest.fixture()
def platform_priv(tmp_path: Path) -> Path:
    """Generate a platform Ed25519 key in PEM form for the test."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    p = tmp_path / "keys" / "jelleo.ed25519"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return p


def test_load_platform_priv_seed_returns_32_bytes(platform_priv: Path) -> None:
    seed = customers_mod.load_platform_priv_seed(platform_priv)
    assert len(seed) == 32


def test_load_platform_priv_seed_missing_file(tmp_path: Path) -> None:
    with pytest.raises(customers_mod.CustomerError, match="not found"):
        customers_mod.load_platform_priv_seed(tmp_path / "nope.ed25519")


def test_derive_customer_seed_is_deterministic(platform_priv: Path) -> None:
    raw = customers_mod.load_platform_priv_seed(platform_priv)
    a = customers_mod.derive_customer_seed(raw, "ottersec")
    b = customers_mod.derive_customer_seed(raw, "ottersec")
    assert a == b
    assert len(a) == 32


def test_derive_customer_seed_distinct_per_id(platform_priv: Path) -> None:
    raw = customers_mod.load_platform_priv_seed(platform_priv)
    a = customers_mod.derive_customer_seed(raw, "ottersec")
    b = customers_mod.derive_customer_seed(raw, "drift")
    assert a != b


def test_derive_customer_seed_salt_isolates(platform_priv: Path) -> None:
    raw = customers_mod.load_platform_priv_seed(platform_priv)
    a = customers_mod.derive_customer_seed(raw, "ottersec", salt=b"")
    b = customers_mod.derive_customer_seed(raw, "ottersec", salt=b"\x00" * 16)
    assert a != b


def test_derive_customer_seed_rejects_bad_seed_length() -> None:
    with pytest.raises(customers_mod.CustomerError, match="32 bytes"):
        customers_mod.derive_customer_seed(b"too-short", "x")


def test_derive_and_persist_writes_files_and_chmod(
    tmp_path: Path, platform_priv: Path,
) -> None:
    priv_path, pub_path = customers_mod.derive_and_persist_customer_keypair(
        tmp_path, "ottersec", platform_priv,
    )
    assert priv_path.exists() and pub_path.exists()
    assert priv_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub_path.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")


def test_derive_and_persist_refuses_overwrite_by_default(
    tmp_path: Path, platform_priv: Path,
) -> None:
    customers_mod.derive_and_persist_customer_keypair(tmp_path, "ottersec", platform_priv)
    with pytest.raises(customers_mod.CustomerError, match="already exists"):
        customers_mod.derive_and_persist_customer_keypair(tmp_path, "ottersec", platform_priv)


def test_derive_and_persist_overwrite_replaces_key(
    tmp_path: Path, platform_priv: Path,
) -> None:
    p1, _ = customers_mod.derive_and_persist_customer_keypair(tmp_path, "ottersec", platform_priv)
    bytes_v1 = p1.read_bytes()
    p2, _ = customers_mod.derive_and_persist_customer_keypair(
        tmp_path, "ottersec", platform_priv, salt=b"\x01" * 16, overwrite=True,
    )
    assert p2 == p1
    assert p2.read_bytes() != bytes_v1, "rotate-key with new salt must produce a new key"
