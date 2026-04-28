"""Structured JSON logging with daily file rotation.

One logger per "channel" (e.g. one for hunt, one for shadow, one for
watch). Each line is a single JSON object so the logs are grep-friendly
AND parseable by jq / log-shipping tooling.

Files rotate daily and the oldest are kept up to retention_days. Old
files are deleted on the first log call after a date rollover.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "channel": record.name,
            "msg": record.getMessage(),
        }
        # Pick up extras passed via logger.info("msg", extra={"foo": 1})
        for k, v in record.__dict__.items():
            if k in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module",
                "msecs", "message", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName", "taskName",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(
    channel: str,
    log_dir: Path | str,
    *,
    level: int = logging.INFO,
    retention_days: int = 14,
    also_stderr: bool = True,
) -> logging.Logger:
    """Get/create a JSON logger with daily rotation.

    Multiple calls with the same channel return the same logger.
    """
    logger = logging.getLogger(f"audit_pipeline.{channel}")
    if getattr(logger, "_audit_pipeline_configured", False):
        return logger

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{channel}.jsonl"

    handler = TimedRotatingFileHandler(
        str(log_path), when="midnight", utc=True,
        backupCount=retention_days, encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level)
    logger.addHandler(handler)

    if also_stderr:
        sh = logging.StreamHandler(stream=sys.stderr)
        sh.setFormatter(JsonFormatter())
        sh.setLevel(level)
        logger.addHandler(sh)

    logger.setLevel(level)
    logger.propagate = False
    logger._audit_pipeline_configured = True  # type: ignore[attr-defined]

    # Best-effort cleanup of files older than retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days * 2)
    for old in log_dir.glob(f"{channel}.jsonl.*"):
        try:
            mtime = datetime.fromtimestamp(old.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                os.remove(old)
        except OSError:
            pass

    return logger
