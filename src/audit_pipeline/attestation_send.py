"""Live on-chain send for `jelleo-attestation` (P4.4) — devnet only, mainnet-BLOCKED.

Signs + submits the program's instructions (`initialize`, `register_protocol`,
`publish_attestation`) using an OPERATOR-SUPPLIED keypair file. The keypair is
loaded from a path the operator passes at call time; this module never embeds,
logs, or transmits secret-key bytes — it only base64s the *signed* transaction
for the RPC.

Hard safety rails:
  * `resolve_cluster` REFUSES mainnet (name or URL) — jelleo-attestation only
    goes to mainnet after the external audit (see build-inventory). Devnet /
    testnet / localnet only.
  * Every send helper defaults to `dry_run=True`: it builds + signs the tx and
    returns the base64 WITHOUT submitting. An actual submit requires an explicit
    `dry_run=False` from the caller.

RPC is plain JSON-RPC over urllib (stdlib) — no solana-py dependency. Tx
building/signing uses solders (already a dependency), imported lazily.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from audit_pipeline.attestation_client import (
    PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    _SEED_CONFIG,
    _SEED_CYCLE,  # noqa: F401  (re-exported for callers deriving the cycle PDA)
    _SEED_LATEST,
    anchor_discriminator,
)

# Cluster name -> RPC URL. Mainnet is intentionally ABSENT and refused below.
_CLUSTERS = {
    "devnet":    "https://api.devnet.solana.com",
    "testnet":   "https://api.testnet.solana.com",
    "localnet":  "http://127.0.0.1:8899",
    "localhost": "http://127.0.0.1:8899",
}

# Genesis hash of mainnet-beta. A string-based cluster guard is defeatable (a
# 127.0.0.1 tunnel, or a URL containing "devnet", that actually routes to
# mainnet), so before any network op we ALSO ask the RPC for its genesis hash
# and refuse if it matches this (threat-modeler P4.4 #1/#4 — the real guard).
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
_MAX_RPC_BYTES = 4_000_000  # cap RPC response read — DoS guard (#8)


class AttestationSendError(Exception):
    """Raised for any cluster/RPC/keypair/submit failure on the live-send path."""


def resolve_cluster(name_or_url: str) -> str:
    """Map a cluster name (devnet/testnet/localnet) or an explicit non-mainnet
    URL to an RPC URL. HARD-REFUSES mainnet in any form (the #1 foot-gun: the
    operator's default `solana config` points at mainnet-beta)."""
    s = (name_or_url or "").strip()
    low = s.lower()
    if "mainnet" in low:
        raise AttestationSendError(
            "mainnet is BLOCKED — jelleo-attestation publishes to mainnet only "
            "after the external audit. Use --cluster devnet."
        )
    if low in _CLUSTERS:
        return _CLUSTERS[low]
    # Accept an explicit localnet / devnet / testnet URL, but never mainnet.
    if low.startswith(("http://127.0.0.1", "http://localhost")):
        return s
    if low.startswith(("http://", "https://")) and ("devnet" in low or "testnet" in low):
        return s
    raise AttestationSendError(
        f"unrecognized cluster {name_or_url!r}; use one of {sorted(_CLUSTERS)} "
        f"(mainnet is intentionally unsupported here)"
    )


def _rpc(url: str, method: str, params: list):
    """Minimal JSON-RPC POST over stdlib urllib. Raises AttestationSendError on
    transport error or an RPC-level `error` object."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read(_MAX_RPC_BYTES).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # Redact any embedded credentials — some RPC providers put an api-key in
        # the URL query string (#10).
        raise AttestationSendError(f"RPC {method} to {url.split('?', 1)[0]} failed: {e}") from e
    if isinstance(resp, dict) and resp.get("error") is not None:
        raise AttestationSendError(f"RPC {method} returned error: {resp['error']}")
    return resp.get("result") if isinstance(resp, dict) else None


def _assert_not_mainnet(url: str) -> str:
    """Network-level mainnet guard: ask the RPC for its genesis hash and REFUSE
    if it is mainnet-beta. This is the trustworthy guard — the string check in
    resolve_cluster is only defense-in-depth (a loopback tunnel or a
    'devnet'-named URL can still point at mainnet)."""
    gh = _rpc(url, "getGenesisHash", [])
    if gh == MAINNET_GENESIS:
        raise AttestationSendError(
            "REFUSING: this RPC endpoint reports the mainnet-beta genesis hash. "
            "jelleo-attestation goes to mainnet only after the external audit."
        )
    if not isinstance(gh, str) or not gh:
        raise AttestationSendError(f"could not verify cluster genesis hash (got {gh!r})")
    return gh


def _solders():
    try:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.keypair import Keypair
        from solders.message import Message  # noqa: F401  (kept for callers)
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise AttestationSendError("solders required for live send: pip install solders") from e
    return Keypair, Pubkey, AccountMeta, Instruction, Hash, Transaction


def load_keypair(path) -> "object":
    """Load the operator's Solana keypair from a JSON file (64-int array) at
    the operator-supplied path. Secret bytes are never logged or returned as
    text — only the live solders Keypair object is returned for signing."""
    Keypair, *_ = _solders()
    p = Path(path)
    if not p.is_file():
        raise AttestationSendError(f"no keypair file at {p}")
    try:
        return Keypair.from_json(p.read_text(encoding="utf-8"))
    except Exception:
        # Suppress the underlying parse error: a malformed file's bytes could
        # echo partial key material into logs (#5). `from None` drops the chain.
        raise AttestationSendError(
            f"could not load keypair '{p.name}': not a valid Solana keypair JSON"
        ) from None


# ─────────────── instruction builders (initialize / register_protocol) ───────────────
# publish_attestation is built by attestation_client.build_instruction.
# Account orders mirror the Rust structs in jelleo-attestation/.../lib.rs EXACTLY.


def build_initialize_ix(payer_b58: str):
    """Initialize struct order: config(init,w), payer(signer,w), system(ro).
    `initialize` forces authority = EXPECTED_AUTHORITY regardless of payer, so
    any funded key may pay."""
    _Kp, Pubkey, AccountMeta, Instruction, _H, _Tx = _solders()
    pid = Pubkey.from_string(PROGRAM_ID)
    config, _ = Pubkey.find_program_address([_SEED_CONFIG], pid)
    metas = [
        AccountMeta(pubkey=config, is_signer=False, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(payer_b58), is_signer=True, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(SYSTEM_PROGRAM_ID), is_signer=False, is_writable=False),
    ]
    return Instruction(program_id=pid, data=anchor_discriminator("initialize"), accounts=metas)


def build_register_protocol_ix(authority_b58: str, protocol: bytes):
    """RegisterProtocol struct order: config(ro,has_one authority), latest(init,w),
    authority(signer,w,payer), system(ro). Must be signed by the authority."""
    _Kp, Pubkey, AccountMeta, Instruction, _H, _Tx = _solders()
    if len(protocol) != 32:
        raise AttestationSendError(f"protocol must be 32 bytes, got {len(protocol)}")
    pid = Pubkey.from_string(PROGRAM_ID)
    config, _ = Pubkey.find_program_address([_SEED_CONFIG], pid)
    latest, _ = Pubkey.find_program_address([_SEED_LATEST, protocol], pid)
    data = anchor_discriminator("register_protocol") + protocol  # borsh Pubkey = 32 raw bytes
    metas = [
        AccountMeta(pubkey=config, is_signer=False, is_writable=False),
        AccountMeta(pubkey=latest, is_signer=False, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(authority_b58), is_signer=True, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(SYSTEM_PROGRAM_ID), is_signer=False, is_writable=False),
    ]
    return Instruction(program_id=pid, data=data, accounts=metas)


def build_signed_tx_b64(url: str, instructions: list, signer_kp) -> tuple[str, int]:
    """Fetch a recent blockhash, build + sign a tx with `signer_kp` as the sole
    fee-payer/signer, and return (base64_tx, byte_len). Does NOT submit."""
    _Kp, _Pk, _AM, _Ix, Hash, Transaction = _solders()
    _assert_not_mainnet(url)  # genesis-hash guard BEFORE we build/sign/submit anything
    bh = _rpc(url, "getLatestBlockhash", [{"commitment": "finalized"}])
    try:
        blockhash = Hash.from_string(bh["value"]["blockhash"])
    except (KeyError, TypeError) as e:
        raise AttestationSendError(f"could not read blockhash from RPC: {e}") from e
    tx = Transaction.new_signed_with_payer(
        list(instructions), signer_kp.pubkey(), [signer_kp], blockhash
    )
    raw = bytes(tx)
    return base64.b64encode(raw).decode("ascii"), len(raw)


def submit_and_confirm(url: str, instructions: list, signer_kp, *, dry_run: bool = True,
                       confirm_timeout_s: int = 60) -> dict:
    """Build + sign the tx. If dry_run (DEFAULT), return the base64 WITHOUT
    submitting. If dry_run=False, submit via sendTransaction and poll until
    confirmed. Returns a result dict either way."""
    tx_b64, size = build_signed_tx_b64(url, instructions, signer_kp)
    if dry_run:
        return {"dry_run": True, "cluster": url, "tx_base64": tx_b64, "size": size}
    sig = _rpc(url, "sendTransaction", [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    if not isinstance(sig, str) or not sig:
        # A null/non-string result means the tx was NOT accepted — say so plainly
        # rather than spin the confirm loop into a misleading "may have landed" (#2).
        raise AttestationSendError(
            f"sendTransaction returned a non-signature result ({sig!r}) — the "
            f"transaction was NOT submitted; safe to retry."
        )
    status = _confirm_signature(url, sig, confirm_timeout_s)
    return {"dry_run": False, "cluster": url, "signature": sig, "status": status}


def _confirm_signature(url: str, sig: str, timeout_s: int) -> dict:
    # Floor the window at 30s — a 1s window false-times-out on devnet (#9).
    eff_timeout = max(30, timeout_s)
    deadline = time.time() + eff_timeout
    while time.time() < deadline:
        # searchTransactionHistory=True so a tx that confirmed then fell out of
        # the live status cache (devnet under load) isn't a false timeout (#6).
        res = _rpc(url, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        st = (res.get("value") or [None])[0] if isinstance(res, dict) else None
        if st:
            if st.get("err") is not None:
                raise AttestationSendError(f"transaction {sig} FAILED on-chain: {st['err']}")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return st
        time.sleep(2)
    raise AttestationSendError(
        f"transaction {sig} not confirmed within {eff_timeout}s (it may still land; "
        f"check the explorer before retrying to avoid a double-send)"
    )
