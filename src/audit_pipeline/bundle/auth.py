"""P3 fix-bundle authorization marker.

ENFORCES THE HARD RULE: engine NEVER auto-opens upstream PRs. Only the
operator (Kirill) authorizes, after joint verification with Claude.

The flow:

  1. `bundle review <id>` shows the diff + verification table + Claude's
     written assessment, then asks the operator to type a long-form
     authorization phrase literally:

         yes-authorize-finding-<id>-<patch-sha>

     A typo aborts. y/N is rejected (prevents fat-finger).

  2. On successful typed phrase, this module writes
     `<bundle-dir>/authorization.json` containing:

         {
           "finding_id":     <id>,
           "engine_sha":     <40-hex>,
           "patch_sha":      <40-hex>,
           "authorized_at":  <ISO 8601 UTC>,
           "expires_at":     <ISO 8601 UTC, default +24h>,
           "authorizer":     "<who-typed-it>",
           "verification_digest": <sha256 of verification.json at time of auth>,
           "phrase":         <the literal phrase typed>
         }

  3. `bundle open-pr <id>` calls `validate_authorization()` which refuses
     to fire unless ALL of:

       - authorization.json exists
       - finding_id matches
       - engine_sha matches the current engine_sha
       - patch_sha matches the current patch (file content hash)
       - verification_digest matches current verification.json sha256
       - now() < expires_at

     Any mismatch raises AuthorizationInvalid. Open-pr never fires.

If the patch changes after authorization, the patch_sha mismatch
invalidates the marker and forces re-review. Same for engine_sha —
upgrading the engine invalidates all open authorizations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit_pipeline.bundle.paths import (
    authorization_path,
    patch_path,
    verification_path,
)


class AuthorizationInvalid(Exception):
    """The bundle's authorization marker is missing, expired, or mismatched."""


@dataclass(frozen=True)
class AuthorizationMarker:
    finding_id: int
    engine_sha: str
    patch_sha: str
    authorized_at: str
    expires_at: str
    authorizer: str
    verification_digest: str
    phrase: str

    def to_json(self) -> dict:
        return {
            "finding_id":          self.finding_id,
            "engine_sha":          self.engine_sha,
            "patch_sha":           self.patch_sha,
            "authorized_at":       self.authorized_at,
            "expires_at":          self.expires_at,
            "authorizer":          self.authorizer,
            "verification_digest": self.verification_digest,
            "phrase":              self.phrase,
        }


def expected_phrase(finding_id: int, patch_sha: str) -> str:
    """The exact string the operator must type to authorize."""
    return f"yes-authorize-finding-{finding_id}-{patch_sha[:12]}"


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes; '' if file is missing."""
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def write_authorization(
    workspace: Path,
    finding_id: int,
    engine_sha: str,
    authorizer: str,
    typed_phrase: str,
    ttl_hours: int = 24,
) -> AuthorizationMarker:
    """Validate the typed phrase and write the authorization marker.

    Raises AuthorizationInvalid if:
      - patch.diff is missing (nothing to authorize)
      - verification.json is missing or shows any FAIL
      - typed phrase doesn't match expected literal
    """
    p_path = patch_path(workspace, finding_id)
    if not p_path.is_file():
        raise AuthorizationInvalid(
            f"no patch.diff at {p_path} — run `bundle draft` first"
        )

    v_path = verification_path(workspace, finding_id)
    if not v_path.is_file():
        raise AuthorizationInvalid(
            f"no verification.json at {v_path} — run `bundle verify` first"
        )

    try:
        verification = json.loads(v_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AuthorizationInvalid(f"verification.json unreadable: {e}") from e

    failed = [k for k, v in (verification.get("gates") or {}).items()
              if v.get("passed") is not True]
    if failed:
        raise AuthorizationInvalid(
            f"verification has {len(failed)} failing gate(s): {sorted(failed)}. "
            f"Re-run `bundle verify` first."
        )

    p_sha = file_sha256(p_path)
    expected = expected_phrase(finding_id, p_sha)
    if typed_phrase.strip() != expected:
        raise AuthorizationInvalid(
            f"typed phrase doesn't match. Expected exactly:\n"
            f"    {expected}\n"
            f"got:\n"
            f"    {typed_phrase.strip()!r}"
        )

    now = datetime.now(timezone.utc)
    marker = AuthorizationMarker(
        finding_id=finding_id,
        engine_sha=engine_sha,
        patch_sha=p_sha,
        authorized_at=now.isoformat(timespec="seconds"),
        expires_at=(now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
        authorizer=authorizer,
        verification_digest=file_sha256(v_path),
        phrase=typed_phrase.strip(),
    )

    out = authorization_path(workspace, finding_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(marker.to_json(), indent=2, sort_keys=True),
                   encoding="utf-8")
    return marker


def load_authorization(workspace: Path, finding_id: int) -> AuthorizationMarker:
    """Read the authorization marker. Raises AuthorizationInvalid if missing."""
    path = authorization_path(workspace, finding_id)
    if not path.is_file():
        raise AuthorizationInvalid(
            f"no authorization marker at {path}. "
            f"Run `bundle review {finding_id}` first."
        )
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AuthorizationInvalid(f"authorization.json unreadable: {e}") from e
    return AuthorizationMarker(
        finding_id=int(d["finding_id"]),
        engine_sha=str(d.get("engine_sha", "")),
        patch_sha=str(d.get("patch_sha", "")),
        authorized_at=str(d.get("authorized_at", "")),
        expires_at=str(d.get("expires_at", "")),
        authorizer=str(d.get("authorizer", "")),
        verification_digest=str(d.get("verification_digest", "")),
        phrase=str(d.get("phrase", "")),
    )


def validate_authorization(
    workspace: Path,
    finding_id: int,
    current_engine_sha: str,
) -> AuthorizationMarker:
    """Strict validation gate called by `bundle open-pr`.

    Refuses to return a marker unless ALL of:
      - file exists
      - finding_id matches
      - engine_sha matches
      - patch_sha matches current patch.diff hash
      - verification_digest matches current verification.json hash
      - now() < expires_at

    Any failure raises AuthorizationInvalid with a precise reason.
    """
    marker = load_authorization(workspace, finding_id)

    if marker.finding_id != finding_id:
        raise AuthorizationInvalid(
            f"marker finding_id={marker.finding_id} != requested={finding_id}"
        )

    if marker.engine_sha != current_engine_sha:
        raise AuthorizationInvalid(
            f"engine_sha mismatch: authorized for {marker.engine_sha!r}, "
            f"current is {current_engine_sha!r}. "
            f"Re-review required."
        )

    current_patch_sha = file_sha256(patch_path(workspace, finding_id))
    if marker.patch_sha != current_patch_sha:
        raise AuthorizationInvalid(
            f"patch_sha mismatch: patch was modified after authorization. "
            f"authorized={marker.patch_sha[:12]}, current={current_patch_sha[:12]}. "
            f"Re-review required."
        )

    current_verification_digest = file_sha256(verification_path(workspace, finding_id))
    if marker.verification_digest != current_verification_digest:
        raise AuthorizationInvalid(
            "verification.json changed after authorization. Re-review required."
        )

    try:
        expires = datetime.fromisoformat(marker.expires_at)
    except ValueError as e:
        raise AuthorizationInvalid(f"unparseable expires_at: {e}") from e
    now = datetime.now(timezone.utc)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        raise AuthorizationInvalid(
            f"authorization expired at {marker.expires_at} (now {now.isoformat()}). "
            f"Re-review required."
        )

    return marker
