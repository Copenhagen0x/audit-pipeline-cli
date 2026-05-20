"""Lightweight Anthropic SDK monkey-patch that logs real billable token
usage from every Messages API response. NO prompt/response content is
logged — only token counts, computed cost, model, caller symbol, ts.

Output file (mode 0600): /root/.audit-spend-log.jsonl

Schema per line:
  {"ts": ISO8601, "model": str, "input_tokens": int, "output_tokens": int,
   "cache_read_input_tokens": int, "cache_creation_input_tokens": int,
   "cost_usd": float, "caller": str}

Baseline + sum-of-log is what the dashboard renders. Anthropic Admin API
isn't available on this account; this wrapper is the local equivalent.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("/root/.audit-spend-log.jsonl")

# Sonnet 4.6 default rates. Claude Code SDK reports model in responses so
# per-call cost uses whatever model the response says ran. Anything we
# don't have rates for falls back to Sonnet.
_RATES_PER_M = {
    "claude-sonnet-4-6":       {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5":       {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-7":         {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    "_default":                {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
}

_lock = threading.Lock()
_installed = False


def _model_rates(model: str) -> dict:
    if not model:
        return _RATES_PER_M["_default"]
    model_lower = model.lower()
    for key, rates in _RATES_PER_M.items():
        if key in model_lower:
            return rates
    return _RATES_PER_M["_default"]


def _cost(usage, model: str) -> float:
    """Compute USD cost from a Messages-API usage block (real billable)."""
    rates = _model_rates(model)
    if isinstance(usage, dict):
        get = usage.get
    else:
        # anthropic SDK Usage object — has attributes
        def get(k, default=0):
            return getattr(usage, k, default) or 0
    inp = get("input_tokens", 0) or 0
    out = get("output_tokens", 0) or 0
    cache_create = get("cache_creation_input_tokens", 0) or 0
    cache_read = get("cache_read_input_tokens", 0) or 0
    return (
        inp * rates["input"] / 1_000_000
        + out * rates["output"] / 1_000_000
        + cache_create * rates["cache_write"] / 1_000_000
        + cache_read * rates["cache_read"] / 1_000_000
    )


def _caller_symbol() -> str:
    """Walk the stack for the nearest audit_pipeline function name.
    Logs only the function symbol (no args, no source), so no scope leaks."""
    try:
        frames = inspect.stack()
    except Exception:
        return "unknown"
    for f in frames[2:]:
        mod = f.frame.f_globals.get("__name__", "") or ""
        if mod.startswith("audit_pipeline.") and "spend_tracker" not in mod:
            return f"{mod.rsplit('.', 1)[-1]}.{f.function}"
    return "unknown"


def _append_record(rec: dict) -> None:
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with _lock:
        # 0600 — root-readable only. Same posture as /root/.audit_api_calls.jsonl.
        if not LOG_PATH.exists():
            LOG_PATH.touch(mode=0o600)
        else:
            try:
                os.chmod(LOG_PATH, 0o600)
            except OSError:
                pass
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)


def _emit(response_or_usage, model: str, caller: str | None = None) -> None:
    """Extract usage from a Messages response (or raw usage dict) and log."""
    if response_or_usage is None:
        return
    usage = getattr(response_or_usage, "usage", None) or response_or_usage
    rec_model = getattr(response_or_usage, "model", None) or model or ""
    cost = _cost(usage, rec_model)

    def _g(k):
        if isinstance(usage, dict):
            return int(usage.get(k) or 0)
        return int(getattr(usage, k, 0) or 0)

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": rec_model,
        "input_tokens": _g("input_tokens"),
        "output_tokens": _g("output_tokens"),
        "cache_read_input_tokens": _g("cache_read_input_tokens"),
        "cache_creation_input_tokens": _g("cache_creation_input_tokens"),
        "cost_usd": round(cost, 6),
        "caller": caller or _caller_symbol(),
    }
    try:
        _append_record(rec)
    except Exception:
        # Never break a hunt because logging failed.
        pass


def install() -> None:
    """Monkey-patch anthropic.resources.messages.Messages.create + AsyncMessages.create
    to emit a spend-log record on every successful response. Idempotent."""
    global _installed
    if _installed:
        return
    try:
        from anthropic.resources.messages import Messages
        try:
            from anthropic.resources.messages import AsyncMessages
        except ImportError:
            AsyncMessages = None
    except ImportError:
        return  # SDK not installed in this env; nothing to wrap.

    _orig_create = Messages.create

    def _wrapped_create(self, *args, **kwargs):
        model = kwargs.get("model") or ""
        resp = _orig_create(self, *args, **kwargs)
        # Streaming returns a Stream/MessageStreamManager — usage lives
        # on the final delta. Skip those (rare in this engine; sample
        # via the non-streaming path covers the bulk of spend).
        if hasattr(resp, "usage"):
            _emit(resp, model)
        return resp

    Messages.create = _wrapped_create

    if AsyncMessages is not None:
        _orig_async = AsyncMessages.create

        async def _wrapped_async_create(self, *args, **kwargs):
            model = kwargs.get("model") or ""
            resp = await _orig_async(self, *args, **kwargs)
            if hasattr(resp, "usage"):
                _emit(resp, model)
            return resp

        AsyncMessages.create = _wrapped_async_create

    _installed = True


def total_since(baseline_ts: str | None = None) -> float:
    """Sum cost_usd in the spend log. If baseline_ts (ISO8601) is given,
    only count records strictly newer than that timestamp."""
    if not LOG_PATH.exists():
        return 0.0
    total = 0.0
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if baseline_ts and (r.get("ts") or "") <= baseline_ts:
                    continue
                total += float(r.get("cost_usd") or 0)
    except OSError:
        return 0.0
    return total
