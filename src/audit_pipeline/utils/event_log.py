"""Cycle event emission helper.

The SSE service (`deploy/jelleo-sse.py`) tails the active cycle's
``hunt.log.jsonl`` and broadcasts each JSON line to subscribed customer
dashboards. Anything that wants to ship a live event to dashboards just
needs to append a JSON line to that file.

`emit_event(kind, **fields)` resolves the active log path and appends a
single line:

  {"event": kind, "ts": <unix>, ...fields}

Path resolution priority:
  1. ``JELLEO_CYCLE_LOG_PATH`` env var (set by hunt.py before spawning
     recon / debate subprocesses, so they emit into the same log)
  2. Latest cycle dir under ``JELLEO_WORKSPACE/hunts/``

No-op if neither resolves (offline test contexts, dev runs without a
workspace, etc). Never raises — broken event emission must NEVER crash
the cycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _resolve_log_path() -> Path | None:
    p = os.environ.get("JELLEO_CYCLE_LOG_PATH", "").strip()
    if p:
        candidate = Path(p)
        # Parent must exist; the file itself may not yet
        if candidate.parent.is_dir():
            return candidate
    ws = os.environ.get("JELLEO_WORKSPACE", "").strip()
    if not ws:
        return None
    hunts = Path(ws) / "hunts"
    if not hunts.is_dir():
        return None
    try:
        cycles = [p for p in hunts.iterdir() if p.is_dir()]
    except OSError:
        return None
    if not cycles:
        return None
    cycles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cycles[0] / "hunt.log.jsonl"


def emit_event(kind: str, **fields: object) -> None:
    """Append a JSON-line event to the active cycle's hunt.log.jsonl.

    Always safe to call. Returns silently on every error path.
    """
    path = _resolve_log_path()
    if path is None:
        return
    payload = {"event": kind, "ts": round(time.time(), 3), **fields}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass


__all__ = ["emit_event"]
