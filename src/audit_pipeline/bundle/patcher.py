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

Each line below is prefixed with its 1-indexed line number followed by `: `.
**The line-number prefix is REFERENCE ONLY** — it lets you cite correct
line numbers in your `@@` hunk headers. **DO NOT** include the `NNNN: `
prefix in the diff body. The diff body must contain the raw source lines
exactly as they appear in the file (without prefix), with a leading space
for context lines, `+` for added lines, `-` for removed lines.

```rust
{target_source}
```

# Output requirements

Reply with EXACTLY:

  1. A brief 1-2 sentence rationale of what your patch does, prefixed with `RATIONALE:`
  2. A blank line
  3. The unified diff, starting with `--- a/{target_file_path}` and `+++ b/{target_file_path}`

NO additional prose. NO markdown fences. NO commentary after the diff.

## Patch philosophy: STRUCTURAL fix, not symptom patch

A *symptom* patch adds a guard, returns early, or rejects the specific input
that triggers the PoC. A new caller that finds a different path into the
buggy state can re-trigger the bug. **DO NOT do this.**

A *structural* fix eliminates the bug at its root so no caller can ever
reach the buggy state. For F7-class (insurance counter shrunk without
debiting vault): the fix is to make every insurance-balance write paired
with the matching vault write *inside the same helper*, so it's
mechanically impossible for them to diverge regardless of who calls it.

Examples:

  WRONG (symptom):
    if loss > self.vault.get() {{
        return;  // skip
    }}
    self.use_insurance_buffer(loss);

  RIGHT (structural):
    // inside use_insurance_buffer, after the insurance debit:
    self.vault = U128::new(self.vault.get().saturating_sub(pay));
    // Now insurance and vault always move together — the F7 invariant
    // is enforced by construction, not by hopeful caller validation.

  WRONG (symptom): add a `require_admin_signer` guard at the top of the
  permissionless-callable function.
  RIGHT (structural): refactor the helper to take a typed
  `AdminAuthority` capability that callers must construct via a
  signer-checking helper, making it a TYPE error to call without auth.

Prefer fixes that:
  1. Make the buggy invariant impossible to violate at the helper level
  2. Are local (modify the helper itself, not every caller)
  3. Don't add new public surface (no new imports / deps / signatures)

Constraints:
  - Modify ONLY the function the PoC exercises (and adjacent helpers it
    calls if needed for a structural fix)
  - Do NOT change function signatures unless required for the structural
    fix (then explain in RATIONALE)
  - Do NOT add new dependencies
  - Patches may touch >5 lines if a structural fix requires it
  - `@@ -<line>,<count> +<line>,<count> @@` MUST cite the actual 1-indexed
    line numbers shown in the prefixed source above
  - Diff context lines MUST match the raw source verbatim (without the
    `NNNN: ` line-number prefix)
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
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4000,
) -> PatchDraft:
    """Ask the LLM to draft a patch. Returns PatchDraft (may be empty if no LLM).

    Sends the FULL target source to the LLM (Sonnet 4.6 has a 200K context
    window). Earlier truncation to 12K chars meant the LLM only saw the
    file header (imports + small structs) and had to invent line numbers
    from memory — patches were unanchored and `git apply --recount` couldn't
    recover them. With full source, hunk headers cite the real line numbers
    and the surrounding context matches verbatim.
    """
    template = template_for(bug_class)

    if not is_available():
        return PatchDraft(
            diff="", rationale="(no LLM available — manual patch required)",
            template_used=bug_class if bug_class in template.headline else "generic",
            llm_available=False,
        )

    # Prepend each line of target_source with its 1-indexed line number so
    # the LLM cites correct line numbers in its hunk headers. Otherwise the
    # LLM has to count newlines mentally across a 10K-line file and gets
    # the @@ -<line>,N +<line>,M @@ wrong, making git apply fail.
    target_source_numbered = "\n".join(
        f"{i+1:5}: {line}" for i, line in enumerate(target_source.splitlines())
    )

    prompt = PATCH_AUTHORSHIP_PROMPT.format(
        hypothesis_id=hypothesis_id,
        bug_class=bug_class,
        severity=severity,
        title=title,
        patch_intent=template.patch_intent,
        poc_source=poc_source,
        target_file_path=target_file_path,
        target_source=target_source_numbered,
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
    """Split the LLM output into rationale + diff.

    When the LLM produces multiple `--- a/...` headers in one response
    (e.g. emits a first draft, second-guesses itself, then emits a
    corrected version), the previous parser greedily captured the FIRST
    `--- a/` to the end of the response — gluing both drafts + the LLM's
    "wait, let me re-read..." commentary into a single malformed patch.
    Now we extract each candidate diff block separately, pick the LAST
    one (LLM's final answer), and reject responses that contain prose
    between the headers and the first `@@`.
    """
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

    # Find every diff block: from each `--- a/` header through its hunks
    # until either the next `--- a/` header (multi-file or multi-draft)
    # or the end of text. A well-formed block is `--- a/...\n+++ b/...\n@@`
    # followed by hunk lines; reject blocks that have prose between the
    # `+++ b/` and `@@` markers (that's LLM commentary, not a diff).
    diff_candidates: list[str] = []
    for m in re.finditer(
        r"(^--- a/.+?)(?=^--- a/|\Z)", text, re.MULTILINE | re.DOTALL,
    ):
        block = m.group(1).rstrip()
        # Quick validity probe: must contain both `+++ b/` and `@@`
        if "+++ b/" not in block or "@@" not in block:
            continue
        # Reject blocks where the LLM injected commentary between the
        # `+++ b/...` header and the first `@@` (or after the diff body).
        # A valid unified diff has no narrative prose inside it. Trim
        # everything after the LAST hunk line so trailing commentary
        # gets dropped.
        lines = block.splitlines()
        last_hunk_idx = -1
        for i, ln in enumerate(lines):
            if (
                ln.startswith(("+", "-", " ", "@@", "\\"))
                and not ln.startswith(("--- ", "+++ "))
            ):
                last_hunk_idx = i
        if last_hunk_idx > 0:
            block = "\n".join(lines[: last_hunk_idx + 1])
        diff_candidates.append(block)

    # Prefer the LAST block — when the LLM emits a draft + correction,
    # the correction is the final intended answer.
    diff = diff_candidates[-1].rstrip() if diff_candidates else ""
    # git apply REQUIRES a trailing newline on the patch text. .strip()
    # would also remove leading whitespace (which is meaningful in diff
    # context lines), so we use .rstrip() then add exactly one newline.
    if diff:
        diff = diff + "\n"

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
