"""Regression test: zero F821 (undefined name) errors in the codebase.

Pre-flight before the cycle-20260511 rerun surfaced two real F821s in
hunt.py:
  - `looks_compile_failed` referenced on a PoC-cache-resume code path
    (would crash with NameError every time a cached PoC was resumed)
  - `log()` referenced in `_post_webhook` at module scope (would crash
    every time a webhook URL failed the SSRF allow-list check)

Both were latent. Both fired on real code paths the operator's about
to traverse. This test pins the no-F821 invariant so future refactors
can't re-introduce undefined-name bugs in the hot path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ruff_available() -> bool:
    try:
        import ruff  # noqa: F401
        return True
    except ImportError:
        # Fall back to attempting `python -m ruff --version` since ruff
        # often isn't importable but is reachable as a module.
        try:
            r = subprocess.run(
                [sys.executable, "-m", "ruff", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False


@pytest.mark.skipif(not _ruff_available(), reason="ruff not installed")
def test_no_undefined_names_in_source_tree() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src/",
         "--select", "F821", "--no-cache"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    # ruff exits 0 when no issues found; non-zero exit + non-empty stdout
    # means an F821 was detected.
    assert proc.returncode == 0, (
        f"ruff F821 check failed — undefined names in src/:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
