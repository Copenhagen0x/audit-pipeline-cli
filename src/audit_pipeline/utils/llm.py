"""LLM client wrapper for auto-mode commands.

Wraps the Anthropic SDK with the audit pipeline's conventions:
- Single source of truth for model + max_tokens
- Graceful fallback when ANTHROPIC_API_KEY is missing
- Structured response shape so callers don't need to know SDK internals
"""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 600


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

    return LLMResponse(
        text="".join(text_parts),
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        model=msg.model,
        stop_reason=msg.stop_reason or "end_turn",
    )
