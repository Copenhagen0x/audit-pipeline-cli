# Prompt 14 — Spec ↔ code gap analysis (Layer 0)

**Use as**: a pre-Layer-1 hypothesis generator. Point this at a target's spec doc + main source file; the agent surfaces every place the spec says X but the code does Y. Each gap becomes a candidate hypothesis for Layer 1.

This is the methodology behind F7 — the Percolator spec said insurance absorption preserves the haircut residual, but the code didn't. Automating this gap detection turns spec drift into a first-class finding source.

---

## When to use

- At the start of an audit, before writing hypotheses
- When the target has a written spec (markdown, PDF text, README, comments)
- When you want to seed the hypothesis library with concrete spec-vs-code disagreements

## How it complements existing prompts

- Prompts 02-08 hunt specific bug classes (overflow, invariant, authorization)
- This prompt hunts **spec-vs-code drift**, which is a meta-class that often produces the most valuable findings because the bug is "the protocol's own design says one thing and the code does another"

---

## Prompt template

```
You are a SPEC-DRIFT auditor. Your job is to find every place the target
program's WRITTEN SPEC disagrees with its IMPLEMENTATION.

A spec-vs-code gap is a finding even when neither side is "buggy" in
isolation. It indicates either: (a) the spec is out of date and someone
is making decisions based on it, or (b) the code drifted from the spec
and a real invariant got broken in the drift.

## Inputs

### Spec
Path: {SPEC_PATH}

```markdown
{SPEC_CONTENT}
```

### Code
Path: {CODE_PATH}

```rust
{CODE_CONTENT}
```

## Your task

1. Extract every CONCRETE CLAIM from the spec — invariants, conservation
   rules, state transition rules, ordering constraints, value ranges.
   Ignore prose / motivation; focus on claims that COULD be checked
   against code.

2. For each claim, find the corresponding code location(s) — the
   function(s) that implement the behavior, the assertion(s) that enforce
   the invariant, the state mutation(s) that respect or violate the rule.

3. For each (claim, code) pair, classify:
   - **MATCH**: code implements the claim faithfully
   - **DRIFT**: code partially implements the claim but with a gap
   - **MISSING**: claim has no corresponding code at all
   - **CONTRADICTION**: code does the OPPOSITE of what the claim says

4. For DRIFT and CONTRADICTION cases especially, this is a candidate
   finding for Layer 1 hypothesis generation.

## Reporting format

```
## Summary
- Claims extracted from spec: N
- MATCH: a
- DRIFT: b   ← candidate findings
- MISSING: c ← candidate findings
- CONTRADICTION: d ← strongest candidate findings

## Gaps worth promoting to Layer 1 hypothesis

| Spec claim | Code location | Class | Severity guess | One-line summary |
|---|---|---|---|---|
| {SPEC_PATH}:L42 — "X is preserved" | {CODE_PATH}:L1234 — fn foo() | DRIFT | HIGH | Function decrements X but doesn't credit the corresponding counter |
| ... | ... | ... | ... | ... |

## Detailed gap analysis (top 5 only)

### Gap 1: <one-line title>

**Spec claim**: {SPEC_PATH}:L<N> says "<exact quote, max 15 words>"
**Code reality**: {CODE_PATH}:L<N> does "<paraphrase>"
**Class**: DRIFT | MISSING | CONTRADICTION
**Why this matters**: 1-2 sentences on the security implication
**Suggested Layer 1 hypothesis**: <one sentence in "is X invariant true?" form>

[Repeat for gaps 2-5]

## Spec claims that MATCH (confidence boost — what the code gets right)

[Bulleted list of 5-10 claims that the code implements faithfully. Useful
 as a reverse signal: these areas are LOW priority for further review.]
```

Cap response at 1500 words. Cite spec line numbers AND code line numbers
precisely. If the spec is sparse or non-claim-bearing, say so plainly
(don't invent claims to find gaps for).
```
```
