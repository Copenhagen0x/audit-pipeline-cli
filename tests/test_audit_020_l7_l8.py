"""Regression tests for audit-020 (Bucket L sub-items L7 + L8).

L7 — `poc_llm.py` was reading engine .rs files into the LLM prompt with no
size cap. A 10GB malicious or build-artifact file would OOM the worker.
Fix: `_read_capped` with a 1MiB default cap (override via
`JELLEO_POC_MAX_RS_BYTES`). Truncation marker appended so the prompt
author sees the cut.

L8 — `anchor_kani_runner.run_kani_proof` hardcoded `timeout_s=1800` with
no env override. Fix: `_default_kani_timeout_s()` reads
`JELLEO_KANI_TIMEOUT_S` and clamps to >= 30s.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --- L7: _read_capped --------------------------------------------------------


def test_read_capped_returns_full_text_under_cap(tmp_path: Path) -> None:
    """A small file should round-trip verbatim through _read_capped."""
    from audit_pipeline.commands.poc_llm import _read_capped

    f = tmp_path / "small.rs"
    f.write_text("fn main() {}", encoding="utf-8")
    out = _read_capped(f)
    assert out == "fn main() {}"


def test_read_capped_truncates_files_over_cap(tmp_path: Path) -> None:
    """A file larger than the cap must be truncated AND marked."""
    from audit_pipeline.commands.poc_llm import _read_capped

    f = tmp_path / "huge.rs"
    payload = "x" * 100  # well under the 1MiB default
    f.write_text(payload, encoding="utf-8")
    out = _read_capped(f, cap=50)
    # First 50 chars come through, then the truncation marker is appended.
    assert out.startswith("x" * 50), out[:60]
    assert "TRUNCATED" in out
    assert "50-byte cap" in out


def test_read_capped_handles_invalid_utf8(tmp_path: Path) -> None:
    """Stray non-UTF-8 bytes should be replaced, not crash."""
    from audit_pipeline.commands.poc_llm import _read_capped

    f = tmp_path / "binary.rs"
    f.write_bytes(b"valid_ascii\x90more")  # \x90 is invalid UTF-8 alone
    out = _read_capped(f)
    # The bad byte is replaced with U+FFFD; the rest is preserved.
    assert "valid_ascii" in out
    assert "more" in out


def test_read_capped_env_override_raises_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``JELLEO_POC_MAX_RS_BYTES`` overrides the module-level default
    at import time. Since the module is already imported, this test
    just verifies the explicit ``cap=`` kwarg path (which is the only
    way to override post-import without reload)."""
    from audit_pipeline.commands.poc_llm import _read_capped

    f = tmp_path / "test.rs"
    f.write_text("x" * 200, encoding="utf-8")
    out_small = _read_capped(f, cap=10)
    out_large = _read_capped(f, cap=500)
    assert "TRUNCATED" in out_small
    assert "TRUNCATED" not in out_large


# --- L8: _default_kani_timeout_s --------------------------------------------


def test_kani_timeout_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default timeout is 1800 (30m) when env var is unset."""
    from audit_pipeline.anchor_kani_runner import _default_kani_timeout_s

    monkeypatch.delenv("JELLEO_KANI_TIMEOUT_S", raising=False)
    assert _default_kani_timeout_s() == 1800


def test_kani_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting JELLEO_KANI_TIMEOUT_S overrides the 1800 default."""
    from audit_pipeline.anchor_kani_runner import _default_kani_timeout_s

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "3600")
    assert _default_kani_timeout_s() == 3600


def test_kani_timeout_floor_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values below 30s are clamped to 30 (a 1-second timeout would
    guarantee timeout-as-failure on every harness)."""
    from audit_pipeline.anchor_kani_runner import _default_kani_timeout_s

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "1")
    assert _default_kani_timeout_s() == 30


def test_kani_timeout_invalid_falls_back_to_1800(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer values fall back to 1800 (don't crash the runner)."""
    from audit_pipeline.anchor_kani_runner import _default_kani_timeout_s

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "not-a-number")
    assert _default_kani_timeout_s() == 1800

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "")
    assert _default_kani_timeout_s() == 1800


def test_kani_timeout_zero_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero / negative env values are clamped up to 30."""
    from audit_pipeline.anchor_kani_runner import _default_kani_timeout_s

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "0")
    assert _default_kani_timeout_s() == 30

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "-100")
    assert _default_kani_timeout_s() == 30


def test_kani_timeout_env_reaches_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit-020 R1 (PG-R0 #1 fix): integration test pinning that the env
    var actually reaches ``subprocess.run`` when ``run_kani_proof`` is
    called WITHOUT an explicit ``timeout_s`` kwarg. Pre-R1, ``hunt.py``
    hardcoded ``timeout_s=1800`` which silently bypassed the env override
    — the contract was nominally there but the wiring was dead.
    """
    from audit_pipeline import anchor_kani_runner

    monkeypatch.setenv("JELLEO_KANI_TIMEOUT_S", "777")

    captured: dict[str, object] = {}

    def _fake_run(*_args, **kwargs):  # noqa: ANN003
        captured["timeout"] = kwargs.get("timeout")

        class _R:
            returncode = 0
            stdout = "VERIFICATION SUCCESSFUL"
            stderr = ""
        return _R()

    monkeypatch.setattr(anchor_kani_runner.subprocess, "run", _fake_run)

    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    anchor_kani_runner.run_kani_proof(
        sidecar_dir=sidecar,
        harness_name="any_harness",
    )

    assert captured.get("timeout") == 777, (
        f"Expected subprocess.run to receive timeout=777 (from "
        f"JELLEO_KANI_TIMEOUT_S env var), got {captured.get('timeout')!r}. "
        f"The env -> subprocess wiring is broken."
    )


def test_hunt_l3_omits_timeout_so_env_takes_effect() -> None:
    """Audit-020 R1: hunt.py's L3 call site must NOT pass ``timeout_s=1800``
    as a literal kwarg, otherwise the env-override path in
    ``run_kani_proof`` is dead code (PG-R0 #1).
    """
    import re as _re

    import audit_pipeline.commands.hunt as hunt_mod

    src = Path(hunt_mod.__file__).read_text(encoding="utf-8")
    bad = _re.findall(
        r"_run_kani_proof\s*\([^)]*?timeout_s\s*=\s*1800",
        src, _re.DOTALL,
    )
    assert not bad, (
        f"hunt.py has a _run_kani_proof call hardcoding timeout_s=1800, "
        f"which silently bypasses the JELLEO_KANI_TIMEOUT_S env override. "
        f"Found {len(bad)} match(es). Omit the kwarg so the helper resolves "
        f"from the env. (Audit-020 PG-R0 #1)"
    )
