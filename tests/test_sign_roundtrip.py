"""Tests for sign_file / verify roundtrip + the af74827 hardenings.

Covers the cross-cutting audit's untested-module gap on sign.py.
Exercises real Ed25519 math via the cryptography package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")

# Imports after importorskip are intentional — the module-level skip
# protects us from ImportError on machines without `cryptography`.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from audit_pipeline.commands import sign as sign_mod  # noqa: E402


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


@pytest.fixture(autouse=True)
def _reset_signing_password_cache():
    """Patch #2 round-3 fix (devils-advocate r3 HIGH #1): module-level
    `_SIGNING_PASSWORD_CACHE` and `_SIGNING_PASSWORD_LOADED` flags persist
    across tests in the same pytest session. Without a teardown, any test
    that runs before this one and triggers `_cache_signing_password()` would
    leak state into subsequent tests. Autouse + function-scoped resets both
    module globals back to their import-time defaults after each test.
    """
    yield
    # teardown
    sign_mod._SIGNING_PASSWORD_CACHE = None
    sign_mod._SIGNING_PASSWORD_LOADED = False


def test_infer_domain_unknown_raises(tmp_path):
    """Patch #2 round-1 (audit HIGH src/audit_pipeline/commands/sign.py:57):
    unknown filename now RAISES SignError instead of falling back to 'raw'
    (the legacy no-domain-separation tag). Closes the attacker-controlled-
    filename → wrong-domain bypass. Callers must pass `domain=` explicitly."""
    import pytest
    with pytest.raises(sign_mod.SignError, match="cannot infer signing domain"):
        sign_mod._infer_domain(tmp_path / "random.txt")


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
    # R5b (2026-05-24) code-reviewer LOW: added "authorization" — auth.py
    # imports SIGN_DOMAINS["authorization"] at validate-time; removing it
    # would break every bundle authorization verify silently.
    required = {"merkle", "bundle", "disclosure", "report", "heartbeat",
                "customer", "raw", "authorization"}
    assert required.issubset(set(sign_mod.SIGN_DOMAINS.keys()))


# ──────── verify_signature (programmatic v2-strict; P4.3 publisher) ────────


def test_verify_signature_roundtrips(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text('{"merkle_root":"abc"}', encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    # Returns the VERIFIED bytes on success (caller uses these — no re-read /
    # TOCTOU). P4.3 review HIGH: was `is None`.
    assert sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle") == b'{"merkle_root":"abc"}'


def test_verify_signature_detects_tamper(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text('{"merkle_root":"abc"}', encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    # Rewrite with SAME byte length so the Signed-Bytes check passes and the
    # crypto signature check is what actually catches the tamper.
    f.write_text('{"merkle_root":"XYZ"}', encoding="utf-8")
    with pytest.raises(sign_mod.SignError):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_rejects_wrong_domain(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="bundle")  # not merkle
    with pytest.raises(sign_mod.SignError, match="domain"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_rejects_filename_rebinding(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    other = tmp_path / "other.json"  # same bytes, different name — sig is bound to "merkle.json"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(sign_mod.SignError):
        sign_mod.verify_signature(other, sig, pub_path, expect_domain="merkle")


def test_verify_signature_refuses_legacy_v1(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    # Downgrade the declared schema to v1 — the strict publisher must refuse.
    sig.write_text(sig.read_text(encoding="utf-8").replace("jelleo-sign/v2", "jelleo-sign/v1"), encoding="utf-8")
    with pytest.raises(sign_mod.SignError, match="v2"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_missing_pubkey_raises(tmp_path, fresh_keypair):
    priv_path, _ = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(sign_mod.SignError):
        sign_mod.verify_signature(f, sig, tmp_path / "no-such.pub", expect_domain="merkle")


# ──── P4.3 hardening: strict-header / pinned-domain tests (review round-2) ────
#
# These cover the HIGH findings the 3-reviewer pass raised on the first
# verify_signature draft: an optional expect_domain (silent any-domain accept),
# a `.startswith` schema check (accepts forged jelleo-sign/v20), no Signed-File/
# Signed-Bytes presence requirement (downgrade), and a raw base64 decode that
# could crash the caller. The verified-read primitive feeds `merkle
# publish-onchain`, so every one of these is a "must refuse" path.


def _drop_header(sig_path, prefix):
    """Remove a whole header line (e.g. 'Signed-File:') from a .sig file."""
    kept = [
        ln for ln in sig_path.read_text(encoding="utf-8").splitlines()
        if not ln.startswith(prefix)
    ]
    sig_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _corrupt_b64_body(sig_path):
    """Replace the base64 signature body with a non-base64 string, leaving all
    headers (and the BEGIN/END markers) intact so the parse reaches b64decode."""
    txt = sig_path.read_text(encoding="utf-8")
    end = "-----END JELLEO SIGNATURE-----"
    before, _, _ = txt.rpartition(end)
    lines = before.rstrip("\n").splitlines()
    lines[-1] = "@@@not-valid-base64@@@"  # last non-blank line is the b64 body
    sig_path.write_text("\n".join(lines) + "\n" + end + "\n", encoding="utf-8")


def test_verify_signature_requires_expect_domain(tmp_path, fresh_keypair):
    """expect_domain is keyword-only with NO default — omitting it is a
    TypeError at call time, not a silent any-domain accept (review HIGH #1)."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(TypeError):
        sign_mod.verify_signature(f, sig, pub_path)  # type: ignore[call-arg]


def test_verify_signature_rejects_raw_expect_domain(tmp_path, fresh_keypair):
    """A caller must never pin the legacy 'raw' (no-separation) domain."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(sign_mod.SignError, match="raw"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="raw")


def test_verify_signature_rejects_unknown_expect_domain(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(sign_mod.SignError, match="known signing domain"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="not-a-domain")


def test_verify_signature_exact_schema_rejects_v20(tmp_path, fresh_keypair):
    """Schema check must be EXACT — a forged 'jelleo-sign/v20' must be refused,
    where the old `.startswith('jelleo-sign/v2')` would have accepted it."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    sig.write_text(
        sig.read_text(encoding="utf-8").replace("jelleo-sign/v2", "jelleo-sign/v20"),
        encoding="utf-8",
    )
    with pytest.raises(sign_mod.SignError, match="non-v2"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_rejects_raw_domain_in_sig(tmp_path, fresh_keypair):
    """A .sig declaring Domain: raw must be refused before any crypto runs."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    sig.write_text(
        sig.read_text(encoding="utf-8").replace("Domain: merkle", "Domain: raw"),
        encoding="utf-8",
    )
    with pytest.raises(sign_mod.SignError, match="raw"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_requires_signed_file_header(tmp_path, fresh_keypair):
    """A v2 sig with no Signed-File header is a downgrade — refuse, don't fall
    back to the on-disk name (which would let it verify any file)."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    _drop_header(sig, "Signed-File:")
    with pytest.raises(sign_mod.SignError, match="Signed-File"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_requires_signed_bytes_header(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    _drop_header(sig, "Signed-Bytes:")
    with pytest.raises(sign_mod.SignError, match="Signed-Bytes"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_rejects_malformed_base64(tmp_path, fresh_keypair):
    """A non-base64 signature body must surface as SignError, never a raw
    binascii.Error escaping to the caller."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    _corrupt_b64_body(sig)
    with pytest.raises(sign_mod.SignError, match="base64"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_missing_file_raises(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(sign_mod.SignError):
        sign_mod.verify_signature(tmp_path / "gone.json", sig, pub_path, expect_domain="merkle")


def test_parse_sig_metadata_first_wins_on_duplicate_domain(tmp_path):
    """_parse_sig_metadata must take the FIRST occurrence of a header key — a
    tampered .sig with a second Domain: line must not override the first
    (.sig headers are NOT covered by the Ed25519 signature)."""
    sig_text = (
        "-----BEGIN JELLEO SIGNATURE-----\n"
        "Schema: jelleo-sign/v2\n"
        "Domain: merkle\n"
        "Domain: bundle\n"
        "Signed-File: merkle.json\n"
        "Signed-Bytes: 2\n"
        "\n"
        "QUJD\n"
        "-----END JELLEO SIGNATURE-----\n"
    )
    meta = sign_mod._parse_sig_metadata(sig_text)
    assert meta["domain"] == "merkle"  # first wins, not "bundle"
    assert meta["schema"] == "jelleo-sign/v2"
    assert meta["signed_bytes"] == 2


def test_sign_domains_tags_unique_nonempty_no_prefix():
    """Domain-separation safety (threat-modeler P4.3 #2 + goober #3): every
    non-'raw' tag must be non-empty, unique, and not a byte-prefix of any other
    tag. NUL-terminated tags already prevent boundary confusion, but enforce the
    invariant so a future tag addition can't silently regress it."""
    from itertools import permutations
    tags = {d: t for d, t in sign_mod.SIGN_DOMAINS.items() if d != "raw"}
    for d, t in tags.items():
        assert t, f"domain {d!r} has an empty tag — would collapse separation"
    assert len(set(tags.values())) == len(tags), "duplicate domain tags"
    for (da, ta), (db, tb) in permutations(tags.items(), 2):
        assert not tb.startswith(ta), f"tag for {da!r} is a byte-prefix of {db!r}"
    # 'raw' is the ONLY empty tag (legacy v1, refused by verify_signature).
    assert sign_mod.SIGN_DOMAINS["raw"] == b""


def test_verify_signature_rejects_missing_blank_separator(tmp_path, fresh_keypair):
    """If the blank line between headers and the base64 body is removed, the
    two-phase parser never enters body mode → no signature bytes → refuse."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    txt = sig.read_text(encoding="utf-8")
    collapsed = "\n".join(ln for ln in txt.splitlines() if ln != "") + "\n"
    sig.write_text(collapsed, encoding="utf-8")
    with pytest.raises(sign_mod.SignError):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="merkle")


def test_verify_signature_rejects_non_ed25519_pubkey(tmp_path, fresh_keypair):
    """A well-formed but wrong-algorithm public key (EC P-256 here) must be
    refused with a clear Ed25519 error, not a confusing InvalidSignature."""
    from cryptography.hazmat.primitives.asymmetric import ec
    priv_path, _ = fresh_keypair
    ec_priv = ec.generate_private_key(ec.SECP256R1())
    ec_pub_path = tmp_path / "ec.pub"
    ec_pub_path.write_bytes(
        ec_priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    with pytest.raises(sign_mod.SignError, match="Ed25519"):
        sign_mod.verify_signature(f, sig, ec_pub_path, expect_domain="merkle")


def test_verify_signature_rejects_empty_tag_domain(tmp_path, fresh_keypair, monkeypatch):
    """Defense-in-depth (goober P4.3 #3): if a non-'raw' domain ever maps to an
    empty tag, verify must refuse rather than silently drop domain separation.
    Patch SIGN_DOMAINS to inject such a domain, sign under it, then verify."""
    priv_path, pub_path = fresh_keypair
    patched = dict(sign_mod.SIGN_DOMAINS)
    patched["evil"] = b""  # empty tag — no domain separation
    monkeypatch.setattr(sign_mod, "SIGN_DOMAINS", patched)
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="evil")
    with pytest.raises(sign_mod.SignError, match="empty-tag|unknown"):
        sign_mod.verify_signature(f, sig, pub_path, expect_domain="evil")


# ────────── CLI `sign verify` / `keygen` hardening (P4 review: verify_cmd) ──────────
# These exercise the click COMMAND path (verify_cmd / keygen_cmd), distinct from
# the strict verify_signature primitive tested above. The P4 paranoid-goober
# flagged the lenient CLI verify path: `domain_tag is None` let a v2 `Domain: raw`
# sig through with NO separation, and the pubkey was used without an Ed25519 check.
from click.testing import CliRunner  # noqa: E402


def _cli_verify(workspace, f, sig, pub):
    return CliRunner().invoke(
        sign_mod.sign_cmd,
        ["verify", str(f), str(sig), "--pubkey", str(pub)],
        obj={"workspace": str(workspace)},
    )


def test_cli_verify_roundtrips(tmp_path, fresh_keypair):
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    r = _cli_verify(tmp_path, f, sig, pub_path)
    assert r.exit_code == 0, r.output
    assert "VALID" in r.output  # ASCII status word, no check-mark glyph (Windows-safe)


def test_cli_verify_rejects_raw_domain_v2_sig(tmp_path, fresh_keypair):
    """A v2 sig declaring `Domain: raw` (tag b"") must be refused — `is None` let
    it verify with no domain separation; `not domain_tag` now catches it."""
    priv_path, pub_path = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    sig.write_text(
        sig.read_text(encoding="utf-8").replace("Domain: merkle", "Domain: raw"),
        encoding="utf-8",
    )
    r = _cli_verify(tmp_path, f, sig, pub_path)
    assert r.exit_code != 0
    assert "domain" in r.output.lower()


def test_cli_verify_rejects_non_ed25519_pubkey(tmp_path, fresh_keypair):
    """verify_cmd loaded the pubkey without an algorithm check; an EC key could
    reach pub.verify(). Refuse early with a clear Ed25519 message."""
    from cryptography.hazmat.primitives.asymmetric import ec
    priv_path, _ = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    ec_pub = tmp_path / "ec.pub"
    ec_pub.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    r = _cli_verify(tmp_path, f, sig, ec_pub)
    assert r.exit_code != 0
    assert "Ed25519" in r.output


def test_cli_keygen_writes_key_with_ascii_note(tmp_path, monkeypatch):
    """keygen must write the key, and its console output must be cp1252/ASCII-safe
    (a glyph used to crash the Windows console BEFORE the key was written)."""
    monkeypatch.setenv("JELLEO_SIGNING_KEY_PASSWORD", "test-pw")
    r = CliRunner().invoke(
        sign_mod.sign_cmd, ["keygen"], obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "keys" / "jelleo.ed25519").is_file()
    assert "encrypted" in r.output.lower()  # encryption note printed AFTER the write
    # cp1252-safe is the real Windows-console invariant (em-dash `—` is ALLOWED —
    # cp1252 0x97; the banned class is → ✓ ✗ ⚠ emoji). NOT the same as ASCII.
    r.output.encode("cp1252")  # must not raise UnicodeEncodeError


def test_cli_verify_rejects_corrupt_pubkey(tmp_path, fresh_keypair):
    """A corrupt / non-PEM public key must surface as a clean ClickException, not
    an uncaught ValueError/TypeError traceback (goober P4 HIGH; matches the
    strict verify_signature primitive which wraps the same call)."""
    priv_path, _ = fresh_keypair
    f = tmp_path / "merkle.json"
    f.write_text("{}", encoding="utf-8")
    sig = sign_mod.sign_file(f, key_path=priv_path, domain="merkle")
    bad_pub = tmp_path / "bad.pub"
    bad_pub.write_text("this is not a PEM public key", encoding="utf-8")
    r = _cli_verify(tmp_path, f, sig, bad_pub)
    assert r.exit_code != 0
    assert "could not parse" in r.output.lower()


def test_cli_keygen_warns_unencrypted_without_password(tmp_path, monkeypatch):
    """No JELLEO_SIGNING_KEY_PASSWORD -> the key is still written (unencrypted)
    and a WARN note is printed AFTER the write, ASCII-safe."""
    monkeypatch.delenv("JELLEO_SIGNING_KEY_PASSWORD", raising=False)
    r = CliRunner().invoke(
        sign_mod.sign_cmd, ["keygen"], obj={"workspace": str(tmp_path)},
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "keys" / "jelleo.ed25519").is_file()
    assert "UNENCRYPTED" in r.output
    r.output.encode("cp1252")  # cp1252-safe (em-dash allowed); must not raise
