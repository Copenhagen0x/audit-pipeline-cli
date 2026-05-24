"""Bundle assembly: package patch + writeup + signature into a bundle dir."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from audit_pipeline.bundle.paths import (
    balance_proof_path,
    bundle_dir,
    hooks_dir,
    meta_path,
    patch_path,
    poc_dir,
    signature_path,
    writeup_path,
)


def write_meta(
    workspace: Path,
    *,
    finding_id: int,
    engine_sha: str,
    bug_class: str,
    hypothesis_id: str,
    severity: str,
    title: str,
    template_used: str,
    status: str = "drafted",
    poc_test_name: str | None = None,
    target_file: str | None = None,
    kani_harness: str | None = None,
) -> Path:
    """Initial meta.json for a new bundle. Idempotent — overwrites status.

    `poc_test_name`, `target_file`, and `kani_harness` are persisted so
    `verify` and `open-pr` can recover them without re-passing flags.
    Operator UX: passing them at draft time means subsequent commands
    pick them up automatically.
    """
    bdir = bundle_dir(workspace, finding_id)
    bdir.mkdir(parents=True, exist_ok=True)
    hooks_dir(workspace, finding_id).mkdir(parents=True, exist_ok=True)

    p = meta_path(workspace, finding_id)
    existing: dict = {}
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {
        "finding_id":     finding_id,
        "engine_sha":     engine_sha,
        "bug_class":      bug_class,
        "hypothesis_id":  hypothesis_id,
        "severity":       severity,
        "title":          title[:200],
        "template_used":  template_used,
        "status":         status,
        "poc_test_name":  poc_test_name or existing.get("poc_test_name"),
        "target_file":    target_file or existing.get("target_file"),
        "kani_harness":   kani_harness or existing.get("kani_harness"),
        "created_at":     existing.get("created_at", now),
        "updated_at":     now,
        "history":        existing.get("history", []) + [
            {"at": now, "to_status": status},
        ],
    }
    p.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return p


def transition_status(
    workspace: Path,
    finding_id: int,
    new_status: str,
    *,
    note: str | None = None,
) -> dict:
    """Move a bundle to new_status and append to history.

    Side effects:
      * Item 17 — appends to per-bundle hook log under <bundle>/hooks/
      * Item 15 — fires notify hook if `notifier.json` configures bundle events

    Security caveat: ``note`` is forwarded verbatim to the configured
    notify-webhook payload (Slack / email / etc). Do NOT put secrets,
    API keys, or pre-disclosure bug content into the note field — assume
    the webhook destination has wider visibility than operator-private
    bundle files.
    """
    p = meta_path(workspace, finding_id)
    if not p.is_file():
        raise FileNotFoundError(f"no bundle meta at {p}")
    meta = json.loads(p.read_text(encoding="utf-8"))
    prev_status = meta.get("status")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["status"] = new_status
    meta["updated_at"] = now
    entry = {"at": now, "to_status": new_status}
    if note:
        entry["note"] = note[:500]
    meta.setdefault("history", []).append(entry)
    p.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    # Item 17: per-bundle hook log. Use microsecond precision + a counter
    # so rapid back-to-back transitions don't collide on the same filename.
    try:
        hd = hooks_dir(workspace, finding_id)
        hd.mkdir(parents=True, exist_ok=True)
        ts_micro = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        log_path = hd / f"transition-{ts_micro}.log"
        n = 0
        while log_path.exists():
            n += 1
            log_path = hd / f"transition-{ts_micro}-{n}.log"
        log_path.write_text(
            json.dumps({
                "at":          now,
                "from_status": prev_status,
                "to_status":   new_status,
                "note":        note,
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Logging is best-effort — never blocks the transition
        pass

    # Item 15: notify hook on bundle state changes
    try:
        _fire_bundle_notification(workspace, finding_id, prev_status, new_status, note)
    except Exception:
        pass

    return meta


def _fire_bundle_notification(
    workspace: Path,
    finding_id: int,
    prev_status: str | None,
    new_status: str,
    note: str | None,
) -> None:
    """Best-effort notify hook for bundle state changes (P3 Item 15).

    Reads `<workspace>/notifier.json` for the configured webhook URL; if
    the config doesn't list bundle_events: true, this is a no-op.

    Format mirrors the existing notify.py webhook payload so downstream
    Slack / email plumbing keeps working.
    """
    cfg_path = workspace / "notifier.json"
    if not cfg_path.is_file():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not cfg.get("bundle_events"):
        return

    webhook = cfg.get("webhook_url")
    if not webhook:
        return

    payload = {
        "kind":         "bundle_transition",
        "finding_id":   finding_id,
        "from_status":  prev_status,
        "to_status":    new_status,
        "note":         note,
        "at":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        import urllib.request
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def write_patch(workspace: Path, finding_id: int, diff: str) -> Path:
    p = patch_path(workspace, finding_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(diff, encoding="utf-8")
    return p


def write_writeup(workspace: Path, finding_id: int, body: str) -> Path:
    p = writeup_path(workspace, finding_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def write_balance_proof(workspace: Path, finding_id: int, body: str) -> Path:
    p = balance_proof_path(workspace, finding_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def copy_poc(workspace: Path, finding_id: int, src_paths: list[Path]) -> list[Path]:
    """Copy PoC test files from confirm-cycle output into the bundle dir."""
    out_dir = poc_dir(workspace, finding_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_paths = []
    for src in src_paths:
        if not src.is_file():
            continue
        dst = out_dir / src.name
        shutil.copy2(str(src), str(dst))
        out_paths.append(dst)
    return out_paths


def bundle_digest(workspace: Path, finding_id: int) -> str:
    """SHA-256 over the bundle's stable artifacts (meta + patch + writeup +
    balance_proof + each PoC file). Used for signing."""
    h = hashlib.sha256()
    bdir = bundle_dir(workspace, finding_id)
    if not bdir.is_dir():
        return ""
    for relpath in [
        "meta.json", "patch.diff", "writeup.md", "balance_proof.md",
    ]:
        p = bdir / relpath
        if p.is_file():
            h.update(relpath.encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
    pdir = poc_dir(workspace, finding_id)
    if pdir.is_dir():
        # Patch #2 round-1 fix (audit HIGH assembly.py:247): pdir.glob("*")
        # also returns SYMLINKS. A symlink in poc/ pointing at /root/.ssh/
        # jelleo-signing or any large file would expand bundle_digest to
        # include attacker-chosen content (signing-key bytes leaking into the
        # signed digest, or DoS via huge file). Two guards:
        #   1. is_file() short-circuits on symlinks pointing to dirs (good)
        #      but does NOT exclude symlinks pointing to regular files.
        #      Explicit `is_symlink()` check before is_file() handles this.
        #   2. p.resolve() must stay INSIDE pdir.resolve() — closes the
        #      "symlink that resolves to /root/.ssh/key" exfil vector.
        pdir_resolved = pdir.resolve()
        for p in sorted(pdir.glob("*")):
            if p.is_symlink():
                continue  # refuse symlinks entirely in poc/
            if not p.is_file():
                continue
            try:
                p_resolved = p.resolve()
                p_resolved.relative_to(pdir_resolved)
            except (ValueError, OSError):
                # path escapes pdir (shouldn't happen w/o symlinks but
                # defense-in-depth) — skip.
                continue
            h.update((f"poc/{p.name}").encode())
            h.update(b"\x00")
            h.update(p.read_bytes())
    return h.hexdigest()


def sign_bundle(workspace: Path, finding_id: int, signing_key: Path) -> Path | None:
    """Sign the bundle digest with the workspace Ed25519 key.

    Falls back to writing a plaintext digest file if signing isn't available
    (e.g., key not found, cryptography missing).
    """
    digest = bundle_digest(workspace, finding_id)
    out = signature_path(workspace, finding_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from audit_pipeline.commands.sign import sign_file
        # sign_file signs a file path; write the digest to a temp file first.
        # Patch #2 round-1 CRITICAL fix (audit assembly.py:266-270): pass
        # `domain="bundle"` EXPLICITLY. Previously sign_file's _infer_domain()
        # fell through to 'raw' on the unrecognised filename `bundle.sig.digest`
        # — same key as legitimate bundles but no domain separation, allowing
        # an attacker who controlled the digest content to produce a signature
        # accepted as a valid bundle. The round-1 fix to _infer_domain() now
        # RAISES on unknown filenames (closes the silent-fallthrough), but
        # passing domain= explicitly here is the correct guard at THIS call
        # site regardless of upstream behaviour.
        digest_file = out.with_suffix(".digest")
        digest_file.write_text(digest, encoding="utf-8")
        return sign_file(digest_file, signing_key, domain="bundle")
    except Exception:
        # Fall back to digest-only attestation (operator can sign manually)
        out.write_text(
            f"# bundle digest (unsigned — signing key unavailable)\n"
            f"sha256:{digest}\n",
            encoding="utf-8",
        )
        return out
