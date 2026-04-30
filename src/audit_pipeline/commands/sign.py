"""`audit-pipeline sign` — Ed25519 signing for disclosure packages.

Cryptographically attests that a given disclosure file (Markdown, PDF, etc.)
was produced by the Jelleo platform key. Verifies sigs from outside the
platform too.

Subcommands:
  keygen   : generate a new Ed25519 keypair (only run once per workspace)
  sign     : sign a file → produce <file>.sig (and <file>.pubkey for verification)
  verify   : verify a signature against a file + pubkey
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

console = Console()


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
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
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
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Signature output path (default: <file_path>.sig)")
@click.pass_context
def sign_file_cmd(
    ctx: click.Context, file_path: Path, key: Path | None, output: Path | None,
) -> None:
    """Sign a file with the Jelleo Ed25519 key."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise click.ClickException("`cryptography` package required.")

    workspace = Path(ctx.obj["workspace"])
    priv_path = key or (workspace / "keys" / "jelleo.ed25519")
    if not priv_path.exists():
        raise click.ClickException(
            f"No private key at {priv_path}. Run `audit-pipeline sign keygen` first."
        )

    priv = serialization.load_pem_private_key(priv_path.read_bytes(), password=None)
    payload = file_path.read_bytes()
    sig = priv.sign(payload)

    sig_b64 = base64.b64encode(sig).decode()
    out_path = output or file_path.with_suffix(file_path.suffix + ".sig")

    metadata = (
        f"-----BEGIN JELLEO SIGNATURE-----\n"
        f"Algorithm: Ed25519\n"
        f"Signed-At: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"Signed-File: {file_path.name}\n"
        f"Signed-Bytes: {len(payload)}\n"
        f"\n"
        f"{sig_b64}\n"
        f"-----END JELLEO SIGNATURE-----\n"
    )
    out_path.write_text(metadata, encoding="utf-8")
    console.print(f"[green]Signed[/green] {file_path}")
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
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
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
    for line in sig_text.splitlines():
        if line.startswith("-----BEGIN JELLEO"):
            in_block = True
            continue
        if line.startswith("-----END JELLEO"):
            break
        if in_block and line and ":" not in line:
            sig_b64 += line.strip()
    if not sig_b64:
        raise click.ClickException("Could not extract signature bytes from sig file.")

    try:
        sig = base64.b64decode(sig_b64)
        pub.verify(sig, file_path.read_bytes())
        console.print(f"[bold green]✓ VALID[/bold green] signature on {file_path}")
    except InvalidSignature:
        console.print(f"[bold red]✗ INVALID[/bold red] signature on {file_path}")
        raise click.ClickException("Signature does not match.")
