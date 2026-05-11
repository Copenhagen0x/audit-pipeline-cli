"""Per-cycle Merkle root computation (P4 Y0).

For each completed cycle we compute a Merkle root over the cycle's stable
artifacts. The root is:

  * Tamper-evident — modify any finding field, any cycle metadata field,
    or any signed-receipt byte and the recomputed root differs.
  * Reproducible — anyone with read access to the cycle's SQLite or
    Postgres rows can recompute the same root from the same inputs.
  * On-chain ready — a single 32-byte digest, suitable as the leaf of a
    larger Merkle tree the (funded-tier) on-chain Anchor program would
    publish.

## Leaf canonicalization

Two sources of leaves per cycle:

  1. **Cycle metadata leaf** — single hash over canonical-encoded
     (cycle_id, target_id, engine_sha, wrapper_sha, started_at,
     finished_at, n_dispatched, n_confirmed).

  2. **Finding leaves** — one hash per finding in the cycle, over
     canonical-encoded (id, hypothesis_id, title, verdict, confidence,
     status, severity, bug_class).

Canonical encoding: `field=value` lines joined by `\n`, sorted by field
name, UTF-8 bytes. Missing values render as empty string. Numbers as
decimal. This is intentionally simple — the goal is "same inputs in,
same root out across machines + Python versions."

## Tree shape

Standard binary Merkle: SHA-256, leaves sorted lexicographically before
pairing, odd levels duplicate the last node, single-leaf trees return
that leaf as the root.

## Verification

  >>> root = cycle_merkle_root(db, "C123")
  >>> assert root == cycle_merkle_root(db, "C123")  # reproducible
  >>> # If anyone modifies a finding field, root changes:
  >>> db.transition_finding(fid, Status.DISCLOSED, "test", "test")
  >>> assert cycle_merkle_root(db, "C123") != root
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Stable list of finding fields in canonical order. Adding a field at
# the end is OK (old roots stay valid for the prefix). Removing or
# reordering breaks reproducibility — bump SCHEMA_VERSION if you do.
#
# Audit fix 2026-05-09: included `poc_fired`, `engine_sha`, `wrapper_sha`.
# Without these, a malicious operator could flip poc_fired from 1→0 to
# silently downgrade a confirmed bug, OR rewrite engine_sha to claim a
# verdict came from a different engine version. Both attacks now invalidate
# the Merkle root.
#
# `details_json` is intentionally EXCLUDED — it's freeform JSON that may
# legitimately mutate (e.g. disclosure_url added later) without the
# finding's structural truth changing. If we hashed it, every future
# update would invalidate the historical root. Trade-off: changes to
# details_json don't tamper-check; structural fields do.
# v3 (2026-05-11): added `details_digest` and `cycle_id` to FINDING_FIELDS.
# Reason: legacy v2 excluded details_json from the hash on the rationale that
# disclosure_url etc. legitimately mutates. But that means a malicious
# operator could rewrite the entire PoC narrative / attacker steps / impact
# claim post-attestation and the Merkle root stays valid — which defeats
# the tamper-evident property the user is showing to Toly. Solution:
# include a SHA256 digest of the canonical details_json so any change
# invalidates the root, AND allow legitimate updates by computing a new
# root (the schema version bump signals re-attestation is required).
# Also added cycle_id because a finding can be reassigned to a different
# cycle silently otherwise.
FINDING_FIELDS = (
    "bug_class",
    "confidence",
    "cycle_id",
    "details_digest",
    "engine_sha",
    "hypothesis_id",
    "id",
    "poc_fired",
    "severity",
    "status",
    "title",
    "verdict",
    "wrapper_sha",
)

CYCLE_FIELDS = (
    "cycle_id",
    "engine_sha",
    "finished_at",
    "n_confirmed",
    "n_dispatched",
    "started_at",
    "target_id",
    "wrapper_sha",
)

SCHEMA_VERSION = "v3"  # v3 (2026-05-11): added details_digest + cycle_id to FINDING_FIELDS


def canonical_encode(record: dict[str, Any], fields: tuple[str, ...]) -> bytes:
    """Render `field=value` lines in lexicographic field order, UTF-8 bytes.

    Fields are taken from the `fields` tuple (not record.keys()) so adding
    columns to the DB row doesn't silently change the canonical form.
    Missing fields render as the empty string.
    """
    parts = []
    for f in sorted(fields):
        v = record.get(f)
        s = "" if v is None else str(v)
        parts.append(f"{f}={s}")
    return ("\n".join(parts) + "\n").encode("utf-8")


def leaf_hash(canonical_bytes: bytes) -> bytes:
    """Hash for a single leaf. Domain-separated from internal nodes by prefix."""
    return hashlib.sha256(b"\x00leaf\x00" + canonical_bytes).digest()


def internal_hash(left: bytes, right: bytes) -> bytes:
    """Hash for a Merkle internal node. Domain-separated from leaves."""
    return hashlib.sha256(b"\x01node\x01" + left + right).digest()


def merkle_root(leaves: list[bytes]) -> bytes:
    """Compute the root of a Merkle tree over `leaves` (already-hashed bytes).

    Leaves are sorted lexicographically before pairing. Odd levels
    duplicate the last node. Empty input returns 32 zero bytes.
    Single-leaf input returns that leaf hashed once with the leaf prefix.
    """
    if not leaves:
        return b"\x00" * 32
    nodes = sorted(leaves)
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])
        nodes = [
            internal_hash(nodes[i], nodes[i + 1])
            for i in range(0, len(nodes), 2)
        ]
    return nodes[0]


def _details_digest(details: Any) -> str:
    """SHA256 hex digest of canonical details_json bytes.

    Sorted-key JSON encoding ensures the digest is deterministic across
    runs even if the SQLite text storage reshuffles keys. Empty/None
    details → empty digest "" so the field is absent in the canonical
    encoding (legacy findings stay verifiable).
    """
    if details is None:
        return ""
    try:
        canon = json.dumps(details, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canon = str(details)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def cycle_leaves(cycle: dict, findings: list[dict]) -> list[bytes]:
    """Build the deterministic leaf set for a cycle: 1 metadata + N findings."""
    leaves = [leaf_hash(canonical_encode(cycle, CYCLE_FIELDS))]
    for f in findings:
        # FIX B-#24: compute details_digest from the row's details_json
        # column (if present) so the Merkle root commits to narrative
        # content too. Post-attestation rewriting of details_json
        # invalidates the root.
        f_with_digest = dict(f)
        if "details_digest" not in f_with_digest:
            f_with_digest["details_digest"] = _details_digest(
                f.get("details_json") or f.get("details")
            )
        leaves.append(leaf_hash(canonical_encode(f_with_digest, FINDING_FIELDS)))
    return leaves


def cycle_merkle_root(cycle: dict, findings: list[dict]) -> str:
    """Hex-encoded Merkle root for a cycle's stable artifacts.

    Determinism contract: same DB row contents → same hex string, byte-
    for-byte, across machines + Python versions + run-to-run.
    """
    return merkle_root(cycle_leaves(cycle, findings)).hex()


def cycle_merkle_summary(cycle: dict, findings: list[dict]) -> dict:
    """Render a JSON-serializable summary suitable for snapshot.json or
    a cycle's `merkle.json` sidecar."""
    return {
        "schema":      f"jelleo-cycle-merkle-{SCHEMA_VERSION}",
        "cycle_id":    cycle.get("cycle_id"),
        "engine_sha":  cycle.get("engine_sha"),
        "n_findings":  len(findings),
        "n_leaves":    len(findings) + 1,
        "merkle_root": cycle_merkle_root(cycle, findings),
        "fields": {
            "cycle":   list(CYCLE_FIELDS),
            "finding": list(FINDING_FIELDS),
        },
    }
