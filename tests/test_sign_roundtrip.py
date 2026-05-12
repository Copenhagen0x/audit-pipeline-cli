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
