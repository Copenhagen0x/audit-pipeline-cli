"""LLM patch authorship from a confirmed finding's PoC.

The LLM is constrained by the per-bug-class template (see templates.py).
Output: a unified-diff-format string.

Safety constraints baked in:
  * Patch must touch at most ONE function (caller can verify)
  * No new dependencies introduced (caller verifies via Cargo.toml diff)
  * No changed function signatures (caller verifies)
  * Output must parse as valid unified diff (caller validates)

This module produces a *draft*. Verification + operator review happens
downstream — see verifier.py + auth.py + bundle.py CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from audit_pipeline.bundle.templates import template_for
from audit_pipeline.utils import LLMUnavailable, complete, is_available


@dataclass
class PatchDraft:
    """The result of LLM patch authorship."""
    diff: str               # the unified diff (may be empty)
    rationale: str          # short explanation for the operator
    template_used: str      # bug_class name or "generic"
    llm_available: bool     # False if no LLM was reachable


PATCH_AUTHORSHIP_PROMPT = """You are writing a minimal-scope security patch for a Solana program.
The bug has already been CONFIRMED via a PoC test that triggers the violation.
Your output MUST be a valid unified diff that, when applied, makes the PoC stop triggering the bug.

# Confirmed finding

Hypothesis ID:   {hypothesis_id}
Bug class:       {bug_class}
Severity:        {severity}
Title:           {title}

# Bug-class fix template

{patch_intent}

# PoC test (this is what your patch must defuse)

```rust
{poc_source}
```

# Target source file (this is the file your patch should modify)

Path: `{target_file_path}`

```rust
{target_source}
```

# Output requirements

Reply with EXACTLY:

  1. A brief 1-2 sentence rationale of what your patch does, prefixed with `RATIONALE:`
  2. A blank line
  3. The unified diff, starting with `--- a/{target_file_path}` and `+++ b/{target_file_path}`

NO additional prose. NO markdown fences. NO commentary after the diff.

Constraints:
  - Modify ONLY the function the PoC exercises
  - Do NOT change the function's signature
  - Do NOT add new imports unless absolutely required
  - Do NOT add new dependencies
  - Keep the diff minimal — fewer changed lines is safer
"""


def author_patch(
    *,
    hypothesis_id: str,
    bug_class: str,
    severity: str,
    title: str,
    poc_source: str,
    target_file_path: str,
    target_source: str,
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 2000,
) -> PatchDraft:
    """Ask the LLM to draft a patch. Returns PatchDraft (may be empty if no LLM)."""
    template = template_for(bug_class)

    if not is_available():
        return PatchDraft(
            diff="", rationale="(no LLM available — manual patch required)",
            template_used=bug_class if bug_class in template.headline else "generic",
            llm_available=False,
        )

    prompt = PATCH_AUTHORSHIP_PROMPT.format(
        hypothesis_id=hypothesis_id,
        bug_class=bug_class,
        severity=severity,
        title=title,
        patch_intent=template.patch_intent,
        poc_source=poc_source[:8000],
        target_file_path=target_file_path,
        target_source=target_source[:12000],
    )

    try:
        resp = complete(prompt, model=model, max_tokens=max_tokens)
    except LLMUnavailable as e:
        return PatchDraft(
            diff="", rationale=f"(LLM error: {e})",
            template_used=bug_class, llm_available=False,
        )

    return _parse_response(resp.text, bug_class)


def _parse_response(raw: str, bug_class: str) -> PatchDraft:
    """Split the LLM output into rationale + diff."""
    text = raw.strip()
    rationale = ""
    m = re.search(r"^RATIONALE:\s*(.+?)$", text, re.MULTILINE)
    if m:
        rationale = m.group(1).strip()
        # Strip the rationale line so what remains is (mostly) the diff
        text = text[m.end():].strip()

    # Strip any markdown code fences the LLM added despite the instructions
    text = re.sub(r"^```(?:diff|patch)?\s*\n", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n```\s*$", "", text)

    # Diff must start with --- a/ to be valid
    diff_match = re.search(r"(^--- a/.+?)(?=\Z)", text, re.MULTILINE | re.DOTALL)
    diff = diff_match.group(1).strip() if diff_match else ""

    return PatchDraft(
        diff=diff,
        rationale=rationale or "(no rationale extracted)",
        template_used=bug_class,
        llm_available=True,
    )


def is_unified_diff(text: str) -> bool:
    """Coarse check that text looks like a unified diff.

    FIX B-#19: also reject patches that contain binary patch markers,
    symlink mode changes, or rename ops. These payload shapes get past
    a simple "has --- and +++" check and `git apply` will happily process
    them — opening a path to binary writes, symlink injection, and file
    moves that the LLM should never be authoring as bug fixes.
    """
    if not text.strip():
        return False
    has_minus_header = bool(re.search(r"^--- (a/|/dev/null)", text, re.MULTILINE))
    has_plus_header = bool(re.search(r"^\+\+\+ (b/|/dev/null)", text, re.MULTILINE))
    has_hunk = bool(re.search(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", text, re.MULTILINE))
    if not (has_minus_header and has_plus_header and has_hunk):
        return False
    # Reject dangerous diff modes — bundle policy is text edits to existing
    # files only. The verifier's _gate_patch_well_formed has the same
    # rejections; we replicate here so the LLM-output extraction stage
    # also rejects, not just the verify-time gate.
    forbidden = (
        "GIT binary patch",
        "Binary files",
        "new file mode 120000",  # symlink
        "rename from",
        "rename to",
        "deleted file mode",
        "new file mode",
        "copy from",
        "copy to",
    )
    return all(marker not in text for marker in forbidden)


def files_touched(diff: str) -> list[str]:
    """Extract the paths the diff modifies (b-side)."""
    return re.findall(r"^\+\+\+ b/(\S+)", diff, re.MULTILINE)
