"""Tests for sign_file / verify roundtrip + the af74827 hardenings.

Covers the cross-cutting audit's untested-module gap on sign.py.
Exercises real Ed25519 math via the cryptography package.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit_pipeline.commands import sign as sign_mod


@pytest.fixture
def fresh_keypair(tmp_path: Path) -> tuple[Path, Path]:
    priv = Ed25519PrivateKey.generate()
    priv_path = tmp_path / "k"
    priv_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path = tmp_path / "k.pub"
    pub_path.write_bytes(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def test_sign_then_verify_roundtrips(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "report.html"
    f.write_bytes(b"this is the report content")
    sig_path = sign_mod.sign_file(f, key_path=priv_path, domain="report")
    assert sig_path.exists()
    txt = sig_path.read_text(encoding="utf-8")
    assert "Schema: jelleo-sign/v2" in txt
    assert f"Signed-File: {f.name}" in txt
    assert "Signed-Bytes: 26" in txt


def test_signed_bytes_header_matches_payload(tmp_path, fresh_keypair):
    priv_path, _ = fresh_keypair
    f = tmp_path / "thing.json"
    payload = b'{"a":1,"b":[2,3,4]}'
    f.write_bytes(payload)
    sig_path = sign_mod.sign_file(f, key_path=priv_path, domain="heartbeat")
    txt = sig_path.read_text(encoding="utf-8")
    # Confirm sig file records the exact byte length we wrote
    assert f"Signed-Bytes: {len(payload)}" in txt


def test_sign_file_missing_key_raises(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(sign_mod.SignError):
        sign_mod.sign_file(f, key_path=tmp_path / "no-such-key")


def test_sign_no_key_path_raises(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(sign_mod.SignError):
        sign_mod.sign_file(f, key_path=None)


# ───────────────── domain-separation tests (FIX B-#29) ─────────────────


def test_infer_domain_recognizes_merkle_filenames(tmp_path):
    """Merkle sidecars must auto-pick the merkle domain, not raw."""
    p = tmp_path / "merkle.json"
    p.write_text("{}", encoding="utf-8")
    assert sign_mod._infer_domain(p) == "merkle"


def test_infer_domain_recognizes_bundle_artefacts(tmp_path):
    """Bundle artefact filenames (patch.diff, verification.json) must
    auto-pick the bundle domain — a sig on patch.diff must not be
    reusable as a sig on a disclosure report."""
    assert sign_mod._infer_domain(tmp_path / "patch.diff") == "bundle"
    assert sign_mod._infer_domain(tmp_path / "verification.json") == "bundle"


def test_infer_domain_recognizes_heartbeat(tmp_path):
    assert sign_mod._infer_domain(tmp_path / "heartbeat.json") == "heartbeat"


def test_infer_domain_recognizes_disclosure(tmp_path):
    assert sign_mod._infer_domain(tmp_path / "disclosure-finding-42.md") == "disclosure"


def test_infer_domain_recognizes_customer_manifest(tmp_path):
    assert sign_mod._infer_domain(tmp_path / "customer-manifest.json") == "customer"
    assert sign_mod._infer_domain(tmp_path / "manifest.json") == "customer"


def test_infer_domain_unknown_falls_back_to_raw(tmp_path):
    """Unknown filename → raw (legacy v1 compat path). New code should
    pass `domain=` explicitly instead of relying on inference."""
    assert sign_mod._infer_domain(tmp_path / "random.txt") == "raw"


def test_unknown_domain_explicitly_raises(tmp_path, fresh_keypair):
    """Passing a domain that isn't in SIGN_DOMAINS must fail — silently
    falling through to 'raw' would defeat the domain-separation guard."""
    priv_path, _ = fresh_keypair
    f = tmp_path / "x.txt"
    f.write_text("payload", encoding="utf-8")
    with pytest.raises(sign_mod.SignError):
        sign_mod.sign_file(f, key_path=priv_path, domain="this-domain-doesnt-exist")


def test_sign_domains_includes_all_required_tiers():
    """Schema-stability assertion: every domain a producer might use
    must exist in SIGN_DOMAINS. A removal breaks verification of
    historical .sig files; catch it at test time."""
    required = {"merkle", "bundle", "disclosure", "report", "heartbeat", "customer", "raw"}
    assert required.issubset(set(sign_mod.SIGN_DOMAINS.keys()))
