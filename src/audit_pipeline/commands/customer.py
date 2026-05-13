"""`audit-pipeline customer` — manage the multi-tenant customer registry.

Tier 5 #27. Operates on ``<workspace>/customers.json`` and the per-customer
directory tree under ``<workspace>/customers/<id>/`` (Tier 5 #26 isolation).

Subcommands:

  add         register a new customer (creates dir + derived keypair)
  remove      remove a customer from the registry (keeps dir on disk by default)
  list        list registered customers (table or JSON)
  show        print one customer's full record (registry + key paths)
  rotate-key  re-derive the customer's keypair under a fresh salt
  pubkey      print one customer's public key (for sharing / pinning)
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline import customers as customers_mod
from audit_pipeline.commands.customer_dashboard import build_dashboard_cmd
from audit_pipeline.commands.sign import default_key_path

console = Console()


@click.group(name="customer")
def customer_cmd() -> None:
    """Multi-tenant customer registry (Tier 5 #26 + #27 + #28)."""


# Register subcommands defined in sibling modules
customer_cmd.add_command(build_dashboard_cmd)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@customer_cmd.command(name="add")
@click.argument("customer_id")
@click.option("--name", required=True, help="Display name (e.g. 'OtterSec · Whirlpools team').")
@click.option("--protocol", "protocol_name", required=True,
              help="Protocol the customer represents (e.g. 'Orca Whirlpools').")
@click.option("--tier", default="Production", show_default=True,
              type=click.Choice(["Foundation", "Production", "Ceiling"]),
              help="Engagement tier (Foundation / Production / Ceiling).")
@click.option("--target-match", default=None,
              help="Substring (case-insensitive) on target name that scopes "
                   "this customer's findings. Defaults to the customer id.")
@click.option("--contact-email", default=None,
              help="Primary contact email (added to notifier.json by hand for now).")
@click.option("--no-key", is_flag=True, default=False,
              help="Skip per-customer keypair derivation (you can rotate-key later).")
@click.option("--platform-key", type=click.Path(path_type=Path), default=None,
              help="Platform private key (default: <workspace>/keys/jelleo.ed25519).")
@click.option("--allow-guessable-id", is_flag=True, default=False,
              help=(
                  "Allow customer_id values that are short / dictionary-word "
                  "(like 'demo' or 'ottersec'). Without this flag, customer_id "
                  "must be at least 16 chars to prevent URL enumeration of "
                  "the token-gated /customer/<id>/manifest.json endpoint. "
                  "Pass an unguessable id like `cus_$(openssl rand -hex 16)`."
              ))
@click.option("--logo-path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Path to a customer logo file (SVG preferred, PNG OK). "
                   "Copied into customers/<id>/branding/ and shown in the "
                   "dashboard nav so the customer knows it's theirs.")
@click.option("--hero-title", default=None,
              help="Display title shown in the customer dashboard hero "
                   "(e.g. 'OtterSec × Jelleo · Vendor Evaluation'). Defaults "
                   "to '<customer name> · audit dashboard'.")
@click.option("--footer-text", default=None,
              help="Footer line on the customer dashboard "
                   "(default: 'Powered by Jelleo · continuous audit').")
@click.option("--pdf-watermark", default=None,
              help="Watermark text for the PDF cover + page corners "
                   "(e.g. 'Confidential — OtterSec evaluation').")
@click.pass_context
def add_cmd(
    ctx: click.Context,
    customer_id: str,
    name: str,
    protocol_name: str,
    tier: str,
    target_match: str | None,
    contact_email: str | None,
    no_key: bool,
    platform_key: Path | None,
    allow_guessable_id: bool,
    logo_path: Path | None,
    hero_title: str | None,
    footer_text: str | None,
    pdf_watermark: str | None,
) -> None:
    """Register a new customer and derive its signing keypair."""
    workspace = Path(ctx.obj["workspace"])
    today = datetime.now(timezone.utc).date().isoformat()

    # FIX D-#3: customer_id flows into the URL path
    # `api.jelleo.com/customer/<id>/manifest.json` which currently has no
    # auth gate at the nginx layer. Defense: require the id be long enough
    # to be enumeration-resistant (16+ chars of high-entropy hex/base32).
    # `demo` and similarly-short ids are grandfathered ONLY via the
    # `--allow-guessable-id` escape hatch — used for the public
    # demo dashboard which IS intended to be publicly readable.
    if not allow_guessable_id:
        if len(customer_id) < 16 or not re.match(r"^[A-Za-z0-9_-]+$", customer_id):
            raise click.ClickException(
                f"refusing customer_id {customer_id!r}: must be 16+ chars of "
                f"[A-Za-z0-9_-] (URL-enumeration resistant). Generate one via "
                f"  $ python -c 'import secrets; print(\"cus_\" + secrets.token_hex(16))'\n"
                f"or pass --allow-guessable-id if this is intentional (e.g. "
                f"the public demo)."
            )

    # Assemble + validate branding dict from CLI args (if any provided).
    # Note: only IDENTITY/COPY fields here, not color palette. Jelleo's
    # visual identity (dark + amber) is shared across all customer
    # dashboards; the per-customer surface is logo, hero title, footer,
    # PDF watermark. Their nameplate on the door — not redecorated walls.
    branding: dict | None = None
    if any((logo_path, hero_title, footer_text, pdf_watermark)):
        branding = {}
        if hero_title:
            branding["hero_title"] = hero_title
        if footer_text:
            branding["footer_text"] = footer_text
        if pdf_watermark:
            branding["pdf_watermark"] = pdf_watermark
        # Logo handling: copy into the customer's branding dir + record
        # the workspace-relative path. We don't store an absolute path
        # in customers.json since the registry may move workspaces.
        if logo_path:
            brand_dir = customers_mod.customer_branding_dir(workspace, customer_id)
            brand_dir.mkdir(parents=True, exist_ok=True)
            ext = logo_path.suffix.lower() or ".svg"
            dst = brand_dir / f"logo{ext}"
            shutil.copyfile(logo_path, dst)
            # Store workspace-relative
            branding["logo_path"] = str(dst.relative_to(workspace))

    try:
        entry = customers_mod.add_customer(
            workspace,
            customer_id=customer_id,
            name=name,
            protocol_name=protocol_name,
            tier=tier,
            target_match=target_match,
            contact_email=contact_email,
            since=today,
            branding=branding,
        )
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    console.print(f"[green]Registered[/green] customer [bold]{customer_id}[/bold] ({tier})")
    console.print(f"  name:          {name}")
    console.print(f"  protocol:      {protocol_name}")
    console.print(f"  target_match:  {entry['target_match']}")
    if contact_email:
        console.print(f"  contact_email: {contact_email}")
    if branding:
        console.print(
            "  branding:      "
            + ", ".join(f"{k}={v!r}" for k, v in entry["branding"].items())
        )

    if no_key:
        console.print("[yellow]--no-key set; skipped keypair derivation.[/yellow]")
        return

    plat = platform_key or default_key_path(workspace)
    try:
        priv_path, pub_path = customers_mod.derive_and_persist_customer_keypair(
            workspace, customer_id, plat,
        )
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    console.print("[green]Derived[/green] customer keypair from platform key")
    console.print(f"  private: {priv_path} (mode 600)")
    console.print(f"  public:  {pub_path}")
    console.print()
    console.print("[dim]Public key contents:[/dim]")
    console.print(pub_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


@customer_cmd.command(name="remove")
@click.argument("customer_id")
@click.option("--purge", is_flag=True, default=False,
              help="Also delete <workspace>/customers/<id>/ on disk (irreversible).")
@click.pass_context
def remove_cmd(ctx: click.Context, customer_id: str, purge: bool) -> None:
    """Remove a customer from the registry. By default keeps per-customer dir on disk."""
    workspace = Path(ctx.obj["workspace"])
    try:
        removed = customers_mod.remove_customer(workspace, customer_id)
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    console.print(f"[green]Removed[/green] customer [bold]{customer_id}[/bold] from registry")

    cdir = customers_mod.customer_dir(workspace, customer_id)
    if purge and cdir.is_dir():
        shutil.rmtree(cdir)
        console.print(f"[yellow]Purged[/yellow] {cdir}")
    elif cdir.is_dir():
        console.print(f"[dim]Per-customer dir preserved at {cdir} (pass --purge to wipe)[/dim]")

    console.print(f"[dim]Removed entry: {json.dumps(removed)}[/dim]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@customer_cmd.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit JSON instead of a table.")
@click.pass_context
def list_cmd(ctx: click.Context, as_json: bool) -> None:
    """List registered customers."""
    workspace = Path(ctx.obj["workspace"])
    customers = customers_mod.load_registry(workspace)

    if as_json:
        click.echo(json.dumps(customers, indent=2, sort_keys=True))
        return

    if not customers:
        console.print("[dim]No customers registered.[/dim]")
        console.print("[dim]The hard-coded `demo` customer is still served at "
                      "/customer/demo/ via dashboard.py fallback.[/dim]")
        return

    tbl = Table(title="Customers", show_lines=False)
    tbl.add_column("id", style="bold")
    tbl.add_column("tier")
    tbl.add_column("protocol")
    tbl.add_column("name")
    tbl.add_column("since", style="dim")
    tbl.add_column("key", style="dim")
    for c in customers:
        priv = customers_mod.customer_priv_key_path(workspace, c["id"])
        key_state = "✓" if priv.exists() else "—"
        tbl.add_row(
            c.get("id", ""),
            c.get("tier", ""),
            c.get("protocol_name", ""),
            c.get("name", ""),
            c.get("since", ""),
            key_state,
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@customer_cmd.command(name="show")
@click.argument("customer_id")
@click.pass_context
def show_cmd(ctx: click.Context, customer_id: str) -> None:
    """Print one customer's full record (registry + key paths)."""
    workspace = Path(ctx.obj["workspace"])
    entry = customers_mod.get_customer(workspace, customer_id)
    if not entry:
        raise click.ClickException(f"customer '{customer_id}' not registered")

    cdir = customers_mod.customer_dir(workspace, customer_id)
    priv = customers_mod.customer_priv_key_path(workspace, customer_id)
    pub = customers_mod.customer_pub_key_path(workspace, customer_id)

    payload = {
        "registry":   entry,
        "paths": {
            "customer_dir":  str(cdir),
            "private_key":   str(priv),
            "public_key":    str(pub),
            "private_key_present": priv.exists(),
            "public_key_present":  pub.exists(),
        },
    }
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# rotate-key
# ---------------------------------------------------------------------------


@customer_cmd.command(name="rotate-key")
@click.argument("customer_id")
@click.option("--platform-key", type=click.Path(path_type=Path), default=None,
              help="Platform private key (default: <workspace>/keys/jelleo.ed25519).")
@click.pass_context
def rotate_key_cmd(
    ctx: click.Context, customer_id: str, platform_key: Path | None,
) -> None:
    """Re-derive the customer's keypair under a fresh random salt.

    The customer's old public key is OBSOLETED. Anyone pinning the old key
    must update. Use sparingly — typically after a suspected key compromise
    or when ending then restarting a customer relationship.
    """
    workspace = Path(ctx.obj["workspace"])
    if not customers_mod.get_customer(workspace, customer_id):
        raise click.ClickException(f"customer '{customer_id}' not registered")

    plat = platform_key or default_key_path(workspace)
    salt = secrets.token_bytes(16)

    try:
        priv_path, pub_path = customers_mod.derive_and_persist_customer_keypair(
            workspace, customer_id, plat, salt=salt, overwrite=True,
        )
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    # POST-AUDIT FIX (2026-05-12): also rotate the URL-token salt stored
    # in customers.json. Previously rotate-key only rotated the Ed25519
    # keypair salt — outstanding URL tokens remained valid because the
    # HMAC key derivation didn't consume any per-customer revocation
    # state. Now we write a fresh url_salt here so all in-flight tokens
    # fail constant-time verify after this command runs.
    url_salt = secrets.token_bytes(16)
    try:
        customers_mod.set_customer_url_salt(workspace, customer_id, url_salt)
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    console.print(f"[yellow]Rotated[/yellow] customer key for [bold]{customer_id}[/bold]")
    console.print(f"  private: {priv_path}")
    console.print(f"  public:  {pub_path}")
    console.print(
        "  [dim]url_salt rotated → all outstanding URL tokens invalidated[/dim]"
    )
    console.print()
    console.print("[dim]New public key:[/dim]")
    console.print(pub_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# pubkey
# ---------------------------------------------------------------------------


@customer_cmd.command(name="pubkey")
@click.argument("customer_id")
@click.pass_context
def pubkey_cmd(ctx: click.Context, customer_id: str) -> None:
    """Print one customer's public key (PEM)."""
    workspace = Path(ctx.obj["workspace"])
    pub = customers_mod.customer_pub_key_path(workspace, customer_id)
    if not pub.exists():
        raise click.ClickException(
            f"no public key at {pub}. Run `audit-pipeline customer add {customer_id}` "
            f"or `customer rotate-key {customer_id}`."
        )
    click.echo(pub.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# issue-url-token
# ---------------------------------------------------------------------------


@customer_cmd.command(name="issue-url-token")
@click.argument("customer_id")
@click.option("--ttl-days", type=int, default=7, show_default=True,
              help="Token expiry, in days from now.")
@click.option("--platform-key", type=click.Path(path_type=Path), default=None,
              help="Platform private key (default: <workspace>/keys/jelleo.ed25519)")
@click.pass_context
def issue_url_token_cmd(
    ctx: click.Context,
    customer_id: str,
    ttl_days: int,
    platform_key: Path | None,
) -> None:
    """Issue an HMAC-signed URL access token for ``customer_id``.

    The token can be appended to the customer's manifest URL like
    ``...?t=<token>``. Verification is stateless — the server
    recomputes HMAC(customer_id || expiry) under a key derived from the
    platform private key and compares constant-time. Tokens are
    revocable via ``customer rotate-key`` (changing the salt invalidates
    all outstanding tokens for that customer).
    """
    workspace = Path(ctx.obj["workspace"])
    customers_mod.validate_customer_id(customer_id)
    if not any(c.get("id") == customer_id for c in customers_mod.load_registry(workspace)):
        raise click.ClickException(f"customer '{customer_id}' is not registered")
    platform_priv_path = platform_key or default_key_path(workspace)
    try:
        seed = customers_mod.load_platform_priv_seed(platform_priv_path)
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))
    # POST-AUDIT FIX (2026-05-12): thread the customer's url_salt through
    # the issuer. Pre-fix, the salt was effectively b"" for every customer,
    # making `rotate-key` cosmetic. Now the registry-backed salt becomes
    # part of the HMAC key derivation; rotating the salt invalidates all
    # outstanding tokens.
    url_salt = customers_mod.get_customer_url_salt(workspace, customer_id)
    token, exp = customers_mod.issue_customer_url_token(
        seed, customer_id,
        ttl_seconds=ttl_days * 24 * 3600,
        salt=url_salt,
    )
    console.print(f"[green]issued[/green] token for [cyan]{customer_id}[/cyan]")
    console.print(f"  token:      {token}")
    console.print(f"  expires_at: {exp} (~{ttl_days}d)")
