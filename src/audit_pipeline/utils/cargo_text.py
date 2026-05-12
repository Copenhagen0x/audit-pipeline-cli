"""Structured parsing of libtest (``cargo test``) text output.

Built in response to the 12-audit pipeline review (2026-05-12). Multiple
load-bearing classifiers across the codebase rely on simple substring
matching against cargo's combined stdout/stderr:

  * P3 ``_gate_poc_fails_pre_patch`` (`bundle/verifier.py`)
  * P3 ``_gate_poc_passes_post_patch`` (`bundle/verifier.py`)
  * ``hunt.py`` fired classifier (lines ~1044-1054)
  * L3 ``deploy/dispatch_layer3_v2.py`` resume parser
  * L4 ``deploy/dispatch_layer4_v1.py`` outcome classifier
  * L4 ``deploy/dispatch_layer4_redo.py`` ``"BUG CONFIRMED"`` substring

The substring approach has produced silent misclassifications:

  * A sibling test panicking gets the whole file marked as "fired"
  * `#[ignore]`-d tests show ``test result: ok`` and look passing
  * Compile errors in another file leak ``"error"`` text that's confused
    with assertion failures
  * Pre-existing repo breakage shows ``"FAILED"`` from a different test
    file and is misattributed to the target

This module parses cargo's libtest text format per-test, so callers can
make decisions about ONE specific named test rather than substring-match
on the whole log. No nightly Rust required (the stable ``cargo test``
output IS structured per-test, it just isn't JSON).

Typical libtest output (one binary, one test):

    running 1 test
    test test_h1_residual_conservation_fires ... FAILED

    failures:

    ---- test_h1_residual_conservation_fires stdout ----
    thread 'test_h1...' panicked at tests/test_h1_residual_conservation.rs:42:5:
    assertion `left == right` failed

    failures:
        test_h1_residual_conservation_fires

    test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out;
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


# `test <name> ... <result>` — emitted by libtest for each test.
# Result is one of: ok | FAILED | ignored | bench
_TEST_RESULT_LINE_RE = re.compile(
    r"^test\s+(?P<name>[A-Za-z_][A-Za-z0-9_:]*)\s+\.\.\.\s+(?P<result>ok|FAILED|ignored|bench)\b",
    re.MULTILINE,
)

# `test result: <ok|FAILED>. N passed; N failed; N ignored; ...` — overall
# binary-level summary line emitted once per test binary.
_TEST_SUMMARY_LINE_RE = re.compile(
    r"^test result:\s+(?P<outcome>ok|FAILED)\.\s+"
    r"(?P<passed>\d+)\s+passed;\s+(?P<failed>\d+)\s+failed;\s+"
    r"(?P<ignored>\d+)\s+ignored",
    re.MULTILINE,
)

# `error[E####]` — Rust compile errors (anywhere in output → compile failed)
_COMPILE_ERROR_RE = re.compile(r"^error(\[E\d+\])?:", re.MULTILINE)

# `error: could not compile` — clearer cargo-level compile-failure signal
_COULD_NOT_COMPILE_RE = re.compile(r"^error: could not compile", re.MULTILINE)


TestStatus = Literal[
    "fired",            # test ran and FAILED (the bug reproduced)
    "passed",           # test ran and OK (no bug found)
    "ignored",          # test is #[ignore]d — must not count as either
    "not_run",          # test was never executed (not in this binary)
    "compile_failed",   # crate didn't compile; verdict undetermined
    "indeterminate",    # log truncated / unparseable
]


@dataclass
class TestOutcome:
    """Structured result of looking up one specific test in a cargo log."""
    test_name: str
    status: TestStatus
    # Whole-binary summary if present
    binary_passed: int = 0
    binary_failed: int = 0
    binary_ignored: int = 0
    # Quick access to lines mentioning the test (for the operator)
    lines: list[str] = field(default_factory=list)
    # Filled in for compile_failed so the caller can show what didn't build
    compile_excerpt: str = ""

    # Tell pytest not to try to collect this dataclass as a test class.
    __test__ = False


def parse_test_outcome(log: str, test_name: str) -> TestOutcome:
    """Parse cargo test output and return outcome for ONE specific test.

    Args:
        log:        combined stdout+stderr of ``cargo test ...``
        test_name:  the libtest test name (e.g. ``test_h1_residual_conservation_fires``)

    Returns:
        ``TestOutcome`` with ``status`` set to one of the ``TestStatus``
        literals.  Callers can pattern-match on ``.status`` rather than
        substring-match on raw text.

    Semantics:
      * If cargo emitted compile errors anywhere, status = ``compile_failed``
        regardless of any test_result lines (the test never ran).
      * If a per-test line `test <name> ... FAILED` is found → ``fired``.
      * If `test <name> ... ok` is found → ``passed``.
      * If `test <name> ... ignored` is found → ``ignored`` (NEVER ``passed``
        even though the binary summary will say ``test result: ok``).
      * If no per-test line for ``test_name`` is found anywhere → ``not_run``.
        A whole-log substring matcher would have been fooled here by an
        unrelated test in the same binary.
      * If the output is empty or truncated below the summary line →
        ``indeterminate``.
    """
    if not log or not log.strip():
        return TestOutcome(test_name=test_name, status="indeterminate")

    # 1. Hard compile failure trumps any later text — the test couldn't have
    #    run. Look for cargo-level "could not compile" first (most reliable),
    #    then fall back to per-source `error[Exxx]:` lines.
    if _COULD_NOT_COMPILE_RE.search(log):
        excerpt = ""
        m = _COMPILE_ERROR_RE.search(log)
        if m:
            excerpt = log[max(0, m.start() - 20):m.start() + 400]
        return TestOutcome(
            test_name=test_name, status="compile_failed",
            compile_excerpt=excerpt,
        )

    # Even without "could not compile", a bare error[Exxxx]: BEFORE any
    # `running N tests` line means we never reached test execution. Only
    # count it as compile_failed when the error appears prior to test run.
    first_running = log.find("running ")
    first_error = log.find("error[")
    if first_error >= 0 and (first_running < 0 or first_error < first_running):
        excerpt = log[max(0, first_error - 20):first_error + 400]
        return TestOutcome(
            test_name=test_name, status="compile_failed",
            compile_excerpt=excerpt,
        )

    # 2. Look for the per-test result line. Prefer the LAST match (cargo
    #    can re-emit test names in failure stanzas; the canonical
    #    "test X ... result" line is the per-test summary).
    per_test_matches = [
        m for m in _TEST_RESULT_LINE_RE.finditer(log)
        if m.group("name") == test_name
    ]
    if per_test_matches:
        result = per_test_matches[-1].group("result")
        status: TestStatus
        if result == "FAILED":
            status = "fired"
        elif result == "ok":
            status = "passed"
        elif result == "ignored":
            status = "ignored"
        else:
            status = "indeterminate"
        # Pull adjacent context lines for the operator's log
        snippets: list[str] = []
        for m in per_test_matches:
            line_start = log.rfind("\n", 0, m.start()) + 1
            line_end = log.find("\n", m.end())
            if line_end < 0:
                line_end = len(log)
            snippets.append(log[line_start:line_end])
        outcome = TestOutcome(test_name=test_name, status=status, lines=snippets)
    else:
        # No per-test line — the test was not in this binary OR the log
        # was truncated before the per-test summary section.
        outcome = TestOutcome(test_name=test_name, status="not_run")

    # 3. Decorate with the binary-level summary line (last match wins —
    #    a cargo run can include summaries from multiple binaries).
    sum_matches = list(_TEST_SUMMARY_LINE_RE.finditer(log))
    if sum_matches:
        last = sum_matches[-1]
        outcome.binary_passed  = int(last.group("passed"))
        outcome.binary_failed  = int(last.group("failed"))
        outcome.binary_ignored = int(last.group("ignored"))
    elif outcome.status == "not_run":
        # No per-test line AND no summary line — the log is incomplete.
        outcome.status = "indeterminate"

    return outcome


def is_test_fired(log: str, test_name: str) -> bool:
    """Convenience: ``True`` iff exactly ``test_name`` ran and FAILED.

    Returns ``False`` for any other status (passed/ignored/not_run/compile_
    failed/indeterminate). Callers that need to distinguish those cases
    should call ``parse_test_outcome`` directly.
    """
    return parse_test_outcome(log, test_name).status == "fired"


def is_test_passed(log: str, test_name: str) -> bool:
    """Convenience: ``True`` iff exactly ``test_name`` ran and passed.

    A ``#[ignore]``d test returns ``False`` here even though the binary
    summary will say ``test result: ok``.
    """
    return parse_test_outcome(log, test_name).status == "passed"


__all__ = [
    "TestOutcome",
    "TestStatus",
    "parse_test_outcome",
    "is_test_fired",
    "is_test_passed",
]
