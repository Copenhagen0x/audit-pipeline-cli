# Prompt 02 — Implicit invariant hunt

**Use when**: looking for unstated assumptions in the spec or code comments that the implementation may not actually enforce.

This is the highest-yield prompt category. Most production-relevant findings come from spec-vs-code gaps where the spec assumes invariant X but the code does not assert / enforce X.

---

## Prompt template

```
You are hunting for IMPLICIT INVARIANTS in the target codebase. An implicit
invariant is a statement that the spec or code comments assume holds, but
that the code does NOT explicitly assert or enforce.

Examples of implicit invariants:
- A docstring that says "this function MUST be called only when X holds",
  but the function does not check X
- A spec section that says "after operation Y, property Z holds", but no
  assertion verifies Z post-operation
- A comment that says "this counter only increases", but no check prevents
  decrement
- A constant named MAX_FOO suggesting an upper bound, but no enforcement at
  the surface where FOO is set

## Files to read

- {ENGINE_PATH}/src/ (all .rs files, focus on the main module)
- {SPEC_PATH} (if a spec doc exists)
- All doc-comments in engine source (lines starting with `///` or `//!`)

## Method

1. Grep for natural-language imperative statements:
   - "MUST", "must"
   - "always"
   - "never"
   - "guaranteed"
   - "invariant", "assumes"
   - "callers should"
   - "spec §"

2. For each statement found, identify:
   - Does an explicit `assert!`, `debug_assert!`, or early `return Err(...)`
     enforce the claim?
   - If not, is the claim verified at the call site by every caller?
   - If neither, this is a candidate implicit invariant.

3. Categorize each candidate by impact:
   - HIGH: violation would corrupt state in an externally observable way
   - MED: violation would cause unexpected behavior
   - LOW: violation would be benign (e.g., dead code path)

## Output format

For each candidate implicit invariant:

```
- ID: invariant_<short_name>
  Source: file:line of the prose claim
  Claim: "<exact prose, quoted>"
  Enforced by: <line:line range of enforcement, or "NONE">
  Impact if violated: <HIGH | MED | LOW>
  Suggested test: <Layer-2 PoC OR Layer-3 Kani SAFE-proof harness>
  Confidence: <HIGH | MED | LOW>
```

Aim for 5-15 candidates. Cap report at 800 words.
Read-only.
```

---

## Why this prompt is high-value

In the Percolator audit, implicit-invariant hunting produced:
- Bug #1 (cursor-wrap consumption reset) — the spec said "wrap = real volatility window expired" but the code doesn't enforce that the wrap requires real volatility absorption
- 5 SAFE proofs from Layer L3 — each formalized an implicit invariant that the engine relied on but didn't explicitly assert

The prompt asks the agent to do exactly what a senior reviewer would do mentally: read the prose, check whether it's enforced, flag gaps.

## Customization tips

- For codebases with sparse documentation: this prompt yields fewer candidates. In that case, run prompt 03 (arithmetic overflow class audit) first.
- For codebases with thorough documentation: this prompt is your primary lever. Spawn multiple agents with overlapping scope to cross-check.
- For codebases with a separate spec document: include the spec path explicitly and have the agent cross-reference spec sections to code sections.
