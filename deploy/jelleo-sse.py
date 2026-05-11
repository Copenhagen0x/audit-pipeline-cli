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

WORKSPACE = Path("/root/audit_runs/percolator-live")
HUNTS_DIR = WORKSPACE / "hunts"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
CRLF = b"\r\n"
# CORS is handled by nginx at the api.jelleo.com server level. This
# service does not add Access-Control-Allow-* headers — that would
# create duplicates in production and conflict with nginx's policy.


def _latest_cycle() -> Path | None:
    if not HUNTS_DIR.exists():
        return None
    cycles = [p for p in HUNTS_DIR.iterdir() if p.is_dir()]
    if not cycles:
        return None
    cycles.sort(key=lambda p: p.stat().st_mtime)
    return cycles[-1]


async def _tail_active_log() -> None:
    """Background task: stream hunt.log.jsonl lines into broadcaster."""
    current_path: Path | None = None
    fh = None

    while True:
        cycle = _latest_cycle()
        if cycle is None:
            await asyncio.sleep(2)
            continue
        log_path = cycle / "hunt.log.jsonl"
        if not log_path.exists():
            await asyncio.sleep(2)
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
                })
            except OSError:
                fh = None
                current_path = None
                await asyncio.sleep(2)
                continue

        line = fh.readline()
        if not line:
            await asyncio.sleep(0.5)
            continue

        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event["_cycle"] = cycle.name
        await _broadcast(event)


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

    cycle = _latest_cycle()
    if cycle:
        await _write_sse(writer, {
            "event": "cycle_active",
            "cycle": cycle.name,
            "ts": time.time(),
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
