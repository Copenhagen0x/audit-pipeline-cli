"""Regression tests for the L2 aptos adapter's pre-compile validator.

Cycle 20260514-151541 — three iteration rounds caught these failure
modes one at a time:

  v1: `engine_function` YAML placeholder taken as a real function name
       → APT12 called `vault::share_value` (doesn't exist).
  v2: cross-module `acquires treasury::Treasury` on a test in a
       non-treasury test module → bytecode verifier rejected.
  v3: invalid hex addresses like `@0xAT`, `@0xDEBT`, `@0xCOLL`
       → Move lexer rejected the non-hex chars.

The validator catches all three BEFORE compile so the hunt can retry
the author call with a feedback message instead of wasting compile
spend.
"""
from __future__ import annotations

from pathlib import Path

from audit_pipeline.poc_adapters.aptos import AptosAdapter

# ───────────────────── invalid hex addresses ─────────────────────


def test_validator_rejects_mnemonic_address_with_T() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    #[test]\n"
        "    fun test() {\n"
        "        let attacker = @0xAT;\n"
        "        let _ = attacker;\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert not ok
    assert err is not None
    assert "@0xAT" in err
    assert "hex" in err.lower()


def test_validator_rejects_multiple_invalid_addresses() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    #[test]\n"
        "    fun test() {\n"
        "        let a = @0xDEBT;\n"
        "        let b = @0xCOLL;\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert not ok
    assert err is not None
    assert "@0xDEBT" in err or "@0xCOLL" in err


def test_validator_accepts_valid_hex_addresses() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    #[test]\n"
        "    fun test() {\n"
        "        let a = @0x42;\n"
        "        let b = @0xAA;\n"
        "        let c = @0xBEEF;\n"
        "        let d = @0xC0DE;\n"
        "        let e = @0xCAFE;\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert ok, f"valid hex addresses incorrectly rejected: {err}"


# ───────────────────── cross-module acquires ─────────────────────


def test_validator_rejects_cross_module_acquires() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::treasury;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test(host: &signer) acquires treasury::Treasury {\n"
        "        treasury::initialize(host, @0x42);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert not ok
    assert err is not None
    assert "acquires" in err.lower()
    assert "treasury::Treasury" in err


def test_validator_accepts_test_without_acquires() -> None:
    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::treasury;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test(host: &signer) {\n"
        "        treasury::initialize(host, @0x42);\n"
        "        treasury::deposit(host, @0x42, 100);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert ok, f"test without acquires rejected: {err}"


# ───────────────────── non-existent module imports ─────────────────────


def test_validator_rejects_nonexistent_module(tmp_path: Path) -> None:
    """When engine has only `treasury.move`, importing `auction` should fail."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "treasury.move").write_text(
        "module mutatis::treasury {\n"
        "    struct Treasury has key { balance: u64 }\n"
        "    public fun initialize(host: &signer, addr: address) {}\n"
        "}\n"
    )

    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::auction;\n"     # NOT in sources/
        "    use mutatis::treasury;\n"    # OK
        "    #[test]\n"
        "    fun test() {}\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert not ok
    assert err is not None
    assert "auction" in err
    assert "treasury" in err  # listed in available modules


def test_validator_allows_framework_imports(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "treasury.move").write_text(
        "module mutatis::treasury {\n"
        "    public fun init() {}\n"
        "}\n"
    )

    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::treasury;\n"
        "    use aptos_framework::account;\n"
        "    use aptos_framework::coin;\n"
        "    use std::signer;\n"
        "    use std::vector;\n"
        "    #[test]\n"
        "    fun test() {}\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert ok, f"framework imports were rejected: {err}"


# ───────────────────── bare source paste ─────────────────────


def test_validator_rejects_bare_source_paste() -> None:
    body = (
        "public entry fun emergency_withdraw(caller: &signer, host: address, amount: u64) {\n"
        "    let vault = borrow_global_mut<Treasury>(host);\n"
        "    vault.balance = vault.balance - amount;\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body)
    assert not ok
    assert err is not None
    assert "module" in err.lower() or "paste" in err.lower()


# ───────────────────── real legit test passes ─────────────────────


def test_validator_rejects_nonexistent_function(tmp_path: Path) -> None:
    """APT12 reproduction: test calls vault::total_shares but only
    a `total_assets` field exists (no public getter named total_shares)."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "vault.move").write_text(
        "module mutatis::vault {\n"
        "    struct Vault has key { total_assets: u64, total_shares: u64 }\n"
        "    public fun initialize(host: &signer, fee_bps: u64, addr: address) {}\n"
        "    public fun deposit(user: &signer, host: address, amount: u64) {}\n"
        "    public fun total_assets(host: address): u64 { 0 }\n"
        "    // NOTE: no public fun total_shares — only the FIELD exists\n"
        "}\n"
    )

    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::vault;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test_x(host: &signer) {\n"
        "        vault::initialize(host, 0, @0x42);\n"
        "        let shares = vault::total_shares(@0x42);\n"
        "        assert!(shares == 0, 1);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert not ok
    assert err is not None
    assert "vault::total_shares" in err
    assert "vault::*" in err  # should hint at available functions


def test_validator_accepts_real_function_calls(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "vault.move").write_text(
        "module mutatis::vault {\n"
        "    public fun initialize(host: &signer, fee_bps: u64, addr: address) {}\n"
        "    public fun deposit(user: &signer, host: address, amount: u64) {}\n"
        "    public fun total_assets(host: address): u64 { 0 }\n"
        "}\n"
    )

    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::vault;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test_x(host: &signer) {\n"
        "        vault::initialize(host, 0, @0x42);\n"
        "        let assets = vault::total_assets(@0x42);\n"
        "        assert!(assets == 0, 1);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert ok, f"valid function calls rejected: {err}"


def test_validator_ignores_framework_function_calls(tmp_path: Path) -> None:
    """account::create_account_for_test is a framework call — don't flag."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "vault.move").write_text(
        "module mutatis::vault {\n"
        "    public fun initialize(host: &signer) {}\n"
        "}\n"
    )

    body = (
        "module mutatis::test_x {\n"
        "    use mutatis::vault;\n"
        "    use aptos_framework::account;\n"
        "    use std::signer;\n"
        "    #[test(host = @0x42)]\n"
        "    fun test_x(host: &signer) {\n"
        "        account::create_account_for_test(@0x42);\n"
        "        let _addr = signer::address_of(host);\n"
        "        vault::initialize(host);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert ok, f"framework calls incorrectly rejected: {err}"


def test_validator_accepts_legit_drain_test(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "treasury.move").write_text(
        "module mutatis::treasury {\n"
        "    public fun initialize(host: &signer, addr: address) {}\n"
        "    public fun balance(addr: address): u64 { 0 }\n"
        "    public fun emergency_withdraw(_caller: &signer, host: address, amount: u64) {}\n"
        "}\n"
    )
    (sources / "acl.move").write_text(
        "module mutatis::acl {\n"
        "    public fun initialize(host: &signer) {}\n"
        "}\n"
    )

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
        "        treasury::emergency_withdraw(&attacker, @0x100, 1000);\n"
        "        assert!(treasury::balance(@0x100) == 1000, 1);\n"
        "    }\n"
        "}\n"
    )
    ok, err = AptosAdapter().validate_test_body(body, engine_repo_root=tmp_path)
    assert ok, f"legit test was rejected: {err}"
