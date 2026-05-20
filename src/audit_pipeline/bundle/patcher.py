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
from pathlib import Path

from audit_pipeline.bundle.sig_index import build_sig_index
from audit_pipeline.bundle.templates import template_for
from audit_pipeline.utils import LLMUnavailable, complete, is_available


@dataclass
class PatchDraft:
    """The result of LLM patch authorship."""
    diff: str               # the unified diff (may be empty)
    rationale: str          # short explanation for the operator
    template_used: str      # bug_class name or "generic"
    llm_available: bool     # False if no LLM was reachable


_HUNK_HDR_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@")


def _repair_hunk_counts(diff: str) -> str:
    """Rewrite each `@@ -A,B +C,D @@` so B and D match the actual hunk body.

    The LLM frequently emits a hunk header that lies about line counts —
    e.g. `@@ -25,6 +25,6 @@` followed by a body that's really 5 old / 5
    new lines. `git apply` then rejects the patch as "corrupt patch at
    line N". This walker counts the actual `-`/`+`/` ` lines in each
    hunk body and rewrites the header to match.
    """
    lines = diff.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _HUNK_HDR_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        old_start = int(m.group(1))
        new_start = int(m.group(3))
        body_start = i + 1
        body_end = body_start
        while body_end < len(lines):
            if lines[body_end].startswith("@@") or lines[body_end].startswith("---"):
                break
            body_end += 1
        old_count = 0
        new_count = 0
        for line in lines[body_start:body_end]:
            if line.startswith("-") and not line.startswith("---"):
                old_count += 1
            elif line.startswith("+") and not line.startswith("+++"):
                new_count += 1
            elif line.startswith(" ") or line == "\n":
                old_count += 1
                new_count += 1
            elif line.startswith("\\"):
                # `\ No newline at end of file` — informational, no count
                pass
        out.append(
            f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"
        )
        out.extend(lines[body_start:body_end])
        i = body_end
    return "".join(out)


PATCH_AUTHORSHIP_PROMPT_SOLIDITY = """You are writing a minimal-scope security patch for a Solidity smart contract.
The bug has already been CONFIRMED via a PoC Foundry test that triggers the violation.
Your output MUST be a valid unified diff that, when applied, makes the PoC stop triggering the bug.

# Confirmed finding

Hypothesis ID:   {hypothesis_id}
Bug class:       {bug_class}
Severity:        {severity}
Title:           {title}

# Bug-class fix template

{patch_intent}

# PoC test (this is what your patch must defuse)

```solidity
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

```solidity
{target_source}
```

{sig_index_section}

# Output requirements

Reply with EXACTLY:

  1. A brief 1-2 sentence rationale of what your patch does, prefixed with `RATIONALE:`
  2. A blank line
  3. The unified diff, starting with `--- a/{target_file_path}` and `+++ b/{target_file_path}`

NO additional prose. NO markdown fences. NO commentary after the diff.

## Patch philosophy: STRUCTURAL fix, not symptom patch

A *symptom* patch adds a guard inside one specific function, returns
early on a specific calldata pattern, or rejects the one input the PoC
sends. A new caller that finds a different path into the buggy state
can re-trigger the bug. **DO NOT do this.**

A *structural* fix eliminates the bug at its root so no caller can ever
reach the buggy state.

Solidity-idiomatic examples of structural vs symptom:

  WRONG (symptom): Add `require(msg.sender == owner)` only inside the
    one function the PoC exercises, leaving sibling functions
    permissionless.
  RIGHT (structural): Add the existing `onlyOwner` modifier (defined
    in CoreBase) to the function signature, matching the consistent
    pattern used by every other privileged setter in the contract.

  WRONG (symptom): Add a `require(amount <= cap)` check on top of the
    one call path that reproduced the over-charge bug.
  RIGHT (structural): Replace the uncapped `amount` argument passed to
    `transferFrom` with the already-computed capped variable
    (`applied = min(amount, debt)`), matching what the sibling
    `liquidate` function already does correctly.

  WRONG (symptom): Add `nonReentrant` modifier only when the PoC
    re-enters via a specific hook.
  RIGHT (structural): Reorder the function body to follow CEI
    (Checks-Effects-Interactions): perform ALL state writes
    (`shareBalance[msg.sender] -= shares;`, `totalShares -= shares;`)
    BEFORE the external token `transfer(...)` call. Reentrancy is
    now impossible regardless of caller behavior.

Solidity-specific fix patterns by bug class:

  - reentrancy / CEI: move state updates before external calls
  - missing access control: add the existing `onlyOwner` /
    `onlyRole(X)` modifier — DO NOT invent new modifiers
  - signature replay: include `block.chainid` AND a per-account
    nonce in the `keccak256(...)` digest (and increment the nonce
    after consumption)
  - share inflation: add `require(shares != 0, ZeroShares())`
    after the toShares computation
  - oracle staleness: require `block.timestamp - oracle.updatedAt()
    <= MAX_STALE_SEC`
  - tx.origin: replace `tx.origin == owner` with `msg.sender == owner`
  - approve-race: insert `token.approve(spender, 0);` before
    `token.approve(spender, amount);`
  - dust loss: pull only `(total / N) * N` from funder, never
    the full `total`
  - DoS batch: replace the for-loop transfer with a `pending[user]
    += amount` mapping + a `claim()` function recipients call

Constraints:
  - Modify ONLY the function the PoC exercises (and adjacent helpers it
    calls if needed for a structural fix)
  - Do NOT change function signatures unless required for the structural
    fix (then explain in RATIONALE)
  - Do NOT add new dependencies (no new `import` lines unless absolutely
    required — prefer reusing existing modifiers / interfaces)
  - Patches may touch >5 lines if a structural fix requires it
  - `@@ -<line>,<count> +<line>,<count> @@` MUST cite the actual 1-indexed
    line numbers shown in the prefixed source above
  - Diff context lines MUST match the raw source verbatim (without the
    `NNNN: ` line-number prefix)
  - Use Solidity error declarations (`error MyError()` + `revert MyError()`)
    rather than `require(cond, "string")` when the contract's existing
    style is custom errors — match the existing codebase's convention
"""


PATCH_AUTHORSHIP_PROMPT_C = """You are writing a minimal-scope security patch for a C program.
The bug has already been CONFIRMED via a PoC test that triggers the violation under
clang + AddressSanitizer / UndefinedBehaviorSanitizer. Your output MUST be a valid
unified diff that, when applied, makes the PoC stop firing.

# Confirmed finding

Hypothesis ID:   {hypothesis_id}
Bug class:       {bug_class}
Severity:        {severity}
Title:           {title}

# Bug-class fix template

{patch_intent}

# PoC test (this is what your patch must defuse)

```c
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

```c
{target_source}
```

{sig_index_section}

# Output requirements

Reply with EXACTLY:

  1. A brief 1-2 sentence rationale of what your patch does, prefixed with `RATIONALE:`
  2. A blank line
  3. The unified diff, starting with `--- a/{target_file_path}` and `+++ b/{target_file_path}`

NO additional prose. NO markdown fences. NO commentary after the diff.

## Patch philosophy: STRUCTURAL fix, not symptom patch

A *symptom* patch enlarges a buffer to accommodate the specific input that
triggered the PoC, or adds a band-aid check at one call site, leaving the
underlying invariant unenforced. A different caller, or the same caller
with slightly different input, re-triggers the bug. **DO NOT do this.**

A *structural* fix eliminates the bug at its root: the buggy invariant
becomes impossible to violate, no matter who calls the function. For
memory-safety bugs, that means using language idioms that the C standard
or POSIX has hardened against the attack class. For logic bugs, it means
enforcing the contract at the WRITE side (the choke point), not on every
READ site.

# C-specific fix patterns (use the one that matches the bug class)

## Off-by-one / out-of-bounds write
WRONG (symptom):
    char payload[128];          /* just make the buffer bigger */
    /* original `>=` guard unchanged — moves the same bug to len==128 */

RIGHT (structural):
    if (sizeof(out->payload) > payload_len) {{   /* strict less-than */
        memcpy(out->payload, src, payload_len);
        out->payload[payload_len] = 0;           /* now in-bounds */
    }} else {{
        return -1;
    }}

## TOCTOU / symlink-follow at `fopen` / `open`
WRONG (symptom): add an `lstat` re-check between guard and open — the
window just shrinks, doesn't close.

RIGHT (structural):
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) return -1;
    struct stat st;
    if (fstat(fd, &st) != 0) {{ close(fd); return -1; }}
    /* validate st via fstat on the fd, not lstat on the path */
    FILE *fp = fdopen(fd, "w");

## Use-after-free in callback
WRONG (symptom): set `job = NULL` after the free — the dereference still
fires via the cb argument.

RIGHT (structural): reorder so the callback is invoked BEFORE the free,
or stop calling the callback in the retry branch entirely. The retry
branch's intent is to LET THE CALLER decide whether to retry — return
a non-zero rc and let the caller call `cb(ctx)` themselves with a fresh
job allocation.

## Format-string injection via `fprintf(stream, attacker_buf)`
WRONG (symptom): grep `attacker_buf` for `%` and reject — incomplete and
brittle.

RIGHT (structural): pass the buffer as a `%s` argument, never as the
format string itself:
    fprintf(stream, "%s", line);          /* one-line fix */
    /* or: */
    fputs(line, stream); fputc('\\n', stream);

## Missing role / privilege gate
WRONG (symptom): one-shot session consumption (zero `flags` on first
check) — breaks every other caller and still doesn't add a role gate.

RIGHT (structural): extend the Session struct with an explicit role /
privilege field, set it at session_create time from the caller's
provenance, and add a parameter to session_check (or a dedicated
session_check_role helper) that gates against the requested operation.

## Save/load divergence (write side accepts unbounded; read side rejects)
WRONG (symptom): enlarge the read-side buffer to match the worst case the
write side might produce — the divergence reappears the moment a slightly
longer value arrives.

RIGHT (structural): enforce the length cap at the WRITE side
(`minimap_put` / `store_save`) so every persisted line satisfies the
read-side parser. The write side is the SINGLE choke point; the read
side trusts the file format invariant.

## Weak token entropy (deterministic hash of literal)
WRONG (symptom): XOR the hash with a hardcoded constant — still
deterministic.

RIGHT (structural): mix in kernel-backed entropy. Use `getrandom(2)`
(Linux) or `arc4random_buf` (BSD) once at process start, or per-token,
and combine the result with the user identity for the final token.

# General principles

Prefer fixes that:
  1. Make the buggy invariant impossible to violate at the helper level
  2. Are local (modify the helper itself, not every caller)
  3. Don't add new public surface (no new function signatures, no new
     headers in the public API, no new dependencies)
  4. Match the existing codebase's conventions (error return values,
     null-check ordering, allocation patterns) so the change is reviewable
  5. Use POSIX / C standard idioms when they exist (`O_NOFOLLOW`,
     `getrandom`, `mkstemp`, strict-less-than bounds checks). Avoid
     reinventing safety mechanisms the kernel or libc already provides.
"""


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

{sig_index_section}

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
    engine_repo: Path | None = None,
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

    When `engine_repo` is provided, the prompt also includes a cross-module
    signature index so the LLM can call real helpers in other files instead
    of inventing function names or arities. Without this index, structural
    fixes that touch shared utilities tend to produce diffs referencing
    non-existent functions.
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

    sig_index_section = ""
    if engine_repo is not None:
        sig_index_section = build_sig_index(engine_repo, target_file_path)

    # Pick the language-appropriate prompt template. Detected from the
    # target path's file extension:
    #   .sol           → Solidity (CEI, onlyOwner, custom errors)
    #   .c / .h / .cpp → C (strict-bounds checks, O_NOFOLLOW, %s in fprintf,
    #                       getrandom, length-cap at write side)
    #   anything else  → Rust / Solana default (capabilities, saturating_*)
    if target_file_path.endswith(".sol"):
        _prompt_template = PATCH_AUTHORSHIP_PROMPT_SOLIDITY
    elif target_file_path.endswith((".c", ".h", ".cc", ".cpp", ".hpp")):
        _prompt_template = PATCH_AUTHORSHIP_PROMPT_C
    else:
        _prompt_template = PATCH_AUTHORSHIP_PROMPT

    prompt = _prompt_template.format(
        hypothesis_id=hypothesis_id,
        bug_class=bug_class,
        severity=severity,
        title=title,
        patch_intent=template.patch_intent,
        poc_source=poc_source,
        target_file_path=target_file_path,
        target_source=target_source_numbered,
        sig_index_section=sig_index_section,
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
        # Repair LLM hunk-header line counts. The LLM frequently emits
        # `@@ -25,6 +25,6 @@` when the actual hunk body has 5 context+
        # mutation lines, not 6. `git apply --recount` recovers some
        # cases but not all; running our own re-counter post-author
        # makes every well-shaped-but-miscounted diff applicable.
        # See scripts/fix_patch_hunk_counts.py for the standalone tool
        # version (kept for one-off patch surgery).
        diff = _repair_hunk_counts(diff)

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
