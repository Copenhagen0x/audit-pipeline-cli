"""Tests for the jelleo-autoupdate auto-deploy unit.

Validates that:
  - The bash script has valid syntax
  - The systemd unit files declare the required sections
  - The install script wires the new unit into the enable/restart loop
  - The timer fires on a sane cadence (not too aggressive)

We don't actually run the script against a live git repo — that needs
a VPS with the repo cloned. Static + syntactic checks only here.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_autoupdate_script_syntax_ok() -> None:
    script = DEPLOY / "jelleo-autoupdate.sh"
    assert script.is_file()
    # `bash -n` parses without executing
    proc = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, (
        f"bash -n failed:\n{proc.stderr}"
    )


def test_autoupdate_service_has_required_sections() -> None:
    p = DEPLOY / "jelleo-autoupdate.service"
    assert p.is_file()
    c = configparser.ConfigParser(strict=False)
    c.read(p)
    assert "Unit" in c.sections()
    assert "Service" in c.sections()
    # Service must declare oneshot type (vs simple/forking)
    assert c.get("Service", "Type").strip() == "oneshot"
    # Must execute the script we shipped
    exec_start = c.get("Service", "ExecStart")
    assert "jelleo-autoupdate.sh" in exec_start
    # Must alert on hard failure via the OnFailure handler.
    # configparser lowercases option keys.
    assert "onfailure" in c.options("Unit")


def test_autoupdate_timer_fires_every_5min() -> None:
    p = DEPLOY / "jelleo-autoupdate.timer"
    assert p.is_file()
    c = configparser.ConfigParser(strict=False)
    c.read(p)
    assert "Timer" in c.sections()
    interval = c.get("Timer", "OnUnitActiveSec").strip()
    # Sanity: not less than 1 min (would hammer GitHub), not more than 1h
    # (would be slow to deploy).
    assert interval in ("5min", "5 min", "300", "300s"), (
        f"unexpected OnUnitActiveSec={interval!r}"
    )
    boot = c.get("Timer", "OnBootSec").strip()
    assert boot in ("2min", "2 min", "120", "120s")
    # Catch-up after downtime
    assert c.getboolean("Timer", "Persistent") is True


def test_install_systemd_wires_autoupdate() -> None:
    """install_systemd.sh must (a) copy the unit files and (b) enable
    the timer. Both arms of the wiring need to be present, otherwise
    the auto-deploy silently won't activate on next install."""
    install = (DEPLOY / "install_systemd.sh").read_text(encoding="utf-8")
    # Unit file copy step
    assert "jelleo-autoupdate" in install, (
        "install_systemd.sh does not reference jelleo-autoupdate — "
        "auto-deploy units won't get installed"
    )
    # Timer enable step
    enable_lines = [
        line for line in install.splitlines()
        if "jelleo-autoupdate" in line
    ]
    # We expect references in BOTH the install for-loop and the enable for-loop
    assert len(enable_lines) >= 2, (
        f"install_systemd.sh references jelleo-autoupdate only {len(enable_lines)} times; "
        f"expected at least 2 (copy + enable)"
    )


def test_autoupdate_script_uses_fast_forward_only() -> None:
    """Hard rule: the script must NEVER do a non-FF pull. A merge or
    rebase on a deploy box could clobber operator hot-fixes."""
    script = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
    assert "--ff-only" in script, (
        "auto-update script must pass --ff-only to git pull"
    )
    # Must NOT contain non-FF pull patterns
    assert "git rebase" not in script.lower(), (
        "auto-update must not rebase"
    )
    assert "git merge" not in script.lower(), (
        "auto-update must not merge — only fast-forward pulls"
    )


def test_autoupdate_script_locks_to_prevent_races() -> None:
    """flock prevents two timer fires from racing during a long
    install_systemd run."""
    script = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
    assert "flock" in script, (
        "auto-update script must use flock to prevent concurrent runs"
    )


def test_autoupdate_script_refuses_when_local_changes_present() -> None:
    """Operator hot-fixes on the VPS must NOT get clobbered by the
    auto-deploy. Local dirty state = skip."""
    script = (DEPLOY / "jelleo-autoupdate.sh").read_text(encoding="utf-8")
    assert "git diff --quiet" in script, (
        "auto-update must check for uncommitted local changes before pulling"
    )
