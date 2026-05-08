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
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline import customers as customers_mod
from audit_pipeline.commands.sign import default_key_path

console = Console()


@click.group(name="customer")
def customer_cmd() -> None:
    """Multi-tenant customer registry (Tier 5 #26 + #27 + #28)."""


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
) -> None:
    """Register a new customer and derive its signing keypair."""
    workspace = Path(ctx.obj["workspace"])
    today = datetime.now(timezone.utc).date().isoformat()

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
        )
    except customers_mod.CustomerError as e:
        raise click.ClickException(str(e))

    console.print(f"[green]Registered[/green] customer [bold]{customer_id}[/bold] ({tier})")
    console.print(f"  name:          {name}")
    console.print(f"  protocol:      {protocol_name}")
    console.print(f"  target_match:  {entry['target_match']}")
    if contact_email:
        console.print(f"  contact_email: {contact_email}")

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

    console.print(f"[yellow]Rotated[/yellow] customer key for [bold]{customer_id}[/bold]")
    console.print(f"  private: {priv_path}")
    console.print(f"  public:  {pub_path}")
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
