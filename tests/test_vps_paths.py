"""Tests for the centralized VPS path module.

Cross-cutting audit Defect 17 (LOW). Before this, prod-only paths like
``/var/www/jelleo.com/cycles`` and ``/root/audit_runs`` were sprinkled
across 8+ modules as literals, so dev/CI couldn't run those code paths
without faking the directory layout. The new ``audit_pipeline.utils.vps_paths``
module centralizes them with env-var overrides.
"""

from __future__ import annotations

from pathlib import Path


def test_defaults_match_prod_paths() -> None:
    """The defaults must continue to point at the canonical prod paths
    so existing systemd units don't need to set env vars."""
    from audit_pipeline.utils.vps_paths import (
        DEFAULT_AUDIT_RUNS_ROOT,
        DEFAULT_PUBLIC_ROOT,
    )
    assert DEFAULT_PUBLIC_ROOT == "/var/www/jelleo.com"
    assert DEFAULT_AUDIT_RUNS_ROOT == "/root/audit_runs"


def test_public_root_env_var_override(monkeypatch, tmp_path: Path) -> None:
    """JELLEO_PUBLIC_ROOT must override the default."""
    from audit_pipeline.utils import vps_paths
    monkeypatch.setenv("JELLEO_PUBLIC_ROOT", str(tmp_path))
    assert vps_paths.public_root() == tmp_path
    assert vps_paths.public_cycles_dir() == tmp_path / "cycles"
    assert vps_paths.public_bundles_dir() == tmp_path / "bundles"
    assert vps_paths.public_customer_dir() == tmp_path / "customer"


def test_audit_runs_env_var_override(monkeypatch, tmp_path: Path) -> None:
    from audit_pipeline.utils import vps_paths
    monkeypatch.setenv("JELLEO_AUDIT_RUNS_ROOT", str(tmp_path))
    assert vps_paths.audit_runs_root() == tmp_path


def test_is_under_trusted_root_honors_env_var(monkeypatch, tmp_path: Path) -> None:
    """The path-traversal guard for the tool-using agent must honor the
    JELLEO_AUDIT_RUNS_ROOT override — otherwise the guard would reject
    every path on a dev box (the default ``/root/audit_runs`` doesn't
    exist there)."""
    from audit_pipeline.utils import vps_paths
    monkeypatch.setenv("JELLEO_AUDIT_RUNS_ROOT", str(tmp_path))
    inside = tmp_path / "percolator-live" / "src" / "lib.rs"
    outside = Path("/tmp/not-allowed")
    assert vps_paths.is_under_trusted_root(inside)
    assert not vps_paths.is_under_trusted_root(outside)


def test_dashboard_count_signed_receipts_uses_public_root(
    monkeypatch, tmp_path: Path,
) -> None:
    """The dashboard signed-receipt count must read from the overridden
    public root, not the hardcoded /var/www path."""
    monkeypatch.setenv("JELLEO_PUBLIC_ROOT", str(tmp_path))
    cycles = tmp_path / "cycles"
    cycles.mkdir()
    # Two cycles, one signed
    (cycles / "C-A").mkdir()
    (cycles / "C-A" / "cycle.html.sig").write_text("sig", encoding="utf-8")
    (cycles / "C-B").mkdir()
    # B has no .sig — must not count
    from audit_pipeline.commands.dashboard import _count_signed_receipts
    assert _count_signed_receipts() == 1
