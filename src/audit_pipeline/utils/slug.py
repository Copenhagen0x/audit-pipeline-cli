"""Canonical hypothesis-ID slug.

Phase B 12-audit L1.5+L2 Defect 03: hunt.py and poc_llm.py each
defined a different ``_slugify``. hunt's truncated at ``[:60]``;
poc_llm's didn't. For any hyp_id whose lowercase slug exceeded 60 chars,
hunt looked for ``test_<60chars>.rs`` while poc_llm wrote
``test_<fullslug>.rs`` — the file was missing under hunt's truncated
name and the F7-flavored fallback scaffold ran instead, attributing
its ``absorb_protocol_loss`` outcome to an unrelated hyp.

One slug definition, imported by both. 60-char cap (Rust + filesystem
safe) is now the SHARED ceiling.
"""

from __future__ import annotations

import re

MAX_SLUG_LEN = 60


def slug_for_hypothesis(text: str) -> str:
    """Canonical hyp_id → identifier slug. Lowercase, ``[a-z0-9_]+``, ≤60 chars."""
    if not text:
        return "hunt_finding"
    out = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return out[:MAX_SLUG_LEN] or "hunt_finding"


__all__ = ["slug_for_hypothesis", "MAX_SLUG_LEN"]
