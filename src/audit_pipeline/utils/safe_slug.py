"""Path-safe slug helper (audit-021 / Bucket L L6).

Centralized sanitization for any user / metadata-controlled string
that flows into a filename component. The classic L6 attack is a
``hypothesis_id`` of ``../../../etc/passwd`` (or its Windows variant)
that lets a malicious upstream write outside the intended output
directory. The historical fix scattered ``.replace("/", "-")`` across
five call sites; ``audit-021`` centralizes it here and adds the
Windows separator + reserved-char defenses.

Threat model covered:
  * POSIX path traversal: ``/`` separator → '-'
  * Windows path traversal: ``\\`` separator → '-'
  * Null byte injection: ``\\0`` → '-'
  * Reserved Windows filename chars (``<>:"|?*``) → '-'
  * All other control characters (``\\x00-\\x1f``) → '-'
  * Empty / dots-only inputs → fallback name (Path treats ``.`` and
    ``..`` as cwd / parent — a slug equal to either would silently
    redirect the write)

Out of scope:
  * Windows reserved device names (``CON``, ``PRN``, etc.) — the
    surrounding ``output / <slug>.<ext>`` shape makes these non-
    exploitable because the path includes a directory component
  * Unicode confusables (e.g. fullwidth slash U+FF0F) — Python's
    ``Path`` does not interpret these as separators
"""

from __future__ import annotations

import re

# Allowlist: alnum + dot + dash + underscore. Everything else is
# replaced with a single dash. Note the allowlist INCLUDES ``.`` so
# slugs like ``HYP1.2`` survive; the "dots-only" guard below catches
# the ``..`` traversal attempt at the higher level.
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_hyp_slug(
    hypothesis_id: str | None,
    *,
    fallback: str = "finding",
    max_len: int = 128,
) -> str:
    """Return a path-safe filename component for a hypothesis ID.

    Strips every character outside the safe allowlist
    (``A-Za-z0-9._-``), trims leading/trailing dashes, and rejects
    empty results or names consisting only of ``.``  /``-`` (which
    ``Path`` would treat as the current/parent dir).

    The ``max_len`` cap prevents path-length-attack variants where a
    long ``hypothesis_id`` blows past the host filesystem's component
    limit (255 on most Linux fs's, 260 MAX_PATH-style limits on
    Windows depending on enabling).
    """
    if not hypothesis_id:
        return fallback
    cleaned = _SAFE_SLUG_RE.sub("-", hypothesis_id).strip("-")
    # Reject "." and ".." style names (Path interprets them as
    # cwd/parent — could write to the wrong directory). Also reject
    # any name that contains only dots and dashes after sanitization.
    if not cleaned or set(cleaned) <= {".", "-"}:
        return fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    return cleaned or fallback
