"""Tests for attestation_send — the live-send module (P4.4), offline parts only.

Covers the safety rails (mainnet hard-block, cluster resolution), the
initialize/register_protocol instruction builders (account order + roles +
pinned Anchor discriminators), and keypair-load error handling. The actual
RPC submit/confirm needs a live cluster + funded key, so it is exercised in the
devnet bring-up, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("solders")

from audit_pipeline import attestation_client as ac  # noqa: E402
from audit_pipeline import attestation_send as s  # noqa: E402

# Pinned discriminators (sha256("global:<ix>")[:8]) — catch any accidental rename.
_DISC_INITIALIZE = "afaf6d1f0d989bed"
_DISC_REGISTER = "3f6b9c88f9e7b741"
_PROTO = ac.parse_pubkey(ac.SYSTEM_PROGRAM_ID)  # deterministic 32-byte protocol


# ───────────────────────── cluster safety ─────────────────────────


def test_resolve_cluster_devnet():
    assert s.resolve_cluster("devnet") == "https://api.devnet.solana.com"
    assert s.resolve_cluster("testnet") == "https://api.testnet.solana.com"
    assert s.resolve_cluster("localnet").startswith("http://127.0.0.1")


@pytest.mark.parametrize("bad", [
    "mainnet-beta",
    "MAINNET",
    "https://api.mainnet-beta.solana.com",
    "https://my-mainnet-rpc.example.com",
    "x-mainnet-y",
])
def test_resolve_cluster_blocks_mainnet(bad):
    with pytest.raises(s.AttestationSendError, match="mainnet"):
        s.resolve_cluster(bad)


def test_resolve_cluster_rejects_unknown():
    with pytest.raises(s.AttestationSendError, match="unrecognized"):
        s.resolve_cluster("frobnitz")


def test_resolve_cluster_accepts_explicit_devnet_url():
    u = "https://rpc.devnet.example.com"
    assert s.resolve_cluster(u) == u


# ───────────────────── instruction builders ─────────────────────


def test_initialize_ix_layout_and_disc():
    ix = s.build_initialize_ix(ac.EXPECTED_AUTHORITY)
    assert str(ix.program_id) == ac.PROGRAM_ID
    assert bytes(ix.data).hex() == _DISC_INITIALIZE          # disc only, no args
    assert len(bytes(ix.data)) == 8
    roles = [(m.is_signer, m.is_writable) for m in ix.accounts]
    # config(w), payer(signer+w), system(ro)
    assert roles == [(False, True), (True, True), (False, False)]
    assert str(ix.accounts[1].pubkey) == ac.EXPECTED_AUTHORITY  # payer == the signer we passed
    assert str(ix.accounts[2].pubkey) == ac.SYSTEM_PROGRAM_ID


def test_register_protocol_ix_layout_and_disc():
    ix = s.build_register_protocol_ix(ac.EXPECTED_AUTHORITY, _PROTO)
    assert str(ix.program_id) == ac.PROGRAM_ID
    data = bytes(ix.data)
    assert data[:8].hex() == _DISC_REGISTER
    assert data[8:40] == _PROTO                              # borsh Pubkey arg = 32 raw bytes
    assert len(data) == 40
    roles = [(m.is_signer, m.is_writable) for m in ix.accounts]
    # config(ro), latest(w), authority(signer+w), system(ro)
    assert roles == [(False, False), (False, True), (True, True), (False, False)]
    assert str(ix.accounts[2].pubkey) == ac.EXPECTED_AUTHORITY  # authority is the signer
    # config + latest PDAs must match attestation_client's derivation (single source of truth)
    pdas = ac.derive_pdas(_PROTO, "x")
    assert str(ix.accounts[0].pubkey) == pdas["config"]
    assert str(ix.accounts[1].pubkey) == pdas["latest"]


def test_register_protocol_rejects_bad_protocol_len():
    with pytest.raises(s.AttestationSendError, match="32 bytes"):
        s.build_register_protocol_ix(ac.EXPECTED_AUTHORITY, b"\x00" * 31)


# ───────────────────────── keypair load ─────────────────────────


def test_load_keypair_missing_raises(tmp_path):
    with pytest.raises(s.AttestationSendError, match="no keypair file"):
        s.load_keypair(tmp_path / "nope.json")


def test_load_keypair_malformed_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not a keypair", encoding="utf-8")
    with pytest.raises(s.AttestationSendError, match="could not load keypair"):
        s.load_keypair(bad)


# ───────── mock-RPC submit/confirm coverage (threat-modeler P4.4 #11) ─────────
# The submit/confirm path had zero coverage, which is what let the null-sig bug
# (#2) hide. We patch urllib.request.urlopen with a method->result router.

import json as _json  # noqa: E402
from unittest.mock import patch  # noqa: E402

from solders.keypair import Keypair  # noqa: E402

_DEVNET = "https://api.devnet.solana.com"
_NONMAINNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"  # devnet genesis (≠ mainnet)
_FAKE_BLOCKHASH = str(Keypair().pubkey())  # any valid base58 32-byte stands in for a blockhash


class _FakeResp:
    def __init__(self, payload):
        self._b = _json.dumps(payload).encode("utf-8")

    def read(self, n=-1):
        return self._b[:n] if isinstance(n, int) and n > 0 else self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(responses, calls=None):
    """Return a urlopen replacement that routes by JSON-RPC method to a canned
    result. `calls` (if given) records the method sequence."""
    def _urlopen(req, timeout=None):
        body = _json.loads(req.data.decode("utf-8"))
        method = body["method"]
        if calls is not None:
            calls.append(method)
        if method not in responses:
            raise AssertionError(f"unexpected RPC method: {method}")
        return _FakeResp({"jsonrpc": "2.0", "id": 1, "result": responses[method]})
    return _urlopen


def _ix_and_kp():
    kp = Keypair()
    return s.build_initialize_ix(str(kp.pubkey())), kp


def test_submit_blocks_mainnet_by_genesis_hash():
    # The real guard: even a devnet-named URL is refused if the RPC reports the
    # mainnet genesis hash (closes the loopback/substring bypass, #1).
    ix, kp = _ix_and_kp()
    with patch("urllib.request.urlopen", _mock_urlopen({"getGenesisHash": s.MAINNET_GENESIS})):
        with pytest.raises(s.AttestationSendError, match="mainnet-beta genesis"):
            s.submit_and_confirm(_DEVNET, [ix], kp, dry_run=True)


def test_dry_run_builds_but_never_submits():
    ix, kp = _ix_and_kp()
    calls = []
    resp = {"getGenesisHash": _NONMAINNET_GENESIS,
            "getLatestBlockhash": {"value": {"blockhash": _FAKE_BLOCKHASH}}}
    with patch("urllib.request.urlopen", _mock_urlopen(resp, calls)):
        out = s.submit_and_confirm(_DEVNET, [ix], kp, dry_run=True)
    assert out["dry_run"] is True and out["tx_base64"]
    assert "sendTransaction" not in calls  # dry-run MUST NOT submit


def test_null_send_result_raises_not_submitted():
    # The #2 bug: a null sendTransaction result must raise "NOT submitted",
    # not spin the confirm loop into a misleading "may have landed".
    ix, kp = _ix_and_kp()
    resp = {"getGenesisHash": _NONMAINNET_GENESIS,
            "getLatestBlockhash": {"value": {"blockhash": _FAKE_BLOCKHASH}},
            "sendTransaction": None}
    with patch("urllib.request.urlopen", _mock_urlopen(resp)):
        with pytest.raises(s.AttestationSendError, match="NOT submitted"):
            s.submit_and_confirm(_DEVNET, [ix], kp, dry_run=False)


def test_real_send_confirms():
    ix, kp = _ix_and_kp()
    resp = {"getGenesisHash": _NONMAINNET_GENESIS,
            "getLatestBlockhash": {"value": {"blockhash": _FAKE_BLOCKHASH}},
            "sendTransaction": "5SigNatureBase58Stub",
            "getSignatureStatuses": {"value": [{"err": None, "confirmationStatus": "confirmed"}]}}
    with patch("urllib.request.urlopen", _mock_urlopen(resp)):
        out = s.submit_and_confirm(_DEVNET, [ix], kp, dry_run=False)
    assert out["dry_run"] is False and out["signature"] == "5SigNatureBase58Stub"


def test_real_send_onchain_failure_raises():
    ix, kp = _ix_and_kp()
    resp = {"getGenesisHash": _NONMAINNET_GENESIS,
            "getLatestBlockhash": {"value": {"blockhash": _FAKE_BLOCKHASH}},
            "sendTransaction": "5SigNatureBase58Stub",
            "getSignatureStatuses": {"value": [{"err": {"InstructionError": [0, "Custom"]},
                                                 "confirmationStatus": "processed"}]}}
    with patch("urllib.request.urlopen", _mock_urlopen(resp)):
        with pytest.raises(s.AttestationSendError, match="FAILED on-chain"):
            s.submit_and_confirm(_DEVNET, [ix], kp, dry_run=False)
