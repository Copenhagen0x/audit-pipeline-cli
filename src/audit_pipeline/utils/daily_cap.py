"""Daily Claude-spend cap.

Tracks total spend per UTC calendar day in a small JSON file alongside
the workspace. Hunt cycles call `can_spend()` before dispatching and
`record_spend()` after each Layer's API usage.

If the day rolls over between calls, the counter resets automatically.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class DailyCap:
    def __init__(self, state_file: Path, cap_usd: float):
        self.state_file = Path(state_file)
        self.cap_usd = float(cap_usd)
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
        return max(0.0, self.cap_usd - self.today_spend())

    def can_spend(self, amount_usd: float) -> bool:
        return (self.today_spend() + float(amount_usd)) <= self.cap_usd

    def record_spend(self, amount_usd: float) -> None:
        data = self._load()
        data["spend_usd"] = float(data.get("spend_usd", 0.0)) + float(amount_usd)
        data["last_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save(data)
