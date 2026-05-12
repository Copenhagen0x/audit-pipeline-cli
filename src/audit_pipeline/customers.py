"""Multi-tenant customer module — path resolution, registry, derived keys.

Tier 5 #26 (workspace isolation), #27 (CLI surface), #28 (derived signing keys).

The findings DB is intentionally shared across customers — physical isolation
buys nothing here, since access scoping happens at the manifest layer
(`dashboard._build_customer_manifest`). What this module gives us is **logical
isolation** of per-customer artefacts that benefit from being on their own
filesystem path: signing key, public key, manifest output dir, mailbox config.

A registered customer lives in two places:

  * ``<workspace>/customers.json`` — declarative entry (id, name, protocol_name,
    tier, since, target_match, contact_email, signing_pubkey)
  * ``<workspace>/customers/<id>/`` — per-customer dir; today contains keys/
    and (optionally) overrides; meant to grow as more isolated artefacts
    appear (per-customer SMTP creds, custom hyp libraries, etc.)

The derived-key story: every customer gets their own Ed25519 keypair seeded
deterministically from the platform private key + the customer id. This is
NOT a SLIP-0010-style hierarchical derivation — it is HKDF(platform_priv_bytes,
customer_id) → 32 bytes → Ed25519PrivateKey.from_private_bytes. Reproducible,
revocable (rotate by adding a salt), and good enough for per-customer trust
labelling. Insurers / partner protocols who need real on-chain primitives will
get those on the funded-state Anchor registry path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CUSTOMERS_FILE = "customers.json"
CUSTOMERS_DIR = "customers"
KEYS_SUBDIR = "keys"

# Cross-cutting audit Defect 06 (MEDIUM): the CLI's regex required 16+
# chars of ``[A-Za-z0-9_-]`` (URL-enumeration resistance), the library's
# required 2-32 lowercase only — every CLI-validated id with mixed case
# or underscore was rejected downstream. Unify on the broader format
# (the CLI's policy of length is enforced separately at the CLI layer).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}[A-Za-z0-9]$")


class CustomerError(Exception):
    """Raised on registry mutations that violate invariants."""


# ---------------------------------------------------------------------------
# Registry — read / write
# ---------------------------------------------------------------------------


def registry_path(workspace: Path) -> Path:
    """Return the path to the registry file (may not exist yet)."""
    return Path(workspace) / CUSTOMERS_FILE


def load_registry(workspace: Path) -> list[dict[str, Any]]:
    """Read the customers.json registry. Returns an empty list if absent.

    Never includes the hard-coded "demo" customer — that's in dashboard.py's
    fallback list. This function returns ONLY what's been registered via the
    `customer` CLI.
    """
    p = registry_path(workspace)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise CustomerError(f"Could not parse {p}: {e}") from e
    if not isinstance(data, list):
        raise CustomerError(
            f"{p} must contain a JSON array of customer dicts, got {type(data).__name__}"
        )
    return [c for c in data if isinstance(c, dict) and c.get("id")]


def save_registry(workspace: Path, customers: list[dict[str, Any]]) -> None:
    """Atomically write the registry. Sorts by id for stable diffs."""
    p = registry_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = sorted(customers, key=lambda c: c.get("id", ""))
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def get_customer(workspace: Path, customer_id: str) -> dict[str, Any] | None:
    """Return the registry entry for ``customer_id`` or None if not registered."""
    for c in load_registry(workspace):
        if c.get("id") == customer_id:
            return c
    return None


def add_customer(
    workspace: Path,
    customer_id: str,
    name: str,
    protocol_name: str,
    tier: str = "Production",
    target_match: str | None = None,
    contact_email: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Add a customer to the registry. Raises if the id is already taken."""
    validate_customer_id(customer_id)
    customers = load_registry(workspace)
    if any(c.get("id") == customer_id for c in customers):
        raise CustomerError(f"customer id '{customer_id}' already registered")

    entry: dict[str, Any] = {
        "id":            customer_id,
        "name":          name,
        "protocol_name": protocol_name,
        "tier":          tier,
        "target_match":  target_match or customer_id,
    }
    if contact_email:
        entry["contact_email"] = contact_email
    if since:
        entry["since"] = since

    customers.append(entry)
    save_registry(workspace, customers)
    customer_dir(workspace, customer_id).mkdir(parents=True, exist_ok=True)
    return entry


def remove_customer(workspace: Path, customer_id: str) -> dict[str, Any]:
    """Remove a customer from the registry. Returns the removed entry.

    Does NOT delete the customer's per-customer directory under
    ``customers/<id>/`` — that is left on disk so a later restoration can
    recover the keys. Use the CLI's ``--purge`` flag to wipe the dir too.
    """
    customers = load_registry(workspace)
    for i, c in enumerate(customers):
        if c.get("id") == customer_id:
            removed = customers.pop(i)
            save_registry(workspace, customers)
            return removed
    raise CustomerError(f"customer id '{customer_id}' not found")


def validate_customer_id(customer_id: str) -> None:
    """Enforce the id format. Used by add + rotate-key."""
    if not _ID_RE.match(customer_id):
        raise CustomerError(
            f"invalid customer id '{customer_id}': must be 2-32 chars, "
            "lowercase alphanumeric + hyphen, must not start or end with hyphen"
        )


# ---------------------------------------------------------------------------
# Per-customer paths (Tier 5 #26 isolation)
# ---------------------------------------------------------------------------


def customer_dir(workspace: Path, customer_id: str) -> Path:
    """Per-customer directory: ``<workspace>/customers/<id>/``."""
    return Path(workspace) / CUSTOMERS_DIR / customer_id


def customer_keys_dir(workspace: Path, customer_id: str) -> Path:
    """Per-customer keys directory: ``<workspace>/customers/<id>/keys/``."""
    return customer_dir(workspace, customer_id) / KEYS_SUBDIR


def customer_priv_key_path(workspace: Path, customer_id: str) -> Path:
    """Per-customer Ed25519 private key path."""
    return customer_keys_dir(workspace, customer_id) / f"{customer_id}.ed25519"


def customer_pub_key_path(workspace: Path, customer_id: str) -> Path:
    """Per-customer Ed25519 public key path."""
    return customer_keys_dir(workspace, customer_id) / f"{customer_id}.ed25519.pub"


# ---------------------------------------------------------------------------
# Derived signing keys (Tier 5 #28)
# ---------------------------------------------------------------------------

KEY_INFO_PREFIX = b"jelleo-customer-key-v1"
HMAC_URL_INFO_PREFIX = b"jelleo-customer-url-hmac-v1"


# ---------------------------------------------------------------------------
# HMAC-signed URLs (T2/T5 follow-up — customer manifest URL gating)
# ---------------------------------------------------------------------------
# Previously the only thing protecting a customer's confidential manifest
# was the unguessability of their customer_id (enforced via the 16+ char
# rule + nginx token map). That has two weaknesses:
#   1. Logs, referrer headers, screenshots all leak the customer_id, so
#      any party with read access to those gets the manifest URL.
#   2. nginx config changes require redeploy — no per-customer or
#      per-request expiry without infra work.
#
# HMAC URLs close this: the platform deterministically issues a token by
# HMACing (customer_id || expires_at) under a key derived from the
# platform key + the customer_id. Verification is stateless: the server
# (or any consumer) recomputes the HMAC and compares — no shared lookup
# table. Tokens are short-lived (default 7 days) and revocable by
# rotating the customer's salt.


def _hmac_url_key(platform_priv_bytes: bytes, customer_id: str) -> bytes:
    """Derive the HMAC key for a given customer's URL tokens.

    Separate domain (HMAC_URL_INFO_PREFIX) from the signing-key derivation
    so the same platform key produces different bytes for different
    purposes — defeats cross-protocol attacks where a leaked URL token
    could be presented as a customer's Ed25519 signature.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    if len(platform_priv_bytes) != 32:
        raise CustomerError(
            f"platform private key seed must be 32 bytes, got {len(platform_priv_bytes)}"
        )
    info = HMAC_URL_INFO_PREFIX + b":" + customer_id.encode("utf-8")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info)
    return hkdf.derive(platform_priv_bytes)


def issue_customer_url_token(
    platform_priv_bytes: bytes,
    customer_id: str,
    *,
    expires_at: int | None = None,
    ttl_seconds: int = 7 * 24 * 3600,
) -> tuple[str, int]:
    """Issue an HMAC-signed access token for ``customer_id``.

    Returns (token_b64, expires_at_unix). Token is the standard form
    ``<expires_at>.<hmac_b64url>``. Consumers verify with
    ``verify_customer_url_token``.

    The token binds the customer_id + the expiry, so leaking one
    customer's token doesn't grant access to another customer's data,
    and the token expires automatically.
    """
    import base64
    import hmac
    import time
    from hashlib import sha256
    if expires_at is None:
        expires_at = int(time.time()) + int(ttl_seconds)
    key = _hmac_url_key(platform_priv_bytes, customer_id)
    msg = f"{customer_id}|{expires_at}".encode("utf-8")
    digest = hmac.new(key, msg, sha256).digest()
    tok = f"{expires_at}.{base64.urlsafe_b64encode(digest).rstrip(b'=').decode()}"
    return tok, expires_at


def verify_customer_url_token(
    platform_priv_bytes: bytes,
    customer_id: str,
    token: str,
) -> bool:
    """Constant-time verify a customer URL token. Returns True iff valid AND unexpired."""
    import base64
    import hmac
    import time
    from hashlib import sha256
    try:
        exp_str, sig_b64 = token.split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    key = _hmac_url_key(platform_priv_bytes, customer_id)
    msg = f"{customer_id}|{exp}".encode("utf-8")
    expected = hmac.new(key, msg, sha256).digest()
    # Pad sig_b64 to multiple of 4 for urlsafe_b64decode
    pad = "=" * (-len(sig_b64) % 4)
    try:
        provided = base64.urlsafe_b64decode(sig_b64 + pad)
    except Exception:
        return False
    return hmac.compare_digest(provided, expected)


def derive_customer_seed(platform_priv_bytes: bytes, customer_id: str, salt: bytes = b"") -> bytes:
    """HKDF-derive a 32-byte Ed25519 seed from the platform key + customer id.

    Args:
        platform_priv_bytes: raw 32-byte Ed25519 private key (NOT PEM).
        customer_id:         customer id used as info / context for the KDF.
        salt:                optional rotate-key salt. Default empty = stable.

    Returns:
        32 bytes suitable for ``Ed25519PrivateKey.from_private_bytes``.

    Why HKDF: deterministic, well-understood, covers the key + context cleanly.
    Why NOT a hierarchical Ed25519 derivation (SLIP-0010): we don't need on-chain
    composability of these subkeys. The funded-state Anchor registry uses the
    *platform* key, not customer subkeys.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(platform_priv_bytes) != 32:
        raise CustomerError(
            f"platform private key seed must be 32 bytes, got {len(platform_priv_bytes)}"
        )

    info = KEY_INFO_PREFIX + b":" + customer_id.encode("utf-8")
    if salt:
        info = info + b":" + salt

    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt or None, info=info)
    return hkdf.derive(platform_priv_bytes)


def load_platform_priv_seed(platform_priv_path: Path) -> bytes:
    """Load the platform private key (PEM-encoded PKCS8) and return its 32-byte seed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not platform_priv_path.exists():
        raise CustomerError(
            f"platform private key not found at {platform_priv_path}; "
            "run `audit-pipeline sign keygen` first"
        )

    priv = serialization.load_pem_private_key(platform_priv_path.read_bytes(), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise CustomerError(
            f"key at {platform_priv_path} is not Ed25519 (got {type(priv).__name__})"
        )

    return priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def derive_and_persist_customer_keypair(
    workspace: Path,
    customer_id: str,
    platform_priv_path: Path,
    salt: bytes = b"",
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Derive the customer keypair and write it under customers/<id>/keys/.

    Returns (priv_path, pub_path). Raises CustomerError if the key already
    exists and ``overwrite`` is False.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv_path = customer_priv_key_path(workspace, customer_id)
    pub_path = customer_pub_key_path(workspace, customer_id)

    if priv_path.exists() and not overwrite:
        raise CustomerError(
            f"customer key already exists at {priv_path}; "
            "pass overwrite=True (or rotate-key) to replace"
        )

    seed = derive_customer_seed(load_platform_priv_seed(platform_priv_path), customer_id, salt)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key()

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    customer_keys_dir(workspace, customer_id).mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path
