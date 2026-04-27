# Prompt 12 — Self-audit critic

**Use when**: the disclosure draft is complete and you're about to publish. Spawn this agent (or several in parallel covering different aspects) to find errors before the maintainer does.

This prompt has the highest single-prompt yield in the pipeline. The Percolator audit caught 13 errors in self-audit before publication, including 5 wrong line numbers, 1 critical leak (TOLY_CALL_PREP referenced in a public doc), and 1 math error (wall-clock conversion off by 4x).

---

## Prompt template

```
You are doing a ruthless final-pass audit of a disclosure document before
publication. Your job: find EVERY error. Be paranoid.

## Files to audit

{LIST_OF_FILES}
  (e.g., DISCLOSURE.md, EXEC_BRIEF.md, RECOMMENDED_PATCH.md, README.md,
   tests/* docstrings)

Cross-reference materials (read these to verify claims):
- {ENGINE_PATH}/src/ (engine source, for verifying line numbers)
- {WRAPPER_PATH}/src/ (wrapper source, for verifying line numbers)
- {OTHER_DOCS} (other published docs, for cross-doc consistency)

## What to find (be ruthless)

### Factual errors
- Wrong engine/wrapper line numbers (verify EVERY citation)
- Wrong function names
- Wrong constant values
- Wrong magnitudes (powers of 2, scientific notation)
- Wrong dates / SHAs / version strings

### Math errors
- Recompute every numeric claim from first principles
- Check unit conversions (slots vs seconds vs years)
- Check rounding (precision loss in derived numbers)
- Check that per-file rollups equal the sum of their parts

### Internal inconsistencies
- Same claim said differently in two places
- Contradiction between paragraph N and table M
- Bug numbering or severity labels that don't match across docs

### Cross-doc inconsistencies
- README claims X but DISCLOSURE says Y
- File counts in tables that don't match actual directory contents

### Tone issues
- Hedge words ("honest", "honestly", "rigorous", "comprehensive")
- Self-praise ("we did an extensive analysis")
- Adversarial framing ("you missed", "you should have")
- Defensive phrasing ("we hope", "we believe")
- Marketing language ("scalable", "robust", "reusable")

### Leaks
- Internal call-prep document referenced in public artifact
- Internal strategy / pricing / business framing
- Reference to prior unrelated audits or relationships
- @mentions or DMs that shouldn't be in a public doc

### Markdown issues
- Broken table column counts
- Unclosed code blocks
- Broken internal links (file paths that don't resolve)
- Bare URLs not in `<>` or markdown link syntax
- Header level skips (# then ###)

### Reproducibility issues
- Reproduction commands that won't work as written
- Missing feature flags
- Wrong file paths in commands

### Severity calibration
- Code defects labeled as exploits
- Active bugs labeled as defense-in-depth
- Inflated value-at-risk claims
- Speculative claims without evidence

## Output format

For each issue found:

```
- File: {path}
  Line: {N}
  Quote: "{exact quote}"
  Issue: {what's wrong}
  Severity: {CRITICAL | HIGH | MED | LOW | TONE}
  Fix: {suggested correction}
```

Then summary:
- Total issues: N
- CRITICAL (must fix before publication): M
- HIGH (should fix): K
- MED (nice to fix): J
- LOW + TONE (judgment calls): I

If no issues found in a category: say "CLEAN — N items checked, 0 issues".

Cap at 1000 words. Read-only.
```

---

## Why this prompt is high-yield

This is the prompt that catches the errors you'd otherwise discover ON THE CALL when the maintainer points them out. Specifically:

| Error class | Example caught (Percolator audit) |
|---|---|
| Wrong line number | Disclosure cited "is_above_initial_margin_trade_open at engine line 5715" — actually defined at 3944 (5715 was the call site, not definition) |
| Internal leak | EXEC_BRIEF.md referenced `output/TOLY_CALL_PREP.md` in a public artifact — would have leaked the existence of the call-prep doc |
| Math error | Bug #2 LiteSVM test claimed "~13.4 million years" but actual derivation at 500ms/slot = ~167 million years |
| Cross-doc inconsistency | DISCLOSURE.md said "29 sites total" but actual grep count was 30 |
| Tone issue | Multiple "honest" / "honestly" hedges weakened the prose |

Each of these would have been caught by the maintainer (Toly) on the call, eroding credibility. Catching them first preserves it.

## Orchestration pattern

For the Percolator audit final sweep, we ran this prompt with 30 different focuses:
- 8 doc-focused agents (one per doc)
- 12 test-file agents (one per test file)
- 10 cross-cutting agents (line-number verification, math re-derivation, tone audit, leak audit, markdown rendering, repro verification, etc.)

Total token cost was significant but the yield justified it: 13 substantive corrections caught.

For smaller audits, a single agent covering the high-priority files (README, DISCLOSURE) with this prompt is enough to catch most issues.

## Customization

- Add target-specific anti-patterns: e.g., for Solana, "don't use 'lamports' when you mean 'micro-tokens'"
- For codebases with frequent terminology drift: enumerate the canonical names and flag deviations
- For multi-file disclosures: spawn one self-audit agent per file, then a final cross-cutting consistency agent
