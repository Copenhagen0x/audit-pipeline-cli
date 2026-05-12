"""Gate 4 — L4.behavior_oracle.

Independent-LLM verification that a finding's claim actually matches the
code at the cited location. Built in response to cycle 20260511-183154
where eight findings claimed behavior contradicted by the real source
(``i128::MIN`` guarded with an explicit comment, ``require_initialized``
called as a first guard, ``assert_public_postconditions`` invoked,
``haircut_ratio`` returning a multiplier ≤ 1, etc.) — yet L1/L2/L3/L4
agreed because they all started from the same wrong premise and shared
context. The pipeline never asked a fresh-context model "given this code
window, does the claim hold?"

The gate sends the LLM a deliberately narrow prompt:

  * the finding's natural-language ``claim``
  * a 5-50 line code window read fresh from disk at the cited location
  * a forced-answer format

The model must answer with one of: ``MATCH`` / ``CONTRADICT`` / ``INCONCLUSIVE``
plus a one-sentence rationale. Anything else fails parsing → SKIP.

Costs API tokens. The caller opt-in via a flag; the default model is
``claude-haiku-3.5`` to keep budget bounded. ``check_behavior`` returns a
``GateResult`` and the (cost_usd, model, tokens) are recorded in
``details`` for spend telemetry.

Used by: ``commands/hunt.py`` (optional L4.5 step after L4 BPF
reproduction, before promoting a finding to ``confirmed``).
"""

from __future__ import annotations

import re
import time

from audit_pipeline.gates import GateResult


# Cheaper model by default — this is a verification gate, not an authoring
# step. Operator can override with a more capable model when the cost
# differential is acceptable.
DEFAULT_GATE_MODEL = "claude-haiku-3-5"

_VERDICT_RE = re.compile(
    r"^\s*VERDICT\s*:\s*(MATCH|CONTRADICT|INCONCLUSIVE)\b",
    re.MULTILINE | re.IGNORECASE,
)


PROMPT_TEMPLATE = """You are an independent code reviewer. A separate
analysis tool flagged a potential security finding against the following
Rust code. Your job is to read ONLY the code shown below and judge whether
the claim accurately describes what the code does.

# Claim under review

{claim}

# Code at the cited location (`{location}`, {line_range})

```rust
{code_window}
```

# Your job

Read the code carefully. Then output EXACTLY one block in this format,
nothing else:

VERDICT: MATCH | CONTRADICT | INCONCLUSIVE
REASON: <one sentence — what the code actually does in light of the claim>

Rules:
- ``MATCH``         — the claim accurately describes the code's behavior
- ``CONTRADICT``    — the code does the OPPOSITE, or already handles the case the claim says is unhandled
- ``INCONCLUSIVE``  — the snippet is too small / context-dependent to judge

Be decisive. If a guard / assertion / early-return is present and the claim says it's absent, that's CONTRADICT — say so. If the claim talks about a function that doesn't appear in the snippet, that's INCONCLUSIVE.
"""


def _parse_verdict(text: str) -> tuple[str | None, str]:
    """Return ``(verdict, reason)`` parsed from an LLM response.

    ``verdict`` is one of ``MATCH`` / ``CONTRADICT`` / ``INCONCLUSIVE`` or
    ``None`` if parsing failed. ``reason`` is the first ``REASON:`` line,
    empty string if none.
    """
    m = _VERDICT_RE.search(text)
    verdict = m.group(1).upper() if m else None
    reason_match = re.search(r"REASON\s*:\s*(.+?)(?:\n\n|\nVERDICT|$)", text, re.DOTALL | re.IGNORECASE)
    reason = (reason_match.group(1).strip() if reason_match else "").splitlines()[0:1]
    return verdict, " ".join(reason).strip()


def check_behavior(
    *,
    claim: str,
    code_window: str,
    location: str = "",
    line_range: str = "",
    model: str = DEFAULT_GATE_MODEL,
    max_tokens: int = 400,
    complete_fn=None,
) -> GateResult:
    """Ask an independent LLM whether ``claim`` matches the behavior of
    ``code_window``.

    Args:
        claim:        natural-language claim the finding makes
        code_window:  ~5-50 lines of Rust source the claim refers to
        location:     human-readable label (file path) for the prompt
        line_range:   e.g. ``"lines 4770-4785"`` for the prompt
        model:        LLM model identifier (defaults to a cheap Haiku-class)
        max_tokens:   completion ceiling
        complete_fn:  injected for unit tests; defaults to
                      ``audit_pipeline.utils.llm.complete``

    Returns:
        ``GateResult(True, …)``  — verdict MATCH (claim is supported)
        ``GateResult(False, …)`` — verdict CONTRADICT (code does opposite)
        ``GateResult(None, …)``  — verdict INCONCLUSIVE, parsing failed,
            LLM unavailable, or empty code window. The caller can choose
            to gate on this (refuse to disclose) or proceed with caveat.
    """
    t0 = time.time()

    claim = (claim or "").strip()
    code_window = (code_window or "").strip()
    if not claim:
        return GateResult(
            passed=None,
            reason="empty claim — nothing to verify",
            duration_s=time.time() - t0,
        )
    if not code_window:
        return GateResult(
            passed=None,
            reason="empty code_window — nothing to verify against",
            duration_s=time.time() - t0,
        )

    if complete_fn is None:
        # Lazy import so the gates module doesn't drag in anthropic SDK
        # for callers that pass complete_fn explicitly (unit tests).
        from audit_pipeline.utils.llm import LLMUnavailable, complete as _complete
        complete_fn = _complete
        # surface LLMUnavailable for cleaner skip handling
        _llm_unavailable_cls = LLMUnavailable
    else:
        _llm_unavailable_cls = Exception  # tests pass an injected callable

    prompt = PROMPT_TEMPLATE.format(
        claim=claim,
        location=location or "(unspecified)",
        line_range=line_range or "(range unspecified)",
        code_window=code_window,
    )

    try:
        resp = complete_fn(
            prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,    # deterministic verification
        )
    except _llm_unavailable_cls as e:
        return GateResult(
            passed=None,
            reason=f"LLM unavailable: {e}",
            duration_s=time.time() - t0,
        )
    except Exception as e:  # noqa: BLE001
        return GateResult(
            passed=None,
            reason=f"behavior oracle call errored: {e}",
            duration_s=time.time() - t0,
        )

    response_text = getattr(resp, "text", str(resp))
    cost_usd = getattr(resp, "cost_usd", 0.0)
    response_model = getattr(resp, "model", model)
    input_tokens = getattr(resp, "input_tokens", 0)
    output_tokens = getattr(resp, "output_tokens", 0)

    verdict, reason = _parse_verdict(response_text)
    details = {
        "model":         response_model,
        "cost_usd":      cost_usd,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "verdict":       verdict,
        "llm_reason":    reason,
    }

    if verdict == "MATCH":
        return GateResult(
            passed=True,
            reason=f"independent reviewer agrees claim matches code: {reason}",
            duration_s=time.time() - t0,
            details=details,
        )
    if verdict == "CONTRADICT":
        return GateResult(
            passed=False,
            reason=(
                "independent reviewer says code CONTRADICTS the claim: "
                f"{reason}. The finding likely misreads the code; re-validate."
            ),
            duration_s=time.time() - t0,
            details=details,
        )
    if verdict == "INCONCLUSIVE":
        return GateResult(
            passed=None,
            reason=f"independent reviewer inconclusive: {reason}",
            duration_s=time.time() - t0,
            details=details,
        )
    # parsing fell through
    return GateResult(
        passed=None,
        reason=(
            "could not parse a VERDICT from the reviewer's response — "
            "treat as inconclusive."
        ),
        duration_s=time.time() - t0,
        details={**details, "raw_tail": response_text[-400:]},
    )


__all__ = ["check_behavior", "DEFAULT_GATE_MODEL", "PROMPT_TEMPLATE"]
