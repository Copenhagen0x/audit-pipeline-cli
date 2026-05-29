"""Tests for attestation_client — the off-chain publish_attestation builder (P4.3).

Pins the Anchor discriminator + PDA derivations so any silent drift in
PROGRAM_ID / seeds / arg-order (which would attest to the WRONG on-chain account
or build a malformed ix) is caught at test time. Exercises the merkle.json →
PublishArgs extraction + every client-side validation guard that mirrors the
program's require!()s.
"""

from __future__ import annotations

import struct

import pytest

# The solders-backed helpers (parse_pubkey/derive_pdas/build_instruction) need
# solders; the pure logic (discriminator/borsh/validate/from_merkle_json) does
# not, but the whole feature is solders-coupled so skip the file if it's absent.
pytest.importorskip("solders")

from audit_pipeline import attestation_client as ac  # noqa: E402

# Pinned regression anchors (computed from PROGRAM_ID + lib.rs seeds 2026-05-28).
_DISC_HEX = "7726782d56169137"
_CONFIG_PDA = "36QCRV3ki7fysq51Mqd2zDHKwuqCw7yi5EGX5o66cNHN"
_CYCLE_PDA_SYSPROTO = "332TPTWqjVpqdEhKj8DkCBDDZjAFYg9XYHykJ951adiF"
_LATEST_PDA_SYSPROTO = "CJhS7fN4s9L51scDT265zssgHhA4r1FQFtsqa6n491FA"

_FIXED_PROTO = bytes(range(32))  # deterministic 32-byte protocol for direct-PublishArgs tests
_PROTO_B58 = "So11111111111111111111111111111111111111112"  # a real valid pubkey (wSOL mint)
_PROTO_BYTES = ac.parse_pubkey(_PROTO_B58)


def _good_merkle(cycle_id="20260513-191318"):
    return {
        "schema": ac.ATTESTATION_MERKLE_SCHEMA,
        "cycle_id": cycle_id,
        "engine_sha": "a" * 40,
        "n_findings": 7,
        "n_leaves": 8,
        "merkle_root": "ab" * 32,  # 64 hex chars
        "protocol": _PROTO_B58,     # P4.4: protocol is now part of the SIGNED sidecar
    }


# ───────────────────────── pinned constants ─────────────────────────


def test_discriminator_pinned():
    assert ac.anchor_discriminator("publish_attestation").hex() == _DISC_HEX
    assert len(ac.anchor_discriminator("publish_attestation")) == 8


def test_config_pda_pinned():
    # config PDA is protocol-independent — pins PROGRAM_ID + the "config" seed.
    pdas = ac.derive_pdas(ac.parse_pubkey(ac.SYSTEM_PROGRAM_ID), "anything")
    assert pdas["config"] == _CONFIG_PDA


def test_cycle_and_latest_pda_pinned():
    proto = ac.parse_pubkey(ac.SYSTEM_PROGRAM_ID)  # all-zero, deterministic
    pdas = ac.derive_pdas(proto, "20260513-191318")
    assert pdas["cycle"] == _CYCLE_PDA_SYSPROTO
    assert pdas["latest"] == _LATEST_PDA_SYSPROTO


# ───────────────────────── borsh / layout ─────────────────────────


def test_borsh_string_length_prefix():
    assert ac._borsh_string("abc") == struct.pack("<I", 3) + b"abc"
    assert ac._borsh_string("") == struct.pack("<I", 0)


def test_instruction_data_layout_decodes():
    args = ac.PublishArgs(
        protocol=_FIXED_PROTO,
        cycle_id="20260513-191318",
        engine_sha="deadbeef",
        invariant_count=7,
        merkle_root=bytes([0xAB]) * 32,
    )
    data = args.instruction_data()
    # Decode byte-for-byte and confirm the exact wire layout.
    off = 0
    assert data[off:off + 8].hex() == _DISC_HEX
    off += 8
    assert data[off:off + 32] == _FIXED_PROTO
    off += 32
    (clen,) = struct.unpack_from("<I", data, off); off += 4
    assert data[off:off + clen].decode() == "20260513-191318"; off += clen
    (elen,) = struct.unpack_from("<I", data, off); off += 4
    assert data[off:off + elen].decode() == "deadbeef"; off += elen
    (count,) = struct.unpack_from("<I", data, off); off += 4
    assert count == 7
    assert data[off:off + 32] == bytes([0xAB]) * 32; off += 32
    assert off == len(data)  # no trailing bytes


# ───────────────────────── validate() guards ─────────────────────────


def _args(**over):
    base = dict(protocol=_FIXED_PROTO, cycle_id="c", engine_sha="sha",
                invariant_count=1, merkle_root=b"\x00" * 32)
    base.update(over)
    return ac.PublishArgs(**base)


def test_validate_rejects_bad_protocol_len():
    with pytest.raises(ac.AttestationError, match="protocol must be 32"):
        _args(protocol=b"\x00" * 31).validate()


def test_validate_rejects_bad_root_len():
    with pytest.raises(ac.AttestationError, match="merkle_root must be 32"):
        _args(merkle_root=b"\x00" * 31).validate()


def test_validate_rejects_long_cycle_id():
    with pytest.raises(ac.AttestationError, match="CycleIdTooLong"):
        _args(cycle_id="x" * (ac.MAX_CYCLE_ID_LEN + 1)).validate()


def test_validate_rejects_empty_engine_sha():
    with pytest.raises(ac.AttestationError, match="engine_sha is empty"):
        _args(engine_sha="").validate()


def test_validate_rejects_long_engine_sha():
    with pytest.raises(ac.AttestationError, match="EngineShaTooLong"):
        _args(engine_sha="x" * (ac.MAX_ENGINE_SHA_LEN + 1)).validate()


def test_validate_rejects_count_out_of_u32():
    with pytest.raises(ac.AttestationError, match="out of u32 range"):
        _args(invariant_count=0x1_0000_0000).validate()


# ───────────────────────── from_merkle_json ─────────────────────────


def test_from_merkle_json_extracts_fields():
    args = ac.from_merkle_json(_good_merkle(), expect_cycle_id="20260513-191318")
    assert args.cycle_id == "20260513-191318"
    assert args.engine_sha == "a" * 40
    assert args.invariant_count == 7  # == n_findings
    assert args.merkle_root == bytes.fromhex("ab" * 32)
    assert args.protocol == _PROTO_BYTES   # read from the sidecar's "protocol" field


def test_from_merkle_json_cycle_id_mismatch_raises():
    with pytest.raises(ac.AttestationError, match="content/path mismatch"):
        ac.from_merkle_json(_good_merkle(), expect_cycle_id="99999999-000000")


def test_from_merkle_json_bad_schema_raises():
    m = _good_merkle()
    m["schema"] = "jelleo-cycle-merkle-v3"
    with pytest.raises(ac.AttestationError, match="unrecognized schema"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_missing_engine_sha_raises():
    m = _good_merkle()
    m["engine_sha"] = None
    with pytest.raises(ac.AttestationError, match="engine_sha"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_bad_root_raises():
    m = _good_merkle()
    m["merkle_root"] = "abcd"  # too short
    with pytest.raises(ac.AttestationError, match="64 hex"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_negative_findings_raises():
    m = _good_merkle()
    m["n_findings"] = -1
    with pytest.raises(ac.AttestationError, match="n_findings"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


# ───────────────────────── solders-backed ─────────────────────────


def test_parse_pubkey_valid_and_invalid():
    assert ac.parse_pubkey(ac.SYSTEM_PROGRAM_ID) == b"\x00" * 32
    with pytest.raises(ac.AttestationError, match="invalid base58"):
        ac.parse_pubkey("not-a-valid-base58-pubkey!!!")


@pytest.mark.parametrize("bad", ["", "   ", "\t", "not-base58!!!"])
def test_parse_pubkey_rejects_empty_whitespace_garbage(bad):
    # Locks the empirical finding (goober P4.4 confirmation): these are refused
    # BEFORE signing in `compute --protocol`, so a garbage/blank protocol can
    # never be signed into an attestable sidecar.
    with pytest.raises(ac.AttestationError):
        ac.parse_pubkey(bad)


def test_build_instruction_account_order_and_roles():
    args = ac.from_merkle_json(_good_merkle(), expect_cycle_id="20260513-191318")
    ix = ac.build_instruction(args, authority=ac.EXPECTED_AUTHORITY)
    assert str(ix.program_id) == ac.PROGRAM_ID
    assert bytes(ix.data) == args.instruction_data()
    roles = [(m.is_signer, m.is_writable) for m in ix.accounts]
    # config(ro), cycle(w), latest(w), authority(signer+w), system(ro)
    assert roles == [(False, False), (False, True), (False, True), (True, True), (False, False)]
    # the signer is the EXPECTED_AUTHORITY, never anyone else
    assert str(ix.accounts[3].pubkey) == ac.EXPECTED_AUTHORITY
    assert str(ix.accounts[4].pubkey) == ac.SYSTEM_PROGRAM_ID
    # Divergence guard (P4.3 r2 code-reviewer + threat-modeler): build_instruction
    # derives PDAs independently of derive_pdas — pin that the two agree, so a
    # future seed-order edit to one path can't silently diverge from the other.
    pdas = ac.derive_pdas(_PROTO_BYTES, "20260513-191318")
    assert str(ix.accounts[0].pubkey) == pdas["config"]
    assert str(ix.accounts[1].pubkey) == pdas["cycle"]
    assert str(ix.accounts[2].pubkey) == pdas["latest"]


def test_from_merkle_json_rejects_bool_n_findings():
    # bool is an int subclass — a JSON `true` must NOT pose as invariant_count=1.
    m = _good_merkle()
    m["n_findings"] = True
    with pytest.raises(ac.AttestationError, match="n_findings"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_rejects_nonstr_engine_sha():
    # truthy non-string must raise AttestationError, not crash with AttributeError.
    m = _good_merkle()
    m["engine_sha"] = 123
    with pytest.raises(ac.AttestationError, match="engine_sha"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_rejects_nonstr_cycle_id():
    m = _good_merkle()
    m["cycle_id"] = 20260513  # non-string
    with pytest.raises(ac.AttestationError, match="cycle_id"):
        ac.from_merkle_json(m, expect_cycle_id="20260513")


@pytest.mark.parametrize("nondict", [[1, 2, 3], None, 42, "hello", 3.14])
def test_from_merkle_json_rejects_nondict(nondict):
    # Valid JSON that isn't an object (list/null/int/str/float) must raise
    # AttestationError, not a raw AttributeError from .get() (goober P4.3 r3).
    with pytest.raises(ac.AttestationError, match="must be a JSON object"):
        ac.from_merkle_json(nondict, expect_cycle_id="x")


def test_from_merkle_json_rejects_lone_surrogate_engine_sha():
    # A lone surrogate is valid JSON (\uD800) and ASCII-clean on disk, so it
    # decodes + parses fine, but is not utf-8-encodable → must raise
    # AttestationError, not a raw UnicodeEncodeError (goober P4.3 r3).
    m = _good_merkle()
    m["engine_sha"] = "abc\ud800def"
    with pytest.raises(ac.AttestationError, match="un-encodable"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_rejects_null_merkle_root():
    # Key present with null value: dict.get(.., "") returns None (default only
    # fires on absent key) → _merkle_root_hex_to_bytes(None) → AttestationError.
    m = _good_merkle()
    m["merkle_root"] = None
    with pytest.raises(ac.AttestationError, match="64 hex"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_requires_protocol():
    # P4.4: protocol must be present in the (signed) sidecar — absent → refuse.
    m = _good_merkle()
    del m["protocol"]
    with pytest.raises(ac.AttestationError, match="no 'protocol'"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_rejects_nonstr_protocol():
    m = _good_merkle()
    m["protocol"] = 123  # non-string → same "no 'protocol'" refusal
    with pytest.raises(ac.AttestationError, match="no 'protocol'"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_rejects_bad_base58_protocol():
    m = _good_merkle()
    m["protocol"] = "not-a-valid-base58!!!"  # present + str, but not a pubkey
    with pytest.raises(ac.AttestationError, match="invalid base58"):
        ac.from_merkle_json(m, expect_cycle_id="20260513-191318")


def test_from_merkle_json_zero_findings_ok():
    """A scoped zero-finding cycle is valid — invariant_count=0 attests fine.
    (lib.rs comment 'n_findings / n_leaves' is a doc typo; the field is n_findings.)"""
    m = _good_merkle()
    m["n_findings"] = 0
    args = ac.from_merkle_json(m, expect_cycle_id="20260513-191318")
    assert args.invariant_count == 0


# ───── CLI `merkle publish-onchain` — the verify-GATE integration (P4.3) ─────
#
# These are the threat-modeler's #4 demand: prove the consumer's FIRST action is
# the Ed25519 verification and that it FAILS CLOSED on a tampered / unsigned
# sidecar — no attestation instruction is built unless the .sig is valid.

import json as _json  # noqa: E402

cryptography = pytest.importorskip("cryptography")  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from audit_pipeline.commands import sign as _sign_mod  # noqa: E402
from audit_pipeline.commands.merkle import merkle_cmd  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_signing_password_cache():
    """sign.py caches JELLEO_SIGNING_KEY_PASSWORD at module level and
    `_make_signed_workspace` calls sign_file. Reset the cache after each test so
    a password set by an earlier test in the same pytest session can't
    contaminate the (unencrypted) keygen here (paranoid-goober P4.3 r2)."""
    yield
    _sign_mod._SIGNING_PASSWORD_CACHE = None
    _sign_mod._SIGNING_PASSWORD_LOADED = False


def _make_signed_workspace(tmp_path, cycle_id="20260513-191318", *, sign=True,
                           tamper=False, content_override=None, omit_protocol=False):
    """Build a minimal workspace: keys/ + a signed hunts/<cid>/merkle.json.

    content_override (bytes): write these raw bytes as merkle.json instead of
    the JSON fixture (used to test the signed-but-non-JSON clean-error path).
    omit_protocol: drop the 'protocol' field before signing (test publish-time
    refusal of a protocol-less sidecar)."""
    keys = tmp_path / "keys"
    keys.mkdir()
    priv = Ed25519PrivateKey.generate()
    (keys / "jelleo.ed25519").write_bytes(priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    (keys / "jelleo.ed25519.pub").write_bytes(priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    hd = tmp_path / "hunts" / cycle_id
    hd.mkdir(parents=True)
    mp = hd / "merkle.json"
    if content_override is not None:
        mp.write_bytes(content_override)
    else:
        m = _good_merkle(cycle_id)
        if omit_protocol:
            del m["protocol"]
        mp.write_text(_json.dumps(m), encoding="utf-8")
    if sign:
        _sign_mod.sign_file(mp, keys / "jelleo.ed25519", domain="merkle")
    if tamper:
        # Same byte length (merkle_root stays 64 hex chars), different content →
        # the Signed-Bytes check passes and the CRYPTO check is what fails.
        m2 = _good_merkle(cycle_id)
        m2["merkle_root"] = "cd" * 32
        mp.write_text(_json.dumps(m2), encoding="utf-8")
    return tmp_path


def _invoke_publish(ws, cycle_id="20260513-191318", *extra):
    runner = CliRunner()
    return runner.invoke(
        merkle_cmd,
        ["publish-onchain", cycle_id, *extra],
        obj={"workspace": str(ws)},
    )


def test_publish_onchain_verifies_and_previews(tmp_path):
    ws = _make_signed_workspace(tmp_path)
    r = _invoke_publish(ws)
    assert r.exit_code == 0, r.output
    assert "signature verified" in r.output
    assert "PREVIEW" in r.output
    assert ("ab" * 32) in r.output         # the attested merkle_root
    assert ac.EXPECTED_AUTHORITY in r.output


def test_publish_onchain_fails_closed_on_tamper(tmp_path):
    ws = _make_signed_workspace(tmp_path, tamper=True)
    r = _invoke_publish(ws)
    assert r.exit_code != 0
    assert "signature check FAILED" in r.output
    assert "PREVIEW" not in r.output        # never reached instruction build


def test_publish_onchain_fails_closed_when_unsigned(tmp_path):
    ws = _make_signed_workspace(tmp_path, sign=False)
    r = _invoke_publish(ws)
    assert r.exit_code != 0
    assert "signature check FAILED" in r.output


def test_publish_onchain_fails_when_sidecar_missing_protocol(tmp_path):
    # P4.4: protocol is no longer a CLI flag — it must be in the SIGNED sidecar.
    # A validly-signed but protocol-less sidecar must be refused, fail-closed.
    ws = _make_signed_workspace(tmp_path, omit_protocol=True)
    r = _invoke_publish(ws)
    assert r.exit_code != 0
    assert "signature verified" in r.output     # gate passes (sidecar IS signed)
    assert "no 'protocol'" in r.output           # refused for the missing binding
    assert "PREVIEW" not in r.output


def test_publish_onchain_writes_request_artifact(tmp_path):
    ws = _make_signed_workspace(tmp_path)
    out = tmp_path / "request.json"
    r = _invoke_publish(ws, "20260513-191318", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert out.is_file()
    req = _json.loads(out.read_text(encoding="utf-8"))
    assert req["program_id"] == ac.PROGRAM_ID
    assert req["authority"] == ac.EXPECTED_AUTHORITY
    assert req["cycle_id"] == "20260513-191318"
    assert req["merkle_root_hex"] == "ab" * 32
    assert req["protocol"] == _PROTO_B58   # the SIGNED-sidecar protocol, not a flag
    # account list must carry exactly one signer, and it's the authority.
    signers = [a for a in req["accounts"] if a["is_signer"]]
    assert len(signers) == 1 and signers[0]["pubkey"] == ac.EXPECTED_AUTHORITY


def test_publish_onchain_clean_error_on_signed_non_json(tmp_path):
    """A signed-but-non-JSON merkle.json must yield a clean ClickException, not a
    raw traceback (P4.3 r2 — goober + threat-modeler). The .sig is valid over the
    bytes, so the verify gate passes and the JSON parse is what must fail cleanly."""
    ws = _make_signed_workspace(tmp_path, content_override=b"not valid json {{{")
    r = _invoke_publish(ws)
    assert r.exit_code != 0
    assert "signature verified" in r.output      # verify gate passed (content-agnostic)
    assert "not valid UTF-8 JSON" in r.output     # clean error, not a traceback
    assert "PREVIEW" not in r.output


@pytest.mark.parametrize("blob", [b"[1,2,3]", b"null", b"42", b'"hello"'])
def test_publish_onchain_clean_error_on_signed_nondict_json(tmp_path, blob):
    """Signed VALID JSON that isn't an object (list/null/int/str) must give a
    clean ClickException, not a raw AttributeError traceback (goober P4.3 r3)."""
    ws = _make_signed_workspace(tmp_path, content_override=blob)
    r = _invoke_publish(ws)
    assert r.exit_code != 0
    assert "signature verified" in r.output            # verify gate still passed first
    assert "must be a JSON object" in r.output          # clean error
    assert "PREVIEW" not in r.output
    # No raw exception leaked — ClickException surfaces as SystemExit, not AttributeError.
    assert r.exception is None or isinstance(r.exception, SystemExit)


# ───── attest-init / attest-register + --send preflight (P4.4 CLI wiring) ─────


def _invoke(ws, *args):
    return CliRunner().invoke(merkle_cmd, list(args), obj={"workspace": str(ws)})


def test_attest_init_preview(tmp_path):
    r = _invoke(tmp_path, "attest-init")
    assert r.exit_code == 0, r.output
    assert "initialize" in r.output and "PREVIEW" in r.output
    assert ac.EXPECTED_AUTHORITY in r.output
    assert "afaf6d1f0d989bed" in r.output  # initialize discriminator


def test_attest_register_preview(tmp_path):
    r = _invoke(tmp_path, "attest-register", ac.SYSTEM_PROGRAM_ID)
    assert r.exit_code == 0, r.output
    assert "register_protocol" in r.output and "PREVIEW" in r.output
    assert ac.SYSTEM_PROGRAM_ID in r.output
    assert "3f6b9c88f9e7b741" in r.output  # register_protocol discriminator


def test_attest_register_rejects_bad_protocol(tmp_path):
    r = _invoke(tmp_path, "attest-register", "not-a-valid-base58!!!")
    assert r.exit_code != 0
    assert "invalid base58" in r.output


def test_attest_register_send_without_keypair_errors(tmp_path):
    r = _invoke(tmp_path, "attest-register", ac.SYSTEM_PROGRAM_ID, "--send")
    assert r.exit_code != 0
    assert "requires --keypair" in r.output


def test_publish_onchain_send_without_keypair_errors(tmp_path):
    ws = _make_signed_workspace(tmp_path)
    r = _invoke_publish(ws, "20260513-191318", "--send")
    assert r.exit_code != 0
    assert "requires --keypair" in r.output


def test_publish_onchain_send_rejects_non_authority_keypair(tmp_path):
    # The #3 preflight: a keypair that isn't EXPECTED_AUTHORITY is refused BEFORE
    # any network call (the tx would be rejected on-chain otherwise).
    ws = _make_signed_workspace(tmp_path)
    from solders.keypair import Keypair
    wrong = Keypair()  # random — not the authority
    kpf = tmp_path / "wrong.json"
    kpf.write_text(_json.dumps(list(bytes(wrong))), encoding="utf-8")
    r = _invoke_publish(ws, "20260513-191318", "--send", "--keypair", str(kpf))
    assert r.exit_code != 0
    assert "NOT the program authority" in r.output
