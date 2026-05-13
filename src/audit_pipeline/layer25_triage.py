"""Layer 2.5 — automated fire triage between Layer 2 (PoC) and Layer 3 (Kani).

Cycle 20260511-183154 produced 64 PoC fires from 320 tested hyps (~20% raw
fire rate). Manual triage by Claude collapsed them into:
    7 STRONG (4 distinct root causes)
   11 SOFT (test mis-frames the claim / by-design / wrong layer)
   45 FALSE (PoC infra panic — 41 of those were the *same* `RiskParams:
            Overflow` panic from a broken params_for_*() factory)

Without this stage, Layer 3 + Layer 4 would have been called on ALL 64
fires (~$326–685 of Kani+LiteSVM spend, ~$280 of which would be wasted
on FALSE/SOFT fires).

This module is the productized form of that manual judgment. Two layers:

  1. FALSE_PATTERNS: regex/substring patterns matched against the cargo
     panic line. These catch the dominant "PoC factory panicked before
     the assertion fired" failure mode with no LLM call. Cycle 20260511
     would have collapsed 41/64 fires here at zero cost.

  2. LLM judge: for fires that DIDN'T match the fast-path, send the
     (claim, test body, panic line, engine source for claimed function)
     to an LLM judge which returns STRONG / SOFT / FALSE / LOST + reason.

STRONG fires are then clustered by root cause (bug_class + engine_function
+ claim-shingle similarity). Each cluster gets ONE representative dispatched
to Layer 3; the others are recorded as covered-by-cluster.

Output: ``triage.jsonl`` in the cycle dir, one row per fire, plus a
``layer3_dispatch_set`` exposed to the caller as the filtered set of hyp_ids.

Auto-mode is opt-in via the hunt CLI's ``--triage-fires`` flag (default ON
in hunt; default OFF when called as a CLI subcommand on a finished cycle).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Fast-path FALSE patterns (no LLM cost)
# ---------------------------------------------------------------------------
# Each entry: (pattern, reason). Matched against the cargo panic line.
# Adding a pattern means appending here — these are well-documented
# false-fire signatures the engine has seen before. New patterns should
# be added with a comment citing the cycle / hypothesis_id where the
# signature was first observed.

_SOLANA_FALSE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Cycle 20260511 — 41/45 FALSE fires were this exact panic from
        # a broken `params_for_*()` factory in the PoC scaffolding. The
        # test never reached the actual claim — the helper factory unwrapped
        # an Err during construction.
        re.compile(r"invalid\s+RiskParams\s*:\s*Overflow", re.IGNORECASE),
        "PoC factory panicked on RiskParams construction "
        "(broken params_for_*() helper, NOT the engine claim)",
    ),
    (
        # Setup-side `let x = ... .unwrap();` where the constructor returned
        # Err. Match any error-shaped variant after the unwrap-on-Err prefix.
        re.compile(
            r"called\s+`Result::unwrap\(\)`\s+on\s+an\s+`Err`\s+value:",
            re.IGNORECASE,
        ),
        "setup helper unwrap() on engine constructor error — test "
        "never reached the assertion",
    ),
    (
        # Anchor to the TEST FILE — bare `attempt to subtract with
        # overflow` ANYWHERE in the panic stack used to suppress real
        # F7-shape engine fires (which DO surface this exact panic
        # when the engine's residual math underflows). Require the
        # panic location to be in a tests/ path to scope this to test
        # setup arithmetic only.
        re.compile(
            r"(?=.*tests/[\w]+\.rs)"
            r"(?=.*attempt to (?:subtract|add|multiply) with overflow)",
            re.IGNORECASE | re.DOTALL,
        ),
        "raw arithmetic overflow in TEST FILE setup math — not the claim "
        "(claims about engine overflow should fire engine code, not test setup)",
    ),
    (
        # Order-independent: panic location is in a tests/test_*.rs file
        # AND the panic message is `index out of bounds`. Either ordering
        # ("tests/test_X.rs ... index out of bounds" or
        #  "index out of bounds at tests/test_X.rs") matches.
        re.compile(
            r"(?=.*test_\w+\.rs)(?=.*index out of bounds)",
            re.IGNORECASE | re.DOTALL,
        ),
        "out-of-bounds in the test file itself (test bug, not engine bug)",
    ),
    (
        re.compile(
            r"assertion `left\s*[!=]=\s*right`\s+failed.*\b(setup|init|construct|build)\w*\b",
            re.IGNORECASE,
        ),
        "assertion failure in test-side setup phase (env construction, "
        "not the claim being tested)",
    ),
)


# PHASE 1e — fast-path FALSE patterns for C (clang + ASan/UBSan output).
# Most C false-fires are sanitizer hits in the TEST file itself, not the
# program under test. The post-cycle gate already filters pseudo-pass
# stubs; these patterns catch the "ASan caught my own malloc bug in
# main()" case.
_C_FALSE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Sanitizer report whose stack frame is in our test_*.c file —
        # we caused the violation, not the program under test.
        re.compile(
            r"(?=.*AddressSanitizer)(?=.*test_\w+\.c)",
            re.IGNORECASE | re.DOTALL,
        ),
        "AddressSanitizer hit inside the test_*.c file itself "
        "(witness state setup broke memory safety, not the program)",
    ),
    (
        # Compile error in the PoC — not a fire, just a broken test source.
        # Note: we shouldn't normally see this through the L2.5 path
        # because adapter.run_test sets phase=compile and fired=False,
        # but defense-in-depth.
        re.compile(
            r"error:\s+(use of undeclared identifier|expected|"
            r"redefinition|implicit declaration)",
            re.IGNORECASE,
        ),
        "clang compile error in the PoC source — broken test, not a real fire",
    ),
    (
        # libubsan reporting a runtime error specifically inside test_*.c's
        # main() function. Same logic as ASan-in-test above.
        re.compile(
            r"runtime error:.*\btest_\w+\.c:",
            re.IGNORECASE,
        ),
        "UBSan caught undefined behavior inside the test file itself, "
        "not the program under test",
    ),
)


# PHASE 1e — fast-path FALSE patterns for Solidity (Foundry forge output).
_SOLIDITY_FALSE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Setup-phase deployment failure — test never reached the claim.
        re.compile(
            r"setUp\(\)\s+failed|Error:\s+Setup failed|FAIL:\s+setUp",
            re.IGNORECASE,
        ),
        "Foundry setUp() failed — test never reached the assertion",
    ),
    (
        # Compile error in the .t.sol file.
        re.compile(
            r"Error.*Source\s+\"\S+\.t\.sol\"|"
            r"Compiler run failed:\s*\n.*\.t\.sol",
            re.IGNORECASE | re.DOTALL,
        ),
        "Solidity compile error in the PoC test source — broken test",
    ),
    (
        # OutOfGas in the test body itself (not in the contract under test).
        re.compile(
            r"OutOfGas.*in\s+test_\w+",
            re.IGNORECASE,
        ),
        "OutOfGas in the PoC test body, not the contract under test",
    ),
)


# PHASE 1e — fast-path FALSE patterns for Aptos Move (aptos move test output).
_APTOS_FALSE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Compile-time error from the move compiler — broken test source.
        re.compile(
            r"error\[\w+\]:|Move\s+compilation\s+failed|"
            r"could\s+not\s+resolve\s+module",
            re.IGNORECASE,
        ),
        "Move compile error in the PoC test source — broken test",
    ),
    (
        # Abort inside the test's #[test] setup (before the call to the
        # function under test). aptos move test reports abort codes; if
        # the abort code matches a setup-phase error, the test never
        # exercised the claim.
        re.compile(
            r"abort\s+code:?\s*\d+.*in.*test_\w+::setup",
            re.IGNORECASE | re.DOTALL,
        ),
        "Move test aborted during setup (test never exercised the claim)",
    ),
    (
        # Aptos framework helper aborted (account::create_account_for_test
        # etc). Same setup-failure category.
        re.compile(
            r"aptos_framework::\w+:?:?\w*\s+aborted",
            re.IGNORECASE,
        ),
        "aptos_framework helper aborted in setup — test never reached the claim",
    ),
)


# Language → FALSE_PATTERNS dispatch. Unknown languages fall back to the
# Solana set (engine's original calibration). The CLI rejects unknown
# languages upstream so this fallback only fires under operator typos.
_FALSE_PATTERNS_BY_LANGUAGE: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "solana":   _SOLANA_FALSE_PATTERNS,
    "rust":     _SOLANA_FALSE_PATTERNS,
    "anchor":   _SOLANA_FALSE_PATTERNS,
    "c":        _C_FALSE_PATTERNS,
    "solidity": _SOLIDITY_FALSE_PATTERNS,
    "evm":      _SOLIDITY_FALSE_PATTERNS,
    "aptos":    _APTOS_FALSE_PATTERNS,
    "move":     _APTOS_FALSE_PATTERNS,
}


# Back-compat: legacy callers expect a module-level FALSE_PATTERNS pointing
# at the Solana set. Keep the alias so existing tests + cycles unaffected.
FALSE_PATTERNS = _SOLANA_FALSE_PATTERNS


@dataclass
class TriageResult:
    hyp_id: str
    classification: str  # STRONG | SOFT | FALSE | LOST
    reason: str
    # Set on STRONG; None for SOFT/FALSE/LOST.
    cluster_id: str | None = None
    is_representative: bool = False
    # Whether the LLM judge was actually called (vs fast-path).
    used_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pattern-match fast path
# ---------------------------------------------------------------------------


def classify_by_pattern(
    panic_line: str,
    language: str = "solana",
) -> tuple[str, str] | None:
    """Return (classification, reason) if any FALSE_PATTERN matches, else None.

    PHASE 1e: language-aware. Each language has its own set of fast-path
    FALSE patterns reflecting its toolchain's failure idioms (cargo
    panics for Rust, ASan reports for C, forge setUp failures for
    Solidity, Move abort codes for Aptos). Unknown languages fall back
    to the Solana set.

    Always returns the FIRST match — order in the patterns tuple is
    precedence.
    """
    if not panic_line:
        return None
    patterns = _FALSE_PATTERNS_BY_LANGUAGE.get(
        language.lower().strip(),
        _SOLANA_FALSE_PATTERNS,
    )
    for pat, reason in patterns:
        if pat.search(panic_line):
            return ("FALSE", reason)
    return None


def extract_panic_line(cargo_log: str, language: str = "solana") -> str:
    """Pull the fire-signal line(s) out of a test runner log.

    Each language's toolchain reports failures differently:
      * Solana / Rust:   ``panicked at ...`` + assertion message
      * Aptos / Move:    ``[ FAIL ] 0x42::mod::test_x`` + ``aborted with
                          code <N>`` (custom error codes) + abort location
      * C / clang+ASan:  ``runtime error: ...`` + ASan/UBSan report
      * Solidity / forge: ``[FAIL]`` + ``Error: ...`` revert payload

    Returns "" if no fire signal detected. **Critical**: when this returns
    "" the LLM judge sees an empty signal and routinely classifies the
    fire as FALSE ("fire signal is empty"). Cycle 20260513-191318 had
    all 7 Aptos fires misclassified FALSE/SOFT for this exact reason —
    extract_panic_line was hardcoded to Rust idioms.

    PHASE 2 — language-aware extraction. Defaults to Solana for back-
    compat with existing callers that don't pass ``language``.
    """
    if not cargo_log:
        return ""
    lang = (language or "solana").lower().strip()
    if lang in ("aptos", "move"):
        return _extract_aptos_fire_signal(cargo_log)
    if lang == "c":
        return _extract_c_fire_signal(cargo_log)
    if lang in ("solidity", "evm"):
        return _extract_solidity_fire_signal(cargo_log)
    return _extract_rust_fire_signal(cargo_log)


def _extract_rust_fire_signal(cargo_log: str) -> str:
    """Original Solana / cargo extractor — preserved verbatim."""
    lines = cargo_log.splitlines()
    for i, line in enumerate(lines):
        if "panicked at" in line.lower():
            # Include the next non-empty line if present (assertion msg)
            tail = lines[i + 1] if i + 1 < len(lines) else ""
            return (line + " " + tail).strip()
    # No explicit panic — look for `assertion ... failed` as fallback
    for i, line in enumerate(lines):
        if re.search(r"assertion.*failed", line, re.IGNORECASE):
            return line.strip()
    return ""


def _extract_aptos_fire_signal(move_test_log: str) -> str:
    """Extract `aptos move test` failure signal.

    Move test output has three signal-bearing lines we want to capture:
      1. ``[ FAIL ] 0x42::module::test_fn`` — test that failed
      2. ``aborted with code <N>`` — the custom error code from the
         test's ``E_BUG_<NAME>: u64 = 9999`` constants. THIS IS THE
         primary STRONG signal — a test author's custom abort code
         means the inverted-assertion fired, which means the bug was
         demonstrated.
      3. ``error[E11001]: test failure ... originating in ... rooted here``
         — abort location pointing inside the test module.

    Captures the FAIL line + next ~15 lines so the judge sees the full
    error tail (Move's compiler-style multi-line error blocks).
    """
    lines = move_test_log.splitlines()
    # Find the first FAIL marker
    fail_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\[\s*FAIL\s*\]", line):
            fail_idx = i
            break
    if fail_idx is None:
        # Fallback — direct hit on "aborted with code" without a FAIL header
        for i, line in enumerate(lines):
            if re.search(r"abort(?:ed)?\s+(?:with\s+)?code", line, re.IGNORECASE):
                fail_idx = i
                break
    if fail_idx is None:
        # Last resort — look for the summary line
        for i, line in enumerate(lines):
            if re.search(r"Test\s+result:\s*FAILED", line, re.IGNORECASE):
                fail_idx = i
                break
    if fail_idx is None:
        return ""
    # Capture FAIL line + tail of error block (cap at 20 lines, 2000 chars)
    tail = lines[fail_idx : min(fail_idx + 20, len(lines))]
    return "\n".join(tail)[:2000].strip()


def _extract_c_fire_signal(clang_log: str) -> str:
    """Extract clang+ASan/UBSan failure signal for C PoCs."""
    lines = clang_log.splitlines()
    # Sanitizer report block — typically starts with `==<pid>==ERROR:` or
    # `runtime error:` (UBSan). Capture the header + a few stack frames.
    for i, line in enumerate(lines):
        if re.search(r"==\d+==ERROR:|runtime error:|AddressSanitizer|"
                     r"UndefinedBehaviorSanitizer", line, re.IGNORECASE):
            tail = lines[i : min(i + 15, len(lines))]
            return "\n".join(tail)[:2000].strip()
    # Plain assertion failure
    for i, line in enumerate(lines):
        if re.search(r"assertion .* failed|Aborted", line, re.IGNORECASE):
            return line.strip()
    return ""


def _extract_solidity_fire_signal(forge_log: str) -> str:
    """Extract Foundry forge test failure signal for Solidity PoCs."""
    lines = forge_log.splitlines()
    # forge marks failed tests with `[FAIL...]` then a reason line.
    for i, line in enumerate(lines):
        if re.search(r"\[\s*FAIL", line):
            tail = lines[i : min(i + 12, len(lines))]
            return "\n".join(tail)[:2000].strip()
    # Compiler errors
    for i, line in enumerate(lines):
        if "Error:" in line or "Compiler run failed" in line:
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# LLM judge (called only for fires that didn't fast-path)
# ---------------------------------------------------------------------------


_SOLANA_JUDGE_PROMPT = """\
You are a Solana security audit triage judge. You receive ONE PoC fire
that the L2 layer reported as a "test failed - bug reproduced." Your job
is to classify the fire into exactly one of:

  STRONG - The assertion message references the actual semantic invariant
           the hypothesis claims. The test exercises the claim path.
           Worth promoting to formal verification (Kani / LiteSVM).
  SOFT   - The test fires, but for a DIFFERENT reason than the claim:
             * test mis-frames the claim (wrong API, wrong precondition)
             * the panic is by-design (engine escape hatch, error path)
             * wrong layer of abstraction (engine fires; wrapper handles)
  FALSE  - The fire is PoC infrastructure error: params factory panicked,
           setup helper unwrapped on an Err, test setup arithmetic
           overflow, etc. The test never reached the claim assertion.

Return JSON only. Schema:
  {"classification": "STRONG"|"SOFT"|"FALSE",
   "reason": "<one-sentence explanation>"}

Do not add commentary outside the JSON.
"""


_APTOS_JUDGE_PROMPT = """\
You are an Aptos / Move security audit triage judge. You receive ONE
PoC fire that the L2 layer reported as a "test failed - bug reproduced."

CRITICAL — UNDERSTAND THE FIRE-SIGNAL FORMAT:
* `aptos move test` reports failures as `[ FAIL ] 0x42::module::test_fn`.
* The Move VM aborts a transaction with a numeric code, like
  `aborted with code 9999`. PoCs in this engine define a per-test
  custom abort code, conventionally `E_BUG_HIT: u64 = 9999` or
  `E_BUG_<NAME>`, and assert!() with that code when the invariant the
  hypothesis claims is violated. A non-zero abort code from the test
  module is the STRONG-fire signal.
* The test pattern is **inverted assertion**: the PoC sets up a state
  where the invariant SHOULD hold, performs the attack action, and
  then asserts the invariant still holds. If the bug exists, the
  invariant breaks, the assertion fails, and the test aborts with the
  PoC's custom code. Aborts inside the test_<name> module aborting
  with the PoC's E_BUG_<NAME>-shaped code = STRONG. Do NOT misread the
  inverted-assertion pattern as "the test contradicts itself" — both
  assertions can never both hold simultaneously, that IS the design.
* Aborts inside `aptos_framework::*`, `0x1::*`, or `0x3::*` modules
  (the stdlib) during setup (account::create_account_for_test,
  coin::register, timestamp::set_time_has_started_for_testing) =
  FALSE — test never reached the claim.
* Compile errors in the test source = FALSE (broken test).

Classify into exactly one of:

  STRONG - Test aborts with the PoC's custom E_BUG code (4-5 digits,
           authored by L2), abort originates in 0x42::test_<name>
           module (the L2-authored PoC), and the test body's claim-
           assertion path matches the hypothesis. Worth promoting to
           Move Prover (L3) + property fuzz (L4).
  SOFT   - Test aborts in 0x42::test_<name> with a custom code, BUT
           the test demonstrates a DIFFERENT invariant than the
           hypothesis claims (e.g. test asserts auth-bypass, hyp
           claims event-emit-missing). Real bug, wrong claim mapping.
  FALSE  - Test aborts inside `aptos_framework::*`, `0x1::*`, `0x3::*`,
           or during pre-claim setup helpers. Compile errors. Abort
           code 0 with no signal. Test never reached the claim.

Return JSON only. Schema:
  {"classification": "STRONG"|"SOFT"|"FALSE",
   "reason": "<one-sentence explanation that cites the abort code if present>"}

Do not add commentary outside the JSON.
"""


_C_JUDGE_PROMPT = """\
You are a C security audit triage judge. You receive ONE PoC fire from
the L2 layer (clang + ASan + UBSan).

CRITICAL — UNDERSTAND THE FIRE-SIGNAL FORMAT:
* AddressSanitizer reports as `==<pid>==ERROR: AddressSanitizer: <kind>`
  followed by stack frames. UBSan reports as `runtime error: <kind>`.
* The stack frame location is the key signal. If the top frame is in
  the program-under-test (not test_<name>.c), the fire is STRONG. If
  the top frame is in test_<name>.c itself, the test caused its own
  memory bug = FALSE.

Classify into exactly one of:
  STRONG - Sanitizer hit inside the program-under-test (heap-buffer-
           overflow / use-after-free / signed-integer-overflow with
           top frame in `src/*.c`, NOT in test_<name>.c).
  SOFT   - Sanitizer hit in PUT but on a different invariant than
           the hypothesis claims.
  FALSE  - Sanitizer hit inside test_<name>.c (we caused the bug).
           Compile errors. Empty signal.

Return JSON only. Schema:
  {"classification": "STRONG"|"SOFT"|"FALSE",
   "reason": "<one-sentence explanation>"}
"""


_SOLIDITY_JUDGE_PROMPT = """\
You are a Solidity security audit triage judge. You receive ONE PoC fire
from the L2 layer (Foundry forge).

CRITICAL — UNDERSTAND THE FIRE-SIGNAL FORMAT:
* forge reports failures as `[FAIL...]` followed by a reason line
  (revert reason, assertion error, or counterexample).
* A `revert(0x...)` with a custom error selector matching the bug
  invariant is STRONG. A revert from `setUp()` or pre-claim helper
  is FALSE.

Classify into exactly one of:
  STRONG - Revert from the contract under test on the claimed
           invariant path (custom error or `require` failure tied to
           the hypothesis).
  SOFT   - Revert from the contract under test but on a different
           invariant.
  FALSE  - Revert during setUp(), test helper, or compile failure.

Return JSON only. Schema:
  {"classification": "STRONG"|"SOFT"|"FALSE",
   "reason": "<one-sentence explanation>"}
"""


_JUDGE_PROMPT_BY_LANGUAGE: dict[str, str] = {
    "solana":   _SOLANA_JUDGE_PROMPT,
    "rust":     _SOLANA_JUDGE_PROMPT,
    "anchor":   _SOLANA_JUDGE_PROMPT,
    "c":        _C_JUDGE_PROMPT,
    "solidity": _SOLIDITY_JUDGE_PROMPT,
    "evm":      _SOLIDITY_JUDGE_PROMPT,
    "aptos":    _APTOS_JUDGE_PROMPT,
    "move":     _APTOS_JUDGE_PROMPT,
}


# Back-compat: external callers can still import JUDGE_SYSTEM_PROMPT.
# Defaults to the Solana prompt to preserve existing behaviour.
JUDGE_SYSTEM_PROMPT = _SOLANA_JUDGE_PROMPT


# Per-language fence tag for code blocks in the judge prompt — keeps
# the LLM's syntax highlighting + parsing accurate when reading test
# bodies and engine source. Falls back to "rust" for backward compat.
_FENCE_TAG_BY_LANGUAGE: dict[str, str] = {
    "solana":   "rust",
    "rust":     "rust",
    "anchor":   "rust",
    "c":        "c",
    "solidity": "solidity",
    "evm":      "solidity",
    "aptos":    "move",
    "move":     "move",
}


def build_judge_user_prompt(
    hyp_id: str,
    claim: str,
    bug_class: str,
    engine_function: str,
    test_body: str,
    panic_line: str,
    engine_source: str = "",
    language: str = "solana",
    framework: str | None = None,
) -> str:
    """Build the user prompt for the L2.5 judge.

    PHASE 1e: language-aware. The fence tag matches the test body's
    language so the LLM parses it correctly. Optional ``framework``
    appears in the prompt header so the judge knows what kind of fire
    signal it's reading (cargo panic vs ASan report vs forge revert
    vs Move abort).
    """
    fence = _FENCE_TAG_BY_LANGUAGE.get(language.lower().strip(), "rust")
    blocks = [
        f"# Hypothesis {hyp_id}",
        f"Language: {language}",
    ]
    if framework:
        blocks.append(f"Test framework: {framework}")
    blocks += [
        f"Claim: {claim}",
        f"Bug class: {bug_class}",
        f"Engine function: {engine_function}",
        "",
        "## Fire signal (panic / assertion / sanitizer report)",
        f"```\n{panic_line[:1500]}\n```",
        "",
        "## Test body (the PoC)",
        f"```{fence}\n{test_body[:4000]}\n```",
    ]
    if engine_source:
        blocks += [
            "",
            "## Engine source for the claimed function",
            f"```{fence}\n{engine_source[:3000]}\n```",
        ]
    blocks += [
        "",
        "Return the classification JSON now.",
    ]
    return "\n".join(blocks)


def _extract_balanced_json_objects(text: str) -> list[str]:
    """Pull every TOP-LEVEL balanced ``{ ... }`` substring out of `text`.

    Old code used ``\\{[^{}]*\\}`` which breaks the moment the judge
    embeds the schema as a literal example (the response then contains
    a nested ``{"classification":"STRONG"|"SOFT"|"FALSE"}`` description
    inside the actual answer JSON), or whenever a `reason` string
    contains a brace.

    Skips braces inside string literals so a `"reason": "foo {bar} baz"`
    doesn't accidentally split the object.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c != "{":
            i += 1
            continue
        depth = 0
        start = i
        in_string = False
        escape = False
        while i < n:
            ch = text[i]
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[start : i + 1])
                    i += 1
                    break
            i += 1
        else:
            # Unbalanced trailing { — stop scanning
            break
    return out


_VALID_CLASSIFICATIONS = frozenset({"STRONG", "SOFT", "FALSE"})


def _parse_judge_response(text: str) -> tuple[str, str]:
    """Extract (classification, reason) from a judge response.

    Robustness:
      * Handles nested braces inside `reason` strings.
      * Tolerates the model echoing the schema as a sibling JSON object
        — we iterate every balanced top-level `{...}` substring and
        return the FIRST one whose `classification` is a valid label.
      * Falls back to SOFT if no valid JSON shape is found, so we never
        miscount an unparseable judge response as a real FALSE.
    """
    if not text:
        return ("SOFT", "judge response was empty (defaulting to SOFT)")
    candidates = _extract_balanced_json_objects(text)
    if not candidates:
        return ("SOFT", "judge response had no JSON object (defaulting to SOFT)")
    for raw in candidates:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        cls = str(d.get("classification", "")).upper().strip()
        if cls not in _VALID_CLASSIFICATIONS:
            continue
        reason = str(d.get("reason") or "")[:300]
        return (cls, reason)
    # We saw JSON objects but none had a valid classification field —
    # the model returned an unknown label (e.g. "MAYBE") or omitted the
    # field entirely. Pull the bad label (if any) into the reason text
    # so operators can see what the model returned without re-reading
    # the raw response.
    bad_label: str | None = None
    for raw in candidates:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(d, dict) and "classification" in d:
            bad_label = str(d.get("classification", ""))
            break
    if bad_label:
        return (
            "SOFT",
            f"judge returned unknown classification {bad_label!r} "
            "(defaulting to SOFT)",
        )
    return ("SOFT", "judge JSON had no valid classification field (defaulting to SOFT)")


def judge_one(
    hyp_id: str,
    claim: str,
    bug_class: str,
    engine_function: str,
    test_body: str,
    panic_line: str,
    engine_source: str = "",
    *,
    model: str | None = None,
    complete_fn=None,  # injection seam for tests
    language: str = "solana",
    framework: str | None = None,
) -> tuple[str, str]:
    """LLM judge call. Returns (classification, reason).

    ``complete_fn`` is an injection seam so tests can stub the LLM. If
    not provided, uses ``audit_pipeline.utils.complete``.

    PHASE 1e: ``language`` + ``framework`` flow through to the user
    prompt so the judge reads the test body with the right syntax
    parser and knows what kind of fire signal it's looking at.
    """
    if complete_fn is None:
        from audit_pipeline.utils import complete as _real_complete
        complete_fn = _real_complete
    user_prompt = build_judge_user_prompt(
        hyp_id, claim, bug_class, engine_function,
        test_body, panic_line, engine_source,
        language=language, framework=framework,
    )
    # PHASE 2 — language-aware system prompt. The Solana prompt mentions
    # `RiskParams`, Anchor, `unwrap()` — irrelevant noise for Aptos /
    # Solidity / C and an active source of FALSE misclassifications.
    # Per-language prompts describe each toolchain's actual fire-signal
    # format (Move abort codes, ASan stack frames, forge revert payload).
    system_prompt = _JUDGE_PROMPT_BY_LANGUAGE.get(
        (language or "").lower().strip(), _SOLANA_JUDGE_PROMPT,
    )
    try:
        if model:
            resp = complete_fn(user_prompt, system=system_prompt, model=model)
        else:
            resp = complete_fn(user_prompt, system=system_prompt)
    except Exception as e:  # noqa: BLE001
        return ("SOFT", f"judge call failed ({type(e).__name__}: {e!s:.150}); defaulting to SOFT")
    return _parse_judge_response(getattr(resp, "text", str(resp)))


# ---------------------------------------------------------------------------
# Root-cause clustering (STRONG fires only)
# ---------------------------------------------------------------------------


def _claim_shingles(claim: str, k: int = 3) -> set[tuple[str, ...]]:
    toks = (claim or "").lower().split()
    return {tuple(toks[i:i + k]) for i in range(max(0, len(toks) - k + 1))}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_strong_fires(
    strong: list[dict[str, Any]],
    *,
    similarity_threshold: float = 0.25,
) -> dict[str, list[str]]:
    """Cluster STRONG fires by root cause.

    Returns ``{cluster_id: [hyp_id, ...]}`` where the cluster_id is the
    FIRST hyp_id added to the cluster.

    Membership rule (in priority order):

      1. **Strong match — same engine_function**: if two STRONG fires
         hit the SAME engine_function with the SAME bug_class, they're
         the same root cause by definition. The LLM judge already
         confirmed both are STRONG (real bugs); two real bugs in the
         same function under the same bug class is one root cause
         expressed twice. Cluster regardless of claim wording.

      2. **Weak match — claim similarity**: if one of bug_class /
         engine_function is missing, fall back to comparing claim
         3-token shingle Jaccard against any prior cluster member.
         Compared to ALL members (not just the cluster representative)
         so claim drift within a cluster doesn't split it apart.

    Cycle 20260511 produced 7 STRONG fires that clustered to 4 distinct
    root causes via this rule (F7 family = 4 hyps in
    ``use_insurance_buffer`` → 1 cluster; AR7 / CI10 / L3 each in their
    own singleton cluster).
    """
    clusters: dict[str, list[str]] = {}
    cluster_meta: dict[str, list[dict[str, Any]]] = {}  # cluster_id → [member metas]

    for fire in strong:
        hyp_id = fire["hyp_id"]
        bc = (fire.get("bug_class") or "").strip().lower()
        ef = (fire.get("engine_function") or "").strip().lower()
        sh = _claim_shingles(fire.get("claim") or "")
        meta = {"bug_class": bc, "engine_function": ef, "shingles": sh}

        matched: str | None = None
        for cid, members in cluster_meta.items():
            # Rule 1: strong match — same bug_class AND engine_function.
            # Two STRONG fires in the same function under the same bug
            # class are one root cause expressed twice.
            if bc and ef:
                if any(m["bug_class"] == bc and m["engine_function"] == ef
                       for m in members):
                    matched = cid
                    break
            # Rule 2: weak match — claim shingle similarity, but ONLY if
            # bug_class agrees AND engine_function isn't a hard mismatch.
            # Different engine_function = different code path = different
            # root cause, even when the claim wording rhymes. This rule
            # only kicks in when at least one side has no engine_function
            # set (e.g. legacy hyps that pre-date the field).
            engine_compatible = (not ef) or any(
                (not m["engine_function"]) or m["engine_function"] == ef
                for m in members
            )
            if engine_compatible and any(
                m["bug_class"] == bc
                and _jaccard(sh, m["shingles"]) >= similarity_threshold
                for m in members
            ):
                matched = cid
                break

        if matched is None:
            clusters[hyp_id] = [hyp_id]
            cluster_meta[hyp_id] = [meta]
        else:
            clusters[matched].append(hyp_id)
            cluster_meta[matched].append(meta)
    return clusters


# ---------------------------------------------------------------------------
# End-to-end triage for a whole cycle
# ---------------------------------------------------------------------------


def triage_cycle(
    cycle_dir: Path,
    *,
    poc_results: dict[str, dict[str, Any]],
    hyp_meta: dict[str, dict[str, Any]],
    engine_src_loader=None,
    complete_fn=None,
    judge_model: str | None = None,
    language: str = "solana",
    framework: str | None = None,
) -> dict[str, Any]:
    """Triage every fired PoC in a cycle.

    Returns:
      {
        "results": [TriageResult.to_dict() per fire],
        "layer3_dispatch_set": [hyp_id, ...],  # STRONG representatives only
        "counts": {"STRONG": n, "SOFT": n, "FALSE": n, "LOST": n},
        "n_llm_calls": int,
        "triage_jsonl_path": str,
      }

    ``engine_src_loader`` is a callable ``(engine_function: str) -> str``
    that returns the source text for the claimed function. If None, the
    judge prompt is built without the engine-source block (slightly weaker
    judgments but works in offline / test contexts).

    PHASE 1e: ``language`` + ``framework`` drive language-specific
    FALSE-pattern matching + the LLM judge's syntax fence + framework-
    aware prompt header. Defaults preserve existing Percolator
    (Rust/cargo) workflows.
    """
    fired_hyp_ids = sorted([
        hid for hid, pr in poc_results.items() if pr.get("fired")
    ])
    results: list[TriageResult] = []
    n_llm_calls = 0

    for hyp_id in fired_hyp_ids:
        poc = poc_results.get(hyp_id, {})
        meta = hyp_meta.get(hyp_id, {})

        # Read test body + cargo log
        scaffold_path = poc.get("scaffold_path")
        cargo_log_path = poc.get("cargo_log_path")
        test_body = ""
        if scaffold_path:
            try:
                test_body = Path(scaffold_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        cargo_log = ""
        if cargo_log_path:
            try:
                cargo_log = Path(cargo_log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        # LOST: test file or log missing
        if not test_body or not cargo_log:
            results.append(TriageResult(
                hyp_id=hyp_id, classification="LOST",
                reason=(
                    f"test_body present: {bool(test_body)}, "
                    f"cargo_log present: {bool(cargo_log)}"
                ),
            ))
            continue

        panic_line = extract_panic_line(cargo_log, language=language)

        # Fast-path FALSE — language-aware pattern set
        fast = classify_by_pattern(panic_line, language=language)
        if fast is not None:
            cls, reason = fast
            results.append(TriageResult(
                hyp_id=hyp_id, classification=cls, reason=reason,
                used_llm=False,
            ))
            continue

        # LLM judge for non-obvious cases
        engine_function = meta.get("engine_function") or ""
        engine_source = ""
        if engine_src_loader and engine_function:
            try:
                engine_source = engine_src_loader(engine_function) or ""
            except Exception:  # noqa: BLE001
                engine_source = ""

        cls, reason = judge_one(
            hyp_id,
            claim=meta.get("claim", ""),
            bug_class=meta.get("bug_class", ""),
            engine_function=engine_function,
            test_body=test_body,
            panic_line=panic_line,
            engine_source=engine_source,
            model=judge_model,
            complete_fn=complete_fn,
            language=language,
            framework=framework,
        )
        n_llm_calls += 1
        results.append(TriageResult(
            hyp_id=hyp_id, classification=cls, reason=reason, used_llm=True,
        ))

    # Cluster STRONG fires
    strong_dicts = [
        {
            "hyp_id": r.hyp_id,
            "bug_class": hyp_meta.get(r.hyp_id, {}).get("bug_class"),
            "engine_function": hyp_meta.get(r.hyp_id, {}).get("engine_function"),
            "claim": hyp_meta.get(r.hyp_id, {}).get("claim"),
        }
        for r in results if r.classification == "STRONG"
    ]
    clusters = cluster_strong_fires(strong_dicts)

    # Mark cluster membership + representatives on the result rows
    cluster_lookup: dict[str, str] = {}
    for cid, members in clusters.items():
        for hid in members:
            cluster_lookup[hid] = cid
    for r in results:
        if r.classification == "STRONG":
            r.cluster_id = cluster_lookup.get(r.hyp_id)
            r.is_representative = (r.cluster_id == r.hyp_id)

    # Write triage.jsonl
    triage_jsonl = cycle_dir / "triage.jsonl"
    triage_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with triage_jsonl.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), sort_keys=True) + "\n")

    counts = {
        "STRONG": sum(1 for r in results if r.classification == "STRONG"),
        "SOFT":   sum(1 for r in results if r.classification == "SOFT"),
        "FALSE":  sum(1 for r in results if r.classification == "FALSE"),
        "LOST":   sum(1 for r in results if r.classification == "LOST"),
    }
    layer3_dispatch_set = [
        r.hyp_id for r in results
        if r.classification == "STRONG" and r.is_representative
    ]

    return {
        "results": [r.to_dict() for r in results],
        "layer3_dispatch_set": layer3_dispatch_set,
        "clusters": clusters,
        "counts": counts,
        "n_llm_calls": n_llm_calls,
        "triage_jsonl_path": str(triage_jsonl),
    }


__all__ = [
    "FALSE_PATTERNS",
    "TriageResult",
    "build_judge_user_prompt",
    "classify_by_pattern",
    "cluster_strong_fires",
    "extract_panic_line",
    "judge_one",
    "triage_cycle",
]
