"""Tests for ``audit_pipeline.utils.cargo_text`` — structured per-test
parsing of libtest output. Replaces substring-match classifiers that
silently misclassified findings in cycle 20260511-183154.
"""

from __future__ import annotations

import pytest

from audit_pipeline.utils.cargo_text import (
    TestOutcome,
    is_test_fired,
    is_test_passed,
    parse_test_outcome,
)


# ---- fired (test ran and FAILED) ---------------------------------------

class TestFired:
    def test_simple_failed(self):
        log = """
running 1 test
test test_h1_fires ... FAILED

failures:

---- test_h1_fires stdout ----
thread 'test_h1_fires' panicked at tests/test_h1.rs:42:5:
assertion `left == right` failed

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out;
"""
        out = parse_test_outcome(log, "test_h1_fires")
        assert out.status == "fired"
        assert out.binary_passed == 0
        assert out.binary_failed == 1
        assert is_test_fired(log, "test_h1_fires")


# ---- passed (test ran and OK) ------------------------------------------

class TestPassed:
    def test_simple_ok(self):
        log = """
running 1 test
test test_h1_fires ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;
"""
        out = parse_test_outcome(log, "test_h1_fires")
        assert out.status == "passed"
        assert is_test_passed(log, "test_h1_fires")
        assert not is_test_fired(log, "test_h1_fires")


# ---- #[ignore]d MUST NOT be counted as passed --------------------------

class TestIgnored:
    def test_ignored_is_distinct_from_passed(self):
        """L1.5+L2 audit Defect 02: a `#[ignore]`d PoC produces `test result:
        ok` for the binary but the specific test was NOT executed. The
        substring approach treated this as passed; the structured parse
        keeps it distinct."""
        log = """
running 1 test
test test_h1_fires ... ignored

test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out;
"""
        out = parse_test_outcome(log, "test_h1_fires")
        assert out.status == "ignored"
        # Critically: an ignored test is NOT "passed"
        assert not is_test_passed(log, "test_h1_fires")
        assert not is_test_fired(log, "test_h1_fires")


# ---- compile failures must not be parsed as "passed" -------------------

class TestCompileFailed:
    def test_could_not_compile(self):
        log = """
error[E0425]: cannot find function `settle_after_close` in this scope
  --> tests/test_h1.rs:7:13
   |
7  |     engine.settle_after_close(0);
   |             ^^^^^^^^^^^^^^^^^^

error: could not compile `engine` due to previous error
"""
        out = parse_test_outcome(log, "test_h1_fires")
        assert out.status == "compile_failed"
        assert "settle_after_close" in out.compile_excerpt

    def test_error_before_running_is_compile_failed(self):
        log = """
   Compiling engine v0.0.0
error[E0277]: trait bound not satisfied
  --> src/lib.rs:42:1
"""
        out = parse_test_outcome(log, "anything")
        assert out.status == "compile_failed"

    def test_test_panic_with_error_in_panic_msg_is_NOT_compile_failed(self):
        """A passing/failing test stanza that has `panicked at` text after
        `running N tests` must NOT be misread as compile failure."""
        log = """
running 1 test

running 1 test
test test_x ... FAILED

failures:

---- test_x stdout ----
thread 'test_x' panicked at 'error[?]: assertion failed', src/lib.rs:99:5

test result: FAILED. 0 passed; 1 failed; 0 ignored;
"""
        out = parse_test_outcome(log, "test_x")
        assert out.status == "fired"


# ---- the substring-attack vector that produced false confirms ---------

class TestSubstringSpoofingResistance:
    def test_sibling_test_failure_does_NOT_attribute_to_target(self):
        """L3+L4 audit Defect 06: a sibling test failing should NOT mark a
        DIFFERENT target test as `fired`. The structured parse must
        attribute only to the named test."""
        log = """
running 2 tests
test test_other_unrelated ... FAILED
test test_h1_fires ... ok

test result: FAILED. 1 passed; 1 failed; 0 ignored;
"""
        # Even though the binary says FAILED, our test_h1_fires PASSED
        assert parse_test_outcome(log, "test_h1_fires").status == "passed"
        assert parse_test_outcome(log, "test_other_unrelated").status == "fired"

    def test_test_not_run_when_binary_only_contained_others(self):
        log = """
running 1 test
test test_some_other ... ok

test result: ok. 1 passed; 0 failed; 0 ignored;
"""
        # test_h1_fires was not in this binary
        assert parse_test_outcome(log, "test_h1_fires").status == "not_run"

    def test_panicked_at_in_unrelated_text_does_NOT_fire(self):
        """A `panicked at` substring in a build script's output or another
        helper should NOT cause the gate to classify our test as fired."""
        log = """
warning: build.rs prints "panicked at" in its debug output
   Compiling engine v0.0.0
   Finished test [unoptimized + debuginfo] target(s) in 1.23s
    Running unittests src/lib.rs

running 1 test
test test_h1_fires ... ok

test result: ok. 1 passed; 0 failed; 0 ignored;
"""
        # The whole-log substring approach saw `panicked at` and marked
        # this fired. The structured parser correctly sees `... ok`.
        assert parse_test_outcome(log, "test_h1_fires").status == "passed"

    def test_assertion_failed_in_other_test_does_NOT_fire(self):
        log = """
running 2 tests
test test_h1_fires ... ok
test test_setup_helper ... FAILED

failures:

---- test_setup_helper stdout ----
assertion failed: setup broke

test result: FAILED. 1 passed; 1 failed; 0 ignored;
"""
        # Target test passed; the sibling failure shouldn't taint it
        assert parse_test_outcome(log, "test_h1_fires").status == "passed"


# ---- truncated / unparseable inputs -----------------------------------

class TestIndeterminate:
    def test_empty_log_indeterminate(self):
        assert parse_test_outcome("", "test_h1").status == "indeterminate"
        assert parse_test_outcome("   \n", "test_h1").status == "indeterminate"

    def test_log_truncated_before_summary(self):
        log = """
running 1 test
"""
        # No per-test line and no summary
        out = parse_test_outcome(log, "test_h1")
        assert out.status == "indeterminate"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
