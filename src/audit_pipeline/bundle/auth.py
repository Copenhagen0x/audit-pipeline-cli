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
           "engine_sha":     <40-hex git SHA>,
           "patch_sha":      <64-hex SHA-256 of patch.diff>,
           "authorized_at":  <ISO 8601 UTC>,
           "expires_at":     <ISO 8601 UTC, default +24h>,
           "authorizer":     "<who-typed-it>",
           "verification_digest": <64-hex SHA-256 of verification.json at time of auth>,
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
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit_pipeline.bundle.paths import (
    authorization_path,
    patch_path,
    verification_path,
)


# Patch #3 round-1 fix (audit MED 540763a6): cap operator-supplied TTL.
# Previously ttl_hours was unbounded so an operator could authorize for
# years and never re-review. Cap at one week (168h). Lower bound is a
# strict positive — TTL=0 would authorize and immediately expire, a
# foot-gun that masquerades as auth-without-actually-authorizing.
_MAX_TTL_HOURS = 168  # 1 week
_MIN_TTL_HOURS = 1


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
    """The exact string the operator must type to authorize.

    FIX B-#17: phrase now binds the FULL 64-char patch_sha instead of just
    12 chars. That's 256 bits of entropy in the authorization phrase,
    closing the 48-bit collision window for substitution attacks.
    """
    return f"yes-authorize-finding-{finding_id}-{patch_sha}"


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
      - verification.json is missing or shows any FAIL / unrecognised skip
      - typed phrase doesn't match expected literal
      - ttl_hours is outside [_MIN_TTL_HOURS, _MAX_TTL_HOURS]
    """
    # P3+P4 audit Defect 03 (HIGH): previously an empty engine_sha was
    # silently accepted, then validate_authorization compared "" == ""
    # at PR-open time and the gate passed trivially. Force a 40-hex
    # check on the way IN to the marker.
    import re as _re_auth
    if not _re_auth.fullmatch(r"[0-9a-fA-F]{40}", engine_sha or ""):
        raise AuthorizationInvalid(
            f"engine_sha must be a 40-character git hash; got "
            f"{(engine_sha or '')!r} (length {len(engine_sha or '')}). "
            f"Resolve via `git rev-parse HEAD` before authorizing."
        )

    # Patch #3 round-1 fix (audit MED 540763a6): TTL bounds check.
    # Round-2 (code-reviewer #7 + threat-modeler #12): bool is a subclass
    # of int in Python; `isinstance(True, int) == True`. Without an
    # explicit bool guard, ttl_hours=True silently authorized for 1 hour
    # (True==1) and ttl_hours=False fell through to the `< _MIN_TTL_HOURS`
    # check (which catches False==0). Reject bool explicitly so a
    # JSON-deserialized config that passes a bool can't slip past.
    if isinstance(ttl_hours, bool) \
            or not isinstance(ttl_hours, int) \
            or ttl_hours < _MIN_TTL_HOURS \
            or ttl_hours > _MAX_TTL_HOURS:
        raise AuthorizationInvalid(
            f"ttl_hours={ttl_hours!r} outside allowed range "
            f"[{_MIN_TTL_HOURS}, {_MAX_TTL_HOURS}]. Bundles cannot be "
            f"authorized for longer than {_MAX_TTL_HOURS}h (1 week). "
            f"(Bool not accepted — pass an explicit int.)"
        )

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

    # FIX B-#15: require ALL expected gate keys present AND each passed.
    # Previously a verification.json with empty `gates: {}` slipped past
    # because list-comprehension found no failures. Empty dict → empty
    # failure list → authorization succeeded with zero verified gates.
    REQUIRED_GATES = (
        "patch_well_formed",
        "poc_fails_pre_patch",
        "poc_passes_post_patch",
        "tests_pass_post_patch",
        # kani_proof_holds is SKIP-able (None) so we don't require it
        # to be True — but it MUST be present in the dict (proves the
        # gate actually ran rather than being silently omitted).
        # Patch #3 round-1: added the new patch_unchanged_during_verify
        # race-check gate to the required-present list.
        "patch_unchanged_during_verify",
    )
    gates = verification.get("gates") or {}
    missing = [k for k in REQUIRED_GATES if k not in gates]
    if missing:
        raise AuthorizationInvalid(
            f"verification.json missing required gates: {missing}. "
            f"Re-run `bundle verify` to produce the full gate set."
        )
    failed = [k for k in REQUIRED_GATES
              if gates.get(k, {}).get("passed") is not True]
    if failed:
        raise AuthorizationInvalid(
            f"verification has {len(failed)} failing gate(s): {sorted(failed)}. "
            f"Re-run `bundle verify` first."
        )

    # Patch #3 round-1 fix (audit CRITICAL 31fd96d0 + 5dcef169): defence
    # in depth — call `all_passed()` so the API path (this function) and
    # the UI path (review_cmd) enforce IDENTICAL semantics. Previously
    # review_cmd called all_passed() but write_authorization rolled its
    # own per-key check with `is not True`, so a crafted verification.json
    # could pass write_authorization while failing all_passed (or vice
    # versa). Lazy import avoids a verifier ↔ auth cycle.
    from audit_pipeline.bundle.verifier import all_passed
    if not all_passed(verification):
        raise AuthorizationInvalid(
            "verification.json gates do not all pass per all_passed() — "
            "re-run `bundle verify` first. (defence-in-depth check; "
            "REQUIRED_GATES per-key validation already passed, so this "
            "indicates an extra optional gate (kani / litesvm) failed or "
            "has an unrecognised skip_reason_code.)"
        )

    # Patch #3 round-6 fix (threat-modeler round-5 #1): cross-check
    # toolchain-absence skip codes against the AUTHORITATIVE
    # `effective_*` fields in verification.json. An attacker who forges
    # verification.json could otherwise claim
    # `no_kani_harness_registered` for a bug class that DOES have a
    # registered harness — the harness would have caught the bad patch
    # but never ran.
    #
    # Patch #3 round-7 fix (threat-modeler round-6 #2): the round-6
    # check read from meta.json, which is mutable between verify and
    # review. An attacker could clear meta.json's kani_harness field
    # AFTER verify ran with the real harness, bypassing the cross-check.
    # Round-7: source the authoritative values from verification.json's
    # `effective_kani_harness` and `effective_litesvm_test_name` fields
    # (snapshotted by run_all_gates at gate-run time and protected by
    # the verification_digest binding in the authorization marker).
    # Patch #3 round-8 fix (devils-advocate round-7 #1): also consult
    # meta.json as a fallback. Round-7 read ONLY from verification.json's
    # effective_* fields — but a competent attacker can simply set those
    # fields to null in their forged verification.json, defeating the
    # cross-check. With round-8, we additionally check meta.json's
    # kani_harness / litesvm_test_name fields and BLOCK if EITHER source
    # claims a real toolchain identifier was registered while the gate
    # skipped with the "not registered" code.
    effective_kani = verification.get("effective_kani_harness")
    effective_litesvm = verification.get("effective_litesvm_test_name")
    meta_kani: str | None = None
    meta_litesvm: str | None = None
    try:
        from audit_pipeline.bundle.paths import meta_path as _mp_r8
        _m_path_r8 = _mp_r8(workspace, finding_id)
        if _m_path_r8.is_file():
            _meta_r8 = json.loads(_m_path_r8.read_text(encoding="utf-8"))
            meta_kani = _meta_r8.get("kani_harness")
            meta_litesvm = _meta_r8.get("litesvm_test_name")
    except (json.JSONDecodeError, OSError):
        pass
    for _gname, _g in (verification.get("gates") or {}).items():
        if _g.get("passed") is not None:
            continue
        _code = _g.get("skip_reason_code")
        if _code == "no_kani_harness_registered" and (effective_kani or meta_kani):
            raise AuthorizationInvalid(
                f"verification.json gate {_gname!r} claims "
                f"no_kani_harness_registered but a harness is registered "
                f"(effective_kani_harness={effective_kani!r}, "
                f"meta.json kani_harness={meta_kani!r}). Crafted "
                f"verification — re-run `bundle verify`."
            )
        if _code == "no_litesvm_test_name_registered" \
                and (effective_litesvm or meta_litesvm):
            raise AuthorizationInvalid(
                f"verification.json gate {_gname!r} claims "
                f"no_litesvm_test_name_registered but a litesvm test "
                f"is registered (effective_litesvm_test_name="
                f"{effective_litesvm!r}, meta.json litesvm_test_name="
                f"{meta_litesvm!r}). Crafted verification — re-run "
                f"`bundle verify`."
            )

    p_sha = file_sha256(p_path)
    expected = expected_phrase(finding_id, p_sha)
    # FIX B-#16: constant-time comparison to avoid timing side-channels.
    if not hmac.compare_digest(typed_phrase.strip(), expected):
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
    # Patch #3 round-1 fix (audit MED f8fe4ebf + HIGH a780c24c): atomic
    # write. Previously a crash mid-`write_text` left a corrupt JSON
    # marker that `load_authorization` couldn't parse — AND if the digest
    # was computed against the partial write, a subsequent successful
    # write would have a digest that no longer matched. tmp + os.replace
    # is the standard POSIX atomic write pattern (POSIX REQUIRES rename
    # over an existing dest to be atomic; Windows ReplaceFileW provides
    # the same for NTFS).
    #
    # Patch #3 round-6 fix (threat-modeler round-5 #2): use
    # tempfile.NamedTemporaryFile with delete=False instead of a fixed
    # `<out>.tmp` name. Otherwise an attacker who pre-creates that path
    # as a symlink to /tmp/attacker_log would have the marker JSON
    # (including operator phrase, timestamps) written into their log
    # before os.replace moves the canonical name into place.
    import tempfile as _tempfile_at
    tmp_fd, tmp_name = _tempfile_at.mkstemp(
        prefix=out.name + ".", suffix=".tmp", dir=str(out.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as _tmp_fh:
            _tmp_fh.write(json.dumps(marker.to_json(), indent=2, sort_keys=True))
        os.replace(tmp_name, str(out))
    except Exception:
        # If anything goes wrong, clean up the temp file.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # Patch #3 round-1 fix (audit HIGH 2984f0e7): authorization.json is
    # now signed as a tamper-evidence sidecar. validate_authorization()
    # verifies the .sig.status sidecar when present (round-3 wiring —
    # see validate_authorization below).
    #
    # Patch #3 round-3 fix (devils-advocate #2): use the SAME key
    # filename `jelleo.ed25519` that `audit-pipeline sign keygen` produces
    # (sign.py:158). Round-2 mistakenly looked for `jelleo.ed25519.priv`
    # which doesn't exist anywhere in the codebase — every authorization
    # was silently UNSIGNED.
    #
    # Patch #3 round-3 fix (threat-modeler #3): the symlink check is
    # now UNCONDITIONAL — round-2's `if key.is_symlink()` only caught
    # symlinks on the LEAF file. If `workspace/keys/` is itself a symlink
    # to /etc/, key.resolve() still escapes but key.is_symlink() is
    # False (the file at the resolved path isn't a symlink). Resolve
    # always and check the result is under the realpath of workspace/keys/.
    #
    # Patch #3 round-3 fix (threat-modeler #9): atomic write of the
    # sidecar status file (tmp + os.replace) so a crash mid-write
    # doesn't leave a partial .sig.status that a hardened
    # validate_authorization would treat as corrupt.
    sidecar_status_path = out.with_suffix(out.suffix + ".sig.status")

    def _write_sidecar_status_atomic(payload: dict) -> None:
        """Tmp + rename so the .sig.status file is either present-and-
        complete or absent — never partial.

        Patch #3 round-6 fix (threat-modeler round-5 #2): use tempfile
        for the tmp name so attacker can't pre-create a predictable
        `.tmp` symlink to exfiltrate the sidecar contents."""
        import tempfile as _tempfile_st
        sd_parent = sidecar_status_path.parent
        sd_parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = _tempfile_st.mkstemp(
            prefix=sidecar_status_path.name + ".",
            suffix=".tmp",
            dir=str(sd_parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as _fh:
                _fh.write(json.dumps(payload))
            os.replace(tmp_name, str(sidecar_status_path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    try:
        from audit_pipeline.commands.sign import sign_file
        key = workspace / "keys" / "jelleo.ed25519"
        keys_root = (workspace / "keys").resolve()
        # UNCONDITIONAL resolve + jail check. Catches:
        #   (a) leaf symlink (jelleo.ed25519 → /root/.ssh/id_ed25519)
        #   (b) parent symlink (workspace/keys → /etc/)
        #   (c) any combination thereof
        #
        # Patch #3 round-4 fix (threat-modeler #7): broken symlinks
        # (jelleo.ed25519 → /nonexistent/path) caused `key.exists()` to
        # return False, which skipped the entire symlink check and
        # silently emitted UNSIGNED — misleading the operator (looked
        # like "no key" when it was actually "broken symlink"). Now
        # explicitly catch the broken-symlink case BEFORE the exists()
        # branch so it becomes REFUSED.
        if key.is_symlink() and not key.exists():
            _write_sidecar_status_atomic({
                "status": "REFUSED",
                "reason": "key path is a broken symlink — refusing to "
                          "ignore (operator should investigate)",
            })
            return marker
        if key.exists():
            try:
                resolved = key.resolve(strict=True)
                resolved.relative_to(keys_root)
            except (ValueError, OSError):
                _write_sidecar_status_atomic({
                    "status": "REFUSED",
                    "reason": "key path escapes workspace/keys/ via "
                              "symlink or path-resolution",
                })
                return marker
        if key.is_file():
            sign_file(out, key_path=key, domain="authorization")
            _write_sidecar_status_atomic({"status": "SIGNED"})
        else:
            _write_sidecar_status_atomic({
                "status": "UNSIGNED",
                "reason": "no key at expected path",
            })
    except Exception as _e_sign:
        # Sign-time failure with the key present is loud — record reason
        # so validate_authorization can refuse this marker in hardened
        # mode (and so the operator notices the WARNING printed below).
        try:
            _write_sidecar_status_atomic({
                "status": "FAILED",
                "reason": str(_e_sign)[:200],
            })
        except Exception:
            pass
        # Also write to stderr so an interactive operator sees the
        # failure rather than discovering it later via the sidecar file.
        try:
            import sys as _sys
            _sys.stderr.write(
                f"[bundle-auth WARNING] authorization signing failed: "
                f"{_e_sign}. Marker written UNSIGNED at {out}. "
                f"Hardened deploys may reject this bundle.\n"
            )
        except Exception:
            pass

    return marker


def load_authorization(workspace: Path, finding_id: int) -> AuthorizationMarker:
    """Read the authorization marker. Raises AuthorizationInvalid if missing
    or if any cryptographic field is malformed (engine_sha not 40-hex,
    patch_sha not 64-hex, verification_digest not 64-hex)."""
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

    # Patch #3 round-1 fix (audit HIGH 1570b9e8): re-validate field formats
    # on the way OUT of the JSON. write_authorization() validates engine_sha
    # at write time, but a tampered-on-disk authorization.json could have
    # bogus values that validate_authorization() then trusted because the
    # 40-hex check ran ONLY at write time. Pull the checks into load so
    # every reader gets the guard for free.
    import re as _re_load
    engine_sha = str(d.get("engine_sha", ""))
    patch_sha = str(d.get("patch_sha", ""))
    verification_digest = str(d.get("verification_digest", ""))
    if not _re_load.fullmatch(r"[0-9a-fA-F]{40}", engine_sha):
        raise AuthorizationInvalid(
            f"authorization.json has malformed engine_sha "
            f"{engine_sha!r} (expected 40-hex). File may have been tampered."
        )
    if not _re_load.fullmatch(r"[0-9a-fA-F]{64}", patch_sha):
        raise AuthorizationInvalid(
            f"authorization.json has malformed patch_sha "
            f"{patch_sha!r} (expected 64-hex). File may have been tampered."
        )
    if not _re_load.fullmatch(r"[0-9a-fA-F]{64}", verification_digest):
        raise AuthorizationInvalid(
            f"authorization.json has malformed verification_digest "
            f"{verification_digest!r} (expected 64-hex). "
            f"File may have been tampered."
        )

    return AuthorizationMarker(
        finding_id=int(d["finding_id"]),
        engine_sha=engine_sha,
        patch_sha=patch_sha,
        authorized_at=str(d.get("authorized_at", "")),
        expires_at=str(d.get("expires_at", "")),
        authorizer=str(d.get("authorizer", "")),
        verification_digest=verification_digest,
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

    # P3+P4 audit Defect 03 (HIGH) cont.: belt + suspenders on validate.
    # Reject if EITHER side is empty/short — the constant-time match in
    # ``hmac.compare_digest`` would otherwise return True for ``"" == ""``.
    import re as _re_auth2
    if (
        not _re_auth2.fullmatch(r"[0-9a-fA-F]{40}", marker.engine_sha or "")
        or not _re_auth2.fullmatch(r"[0-9a-fA-F]{40}", current_engine_sha or "")
    ):
        raise AuthorizationInvalid(
            f"engine_sha must be a 40-hex git hash on BOTH sides; got "
            f"marker={marker.engine_sha!r}, current={current_engine_sha!r}."
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

    # Patch #3 round-1 fix (audit HIGH a834b8f4 + d5bae828): the
    # verification_digest check ABOVE proves the file wasn't modified
    # SINCE authorization, but it doesn't prove the gate semantics
    # were ever valid. If verification.json was crafted with all-passes
    # (e.g., attacker had write access to the bundle dir before review),
    # digest match alone trusts attacker-controlled state. Re-call
    # all_passed() on the bound content to make the gate-pass invariant
    # part of the open-pr precondition, not just an at-auth-time check.
    try:
        verification = json.loads(
            verification_path(workspace, finding_id).read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as e:
        raise AuthorizationInvalid(
            f"verification.json unreadable at validate: {e}"
        ) from e
    # Lazy import — avoid the verifier ↔ auth import cycle.
    from audit_pipeline.bundle.verifier import all_passed
    if not all_passed(verification):
        raise AuthorizationInvalid(
            "verification.json gates do not all pass — re-run "
            "`bundle verify` AND re-review. The previous "
            "verification_digest matched but the gates themselves "
            "now fail (either gate semantics changed, or the original "
            "verification was crafted with weak gates)."
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

    # Patch #3 round-3 fix (code-reviewer #2 + devils-advocate #3 +
    # threat-modeler #1): wire the .sig.status sidecar that
    # write_authorization() produces into the validate path. Without
    # this, the entire round-2 signing block was theater — sidecar
    # written but never consulted. Now hardened deploys can set
    # JELLEO_AUTHZ_REQUIRE_SIGNED=1 to make UNSIGNED markers a hard
    # reject; FAILED/REFUSED are always hard rejects regardless.
    auth_path = authorization_path(workspace, finding_id)
    sidecar_status_path = auth_path.with_suffix(auth_path.suffix + ".sig.status")
    require_signed = os.environ.get("JELLEO_AUTHZ_REQUIRE_SIGNED") == "1"
    if sidecar_status_path.is_file():
        try:
            sidecar = json.loads(sidecar_status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise AuthorizationInvalid(
                f"authorization.json.sig.status unreadable: {e}. "
                f"File may be corrupted or attacker-tampered."
            ) from e
        status = sidecar.get("status")
        # FAILED / REFUSED are ALWAYS hard rejects — signing infra
        # attempted to run and produced a known-bad result.
        if status in ("FAILED", "REFUSED"):
            raise AuthorizationInvalid(
                f"authorization sidecar status is {status!r}: "
                f"{sidecar.get('reason', '<no reason>')}. "
                f"Signing infrastructure produced a known-bad result; "
                f"refusing to validate the marker."
            )
        if status not in ("SIGNED", "UNSIGNED"):
            raise AuthorizationInvalid(
                f"authorization sidecar status {status!r} is not "
                f"recognised (expected one of: SIGNED, UNSIGNED, "
                f"FAILED, REFUSED). Tampered sidecar?"
            )
        # UNSIGNED is rejected only if the operator opted into hardened mode.
        if status == "UNSIGNED" and require_signed:
            raise AuthorizationInvalid(
                "authorization sidecar status is UNSIGNED but "
                "JELLEO_AUTHZ_REQUIRE_SIGNED=1 demands a signed marker. "
                "Generate a signing key with `audit-pipeline sign keygen` "
                "and re-authorize."
            )
        # If status==SIGNED, verify the .sig file actually exists and
        # matches the authorization.json bytes. The sidecar status alone
        # is attacker-writable; only the cryptographic signature is
        # tamper-evident.
        #
        # Patch #3 round-4 fix (code-reviewer #1 + devils-advocate #1 #2 +
        # threat-modeler #1 #2): three independent reviewers flagged the
        # round-3 verification was broken:
        #   (a) parser looked for "Signature: " header that sign_file
        #       NEVER emits — sign_file writes the base64 as a bare line
        #       between the headers and -----END----- marker.
        #   (b) AuthorizationInvalid raised by parser was caught by the
        #       outer `except Exception as _e_pub` and downgraded to a
        #       warning in default mode (silent bypass).
        #   (c) Pub-key absent → entire verify block skipped → SIGNED
        #       status trusted without verification (silent bypass).
        # Round-4 fixes all three: (a) use the same bare-base64 parser
        # `verify_cmd` uses, (b) raise AuthorizationInvalid outside the
        # try-except-Exception so it propagates, (c) refuse SIGNED when
        # pub key is absent (the operator can't claim "I signed this"
        # without a way to prove it).
        if status == "SIGNED":
            sig_path = auth_path.with_suffix(auth_path.suffix + ".sig")
            if not sig_path.is_file():
                raise AuthorizationInvalid(
                    f"authorization sidecar claims SIGNED but {sig_path} "
                    f"is missing. Tampered sidecar?"
                )
            pub_key = workspace / "keys" / "jelleo.ed25519.pub"
            # Patch #3 round-6 fix (threat-modeler #3 + devils-advocate
            # round-5 #1): reorder so broken-symlink check runs BEFORE
            # is_file() (which follows symlinks and would report
            # "missing" for a broken symlink, masking the actual
            # condition). Mirrors the priv-key handling in
            # write_authorization above.
            if pub_key.is_symlink() and not pub_key.exists():
                raise AuthorizationInvalid(
                    f"pub key path is a broken symlink at {pub_key} — "
                    f"refusing to verify (operator should investigate)."
                )
            if not pub_key.is_file():
                # Round-4 fix (threat-modeler #1): SIGNED claim with no
                # way to verify it is NOT trustworthy. Reject rather than
                # fall through to "trust the sidecar."
                raise AuthorizationInvalid(
                    f"authorization sidecar claims SIGNED but pub key "
                    f"{pub_key} is missing. Cannot verify signature. "
                    f"Either provide the public key or re-authorize "
                    f"without signing."
                )
            # Patch #3 round-5 fix (devils-advocate #8 + threat-modeler #3):
            # apply the same symlink jail check to the PUB key that
            # write_authorization applies to the PRIV key. Without this,
            # an attacker who can write to workspace/keys/ swaps the pub
            # key with a symlink to /attacker/their_key.pub, signs forged
            # authorization.json with their matching priv key, and
            # validate_authorization happily loads attacker-controlled
            # crypto material. This completely defeats the round-4
            # signature wiring for workspace-write attackers.
            pub_keys_root = (workspace / "keys").resolve()
            try:
                pub_resolved = pub_key.resolve(strict=True)
                pub_resolved.relative_to(pub_keys_root)
            except (ValueError, OSError) as _e_jail:
                raise AuthorizationInvalid(
                    f"pub key path escapes workspace/keys/ via symlink "
                    f"or path resolution ({_e_jail}). Refusing to load "
                    f"attacker-controllable public key for verification."
                ) from _e_jail
            # Parse the .sig file using the SAME logic verify_cmd uses
            # (sign.py:296-311): walk lines inside BEGIN/END markers and
            # collect any non-empty line where `:` is NOT present — that
            # is the bare base64 payload.
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.exceptions import InvalidSignature
            except ImportError as _e_imp:
                # cryptography missing — fatal regardless of hardened mode
                # because SIGNED was claimed and we can't verify.
                raise AuthorizationInvalid(
                    f"authorization sidecar claims SIGNED but the "
                    f"`cryptography` package is not installed. "
                    f"Install cryptography or re-authorize unsigned."
                ) from _e_imp

            try:
                pub = serialization.load_pem_public_key(pub_key.read_bytes())
            except Exception as _e_pub_load:
                raise AuthorizationInvalid(
                    f"authorization.json.sig.pub failed to load as "
                    f"PEM public key: {_e_pub_load}. Pub key file may "
                    f"be corrupt or attacker-tampered."
                ) from _e_pub_load

            sig_text = sig_path.read_text(encoding="utf-8")
            import base64 as _b64
            in_block = False
            sig_b64_parts: list[str] = []
            for line in sig_text.splitlines():
                if line.startswith("-----BEGIN JELLEO SIGNATURE-----"):
                    in_block = True
                    continue
                if line.startswith("-----END JELLEO SIGNATURE-----"):
                    in_block = False
                    continue
                if not in_block:
                    continue
                # Inside the block: header lines contain ":", base64
                # payload lines do not. Collect non-empty lines without
                # a ":" — they are the base64 payload.
                stripped = line.strip()
                if not stripped or ":" in stripped:
                    continue
                sig_b64_parts.append(stripped)
            if not sig_b64_parts:
                raise AuthorizationInvalid(
                    f"authorization.json.sig has no base64 payload "
                    f"inside the JELLEO SIGNATURE block. Malformed."
                )
            sig_b64 = "".join(sig_b64_parts)
            try:
                sig_bytes = _b64.b64decode(sig_b64, validate=True)
            except Exception as _e_b64:
                raise AuthorizationInvalid(
                    f"authorization.json.sig base64 payload doesn't "
                    f"decode: {_e_b64}. Sig file may have been tampered."
                ) from _e_b64

            # Domain tag for "authorization" (must match
            # write_authorization → sign_file).
            domain_tag = b"jelleo-authorization/v2\x00"
            signed_message = (
                domain_tag
                + auth_path.name.encode("utf-8")
                + b"\x00"
                + auth_path.read_bytes()
            )
            try:
                pub.verify(sig_bytes, signed_message)
            except InvalidSignature as ie:
                raise AuthorizationInvalid(
                    f"authorization.json.sig signature verification "
                    f"FAILED: {ie}. Marker and/or .sig has been "
                    f"tampered."
                ) from ie
    elif require_signed:
        # No sidecar AND hardened mode → hard reject (legacy markers
        # don't get a free pass in hardened deployments).
        raise AuthorizationInvalid(
            "JELLEO_AUTHZ_REQUIRE_SIGNED=1 demands a signed marker, "
            "but no authorization.json.sig.status sidecar exists. "
            "Re-authorize with the signing key present."
        )
    else:
        # Patch #3 round-5 fix (threat-modeler #4): default-mode accepts
        # a missing sidecar (legacy markers, or attacker-deleted sidecar)
        # but at least leave an audit trail — a silent accept here would
        # let an attacker who can write the workspace delete the sidecar
        # to escape any future hardened-mode rollout. Stderr-warning is
        # the minimum: operators in CI logs see it without runtime cost.
        try:
            import sys as _sys3
            _sys3.stderr.write(
                f"[validate_authorization WARNING] no .sig.status "
                f"sidecar at {sidecar_status_path}; accepting marker "
                f"as legacy unsigned. (Set JELLEO_AUTHZ_REQUIRE_SIGNED=1 "
                f"for hardened deploys.)\n"
            )
        except Exception:
            pass

    return marker
