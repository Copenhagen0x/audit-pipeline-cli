"""`audit-pipeline shadow` — live mainnet shadow audit for a Solana program.

Layer 6 — continuous monitoring. Two complementary detection modes:

  1. **Log pattern matching.** Pulls recent transactions for the target
     program and runs each tx's log lines against an invariant-violation
     regex set. Catches panics, explicit invariant assertions, overflow
     errors, etc. Cheap and runs anywhere.

  2. **Account-state-delta tracking** (production tier). Polls the raw
     bytes of one or more "watched" accounts (passed with --watch-account)
     and surfaces ANY change in state across consecutive polls. Optionally
     extracts specific u128 fields at known byte offsets (e.g. an
     insurance fund balance) and alerts on decreases beyond a threshold.
     This is what catches the F7-class drains: the log says nothing
     unusual, but the insurance counter just shrunk.

Designed to run on the existing VPS as a long-lived process (use
--interval to control polling cadence; default 60s).
"""

import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
DEFAULT_INTERVAL_SEC = 60
DEFAULT_TX_LIMIT_PER_POLL = 25


@dataclass
class Alert:
    timestamp: str
    signature: str
    slot: int
    pattern_matched: str
    log_excerpt: str
    program: str
    # Optional context for state-delta alerts
    account: str | None = None
    field_name: str | None = None
    before: str | None = None
    after: str | None = None
    delta: str | None = None


@dataclass
class WatchedField:
    """A specific u128 field at a byte offset within an account's data.

    Example for percolator insurance balance:
      WatchedField(account="CJKBStEn5VXEF9VNTChKKb5YW84MV7LycqMMziVuxJSc",
                   name="insurance_fund_balance",
                   offset=288,
                   alert_on_decrease_above=1_000_000)
    """
    account: str
    name: str
    offset: int
    alert_on_decrease_above: int = 0  # 0 = alert on any decrease


# Default invariant-violation patterns — MVP starting set.
# Extend per-program by passing --patterns-file.
DEFAULT_PATTERNS = {
    "panic_in_program_log": r"Program log:.*panicked at",
    "explicit_invariant_violation": r"Program log:.*INVARIANT VIOLATION",
    "corrupt_state_error": r"Program log:.*CorruptState",
    "overflow_error": r"Program log:.*Overflow",
    "insurance_balance_zero": r"Program log:.*insurance.*balance.*[=:]\s*0",
}


@click.group(name="shadow")
def shadow_group() -> None:
    """Live mainnet shadow audit (poll + invariant pattern check)."""


@shadow_group.command(name="start")
@click.option(
    "--program",
    "-p",
    required=True,
    help="Solana program address (base58) to watch",
)
@click.option(
    "--rpc",
    default=DEFAULT_RPC,
    show_default=True,
    help="Solana RPC endpoint (use Helius / Triton for higher rate limits)",
)
@click.option(
    "--interval",
    type=int,
    default=DEFAULT_INTERVAL_SEC,
    show_default=True,
    help="Seconds between polls",
)
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_TX_LIMIT_PER_POLL,
    show_default=True,
    help="Max txs to fetch per poll cycle",
)
@click.option(
    "--patterns-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="JSON file of {pattern_name: regex} to override defaults",
)
@click.option(
    "--watch-account",
    "watch_accounts",
    multiple=True,
    help=(
        "Account address (base58) whose raw bytes to watch for ANY change. "
        "Repeat the flag for multiple accounts. Bytes-level diffs are "
        "alerted on each poll (independent of tx log patterns)."
    ),
)
@click.option(
    "--watch-fields",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "JSON file of [{account, name, offset, alert_on_decrease_above}] "
        "describing specific u128 fields to extract + monitor. Lets you "
        "alert on e.g. insurance balance decreases without parsing logs."
    ),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir for alert + state files (defaults to <workspace>/shadow/)",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single poll cycle and exit (useful for cron / smoke tests)",
)
@click.pass_context
def shadow_start(
    ctx: click.Context,
    program: str,
    rpc: str,
    interval: int,
    limit: int,
    patterns_file: str | None,
    watch_accounts: tuple[str, ...],
    watch_fields: str | None,
    output: Path | None,
    once: bool,
) -> None:
    """Start polling RPC for new transactions on PROGRAM and check logs.

    Long-lived process. Logs alerts to <output>/alerts.jsonl. Persists
    last-seen signature to <output>/state.json so restarts don't re-scan.

    Run with --once for a single poll cycle (smoke test, cron mode).
    """
    workspace = Path(ctx.obj["workspace"])
    if output is None:
        output = workspace / "shadow"
    output.mkdir(parents=True, exist_ok=True)

    state_path = output / "state.json"
    alerts_path = output / "alerts.jsonl"
    log_path = output / "poll.log"

    # Load patterns
    patterns: dict[str, str] = dict(DEFAULT_PATTERNS)
    if patterns_file:
        patterns = json.loads(Path(patterns_file).read_text())

    import re as _re
    compiled_patterns = {name: _re.compile(p, _re.IGNORECASE) for name, p in patterns.items()}

    # Load watched fields config (optional)
    watched_fields: list[WatchedField] = []
    if watch_fields:
        for entry in json.loads(Path(watch_fields).read_text()):
            watched_fields.append(WatchedField(**entry))

    # Load persistent state (last-seen sig + previous account snapshots)
    persisted: dict[str, Any] = {}
    if state_path.exists():
        persisted = json.loads(state_path.read_text())
    last_seen_sig: str | None = persisted.get("last_seen_sig")
    prev_account_hashes: dict[str, str] = persisted.get("account_hashes", {})
    prev_field_values: dict[str, int] = persisted.get("field_values", {})

    console.print(
        f"[bold]Shadow audit starting[/bold]\n"
        f"  Program:        {program}\n"
        f"  RPC:            {rpc}\n"
        f"  Interval:       {interval}s ({'one-shot' if once else 'continuous'})\n"
        f"  Log patterns:   {len(patterns)} ({', '.join(patterns.keys())})\n"
        f"  Watched accts:  {len(watch_accounts)}\n"
        f"  Watched fields: {len(watched_fields)}\n"
        f"  Alerts:         {alerts_path}\n"
        f"  State:          {state_path}\n"
    )

    poll_count = 0
    while True:
        poll_count += 1
        # ============== 1. Tx log scanning ==============
        try:
            sigs = _fetch_recent_signatures(rpc, program, limit=limit, until=last_seen_sig)
        except Exception as e:  # noqa: BLE001 — RPC errors are expected; log and continue
            _log(log_path, f"poll #{poll_count} ERROR fetching signatures: {e}")
            console.print(f"[red]poll #{poll_count} error fetching sigs: {e}[/red]")
            if once:
                return
            time.sleep(interval)
            continue

        new_sigs = [s for s in sigs if s["signature"] != last_seen_sig]
        _log(
            log_path,
            f"poll #{poll_count}: {len(new_sigs)} new sigs "
            f"(last_seen={last_seen_sig[:10] + '...' if last_seen_sig else '(none)'})",
        )

        # Process oldest first so state advances correctly
        for sig_meta in reversed(new_sigs):
            sig = sig_meta["signature"]
            try:
                tx = _fetch_transaction(rpc, sig)
            except Exception as e:  # noqa: BLE001
                _log(log_path, f"  ERROR fetching tx {sig[:10]}...: {e}")
                continue

            alerts = _check_tx_against_patterns(tx, sig, program, compiled_patterns)
            for alert in alerts:
                _append_alert(alerts_path, alert)
                console.print(
                    f"[bold red]LOG ALERT[/bold red] "
                    f"sig=[cyan]{alert.signature[:16]}...[/cyan] "
                    f"pattern=[yellow]{alert.pattern_matched}[/yellow]"
                )

            last_seen_sig = sig

        # ============== 2. Watched account byte-diff ==============
        for acct in watch_accounts:
            try:
                acct_data = _fetch_account_data(rpc, acct)
            except Exception as e:  # noqa: BLE001
                _log(log_path, f"  ERROR fetching account {acct[:10]}...: {e}")
                continue
            if acct_data is None:
                continue
            new_hash = hashlib.sha256(acct_data).hexdigest()
            old_hash = prev_account_hashes.get(acct)
            if old_hash is not None and old_hash != new_hash:
                alert = Alert(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    signature="(state-delta poll)",
                    slot=0,
                    pattern_matched="account_state_changed",
                    log_excerpt=f"sha256 {old_hash[:16]}... -> {new_hash[:16]}...",
                    program=program,
                    account=acct,
                    before=old_hash,
                    after=new_hash,
                )
                _append_alert(alerts_path, alert)
                console.print(
                    f"[bold magenta]STATE DELTA[/bold magenta] "
                    f"account=[cyan]{acct[:16]}...[/cyan] "
                    f"hash changed"
                )
            prev_account_hashes[acct] = new_hash

        # ============== 3. Watched field extraction ==============
        for wf in watched_fields:
            try:
                acct_data = _fetch_account_data(rpc, wf.account)
            except Exception as e:  # noqa: BLE001
                _log(log_path, f"  ERROR fetching field-watched account {wf.account[:10]}...: {e}")
                continue
            if acct_data is None or len(acct_data) < wf.offset + 16:
                continue

            value = int.from_bytes(acct_data[wf.offset : wf.offset + 16], "little")
            key = f"{wf.account}:{wf.name}"
            prev_value = prev_field_values.get(key)
            if prev_value is not None and value < prev_value:
                delta = prev_value - value
                if delta > wf.alert_on_decrease_above:
                    alert = Alert(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        signature="(field-delta poll)",
                        slot=0,
                        pattern_matched=f"field_decrease:{wf.name}",
                        log_excerpt=(
                            f"{wf.name} on {wf.account[:16]}... decreased "
                            f"{prev_value} -> {value} (delta -{delta})"
                        ),
                        program=program,
                        account=wf.account,
                        field_name=wf.name,
                        before=str(prev_value),
                        after=str(value),
                        delta=str(-delta),
                    )
                    _append_alert(alerts_path, alert)
                    console.print(
                        f"[bold red]FIELD DECREASE[/bold red] "
                        f"{wf.name}: {prev_value:,} -> {value:,} "
                        f"(delta [bold]-{delta:,}[/bold])"
                    )
            prev_field_values[key] = value

        # Persist state
        state_path.write_text(
            json.dumps({
                "last_seen_sig": last_seen_sig,
                "account_hashes": prev_account_hashes,
                "field_values": prev_field_values,
            }, indent=2)
        )

        if once:
            console.print(
                f"\n[green]One-shot poll complete:[/green] "
                f"processed {len(new_sigs)} new txs, "
                f"{len(watch_accounts)} watched accts, "
                f"{len(watched_fields)} watched fields."
            )
            return

        time.sleep(interval)


@shadow_group.command(name="tail")
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Shadow output dir (defaults to <workspace>/shadow/)",
)
@click.option("--lines", "-n", type=int, default=20, show_default=True, help="Number of recent alerts to show")
@click.pass_context
def shadow_tail(ctx: click.Context, output: Path | None, lines: int) -> None:
    """Show the last N alerts from the alerts log."""
    workspace = Path(ctx.obj["workspace"])
    if output is None:
        output = workspace / "shadow"

    alerts_path = output / "alerts.jsonl"
    if not alerts_path.exists():
        console.print(f"[yellow]No alerts log at {alerts_path} yet.[/yellow]")
        return

    all_alerts = [json.loads(line) for line in alerts_path.read_text().splitlines() if line.strip()]
    recent = all_alerts[-lines:]

    if not recent:
        console.print("[dim]No alerts recorded yet.[/dim]")
        return

    table = Table(title=f"Recent alerts ({len(recent)} of {len(all_alerts)})")
    table.add_column("Time (UTC)", style="dim")
    table.add_column("Pattern", style="yellow")
    table.add_column("Signature", style="cyan")
    table.add_column("Slot", justify="right")
    table.add_column("Excerpt", style="dim")

    for a in recent:
        excerpt = a["log_excerpt"]
        if len(excerpt) > 60:
            excerpt = excerpt[:60] + "..."
        table.add_row(
            a["timestamp"],
            a["pattern_matched"],
            a["signature"][:16] + "...",
            str(a["slot"]),
            excerpt,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------


def _rpc_call(rpc_url: str, method: str, params: list[Any], timeout: int = 30) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    resp = requests.post(rpc_url, json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"RPC error from {method}: {body['error']}")
    return body.get("result")


def _fetch_recent_signatures(
    rpc_url: str,
    program: str,
    limit: int = 25,
    until: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [program, {"limit": limit}]
    if until:
        params[1]["until"] = until
    return _rpc_call(rpc_url, "getSignaturesForAddress", params) or []


def _fetch_transaction(rpc_url: str, signature: str) -> dict[str, Any] | None:
    return _rpc_call(
        rpc_url,
        "getTransaction",
        [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    )


def _fetch_account_data(rpc_url: str, account: str) -> bytes | None:
    """Fetch raw account bytes via getAccountInfo with base64 encoding."""
    result = _rpc_call(
        rpc_url,
        "getAccountInfo",
        [account, {"encoding": "base64"}],
    )
    if not result or not result.get("value"):
        return None
    data = result["value"].get("data")
    if not data or len(data) < 2:
        return None
    encoded, encoding = data[0], data[1]
    if encoding != "base64":
        return None
    return base64.b64decode(encoded)


# ---------------------------------------------------------------------------
# Pattern checking
# ---------------------------------------------------------------------------


def _check_tx_against_patterns(
    tx: dict[str, Any] | None,
    signature: str,
    program: str,
    compiled_patterns: dict[str, "Any"],
) -> list[Alert]:
    if tx is None:
        return []
    meta = tx.get("meta") or {}
    log_messages: list[str] = meta.get("logMessages") or []
    slot: int = tx.get("slot", 0)

    alerts: list[Alert] = []
    timestamp = datetime.now(timezone.utc).isoformat()

    for log in log_messages:
        for name, pattern in compiled_patterns.items():
            if pattern.search(log):
                alerts.append(
                    Alert(
                        timestamp=timestamp,
                        signature=signature,
                        slot=slot,
                        pattern_matched=name,
                        log_excerpt=log,
                        program=program,
                    )
                )
    return alerts


def _append_alert(alerts_path: Path, alert: Alert) -> None:
    # Drop None fields so the JSONL stays compact
    payload = {k: v for k, v in asdict(alert).items() if v is not None}
    with alerts_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def _log(log_path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")
