"""`audit-pipeline shadow` — live mainnet shadow audit for a Solana program.

Layer 6 — continuous monitoring. Polls a Solana RPC for recent transactions
on a target program, fetches each one, and checks the transaction logs
against a list of invariant-violation patterns. Writes alerts to a file
the user can tail.

For the MVP: pattern-based log inspection (cheap, fast, runs anywhere).
The LiteSVM replay step (full state-diff invariant check) is scaffolded
but not yet wired to a verifier — that's the next iteration.

Designed to run on the existing VPS as a long-lived process (use
--interval to control polling cadence; default 60s).
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console
from rich.live import Live
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

    # Load state (last-seen signature)
    last_seen_sig: str | None = None
    if state_path.exists():
        state = json.loads(state_path.read_text())
        last_seen_sig = state.get("last_seen_sig")

    console.print(
        f"[bold]Shadow audit starting[/bold]\n"
        f"  Program:  {program}\n"
        f"  RPC:      {rpc}\n"
        f"  Interval: {interval}s ({'one-shot' if once else 'continuous'})\n"
        f"  Patterns: {len(patterns)} ({', '.join(patterns.keys())})\n"
        f"  Alerts:   {alerts_path}\n"
        f"  State:    {state_path}\n"
    )

    poll_count = 0
    while True:
        poll_count += 1
        try:
            sigs = _fetch_recent_signatures(rpc, program, limit=limit, until=last_seen_sig)
        except Exception as e:  # noqa: BLE001 — RPC errors are expected; log and continue
            _log(log_path, f"poll #{poll_count} ERROR fetching signatures: {e}")
            console.print(f"[red]poll #{poll_count} error: {e}[/red]")
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
        new_sigs_chronological = list(reversed(new_sigs))
        for sig_meta in new_sigs_chronological:
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
                    f"[bold red]ALERT[/bold red] "
                    f"sig=[cyan]{alert.signature[:16]}...[/cyan] "
                    f"pattern=[yellow]{alert.pattern_matched}[/yellow]"
                )

            last_seen_sig = sig
            state_path.write_text(json.dumps({"last_seen_sig": last_seen_sig}, indent=2))

        if once:
            console.print(f"\n[green]One-shot poll complete: processed {len(new_sigs_chronological)} new txs.[/green]")
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
    with alerts_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(alert.__dict__) + "\n")


def _log(log_path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")
