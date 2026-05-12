"""Daily Claude-spend cap.

Tracks total spend per UTC calendar day in a small JSON file alongside
the workspace. Hunt cycles call `can_spend()` before dispatching and
`record_spend()` after each Layer's API usage.

If the day rolls over between calls, the counter resets automatically.

Set cap_usd <= 0 to disable the daily cap entirely (treated as unlimited).
The state file still tracks daily spend for visibility, but never gates
execution. Use this when the per-cycle `--budget-cap-usd` is the only
limit you want.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _lock_file(path: Path, timeout: float = 30.0):
    """Cross-platform exclusive file lock.

    On POSIX uses ``fcntl.flock``. On Windows uses ``msvcrt.locking`` with
    a polling retry (msvcrt has no built-in timeout). Returns a context
    manager that releases the lock on exit. If acquisition exceeds
    ``timeout`` seconds, raises TimeoutError.

    Spend audit Defect 03 (HIGH): concurrent agents (yesterday: 4
    max_concurrent) raced on _load/_save; last-writer-wins silently
    dropped earlier deltas from the daily cap counter. This lock
    serialises the read-modify-write so concurrent record_spend() calls
    accumulate correctly.
    """
    @contextmanager
    def _ctx():
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        deadline = time.time() + timeout
        fh = open(lock_path, "a+b")
        try:
            try:
                import fcntl
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() > deadline:
                            raise TimeoutError(
                                f"could not acquire lock {lock_path} in {timeout}s"
                            )
                        time.sleep(0.05)
            except ImportError:
                # Windows fallback
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.time() > deadline:
                            raise TimeoutError(
                                f"could not acquire lock {lock_path} in {timeout}s"
                            )
                        time.sleep(0.05)
            yield
        finally:
            try:
                fh.close()
            except OSError:
                pass
    return _ctx()


class DailyCap:
    def __init__(self, state_file: Path, cap_usd: float):
        self.state_file = Path(state_file)
        self.cap_usd = float(cap_usd)
        # cap_usd <= 0 means "no daily cap" — treated as unlimited downstream.
        self.unlimited = self.cap_usd <= 0
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        if not self.state_file.exists():
            return {"date": self._today(), "spend_usd": 0.0}
        try:
            data = json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {"date": self._today(), "spend_usd": 0.0}
        if data.get("date") != self._today():
            return {"date": self._today(), "spend_usd": 0.0}
        return data

    def _save(self, data: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_file)

    def today_spend(self) -> float:
        return float(self._load().get("spend_usd", 0.0))

    def remaining_today(self) -> float:
        if self.unlimited:
            return math.inf
        return max(0.0, self.cap_usd - self.today_spend())

    def can_spend(self, amount_usd: float) -> bool:
        if self.unlimited:
            return True
        # Hold the lock for the read so a concurrent record_spend() can't
        # slip an update in between our load and the caller's decision.
        with _lock_file(self.state_file):
            data = self._load()
            return (float(data.get("spend_usd", 0.0)) + float(amount_usd)) <= self.cap_usd

    def record_spend(self, amount_usd: float) -> None:
        with _lock_file(self.state_file):
            data = self._load()
            data["spend_usd"] = float(data.get("spend_usd", 0.0)) + float(amount_usd)
            data["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save(data)
