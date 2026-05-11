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
    "merkle":     b"jelleo-merkle/v2\x00",
    "bundle":     b"jelleo-bundle/v2\x00",
    "disclosure": b"jelleo-disclosure/v2\x00",
    "report":     b"jelleo-report/v2\x00",
    "heartbeat":  b"jelleo-heartbeat/v2\x00",
    "customer":   b"jelleo-customer/v2\x00",
    "raw":        b"",  # legacy v1 — pre-domain-separation, KEEP for verify
}


def _infer_domain(file_path: Path) -> str:
    """Pick a domain tag based on the file name. Conservative: defaults to
    'raw' so legacy v1 .sig files keep verifying."""
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
    return "raw"


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
    `audit_pipeline.commands.disclose`, and `audit_pipeline.commands.merkle`.
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

    priv = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
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

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

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
@click.pass_context
def sign_file_cmd(
    ctx: click.Context, file_path: Path, key: Path | None, customer_id: str | None,
    output: Path | None,
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
        out_path = sign_file(file_path, priv_path, output)
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
            elif ":" not in line:
                sig_b64 += line.strip()
    if not sig_b64:
        raise click.ClickException("Could not extract signature bytes from sig file.")

    # Reconstruct the signed message exactly as sign_file did.
    if schema.startswith("jelleo-sign/v2"):
        domain_tag = SIGN_DOMAINS.get(domain_id)
        if domain_tag is None:
            raise click.ClickException(
                f"signature uses unknown domain '{domain_id}'; cannot verify"
            )
        # Verify the filename matches — prevents sig-rebinding attacks.
        if signed_file_name and signed_file_name != file_path.name:
            console.print(
                f"[yellow]Warning: signature was issued for "
                f"{signed_file_name!r} but verifying against "
                f"{file_path.name!r}[/yellow]"
            )
        signed_message = (
            domain_tag
            + (signed_file_name or file_path.name).encode("utf-8")
            + b"\x00"
            + file_path.read_bytes()
        )
    else:
        # Legacy v1: raw bytes only.
        signed_message = file_path.read_bytes()

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
