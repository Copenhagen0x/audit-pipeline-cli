"""Off-chain client for the `jelleo-attestation` on-chain program (P4.3).

Turns a **verified** ``merkle.json`` sidecar into a ready-to-send
``publish_attestation`` instruction for the Anchor program at
``programs/jelleo-attestation/src/lib.rs``.

Trust model: this module does NOT verify signatures — the CALLER
(`merkle publish-onchain`) must call ``sign.verify_signature(..., expect_domain=
"merkle")`` FIRST and only hand a sidecar here once the Ed25519 signature over
the exact bytes has been confirmed. The on-chain ``merkle_root`` is the bytes
from the SIGNED sidecar — never recomputed (a recompute would let a DB drift
silently change what gets attested).

The constants here MIRROR the Rust program; if the program changes any of
PROGRAM_ID / EXPECTED_AUTHORITY / the seeds / the arg order, update both sides.

`solders` is imported LAZILY — the constants, the Anchor discriminator, the
borsh arg-encoding and all validation are pure-Python and unit-testable without
solders; only base58 pubkey parsing, PDA derivation and Instruction assembly
need it.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

# ── Program constants — MUST match programs/jelleo-attestation/src/lib.rs ──
PROGRAM_ID = "72TF95FUNttvDDsQSFzWEqY7Vu6Xm5h81fFNgoeRYPTk"
# The only key the program will accept as authority (forced in `initialize`).
# Operator-held secret, off-repo (rotated 2026-05-28 from a retired stray key).
EXPECTED_AUTHORITY = "CLvf1DNy6argHzTcQQgvP2KyxLHhdRd2H6R8CLcNAzqL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

# Mirrors the Rust `require!(... <= MAX_*_LEN)` guards — we reject client-side
# with an actionable error rather than burning a failed transaction on-chain.
MAX_CYCLE_ID_LEN = 32
MAX_ENGINE_SHA_LEN = 64

# The merkle.json schema this publisher understands. A schema bump (v4→v5)
# changes the canonical leaf encoding, so refuse to publish an unrecognized
# schema rather than attest bytes we can't interpret.
ATTESTATION_MERKLE_SCHEMA = "jelleo-cycle-merkle-v4"

# Anchor PDA seeds (byte-for-byte the Rust `seeds = [...]`).
_SEED_CONFIG = b"config"
_SEED_CYCLE = b"cycle"
_SEED_LATEST = b"latest"


class AttestationError(Exception):
    """Raised when a merkle.json can't be turned into a SAFE publish ix."""


def anchor_discriminator(ix_name: str) -> bytes:
    """8-byte Anchor instruction discriminator = sha256("global:<name>")[:8].

    Same construction the litesvm harness uses (sha256 of "global:<ix>"),
    which is how Anchor routes instructions.
    """
    return hashlib.sha256(f"global:{ix_name}".encode("utf-8")).digest()[:8]


def _borsh_string(s: str) -> bytes:
    """Borsh string = u32-LE byte-length prefix + UTF-8 bytes."""
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def _merkle_root_hex_to_bytes(root_hex: str) -> bytes:
    """A merkle.json root is a 64-char hex sha256 digest → 32 raw bytes."""
    if not isinstance(root_hex, str) or len(root_hex) != 64:
        raise AttestationError(
            f"merkle_root must be 64 hex chars (32 bytes); got "
            f"{len(root_hex) if isinstance(root_hex, str) else type(root_hex).__name__}"
        )
    try:
        b = bytes.fromhex(root_hex)
    except ValueError as e:
        raise AttestationError(f"merkle_root is not valid hex: {e}") from e
    if len(b) != 32:
        raise AttestationError(f"merkle_root decoded to {len(b)} bytes, expected 32")
    return b


@dataclass(frozen=True)
class PublishArgs:
    """The 5 args of `publish_attestation`, already extracted + bound to a
    verified sidecar. ``protocol`` is raw 32 bytes (base58 parsed by the
    caller); ``merkle_root`` is raw 32 bytes."""

    protocol: bytes        # 32-byte audited-program pubkey
    cycle_id: str
    engine_sha: str
    invariant_count: int
    merkle_root: bytes     # exactly 32 bytes

    def validate(self) -> None:
        """Mirror the on-chain require!()s so we fail BEFORE spending a tx."""
        if len(self.protocol) != 32:
            raise AttestationError(f"protocol must be 32 bytes, got {len(self.protocol)}")
        if len(self.merkle_root) != 32:
            raise AttestationError(f"merkle_root must be 32 bytes, got {len(self.merkle_root)}")
        # Encode the string fields ONCE here — this is the single choke point
        # where cycle_id/engine_sha become utf-8 bytes (the borsh wire-encoding
        # uses utf-8 too). A lone-surrogate / un-encodable str (constructible as
        # valid JSON via \uXXXX escapes) must surface as AttestationError, not a
        # raw UnicodeEncodeError that escapes the caller's except-block as a
        # traceback (paranoid-goober P4.3 r3).
        try:
            cid_len = len(self.cycle_id.encode("utf-8"))
            esha_len = len(self.engine_sha.encode("utf-8"))
        except UnicodeEncodeError as e:
            raise AttestationError(
                f"cycle_id / engine_sha contains un-encodable characters "
                f"(e.g. a lone surrogate): {e}"
            )
        if cid_len > MAX_CYCLE_ID_LEN:
            raise AttestationError(
                f"cycle_id exceeds {MAX_CYCLE_ID_LEN} bytes (on-chain CycleIdTooLong)"
            )
        if not self.engine_sha:
            raise AttestationError("engine_sha is empty — refusing to attest an unidentified engine")
        if esha_len > MAX_ENGINE_SHA_LEN:
            raise AttestationError(
                f"engine_sha exceeds {MAX_ENGINE_SHA_LEN} bytes (on-chain EngineShaTooLong)"
            )
        if not (0 <= self.invariant_count <= 0xFFFFFFFF):
            raise AttestationError(f"invariant_count {self.invariant_count} out of u32 range")

    def instruction_data(self) -> bytes:
        """Discriminator + borsh(protocol, cycle_id, engine_sha,
        invariant_count, merkle_root) — the exact `data` field of the ix."""
        self.validate()
        return (
            anchor_discriminator("publish_attestation")
            + self.protocol
            + _borsh_string(self.cycle_id)
            + _borsh_string(self.engine_sha)
            + struct.pack("<I", self.invariant_count)
            + self.merkle_root
        )


def from_merkle_json(merkle: dict, expect_cycle_id: str) -> PublishArgs:
    """Build PublishArgs from a VERIFIED merkle.json dict.

    The ``protocol`` is read from the SIGNED sidecar itself — never from an
    operator-supplied flag (P4.4 precondition: closes the confused-deputy where
    a wrong --protocol at publish time could attest a real cycle against a
    protocol we never audited). A sidecar with no ``protocol`` field cannot be
    published on-chain — recompute it with `merkle compute --protocol <pk>` so
    the binding is signature-covered.

    Cross-checks the sidecar's internal ``cycle_id`` against the cycle the
    operator named (``expect_cycle_id``) — the .sig binds the FILENAME, not the
    cycle_id, so this closes a swapped-content attestation. Refuses an
    unrecognized merkle schema (we won't attest bytes we can't interpret).
    """
    # Reject non-dict JSON FIRST (paranoid-goober P4.3 r3 HIGH): a signed blob
    # that is valid JSON but a list/str/int/null (e.g. `[1,2,3]`, `null`) would
    # otherwise reach `merkle.get(...)` and raise a raw AttributeError, escaping
    # the caller's `except AttestationError` as an ugly traceback.
    if not isinstance(merkle, dict):
        raise AttestationError(
            f"merkle.json must be a JSON object, got {type(merkle).__name__}"
        )
    schema = merkle.get("schema")
    if schema != ATTESTATION_MERKLE_SCHEMA:
        raise AttestationError(
            f"merkle.json schema is {schema!r}, expected {ATTESTATION_MERKLE_SCHEMA!r} "
            f"— refusing to publish an unrecognized schema"
        )
    cycle_id = merkle.get("cycle_id")
    if not isinstance(cycle_id, str) or not cycle_id:
        raise AttestationError(f"merkle.json cycle_id is {cycle_id!r}; expected a non-empty string")
    if cycle_id != expect_cycle_id:
        raise AttestationError(
            f"merkle.json cycle_id is {cycle_id!r} but you asked to publish "
            f"{expect_cycle_id!r} — refusing (content/path mismatch)"
        )
    engine_sha = merkle.get("engine_sha")
    if not isinstance(engine_sha, str) or not engine_sha:
        raise AttestationError(f"merkle.json engine_sha is {engine_sha!r}; expected a non-empty string")
    # protocol comes ONLY from the signed sidecar — never an operator flag.
    protocol_b58 = merkle.get("protocol")
    if not isinstance(protocol_b58, str) or not protocol_b58:
        raise AttestationError(
            "merkle.json has no 'protocol' field — recompute with "
            "`merkle compute --protocol <pubkey>` so the audited protocol is "
            "signature-bound before publishing on-chain"
        )
    protocol = parse_pubkey(protocol_b58)  # base58 → 32 bytes (raises AttestationError)
    n_findings = merkle.get("n_findings")
    # bool is a subclass of int in Python — exclude it explicitly so a JSON
    # `true`/`false` can't pose as invariant_count 1/0 (paranoid-goober P4.3 #2).
    if isinstance(n_findings, bool) or not isinstance(n_findings, int) or n_findings < 0:
        raise AttestationError(f"merkle.json n_findings is {n_findings!r}; expected a non-negative int")
    root_bytes = _merkle_root_hex_to_bytes(merkle.get("merkle_root", ""))
    args = PublishArgs(
        protocol=protocol,
        cycle_id=cycle_id,
        engine_sha=engine_sha,
        invariant_count=n_findings,  # # of per-finding receipts attested
        merkle_root=root_bytes,
    )
    args.validate()
    return args


# ───────────────────── solders-backed helpers (lazy) ─────────────────────


def _solders():
    """Import solders lazily; one clear error if it's not installed."""
    try:
        from solders.instruction import AccountMeta, Instruction
        from solders.pubkey import Pubkey
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise AttestationError(
            "solders is required for pubkey parsing / PDA derivation / "
            "instruction build. Install it: pip install solders"
        ) from e
    return Pubkey, Instruction, AccountMeta


def parse_pubkey(b58: str) -> bytes:
    """base58 pubkey string → 32 raw bytes (via solders)."""
    Pubkey, _, _ = _solders()
    try:
        return bytes(Pubkey.from_string(b58))
    except Exception as e:
        raise AttestationError(f"invalid base58 pubkey {b58!r}: {e}") from e


def derive_pdas(protocol: bytes, cycle_id: str) -> dict[str, str]:
    """Derive the 3 PDAs the publish ix touches, as base58 strings.

    Seeds mirror the Rust program exactly:
      config = ["config"], latest = ["latest", protocol],
      cycle  = ["cycle", protocol, cycle_id_utf8]
    """
    Pubkey, _, _ = _solders()
    pid = Pubkey.from_string(PROGRAM_ID)
    config, _ = Pubkey.find_program_address([_SEED_CONFIG], pid)
    latest, _ = Pubkey.find_program_address([_SEED_LATEST, protocol], pid)
    cycle, _ = Pubkey.find_program_address(
        [_SEED_CYCLE, protocol, cycle_id.encode("utf-8")], pid
    )
    return {"config": str(config), "latest": str(latest), "cycle": str(cycle)}


def build_instruction(args: PublishArgs, authority: str):
    """Assemble the solders Instruction. Account order MIRRORS the Rust
    PublishAttestation struct: config(ro), cycle(w), latest(w), authority
    (signer+w), system_program(ro). Returns a solders Instruction."""
    Pubkey, Instruction, AccountMeta = _solders()
    pid = Pubkey.from_string(PROGRAM_ID)
    authority_pk = Pubkey.from_string(authority)
    config, _ = Pubkey.find_program_address([_SEED_CONFIG], pid)
    latest, _ = Pubkey.find_program_address([_SEED_LATEST, args.protocol], pid)
    cycle, _ = Pubkey.find_program_address(
        [_SEED_CYCLE, args.protocol, args.cycle_id.encode("utf-8")], pid
    )
    metas = [
        AccountMeta(pubkey=config, is_signer=False, is_writable=False),
        AccountMeta(pubkey=cycle, is_signer=False, is_writable=True),
        AccountMeta(pubkey=latest, is_signer=False, is_writable=True),
        AccountMeta(pubkey=authority_pk, is_signer=True, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(SYSTEM_PROGRAM_ID), is_signer=False, is_writable=False),
    ]
    return Instruction(program_id=pid, data=args.instruction_data(), accounts=metas)
