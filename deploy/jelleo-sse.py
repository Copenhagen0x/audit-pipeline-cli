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
    """Return the most-recent cycle directory under ``workspace/hunts/``.

    Filtering — only consider cycles that:
      1. Are directories (skip stray files).
      2. Have a ``hunt.log.jsonl`` (the orchestrator creates the
         cycle dir before Layer 1 starts, but ``hunt.log.jsonl``
         doesn't appear until the first event lands). Without this
         filter, a freshly-mkdir'd cycle dir is returned, the tailer
         busy-loops waiting for the log to appear, and dashboards
         can see a phantom ``cycle_active`` for a half-set-up cycle.
      3. Are NOT retracted (a ``retraction.json`` sidecar means the
         cycle is dead — never return it as the "active" cycle).

    Sort key — `mtime` of `hunt.log.jsonl` itself (NOT the cycle dir),
    so that an actively-being-written cycle out-ranks a stale empty
    cycle dir that happens to have a recent dir-mtime from `mkdir`.
    """
    hunts = workspace / "hunts"
    if not hunts.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in hunts.iterdir():
        if not p.is_dir():
            continue
        if (p / "retraction.json").is_file():
            continue
        log_path = p / "hunt.log.jsonl"
        if not log_path.is_file():
            continue
        try:
            candidates.append((log_path.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


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

    Robustness:
      * Partial-line buffer. ``readline()`` on a live-tailed file can
        return mid-line bytes if the orchestrator's writer flushed
        before its trailing ``\\n``. We accumulate into ``buf`` until
        we see ``\\n`` and only parse JSON once the line is complete.
        Without this, the JSON decoder swallows partial events and
        the dashboard loses entire batches under heavy concurrent
        writes.
      * Inode-rotation detection. If the log is truncated or unlinked
        (cycle dir wiped + recreated), we re-stat the path each loop
        iteration; if size shrank below our position, we reopen.
      * Bounded inter-cycle retry. On OSError opening a new cycle we
        back off rather than busy-loop.
    """
    current_path: Path | None = None
    fh = None
    buf = ""
    while True:
        cycle = _latest_cycle_in(workspace)
        if cycle is None:
            await asyncio.sleep(1)
            continue
        log_path = cycle / "hunt.log.jsonl"
        if not log_path.exists():
            await asyncio.sleep(1)
            continue

        # Inode-rotation / truncation detection — if the file shrank
        # below our current read position, reopen.
        needs_reopen = log_path != current_path
        if not needs_reopen and fh is not None:
            try:
                pos = fh.tell()
                sz = log_path.stat().st_size
                if sz < pos:
                    needs_reopen = True
            except (OSError, ValueError):
                needs_reopen = True

        if needs_reopen:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            buf = ""  # any half-read line from the old file is invalid
            try:
                fh = open(log_path, encoding="utf-8", errors="replace")
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

        # Read whatever bytes are available. read() with no arg on a
        # tailed file returns "" when no new data; we accumulate into
        # buf and emit only complete lines.
        chunk = fh.read()
        if not chunk:
            await asyncio.sleep(0.1)
            continue
        buf += chunk
        while True:
            nl = buf.find("\n")
            if nl < 0:
                break  # incomplete trailing line — wait for more bytes
            line = buf[:nl].strip()
            buf = buf[nl + 1:]
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


_dropped_event_count: dict[str, int] = defaultdict(int)


async def _broadcast(event: dict) -> None:
    customer = event.get("customer_id", "demo")
    queues = set(_subscribers[customer]) | set(_subscribers["*"])
    for q in queues:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Slow client. Drop the OLDEST event from the queue and
            # retry the new one — newer events are usually more
            # relevant for a live dashboard. Also stamp a sentinel
            # so the operator sees drops happened on this client.
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            _dropped_event_count[customer] += 1
            # Surface to stderr every 100 drops so a flood is visible
            # in journalctl without spamming it.
            n = _dropped_event_count[customer]
            if n % 100 == 1:
                print(
                    f"sse: queue overflow for customer={customer!r}, "
                    f"dropped {n} events so far",
                    file=sys.stderr,
                    flush=True,
                )


async def _write_sse(writer: asyncio.StreamWriter, event: dict) -> None:
    payload = json.dumps(event, default=str)
    writer.write(f"data: {payload}\n\n".encode())
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
    # Multi-cell on-connect cycle pick: for OSec (12 cells), if any
    # cell has an in-progress cycle we MUST pick that one — even if
    # another cell has a more-recently-touched-but-finished cycle.
    # Otherwise the dashboard sees a `cycle_complete` for cell A
    # while cell B is actively running and paints "complete" while
    # events still stream in for B.
    #
    # Two-pass selection:
    #   pass 1: in-progress cells (no hunt_summary.json, no publish-blocked,
    #           no retraction). Among these, pick newest log mtime.
    #   pass 2: if no in-progress found, fall back to the newest
    #           finished cell so the dashboard at least shows the
    #           most recent completion.
    def _cycle_is_done(p: Path) -> bool:
        return (
            (p / "hunt_summary.json").is_file()
            or (p / ".publish-blocked").is_file()
            or (p / "retraction.json").is_file()
        )
    latest_cycle_path: Path | None = None
    latest_mtime: float = 0.0
    for ws in customer_workspaces:
        c = _latest_cycle_in(ws)
        if c and not _cycle_is_done(c):
            try:
                mt = (c / "hunt.log.jsonl").stat().st_mtime
            except OSError:
                mt = c.stat().st_mtime
            if mt > latest_mtime:
                latest_mtime = mt
                latest_cycle_path = c
    if latest_cycle_path is None:
        # No in-progress cell — fall back to most-recent finished one.
        for ws in customer_workspaces:
            c = _latest_cycle_in(ws)
            if c:
                try:
                    mt = (c / "hunt.log.jsonl").stat().st_mtime
                except OSError:
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
