"""Centralized VPS path resolution.

Cross-cutting audit Defect 17 (LOW): the codebase hardcoded production
VPS paths like ``/var/www/jelleo.com/cycles`` and ``/root/audit_runs``
in multiple modules. That made the pipeline impossible to run on a
laptop without first faking the directory layout, and turned any future
VPS path change into a 20-file grep-and-edit job.

This module is the ONE place those paths live. Override via env vars:

  JELLEO_PUBLIC_ROOT       (default ``/var/www/jelleo.com``)
  JELLEO_AUDIT_RUNS_ROOT   (default ``/root/audit_runs``)

Use the helpers below — never hand-roll the path string in another module.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PUBLIC_ROOT = "/var/www/jelleo.com"
DEFAULT_AUDIT_RUNS_ROOT = "/root/audit_runs"


def public_root() -> Path:
    """Root of the publicly-served jelleo.com tree.

    On the prod VPS this is ``/var/www/jelleo.com``. On dev it can be
    overridden via ``JELLEO_PUBLIC_ROOT`` so smoke-tests and CI don't
    try to write into ``/var/www``.
    """
    return Path(os.environ.get("JELLEO_PUBLIC_ROOT", DEFAULT_PUBLIC_ROOT))


def public_cycles_dir() -> Path:
    """Where signed per-cycle artefacts get published."""
    return public_root() / "cycles"


def public_bundles_dir() -> Path:
    """Where signed fix bundles get published."""
    return public_root() / "bundles"


def public_customer_dir() -> Path:
    """Where per-customer dashboard manifests live."""
    return public_root() / "customer"


def audit_runs_root() -> Path:
    """Root where target workspaces are cloned (engine + wrapper sources).

    On the prod VPS this is ``/root/audit_runs``. Override via
    ``JELLEO_AUDIT_RUNS_ROOT`` for development.
    """
    return Path(os.environ.get("JELLEO_AUDIT_RUNS_ROOT", DEFAULT_AUDIT_RUNS_ROOT))


def is_under_trusted_root(p: Path) -> bool:
    """True iff ``p`` is under any path that's considered trusted by the
    tool-using agent's path guard (workspace OR audit_runs root).

    Used by llm_tools._normalize_path. Centralizing this means the audit
    runs root override flows through to the tool sandbox automatically.
    """
    pstr = str(p)
    return pstr.startswith(str(audit_runs_root()))


__all__ = [
    "DEFAULT_PUBLIC_ROOT",
    "DEFAULT_AUDIT_RUNS_ROOT",
    "public_root",
    "public_cycles_dir",
    "public_bundles_dir",
    "public_customer_dir",
    "audit_runs_root",
    "is_under_trusted_root",
]
