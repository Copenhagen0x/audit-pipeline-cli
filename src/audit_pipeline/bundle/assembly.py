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
        # Reviewer MEDIUM (P7 R5c): hook-log write was unbounded — a
        # 10 MB `note` produced a 10 MB log file per invocation, easy
        # disk-exhaustion DoS for an operator with CLI access. Cap
        # consistent with the meta.json history entry (500 chars).
        # Goober LOW (P7 R5c): truncate BEFORE repr() so a 10 MB
        # `bytes` value isn't fully materialised in memory just to be
        # sliced. `repr(note[:500])` slices the underlying bytes/str
        # first; for arbitrary objects without `__getitem__`, fall
        # back to `repr(note)[:500]` (the original slow path).
        if note is None:
            log_note = None
        elif isinstance(note, str):
            log_note = note[:500]
        else:
            try:
                log_note = repr(note[:500])  # type: ignore[index]
            except (TypeError, KeyError):
                # Object doesn't support integer slicing — `int` raises
                # TypeError, `dict` raises KeyError (it treats the slice
                # as a key lookup). Fall back to repr-then-truncate,
                # accepting the unbounded repr() cost (rare; not
                # reachable from any current caller per the
                # `note: str | None` type annotation).
                log_note = repr(note)[:500]
        log_path.write_text(
            json.dumps({
                "at":          now,
                "from_status": prev_status,
                "to_status":   new_status,
                "note":        log_note,
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

    # Reviewer CRITICAL (P7 R5b): this dispatch previously called
    # urlopen() on whatever URL the operator put in notifier.json with
    # zero validation — a classic SSRF sink (POST to 169.254.169.254 for
    # cloud-metadata, or http:// to leak the payload in clear). Gate
    # through the same allow-list as hunt.py and health.py.
    from audit_pipeline.notifier import validate_webhook_url
    allowed, reason = validate_webhook_url(webhook)
    if not allowed:
        import sys
        print(
            f"bundle_webhook_blocked: {reason} (url={str(webhook)[:80]})",
            file=sys.stderr,
        )
        return

    # Reviewer MEDIUM (P7 R5b): unbounded `note` can balloon the JSON
    # payload and trip downstream webhook size limits / log-injection.
    # Truncate defensively before serialising.
    safe_note: str | None
    if note is None:
        safe_note = None
    elif not isinstance(note, str):
        safe_note = repr(note)[:2000]
    else:
        safe_note = note if len(note) <= 2000 else note[:2000] + "…[truncated]"

    payload = {
        "kind":         "bundle_transition",
        "finding_id":   finding_id,
        "from_status":  prev_status,
        "to_status":    new_status,
        "note":         safe_note,
        "at":           datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        import urllib.request
        from urllib.error import HTTPError

        # Threat-modeler HIGH (P7 R5c): default urllib follows 3xx
        # redirects. An allow-listed webhook host (e.g. webhook.site,
        # or even a compromised Slack-shaped endpoint) could 301 us to
        # http://169.254.169.254/ — bypassing validate_webhook_url
        # because the validator only sees the *initial* URL, not the
        # post-redirect target. Install a no-op redirect handler that
        # raises HTTPError on any 3xx, killing the redirect chain.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise HTTPError(
                    req.full_url, code,
                    f"refusing webhook redirect to {newurl[:80]}",
                    headers, fp,
                )

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Threat-modeler HIGH (P7 R5c): unbounded .read() lets a slow
        # adversary stream gigabytes into process memory; cap at 8 KB
        # since we discard the body anyway.
        opener.open(req, timeout=5).read(8192)
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
        for p in sorted(pdir.glob("*")):
            if p.is_file():
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
        # sign_file signs a file path; write the digest to a temp file first
        digest_file = out.with_suffix(".digest")
        digest_file.write_text(digest, encoding="utf-8")
        return sign_file(digest_file, signing_key)
    except Exception:
        # Fall back to digest-only attestation (operator can sign manually)
        out.write_text(
            f"# bundle digest (unsigned — signing key unavailable)\n"
            f"sha256:{digest}\n",
            encoding="utf-8",
        )
        return out
