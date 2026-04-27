# Prompt 11 — Disclosure documentation

**Use when**: drafting the per-bug write-up for inclusion in the disclosure document.

This prompt produces a polished disclosure section ready to drop into DISCLOSURE.md, following the structure in `docs/disclosure-template.md`.

---

## Prompt template

```
You are writing the disclosure write-up for a single finding.

## Inputs

Finding ID: Bug #{N}
Short title: {ONE_LINE_TITLE}
Class: {ACTIVE | CODE-DEFECT | NOT-A-BUG (defense-in-depth)}

Engine line: {LINE}
Engine file: {FILE}

Layer 2 PoC: {TEST_PATH}
Layer 3 Kani harness: {HARNESS_PATH}, verdict: {SUCCESSFUL | CEX in N s}
Layer 4 LiteSVM:
  - Reachability skeleton: {PATH or "n/a"}
  - Bound analysis: {PATH or "n/a"}, conclusion: {ACTIVE | N years to reach}

Engine call chain (verified):
{CHAIN}

Suggested fix (if known):
{FIX_DESCRIPTION}

## Files to read

- {ENGINE_PATH}/src/percolator.rs (for the actual code snippet around the bug)
- {DOC_TEMPLATE} (docs/disclosure-template.md for formatting conventions)

## Method

1. Read the per-bug template structure in docs/disclosure-template.md
2. Extract the exact code snippet from engine source — DO NOT paraphrase
3. Annotate the snippet with `// <-- BUG site` or similar comment showing
   the exact line of concern
4. Write each section in order:
   - Title (one line)
   - Concrete state transition (with code snippet)
   - Why it commits partial progress incorrectly (one paragraph)
   - Public-API call chain (verified empirically)
   - Engine math IS unsafe — Kani CEX confirms (citations)
   - Reachability bound analysis (citations + numbers)
   - Reachability finding (verdict)
   - Suggested fix

5. Apply tone guardrails:
   - NO "you" / "your code" / "your engine" — use "the code" / "the engine"
   - NO "honest" / "honestly" / "rigorous" / "comprehensive" hedge words
   - NO speculation — every claim has a citation or PoC backing it
   - NO inflation — if it's a code defect, say so; don't dress it as exploit

## Output format

The complete markdown for the per-bug section, ready to paste into
DISCLOSURE.md between the previous bug and the next.

Cap at 600 words for the bug section itself.
Read-only on source.
```

---

## Why a dedicated disclosure prompt

Disclosure prose is a craft separate from technical investigation. The same agent that's great at finding bugs is often verbose or self-praising in writing. A dedicated prompt with explicit tone guardrails produces tighter, more professional disclosure prose.

## Tone discipline

The prompt enforces three vocabulary constraints learned from the Percolator audit:

1. **No "your code"** — subtly accusatory; reads as "you screwed up." Use "the code."
2. **No hedge words** ("honest", "honestly", "rigorous") — sounds like you're insisting on something the evidence should show by itself.
3. **No inflation** — if Layer 4 said it's a code defect, say so. Don't fudge to make it sound more severe.

These are non-negotiable for production-grade disclosure.

## Worked example output

Input:
- Finding ID: Bug #1
- Title: "Cursor-wrap atomically resets consumption budget without absorbing volatility"
- Class: ACTIVE
- Engine line: 6155
- ...

Output (excerpt):

> ## Bug #1 — Cursor-wrap atomically resets consumption budget without absorbing volatility
>
> **Concrete state transition** (engine `src/percolator.rs`, line 6155, inside atomic block lines 6149–6158 of `keeper_crank_not_atomic`):
>
> ```rust
> if sweep_end >= wrap_bound {
>     self.rr_cursor_position = 0;                              // OK
>     self.sweep_generation = self.sweep_generation.checked_add(1)?;  // OK
>     self.price_move_consumed_bps_this_generation = 0;         // <-- BUG
> } else {
>     self.rr_cursor_position = sweep_end;
> }
> ```
>
> **Why it commits partial progress incorrectly**:
>
> The reset implies "this is a fresh generation — start a new consumption budget." The trigger (`sweep_end >= wrap_bound`) is purely call-count arithmetic. ...

This output is publication-ready.

## Customization

For audits delivered as PRs (rather than disclosure documents), use a variant of this prompt that produces PR description content + commit message instead of standalone disclosure prose.
