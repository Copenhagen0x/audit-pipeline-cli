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

    Patch #8 (audit CRITICAL 5ecc0355 + 8f37cf2a): refuse to return an
    empty / relative / unsafe root. The previous code accepted
    JELLEO_AUDIT_RUNS_ROOT="" and downstream `is_under_trusted_root`
    used `startswith("")` which is True for ANY path → the LLM tool
    sandbox could be tricked into reading /root/.ssh/jelleo-signing
    or any other absolute path. Now: any empty / non-absolute value
    raises RuntimeError so the misconfiguration fails closed instead
    of opening the sandbox.
    """
    raw = os.environ.get("JELLEO_AUDIT_RUNS_ROOT", DEFAULT_AUDIT_RUNS_ROOT)
    if not raw or not raw.strip():
        raise RuntimeError(
            "JELLEO_AUDIT_RUNS_ROOT is set but empty — refusing to compute "
            "audit_runs_root (empty root would let the LLM tool sandbox "
            "read any path on disk). Unset the env var to use the default "
            f"({DEFAULT_AUDIT_RUNS_ROOT!r}), or set it to an absolute "
            f"directory path."
        )
    p = Path(raw)
    if not p.is_absolute():
        raise RuntimeError(
            f"JELLEO_AUDIT_RUNS_ROOT must be an ABSOLUTE path; got "
            f"{raw!r}. A relative root resolves against the current "
            f"working directory, which the LLM tool sandbox treats as "
            f"trusted — that's not safe."
        )
    # P8 R0+R1 (goober + threat-modeler CRITICAL): also refuse
    # filesystem root and depth-1 directories. The original CRITICAL
    # 5ecc0355 was titled "empty audit_runs_root" but the actual
    # vulnerability class is "insufficiently bounded sandbox root".
    # `JELLEO_AUDIT_RUNS_ROOT=/` passes the empty/absolute checks but
    # makes every absolute path on the system pass relative_to(root) —
    # which is the exact failure mode the patch was supposed to close.
    # Refuse anything shallower than `/<two-segments>/...` so root must
    # be at least two levels deep (e.g. `/root/audit_runs`, never `/`
    # or `/root`).
    if len(p.parts) < 3:
        raise RuntimeError(
            f"JELLEO_AUDIT_RUNS_ROOT must be at least two directories "
            f"deep; got {raw!r}. A shallow root (e.g. `/`, `/tmp`, "
            f"`/root`) trusts too much of the filesystem — every path "
            f"under it would be readable by the LLM tool sandbox. Use "
            f"a dedicated subdirectory like `/root/audit_runs`."
        )
    return p


def is_under_trusted_root(p: Path) -> bool:
    """True iff ``p`` is under any path that's considered trusted by the
    tool-using agent's path guard (workspace OR audit_runs root).

    Used by llm_tools._normalize_path. Centralizing this means the audit
    runs root override flows through to the tool sandbox automatically.

    Patch #8 (audit CRITICAL 5ecc0355): the previous `startswith(str(
    audit_runs_root()))` check was vulnerable to:
      (a) empty root (`""`) → startswith always True → unbounded sandbox
      (b) prefix-match false positives (`/root/audit_runs2/...` matches
          `/root/audit_runs`) → cross-workspace read
      (c) symlinks under audit_runs_root pointing outside (e.g.,
          /root/audit_runs/percolator → /root/.ssh/) → exfil
    Round-1 fix: use Path.resolve() + relative_to() so symlinks are
    followed AND we get a real directory-boundary check, not a string
    prefix.
    """
    try:
        root = audit_runs_root().resolve(strict=False)
        candidate = p.resolve(strict=False)
        candidate.relative_to(root)
        return True
    except (ValueError, OSError):
        return False
    except RuntimeError:
        # audit_runs_root() itself raised (misconfigured env). Treat as
        # untrusted — fail closed.
        return False


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
