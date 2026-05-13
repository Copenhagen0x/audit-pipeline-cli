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

# The literal template line from PROMPT_TEMPLATE that the model may echo
# back verbatim. If we naively take the first VERDICT match we silently
# accept this as MATCH (Phase B self-audit Defect 01). Detect + skip.
_TEMPLATE_VERDICT_LINE_RE = re.compile(
    r"^\s*VERDICT\s*:\s*MATCH\s*\|\s*CONTRADICT\s*\|\s*INCONCLUSIVE\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Patterns that, if found in the input `claim` or `code_window`, indicate
# an attempt to inject a verdict directly. Reject the call rather than let
# the oracle rubber-stamp a finding (Phase B self-audit Defect 03).
_INJECTION_PATTERNS = (
    # ``VERDICT:`` anywhere in claim/code_window — block; the model should
    # ONLY see this token at the end of the prompt template, not in input.
    re.compile(r"VERDICT\s*:", re.IGNORECASE),
    # Closing triple-backticks — would break out of the fenced rust block
    # and let injected text impersonate prompt instructions.
    re.compile(r"```\s*$", re.MULTILINE),
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

    Hardening (Phase B self-audit Defect 01):
      * Strip any line that matches the literal template
        ``VERDICT: MATCH | CONTRADICT | INCONCLUSIVE`` before scanning —
        chatty models echo the template back and we'd otherwise accept it
        as MATCH.
      * Take the LAST verdict found, not the first. If the model thinks
        out loud (``"At first I thought MATCH, but on closer reading
        VERDICT: CONTRADICT"``) the final commit wins.
    """
    # 1) strip echoed template lines
    sanitised = _TEMPLATE_VERDICT_LINE_RE.sub("", text)

    # 2) find ALL verdicts in the sanitised text; prefer the last one
    matches = list(_VERDICT_RE.finditer(sanitised))
    verdict = matches[-1].group(1).upper() if matches else None

    # 3) extract REASON near the verdict we accepted (not the first)
    if matches:
        last_verdict_end = matches[-1].end()
        reason_match = re.search(
            r"REASON\s*:\s*(.+?)(?:\n\n|\nVERDICT|$)",
            sanitised[last_verdict_end:],
            re.DOTALL | re.IGNORECASE,
        )
    else:
        reason_match = None
    raw_reason = reason_match.group(1).strip() if reason_match else ""
    reason = raw_reason.split("\n", 1)[0].strip() if raw_reason else ""
    return verdict, reason


def _detect_prompt_injection(value: str) -> str | None:
    """Return a human-readable reason if ``value`` looks like an attempt to
    inject verdict-shaped content into the oracle prompt; else ``None``.

    Catches the Phase B self-audit Defect 03: a malicious code comment in
    the engine source containing a literal ``VERDICT: MATCH`` line, or
    closing triple-backticks that break out of the fenced code block.
    """
    if not value:
        return None
    for pat in _INJECTION_PATTERNS:
        m = pat.search(value)
        if m:
            return f"contains pattern {pat.pattern!r} at offset {m.start()}"
    return None


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

    # Phase B self-audit Defect 03: refuse to feed prompt-injection payloads
    # to the oracle. A code comment containing ``VERDICT: MATCH`` or a stray
    # closing ``` would otherwise be quoted into the user message and the
    # oracle would rubber-stamp the finding. Refuse + tell the operator.
    for label, value in (("claim", claim), ("code_window", code_window)):
        reason = _detect_prompt_injection(value)
        if reason:
            return GateResult(
                passed=None,
                reason=(
                    f"behavior oracle refused: {label} {reason}. Likely "
                    "prompt-injection attempt or accidental delimiter "
                    "collision; remove the offending substring before retry."
                ),
                duration_s=time.time() - t0,
            )

    if complete_fn is None:
        # Lazy import so the gates module doesn't drag in anthropic SDK
        # for callers that pass complete_fn explicitly (unit tests).
        from audit_pipeline.utils.llm import LLMUnavailable
        from audit_pipeline.utils.llm import complete as _complete
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
