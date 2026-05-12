"""LLM client wrapper for auto-mode commands.

Wraps the Anthropic SDK with the audit pipeline's conventions:
- Single source of truth for model + max_tokens
- Graceful fallback when ANTHROPIC_API_KEY is missing
- Structured response shape so callers don't need to know SDK internals
- Per-call cost computed from real input/output tokens at published Sonnet
  4.6 pricing, written to a per-host append-only log so the dashboard can
  show ground-truth spend instead of flat-rate estimates.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 16384  # bumped from 8192: recon responses on complex
                            # hyps were running out mid-analysis and producing
                            # truncated outputs with no verdict line, which
                            # parsed as UNKNOWN. Sonnet 4.6 supports up to
                            # ~64k output tokens; 16k gives plenty of room.
DEFAULT_TIMEOUT_SECONDS = 600

# Sonnet 4.6 pricing (USD per 1M tokens). Pulled from Anthropic public pricing.
# Input cache hits would be cheaper but the SDK doesn't expose a cache_hit flag
# uniformly, so we conservatively bill at full input rate.
SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0

# Append-only log of every API call. Override via env var. Default is per-host
# so all subprocess invocations of the pipeline contribute to the same total.
SPEND_LOG_PATH = Path(
    os.environ.get("JELLEO_SPEND_LOG", "/root/.audit_api_calls.jsonl")
)


def compute_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """USD cost of one Anthropic call at Sonnet 4.6 prices."""
    return (
        (input_tokens / 1_000_000.0) * SONNET_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000.0) * SONNET_OUTPUT_USD_PER_MTOK
    )


def _append_spend_event(event: dict) -> None:
    """Write a single JSON line to the spend log. Best-effort, never raises."""
    try:
        SPEND_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # `open(..., 'a')` + single short write is atomic on POSIX, so
        # parallel callers won't garble each other's lines.
        with SPEND_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        # Don't take down the API call if logging fails. The dashboard will
        # just under-count this event.
        pass


class LLMUnavailable(RuntimeError):
    """Raised when no LLM backend is configured (no API key, SDK missing)."""


@dataclass
class LLMResponse:
    """Normalized completion response."""
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str
    cost_usd: float = 0.0


def is_available() -> bool:
    """True iff a usable LLM backend is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 1.0,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> LLMResponse:
    """Run a single-turn completion against the configured LLM.

    Raises LLMUnavailable if no backend is configured. Caller is expected
    to either fall back to render-mode or surface the error to the user.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set. Either set it in your environment "
            "to enable auto-mode, or omit --auto to run in render-mode."
        )
    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailable(
            "anthropic SDK is not installed. Run `pip install anthropic` "
            "or `pip install -e .[dev]` from the audit-pipeline-cli root."
        ) from e

    client = anthropic.Anthropic(timeout=timeout)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    msg = client.messages.create(**kwargs)

    text_parts: list[str] = []
    for block in msg.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            text_parts.append(block["text"])

    in_tok = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens
    cost = compute_cost_usd(in_tok, out_tok)
    _append_spend_event({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": msg.model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": round(cost, 6),
        "stop_reason": msg.stop_reason or "end_turn",
        "caller": os.environ.get("JELLEO_SPEND_CALLER", "unknown"),
    })

    return LLMResponse(
        text="".join(text_parts),
        input_tokens=in_tok,
        output_tokens=out_tok,
        model=msg.model,
        stop_reason=msg.stop_reason or "end_turn",
        cost_usd=cost,
    )
