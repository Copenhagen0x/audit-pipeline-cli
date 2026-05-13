#!/usr/bin/env python3
"""Jelleo SSE service — live event stream for customer dashboards.

Tails the active hunt cycle's ``hunt.log.jsonl`` and broadcasts each
JSON line to subscribers as a Server-Sent Event. Auto-detects when a
new cycle starts (latest-mtime dir under HUNTS_DIR) and switches the
tail target. Sends a 15-second heartbeat comment line so nginx and the
browser keep the connection alive between events.

Stdlib only (asyncio + raw HTTP/1.1). Listens on 127.0.0.1:8765;
nginx at api.jelleo.com proxies ``/events/*`` here.

Endpoint:
    GET /events/<customer_id>     SSE stream (Content-Type text/event-stream)
    OPTIONS /events/<customer_id> CORS preflight

The customer_id segment is informational today (every dashboard sees
every event from the active cycle). Per-customer filtering can be
layered on later by joining each event against the per-customer
target_match in the findings DB.

Install via deploy/jelleo-sse.service + nginx /events/ proxy block.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765

# Multi-workspace map: customer_id -> list of workspace dirs to watch.
# Each workspace dir must contain a `hunts/` subdir; the tailer picks
# the most recent cycle in that subdir and tails its hunt.log.jsonl.
#
# For OtterSec: every cell workspace (4 langs × 3 sizes) is watched
# so whichever cell is currently running streams events to ottersec
# subscribers in real time. The engine fires one cell at a time, so
# only the active cell's tailer produces events.
_OSEC_BASE = Path("/root/audit_runs/ottersec-eval/workspaces")
_OSEC_CELLS = [
    "solana-small", "solana-medium", "solana-large",
    "c-small",      "c-medium",      "c-large",
    "solidity-small", "solidity-medium", "solidity-large",
    "aptos-small",  "aptos-medium",  "aptos-large",
]
WATCHED_WORKSPACES: dict[str, list[Path]] = {
    "demo":     [Path("/root/audit_runs/percolator-live")],
    "ottersec": [_OSEC_BASE / cell for cell in _OSEC_CELLS],
}

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
CRLF = b"\r\n"
# CORS is handled by nginx at the api.jelleo.com server level. This
# service does not add Access-Control-Allow-* headers — that would
# create duplicates in production and conflict with nginx's policy.


def _latest_cycle_in(workspace: Path) -> Path | None:
    hunts = workspace / "hunts"
    if not hunts.exists():
        return None
    cycles = [p for p in hunts.iterdir() if p.is_dir()]
    if not cycles:
        return None
    cycles.sort(key=lambda p: p.stat().st_mtime)
    return cycles[-1]


def _latest_cycle() -> Path | None:
    """Back-compat shim — returns latest cycle from demo workspace."""
    demo_ws = WATCHED_WORKSPACES.get("demo", [None])[0]
    return _latest_cycle_in(demo_ws) if demo_ws else None


async def _tail_one_workspace(customer_id: str, workspace: Path) -> None:
    """Tail the latest cycle's hunt.log.jsonl in a single workspace.

    Spawned once per workspace at startup. Picks up new cycles when
    they appear under workspace/hunts/. Events are stamped with the
    given customer_id so _broadcast routes them to the right
    subscribers (NOT every customer's subscribers).
    """
    current_path: Path | None = None
    fh = None
    while True:
        cycle = _latest_cycle_in(workspace)
        if cycle is None:
            await asyncio.sleep(1)
            continue
        log_path = cycle / "hunt.log.jsonl"
        if not log_path.exists():
            await asyncio.sleep(1)
            continue

        if log_path != current_path:
            if fh is not None:
                fh.close()
            try:
                fh = open(log_path, "r")
                fh.seek(0, 2)
                current_path = log_path
                await _broadcast({
                    "event": "cycle_active",
                    "cycle": cycle.name,
                    "ts": time.time(),
                    "customer_id": customer_id,
                    "workspace": str(workspace),
                })
            except OSError:
                fh = None
                current_path = None
                await asyncio.sleep(1)
                continue

        line = fh.readline()
        if not line:
            # Live tail — short sleep so events propagate sub-second
            await asyncio.sleep(0.1)
            continue

        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event["_cycle"] = cycle.name
        event["customer_id"] = customer_id  # FORCE override per-workspace
        await _broadcast(event)


async def _tail_active_log() -> None:
    """Spawn one tailer per workspace listed in WATCHED_WORKSPACES.

    Replaces the old single-workspace tailer. Each tailer runs
    independently; the broadcast routing layer keeps them isolated.
    """
    tasks = []
    for customer_id, workspaces in WATCHED_WORKSPACES.items():
        for ws in workspaces:
            tasks.append(asyncio.create_task(_tail_one_workspace(customer_id, ws)))
    # Never returns — gather forever
    await asyncio.gather(*tasks)


async def _broadcast(event: dict) -> None:
    customer = event.get("customer_id", "demo")
    queues = set(_subscribers[customer]) | set(_subscribers["*"])
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _write_sse(writer: asyncio.StreamWriter, event: dict) -> None:
    payload = json.dumps(event, default=str)
    writer.write(f"data: {payload}\n\n".encode("utf-8"))
    await writer.drain()


async def _respond_options(writer) -> None:
    # CORS headers are added by nginx; we just return 204.
    lines = [
        b"HTTP/1.1 204 No Content",
        b"Content-Length: 0",
        b"Connection: close",
        b"",
        b"",
    ]
    writer.write(CRLF.join(lines))
    await writer.drain()


async def _respond_status(writer, code: int, msg: str) -> None:
    body = msg.encode()
    lines = [
        f"HTTP/1.1 {code} {msg}".encode("ascii"),
        b"Content-Type: text/plain",
        f"Content-Length: {len(body)}".encode("ascii"),
        b"Connection: close",
        b"",
        b"",
    ]
    writer.write(CRLF.join(lines) + body)
    await writer.drain()


async def _handle_sse(writer, customer_id) -> None:
    # CORS headers added by nginx; we just stream the SSE response.
    headers = [
        b"HTTP/1.1 200 OK",
        b"Content-Type: text/event-stream; charset=utf-8",
        b"Cache-Control: no-cache, no-store, must-revalidate",
        b"Pragma: no-cache",
        b"Connection: keep-alive",
        b"X-Accel-Buffering: no",
        b"",
        b"",
    ]
    writer.write(CRLF.join(headers))
    await writer.drain()

    await _write_sse(writer, {
        "event": "sse_connected",
        "customer_id": customer_id,
        "ts": time.time(),
    })

    # On-connect cycle-active hint: scan THIS customer's watched
    # workspaces and emit a cycle_active ONLY for cycles that are
    # actually in_progress (hunt_summary.json does NOT exist).
    #
    # Without the in_progress gate, every page refresh on a finished
    # cycle re-painted "L1 running" — the dashboard saw the on-connect
    # cycle_active and set L1=active, even though the cycle ended hours
    # ago. Operator caught this with the report:
    # "after L2 was done all waterfall items fired as done and now it
    # shows that L1 is running and all of the others are in queue".
    customer_workspaces = WATCHED_WORKSPACES.get(customer_id, [])
    latest_cycle_path: Path | None = None
    latest_mtime: float = 0.0
    for ws in customer_workspaces:
        c = _latest_cycle_in(ws)
        if c:
            mt = c.stat().st_mtime
            if mt > latest_mtime:
                latest_mtime = mt
                latest_cycle_path = c
    if latest_cycle_path:
        # Cycle is "in progress" if hunt_summary.json does NOT exist
        # in its dir (the orchestrator writes that file ONLY at end of
        # cycle). A .publish-blocked sentinel also signals end-of-cycle.
        hunt_summary = latest_cycle_path / "hunt_summary.json"
        publish_blocked = latest_cycle_path / ".publish-blocked"
        retraction = latest_cycle_path / "retraction.json"
        cycle_is_done = (
            hunt_summary.is_file()
            or publish_blocked.is_file()
            or retraction.is_file()
        )
        if not cycle_is_done:
            await _write_sse(writer, {
                "event": "cycle_active",
                "cycle": latest_cycle_path.name,
                "ts": time.time(),
                "customer_id": customer_id,
            })
        else:
            # Cycle is done — emit a cycle_complete instead so the
            # dashboard paints "Cycle complete" + all layers done
            # rather than waiting for the manifest tick.
            await _write_sse(writer, {
                "event": "cycle_complete",
                "cycle": latest_cycle_path.name,
                "ts": time.time(),
                "customer_id": customer_id,
            })

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers[customer_id].add(queue)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await _write_sse(writer, event)
            except asyncio.TimeoutError:
                writer.write(b": heartbeat\n\n")
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        _subscribers[customer_id].discard(queue)


async def _handle_client(reader, writer) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        request_line = request_line.decode("ascii", errors="replace").strip()
        method, _, rest = request_line.partition(" ")
        path, _, _ = rest.partition(" ")

        # Drain headers (we don't need any specific values now that nginx owns CORS).
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line.strip():
                break

        m = re.match(r"^/events/([A-Za-z0-9_-]{1,64})(?:\?.*)?$", path)
        if method == "OPTIONS":
            await _respond_options(writer)
            return
        if method != "GET" or m is None:
            await _respond_status(writer, 404, "Not Found")
            return

        await _handle_sse(writer, m.group(1))
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        print(f"handler error: {e!r}", file=sys.stderr, flush=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(_handle_client, LISTEN_HOST, LISTEN_PORT)
    asyncio.create_task(_tail_active_log())
    print(f"jelleo-sse listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
