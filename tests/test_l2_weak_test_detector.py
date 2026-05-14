"""Regression tests for the L2 aptos adapter's weak-test detector.

Added on cycle 20260514-151541 after APT12-fixed-point-precision-drop
produced an L2 PoC test with ~80 lines of precision-divergence math in
``// ...`` comments, then ended with ``assert!(rate == 0, 0)`` after no
state mutation. The test "passed" trivially, recording fired=false
without ever exercising the bug-triggering code path.

The detector flags three weak-test patterns BEFORE the test compiles
and runs (so we don't waste the compile/run cost on tests that won't
prove anything either way):

  1. Comment-to-code ratio > 3:1 — bug exploration was offloaded into
     comments instead of into the test setup.
  2. Test fn body has < 5 non-comment lines — almost certainly not
     exercising the bug-triggering path.
  3. Sole assertion is trivial (`assert!(true, ...)`,
     `assert!(rate == 0, ...)` with no mutation).
"""
from __future__ import annotations

from audit_pipeline.poc_adapters.aptos import AptosAdapter


def _adapter() -> AptosAdapter:
    return AptosAdapter()


# ───────────────── Anti-bullshit detector ─────────────────


def test_detect_weak_test_flags_apt12_style_comment_overflow() -> None:
    """APT12 reproduction: ~80 lines of math in comments + a trivial assert."""
    body = (
        "module mutatis::test_x {\n"
        "    #[test]\n"
        "    fun test() {\n"
        + "\n".join(f"        // math line {i}" for i in range(80))
        + "\n"
        "        let rate = 0;\n"
        "        assert!(rate == 0, 0);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert is_weak
    assert reason is not None
    assert "comment" in reason.lower()


def test_detect_weak_test_flags_short_body() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    #[test]\n"
        "    fun test() {\n"
        "        assert!(true, 0);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert is_weak
    assert reason is not None


def test_detect_weak_test_flags_trivial_rate_eq_zero() -> None:
    """`assert!(rate == 0, 0)` as the sole assertion after no mutation."""
    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::lending_pool;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test(host: &signer) {\n"
        "        lending_pool::initialize(host, 0, 1, 10000, 5000, 8000, @0x42, @0x99, 500);\n"
        "        let rate = lending_pool::current_borrow_rate(@0x42);\n"
        "        let bal = 0;\n"
        "        let unused = bal;\n"
        "        assert!(rate == 0, 0);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert is_weak
    assert reason is not None
    assert "trivial" in reason.lower() or "0" in reason


def test_detect_weak_test_accepts_real_drain_test() -> None:
    """APT1 reproduction: legit drain test with state-dependent assertion."""
    body = (
        "module mutatis::test_apt1 {\n"
        "    use mutatis::treasury;\n"
        "    use mutatis::acl;\n"
        "    use aptos_framework::account;\n"
        "    #[test(host = @0x100, attacker = @0x999)]\n"
        "    fun test_apt1(host: signer, attacker: signer) {\n"
        "        account::create_account_for_test(@0x100);\n"
        "        account::create_account_for_test(@0x999);\n"
        "        acl::initialize(&host);\n"
        "        treasury::initialize(&host, @0x100);\n"
        "        treasury::deposit(&host, @0x100, 1000000);\n"
        "        let bal_before = treasury::balance(@0x100);\n"
        "        assert!(bal_before == 1000000, 1);\n"
        "        treasury::emergency_withdraw(&attacker, @0x100, 1000000);\n"
        "        let bal_after = treasury::balance(@0x100);\n"
        "        assert!(bal_after == 1000000, 2);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert not is_weak, f"legit drain test was flagged as weak: {reason}"


def test_detect_weak_test_accepts_real_overflow_test() -> None:
    """APT10 reproduction: legit overflow test."""
    body = (
        "module mutatis::test_apt10 {\n"
        "    use mutatis::treasury;\n"
        "    use mutatis::acl;\n"
        "    #[test(host = @0x42, depositor = @0x99)]\n"
        "    fun test_apt10(host: &signer, depositor: &signer) {\n"
        "        acl::initialize(host);\n"
        "        treasury::initialize(host, @0x42);\n"
        "        let max_u64: u64 = 18446744073709551615;\n"
        "        treasury::deposit(depositor, @0x42, max_u64);\n"
        "        let bal = treasury::balance(@0x42);\n"
        "        assert!(bal == max_u64, 1001);\n"
        "        treasury::deposit(depositor, @0x42, 1);\n"
        "        assert!(false, 9999);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert not is_weak, f"legit overflow test was flagged as weak: {reason}"


def test_detect_weak_test_accepts_well_reasoned_negative() -> None:
    """APT11 reproduction: legit pass-no-bug test that DOES call the
    function with non-trivial inputs. Bug class is genuinely unreachable
    here (bps cap blocks underflow)."""
    body = (
        "module mutatis::test_apt11 {\n"
        "    use mutatis::fee_manager;\n"
        "    use mutatis::acl;\n"
        "    use aptos_framework::account;\n"
        "    #[test(host = @0x42, caller = @0x99)]\n"
        "    fun test_apt11(host: &signer, caller: &signer) {\n"
        "        account::create_account_for_test(@0x42);\n"
        "        account::create_account_for_test(@0xAA);\n"
        "        acl::initialize(host);\n"
        "        fee_manager::initialize(host);\n"
        "        fee_manager::add_recipient(host, @0x42, @0xAA, 5000);\n"
        "        fee_manager::add_recipient(host, @0x42, @0xBB, 5000);\n"
        "        fee_manager::distribute(caller, @0x42, 1);\n"
        "        assert!(fee_manager::balance(@0x42) == 1, 9999);\n"
        "    }\n"
        "}\n"
    )
    is_weak, reason = _adapter().detect_weak_test(body)
    assert not is_weak, f"legit pass-no-bug test was flagged as weak: {reason}"
