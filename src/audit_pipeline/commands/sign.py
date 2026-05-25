"""`audit-pipeline sign` — Ed25519 signing for disclosure packages.

Cryptographically attests that a given disclosure file (Markdown, PDF, etc.)
was produced by the Jelleo platform key. Verifies sigs from outside the
platform too.

Subcommands:
  keygen   : generate a new Ed25519 keypair (only run once per workspace)
  sign     : sign a file → produce <file>.sig (and <file>.pubkey for verification)
  verify   : verify a signature against a file + pubkey

Programmatic API:
  sign_file(file_path, key_path=None, output=None) — non-CLI helper used by
                                                     report.py to auto-sign
                                                     every generated report.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Programmatic API (non-CLI) — called by report.py and the lifecycle hooks
# ---------------------------------------------------------------------------


class SignError(Exception):
    """Raised when signing fails for a recoverable reason (key missing etc.)."""


# FIX B-#29: Domain separation tags prevent cross-protocol signature reuse.
# Without these, a signature on a Merkle cycle root could be presented as a
# signature on a bundle digest (or vice versa). Each producer prepends its
# tag to the bytes BEFORE signing; the verifier prepends the same tag before
# verifying. Tags are NUL-terminated to prevent length-extension attacks at
# the tag boundary. Schema version 2.
SIGN_DOMAINS = {
    "merkle":         b"jelleo-merkle/v2\x00",
    "bundle":         b"jelleo-bundle/v2\x00",
    "disclosure":     b"jelleo-disclosure/v2\x00",
    "report":         b"jelleo-report/v2\x00",
    "heartbeat":      b"jelleo-heartbeat/v2\x00",
    "customer":       b"jelleo-customer/v2\x00",
    # Patch #3 round-1 (audit HIGH 2984f0e7): dedicated domain for
    # bundle authorization.json sidecars so a forged bundle digest can't
    # be re-presented as a valid authorization sig (and vice-versa).
    "authorization":  b"jelleo-authorization/v2\x00",
    "raw":            b"",  # legacy v1 — pre-domain-separation, KEEP for verify
}


# Patch #2 round-3: module-level signing-password cache. Reading the env
# var on every sign_file call broke multi-call flows (HTML+PDF in one
# process, rebuild-all over N cycles). Cache the bytes module-level on
# first read, pop from environ ONCE to close subprocess inheritance.
# R5b (2026-05-24): added threading.Lock guard around check-then-pop —
# goober found concurrent first-call race could let two threads both
# observe LOADED=False, both pop (second pop returns None), and end up
# with cache=None even when password was set. CPython GIL makes
# individual bytecode ops atomic but the check-then-act sequence is
# not. Lock closes the window.
import threading as _threading  # noqa: E402  (deferred import — see comment block above)

_SIGNING_PASSWORD_CACHE: bytes | None = None
_SIGNING_PASSWORD_LOADED: bool = False
_SIGNING_PASSWORD_LOCK = _threading.Lock()


def _cache_signing_password() -> bytes | None:
    """Read JELLEO_SIGNING_KEY_PASSWORD from environ on first call, cache it
    module-level, and POP from environ so subprocesses can't inherit. Return
    cached bytes on subsequent calls (no env re-read).

    Returns None if the env var was never set (unencrypted-key flow).

    Thread-safe via _SIGNING_PASSWORD_LOCK — concurrent first-call by N
    threads pops the env var exactly once.
    """
    global _SIGNING_PASSWORD_CACHE, _SIGNING_PASSWORD_LOADED
    # Fast path: already loaded, no lock needed (read of a bool is atomic
    # under CPython GIL, no torn read).
    if _SIGNING_PASSWORD_LOADED:
        return _SIGNING_PASSWORD_CACHE
    with _SIGNING_PASSWORD_LOCK:
        # Re-check inside the lock — another thread may have populated
        # while we were waiting.
        if _SIGNING_PASSWORD_LOADED:
            return _SIGNING_PASSWORD_CACHE
        _pw_env = os.environ.pop("JELLEO_SIGNING_KEY_PASSWORD", None)
        if _pw_env:
            _SIGNING_PASSWORD_CACHE = _pw_env.encode("utf-8")
        _SIGNING_PASSWORD_LOADED = True
        return _SIGNING_PASSWORD_CACHE


def _infer_domain(file_path: Path) -> str:
    """Pick a domain tag based on the file name.

    Patch #2 round-1 fix (audit HIGH src/audit_pipeline/commands/sign.py:57):
    previously defaulted to 'raw' (the no-domain-separation legacy tag) which
    let an attacker who controls a filename (e.g. via temp-file naming in
    sign_bundle) get a signature under a weaker domain. We now RAISE on
    unknown filenames so the caller is forced to pass `domain=` explicitly,
    closing the attacker-controlled-filename → wrong-domain bypass.

    The 'raw' tag is still in SIGN_DOMAINS for backward-compatible VERIFY
    of legacy v1 signatures (read-only); it is no longer reachable from
    sign-side inference.
    """
    name = file_path.name.lower()
    if name.startswith("merkle.") or name.endswith("merkle.json"):
        return "merkle"
    if "bundle" in name or name in ("patch.diff", "verification.json"):
        return "bundle"
    if "disclosure" in name:
        return "disclosure"
    if "heartbeat" in name:
        return "heartbeat"
    if "report" in name:
        return "report"
    if "customer" in name or "manifest" in name:
        return "customer"
    # Patch #2 round-1 fix: refuse to infer 'raw' from an unknown filename.
    # Callers must pass explicit `domain=` if they want any other domain
    # (including 'raw' for legacy v1 compatibility — operator-explicit only).
    raise SignError(
        f"cannot infer signing domain from filename {file_path.name!r}; "
        f"pass explicit `domain=` (one of {sorted(SIGN_DOMAINS)}). "
        f"Note: 'raw' is the legacy v1 no-domain-separation tag — pass it "
        f"only when re-signing a v1 file is intentional and operator-approved."
    )


def sign_file(
    file_path: Path,
    key_path: Path | None = None,
    output: Path | None = None,
    domain: str | None = None,
) -> Path:
    """Sign a file with the Jelleo Ed25519 key. Returns the signature path.

    Raises SignError if the key file is missing or the cryptography package
    is not installed. Does not raise on a successful sign.

    The `domain` arg selects a domain-separation tag (see SIGN_DOMAINS).
    Defaults to inference from the filename. A signed payload from one
    domain (e.g. merkle) cannot be re-presented as valid in another (bundle)
    even though the same key signed both.

    The signature is computed over `domain_tag || file_name || NUL ||
    file_bytes` — binding the signature to a SPECIFIC filename closes
    sig-rebinding attacks (claim sig is for X when it's actually on Y).

    This is the non-CLI helper used by `audit_pipeline.commands.report`,
    `audit_pipeline.commands.merkle`, `audit_pipeline.commands.heartbeat`,
    and `audit_pipeline.bundle.assembly`. (R5b: removed stale `disclose`
    reference — grep confirms disclose.py does not call sign_file.)
    """
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:
        raise SignError(
            "`cryptography` package required. Run: pip install cryptography"
        ) from e

    if key_path is None:
        raise SignError("key_path required (no default — pass explicit path)")

    if not key_path.exists():
        raise SignError(f"No private key at {key_path}. Run `audit-pipeline sign keygen` first.")

    domain_id = domain or _infer_domain(file_path)
    if domain_id not in SIGN_DOMAINS:
        raise SignError(
            f"unknown signing domain '{domain_id}'. Valid: {sorted(SIGN_DOMAINS)}"
        )
    domain_tag = SIGN_DOMAINS[domain_id]

    # Patch #2 round-1 fix (audit HIGH src/audit_pipeline/commands/sign.py:119):
    # support env-var key password so the private key on disk can be encrypted
    # at rest. JELLEO_SIGNING_KEY_PASSWORD (bytes; UTF-8 encoded) is read once
    # per sign call and never logged. If unset, password=None preserves the
    # previous unencrypted-key behaviour for backward compat (operator can
    # migrate at their own pace by running `ssh-keygen -p` + setting the env
    # var). The cryptography library raises TypeError if password is provided
    # but key is unencrypted (or vice versa) — that's caught + re-raised with
    # an actionable message rather than the raw TypeError.
    # Patch #2 round-3 fix (audit round-2 HIGH from all 3 reviewers): the
    # round-2 design popped JELLEO_SIGNING_KEY_PASSWORD from os.environ on
    # every call. That broke the second sign_file() in the same process —
    # report.py signs HTML then PDF, merkle.py rebuild-all signs N sidecars.
    # Round-3 design: cache the password in a module-level variable on first
    # read, pop from environ ONCE (closes subprocess inheritance), reuse the
    # cached bytes on subsequent calls. Resets only on `unset` by the
    # operator (env var goes from set to unset is treated as no-op; once set
    # within the process the bytes stay until process exit).
    _key_pw_bytes = _cache_signing_password()
    try:
        priv = serialization.load_pem_private_key(
            key_path.read_bytes(), password=_key_pw_bytes,
        )
    except TypeError as e:
        msg = str(e)
        if "encrypted" in msg.lower() and _key_pw_bytes is None:
            raise SignError(
                f"Private key at {key_path} is encrypted but "
                f"JELLEO_SIGNING_KEY_PASSWORD is not set. Export the password "
                f"in the environment and retry."
            ) from e
        if "not encrypted" in msg.lower() and _key_pw_bytes is not None:
            raise SignError(
                f"Private key at {key_path} is NOT encrypted but "
                f"JELLEO_SIGNING_KEY_PASSWORD is set. Either encrypt the key "
                f"(`ssh-keygen -p -f {key_path}`) or unset the env var."
            ) from e
        raise SignError(f"Failed to load private key: {msg}") from e
    except ValueError as e:
        # R5b (2026-05-24): goober finding HIGH #4. cryptography raises
        # ValueError (NOT TypeError) when the password is provided but
        # WRONG ("Bad decrypt. Incorrect password?"). Pre-R5b this
        # propagated as an uncaught ValueError through every caller
        # (silent digest fallback in assembly.py; crash in
        # heartbeat.py/report.py click paths). Catch and translate to
        # actionable SignError. Stale-cache + key-rotation scenarios
        # (operator swaps key B with pw_B, but cache still has pw_A
        # from key A) land here too.
        msg = str(e)
        if _key_pw_bytes is not None:
            raise SignError(
                f"Private key at {key_path} could not be decrypted with the "
                f"cached JELLEO_SIGNING_KEY_PASSWORD (wrong password, key "
                f"rotated mid-process, or key corrupted). Restart the process "
                f"to re-cache from environment, or `ssh-keygen -p -f {key_path}` "
                f"to change the on-disk password. Underlying error: {msg}"
            ) from e
        raise SignError(f"Failed to parse private key: {msg}") from e
    finally:
        # Drop reference — CPython can't zero immutable bytes. Module-level
        # cache still retains the bytes for the next sign_file call.
        _key_pw_bytes = None
    file_bytes = file_path.read_bytes()
    # Compose the signed message: domain tag + filename + NUL + bytes.
    # This binds the signature to (domain, filename, content) tuple; any
    # mismatch on verify fails. Filename is the .name (no directory) so a
    # rename of the file doesn't break verification.
    signed_message = (
        domain_tag
        + file_path.name.encode("utf-8")
        + b"\x00"
        + file_bytes
    )
    sig = priv.sign(signed_message)

    sig_b64 = base64.b64encode(sig).decode()
    out_path = output or file_path.with_suffix(file_path.suffix + ".sig")

    metadata = (
        f"-----BEGIN JELLEO SIGNATURE-----\n"
        f"Algorithm: Ed25519\n"
        f"Schema: jelleo-sign/v2\n"
        f"Domain: {domain_id}\n"
        f"Signed-At: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"Signed-File: {file_path.name}\n"
        f"Signed-Bytes: {len(file_bytes)}\n"
        f"\n"
        f"{sig_b64}\n"
        f"-----END JELLEO SIGNATURE-----\n"
    )
    out_path.write_text(metadata, encoding="utf-8")
    return out_path


def default_key_path(workspace: Path) -> Path:
    """The conventional location for the workspace's signing key."""
    return workspace / "keys" / "jelleo.ed25519"


@click.group(name="sign")
def sign_cmd() -> None:
    """Cryptographic attestation for disclosure packages (Ed25519)."""


@sign_cmd.command(name="keygen")
@click.option("--key-dir", type=click.Path(path_type=Path), default=None,
              help="Directory for keys (default: <workspace>/keys/)")
@click.option("--force", is_flag=True, help="Overwrite existing keys")
@click.pass_context
def keygen_cmd(ctx: click.Context, key_dir: Path | None, force: bool) -> None:
    """Generate a new Ed25519 keypair for signing disclosures."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise click.ClickException(
            "`cryptography` package required. Run: pip install cryptography"
        )

    workspace = Path(ctx.obj["workspace"])
    key_dir = key_dir or (workspace / "keys")
    key_dir.mkdir(parents=True, exist_ok=True)

    priv_path = key_dir / "jelleo.ed25519"
    pub_path = key_dir / "jelleo.ed25519.pub"

    if priv_path.exists() and not force:
        raise click.ClickException(
            f"Key already exists at {priv_path}. Pass --force to overwrite."
        )

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    # Patch #2 round-3 fix: use the module-level cache instead of popping
    # env directly. This way if keygen is followed by sign_file in the same
    # process (which the test suite does), both share the cached password.
    _kg_pw_bytes = _cache_signing_password()
    if _kg_pw_bytes:
        _enc = serialization.BestAvailableEncryption(_kg_pw_bytes)
        console.print(
            "[green]→[/green] encrypting private key with "
            "$JELLEO_SIGNING_KEY_PASSWORD (BestAvailableEncryption)"
        )
    else:
        _enc = serialization.NoEncryption()
        console.print(
            "[yellow]WARN: JELLEO_SIGNING_KEY_PASSWORD not set — generating "
            "UNENCRYPTED private key. To encrypt at rest: set the env var "
            "and re-run with --force, OR run `ssh-keygen -p -f <key>` after.[/yellow]"
        )

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_enc,
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _kg_pw_bytes = None  # drop local reference (cache still has it)

    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)

    console.print(f"[green]Generated[/green] {priv_path} (mode 600)")
    console.print(f"[green]Generated[/green] {pub_path}")
    console.print()
    console.print("[bold]Public key (share this):[/bold]")
    console.print(pub_pem.decode())
    console.print(
        "[dim]Add this public key to your published methodology repo so "
        "anyone can verify Jelleo-signed disclosures.[/dim]"
    )


@sign_cmd.command(name="sign")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--key", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Private key path (default: <workspace>/keys/jelleo.ed25519)")
@click.option("--customer", "customer_id", default=None,
              help="Sign with the per-customer derived key under "
                   "<workspace>/customers/<id>/keys/<id>.ed25519 (Tier 5 #28). "
                   "Mutually exclusive with --key.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Signature output path (default: <file_path>.sig)")
@click.option("--domain", "domain", default=None,
              type=click.Choice([d for d in sorted(SIGN_DOMAINS) if d != "raw"]),
              help="Signing domain tag (default: inferred from filename; "
                   "REQUIRED for filenames that don't match the inference rules "
                   "since round-2 hardening removed the 'raw' fallback). "
                   "R5b: 'raw' is excluded from CLI choices — it's the legacy "
                   "v1 no-domain-separation tag, available only for VERIFY of "
                   "old sigs (read-only). Closing the explicit-pass back-door.")
@click.pass_context
def sign_file_cmd(
    ctx: click.Context, file_path: Path, key: Path | None, customer_id: str | None,
    output: Path | None, domain: str | None,
) -> None:
    """Sign a file with the Jelleo Ed25519 key (platform or per-customer)."""
    if key and customer_id:
        raise click.ClickException("--key and --customer are mutually exclusive")

    workspace = Path(ctx.obj["workspace"])
    if customer_id:
        from audit_pipeline import customers as customers_mod
        priv_path = customers_mod.customer_priv_key_path(workspace, customer_id)
        if not priv_path.exists():
            raise click.ClickException(
                f"no per-customer key at {priv_path}; "
                f"run `audit-pipeline customer add {customer_id}` first"
            )
    else:
        priv_path = key or default_key_path(workspace)

    try:
        # Patch #2 round-2 fix (audit Patch #2 round-1 HIGH #3): --domain flag
        # added so operators can sign files whose names don't match the
        # `_infer_domain` inference rules. Before this, operators got a hard
        # error ("cannot infer signing domain") with no CLI workaround.
        out_path = sign_file(file_path, priv_path, output, domain=domain)
    except SignError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]Signed[/green] {file_path}")
    if customer_id:
        console.print(f"  [dim]signing key: customer '{customer_id}'[/dim]")
    console.print(f"[green]Signature[/green] {out_path}")


@sign_cmd.command(name="verify")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("sig_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--pubkey", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Public key path (default: <workspace>/keys/jelleo.ed25519.pub)")
@click.pass_context
def verify_cmd(
    ctx: click.Context, file_path: Path, sig_path: Path, pubkey: Path | None,
) -> None:
    """Verify a Jelleo signature against a file."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise click.ClickException("`cryptography` package required.")

    workspace = Path(ctx.obj["workspace"])
    pub_path = pubkey or (workspace / "keys" / "jelleo.ed25519.pub")
    if not pub_path.exists():
        raise click.ClickException(f"No public key at {pub_path}")

    pub = serialization.load_pem_public_key(pub_path.read_bytes())

    sig_text = sig_path.read_text(encoding="utf-8")
    sig_b64 = ""
    in_block = False
    schema = "jelleo-sign/v1"   # default for legacy .sig files without Schema:
    domain_id = "raw"           # default for legacy files
    signed_file_name = ""
    signed_bytes_claim: int | None = None
    for line in sig_text.splitlines():
        if line.startswith("-----BEGIN JELLEO"):
            in_block = True
            continue
        if line.startswith("-----END JELLEO"):
            break
        if in_block and line:
            if line.startswith("Schema:"):
                schema = line.split(":", 1)[1].strip()
            elif line.startswith("Domain:"):
                domain_id = line.split(":", 1)[1].strip()
            elif line.startswith("Signed-File:"):
                signed_file_name = line.split(":", 1)[1].strip()
            elif line.startswith("Signed-Bytes:"):
                try:
                    signed_bytes_claim = int(line.split(":", 1)[1].strip())
                except ValueError:
                    signed_bytes_claim = None
            elif ":" not in line:
                sig_b64 += line.strip()
    if not sig_b64:
        raise click.ClickException("Could not extract signature bytes from sig file.")

    # Cross-cutting audit Defect 09: previously this only printed a YELLOW
    # WARNING when ``Signed-File:`` didn't match the on-disk filename and
    # then proceeded to verify anyway, so a CI script grepping for the
    # success line accepted a rebound .sig as valid. Now: hard-FAIL.
    if signed_file_name and signed_file_name != file_path.name:
        raise click.ClickException(
            f"signature was issued for {signed_file_name!r} but verifying "
            f"against {file_path.name!r}: refusing — rename the file to "
            f"match Signed-File: header, or re-sign."
        )

    # Cross-cutting audit Defect 09 cont.: also cross-check Signed-Bytes
    # against the actual file length so a forged sig claiming
    # ``Signed-Bytes: 0`` can't sneak through.
    if signed_bytes_claim is not None:
        actual_bytes = file_path.stat().st_size
        if signed_bytes_claim != actual_bytes:
            raise click.ClickException(
                f"signature claims Signed-Bytes={signed_bytes_claim} but "
                f"file is {actual_bytes} bytes — refusing."
            )

    # Reconstruct the signed message exactly as sign_file did.
    if schema.startswith("jelleo-sign/v2"):
        domain_tag = SIGN_DOMAINS.get(domain_id)
        if domain_tag is None:
            raise click.ClickException(
                f"signature uses unknown domain '{domain_id}'; cannot verify"
            )
        signed_message = (
            domain_tag
            + (signed_file_name or file_path.name).encode("utf-8")
            + b"\x00"
            + file_path.read_bytes()
        )
    else:
        # P3+P4 audit Defect 06: previously the v1-legacy verify path was
        # an unconditional fall-through. An attacker who delivered a
        # forged .sig with a stripped ``Schema:`` line would have the
        # signature reconstructed as raw-bytes-only — no domain
        # separation, no filename binding. Now require an explicit
        # operator opt-in via env to verify legacy v1 sigs.
        if (os.environ.get("JELLEO_ALLOW_LEGACY_V1_VERIFY") or "") != "1":
            raise click.ClickException(
                "signature is jelleo-sign/v1 (legacy raw-bytes mode). "
                "v1 has no domain-separation and no filename binding — "
                "refusing by default. If verifying a historical signature "
                "this is expected for, set "
                "JELLEO_ALLOW_LEGACY_V1_VERIFY=1 and re-run."
            )
        # Patch #2 round-2 fix (audit Patch #2 round-1 HIGH/CRITICAL from
        # threat-modeler + devils-advocate): the audit log feature itself
        # introduced TWO new vulns:
        #   1. JELLEO_V1_VERIFY_AUDIT was attacker-controllable → arbitrary
        #      file write as root (e.g. /etc/cron.d/jelleo). Round-2 jails
        #      the path under /var/log/jelleo/ — any other prefix REFUSED.
        #   2. file_path was written verbatim → newline-injection vector
        #      (forge additional log lines, or inject into authorized_keys
        #      if log path was also redirected). Round-2 escapes \n + \r.
        #   3. /dev/null silently swallowed audit (Linux). Round-2 rejects.
        # Patch #2 round-4 fix (devils-advocate r3 MED #4): assign
        # signed_message BEFORE the audit-log try/except so any non-OSError
        # exception escaping the audit block doesn't leave signed_message
        # unbound (which would cause UnboundLocalError at pub.verify below).
        # The audit block ONLY catches OSError; PermissionError etc. ARE
        # OSError subclasses but a future RuntimeError or non-IO exception
        # would escape.
        signed_message = file_path.read_bytes()
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        try:
            _audit_log = os.environ.get(
                "JELLEO_V1_VERIFY_AUDIT", "/var/log/jelleo/v1-verify-audit.log"
            )
            # Path jail: must start with /var/log/jelleo/ AND not contain /../
            _audit_log_abs = os.path.realpath(_audit_log)
            _allowed_prefix = os.path.realpath("/var/log/jelleo")
            # Patch #2 round-3 fix: check /dev/null + Windows BEFORE path-jail
            # so the more specific error message wins (devils-advocate r2 #2).
            if _audit_log_abs in ("/dev/null", "/dev/zero", "nul"):
                raise OSError(
                    f"JELLEO_V1_VERIFY_AUDIT cannot target {_audit_log_abs} "
                    f"(audit log would be silently discarded)"
                )
            if os.name == "nt":
                raise OSError(
                    "JELLEO_V1_VERIFY_AUDIT path-jail is Linux-only; "
                    "audit log skipped on Windows"
                )
            if not (_audit_log_abs == _allowed_prefix or
                    _audit_log_abs.startswith(_allowed_prefix + os.sep)):
                raise OSError(
                    f"JELLEO_V1_VERIFY_AUDIT={_audit_log!r} (resolved: "
                    f"{_audit_log_abs!r}) is not under /var/log/jelleo/ — "
                    f"refusing to write."
                )
            # Patch #2 round-3 fix (devils-advocate r2 #1): use the RESOLVED
            # path for both makedirs and open. Previously `os.path.dirname
            # (_audit_log)` used the raw env var, leaving a TOCTOU window
            # between the realpath check and the file open.
            _audit_dir = os.path.dirname(_audit_log_abs)
            if _audit_dir:
                os.makedirs(_audit_dir, exist_ok=True)
            # Sanitize BOTH file_path AND schema (round-3 fix for code-reviewer
            # r2 #2: schema came from attacker-influenced .sig header text).
            _safe_path = str(file_path).replace("\r", "\\r").replace("\n", "\\n")
            _safe_schema = str(schema).replace("\r", "\\r").replace("\n", "\\n")
            with open(_audit_log_abs, "a", encoding="utf-8") as _f:
                _f.write(
                    f"{_dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"v1-verify bypass used pid={os.getpid()} "
                    f"file={_safe_path} schema={_safe_schema}\n"
                )
        except OSError as _ose:
            # Don't block the verify on audit-log write failure. But surface
            # a clear warning so operator notices the gap.
            console.print(
                f"[yellow]WARN: could not write JELLEO_V1_VERIFY_AUDIT log: "
                f"{_ose}. Verify continuing.[/yellow]"
            )
        # NOTE: signed_message already assigned ABOVE the try block (round-4
        # fix). Removed the duplicate assignment that was here pre-round-4.

    try:
        sig = base64.b64decode(sig_b64)
        pub.verify(sig, signed_message)
        console.print(
            f"[bold green]✓ VALID[/bold green] {schema} ({domain_id}) "
            f"signature on {file_path}"
        )
    except InvalidSignature:
        console.print(f"[bold red]✗ INVALID[/bold red] signature on {file_path}")
        raise click.ClickException("Signature does not match.")
