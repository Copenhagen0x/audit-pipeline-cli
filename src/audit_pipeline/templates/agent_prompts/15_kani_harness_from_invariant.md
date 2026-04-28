# Prompt 15 — Kani harness from natural-language invariant (Layer 3 author)

**Use as**: collapses the Layer 2 → Layer 3 transition from "expert formal-methods work" to "type a sentence, get a proof." Takes a natural-language description of an invariant + the relevant function under test, returns a complete Kani harness ready to run.

---

## When to use

- You have an invariant in your head ("the residual cash should never grow without a matching vault credit") but writing a Kani harness from scratch would take hours
- You've confirmed a finding empirically (Layer 2 PoC) and want to formally generalize it (Layer 3)
- You're authoring a SAFE proof (proving the invariant holds) — the same template works for CEX harnesses by inverting the assertion

---

## Prompt template

```
You are a Kani harness author. Your job is to convert a natural-language
invariant into a complete, compilable Kani harness file.

## Inputs

### Invariant (natural language)
{INVARIANT_NL}

### Function under test
- Engine source: {ENGINE_PATH}
- Function: {ENGINE_FUNCTION}
- Function signature (verify by reading source):
{FUNCTION_SIGNATURE}

### Known engine constants / bounds (use as kani::assume bounds)
{ENGINE_CONSTANTS}

### Existing Kani harness template (use as structural reference)
{KANI_TEMPLATE}

## Your task

1. Parse the natural-language invariant into formal terms:
   - Identify the state variables it references
   - Identify the temporal scope (is this a per-call invariant or
     end-of-state invariant?)
   - Identify any preconditions implied by the wording

2. Generate a Kani harness file with:
   - `#[kani::proof]` annotation
   - `#[kani::unwind(N)]` with N chosen for the function's loop depth
   - `#[kani::solver(cadical)]` (or another if cadical is known to be slow
     for this property)
   - Symbolic state setup using `kani::any()` + `kani::assume()` to
     bound to engine-realistic ranges (use the constants above)
   - The function call under test
   - The assertion (formal version of the invariant)
   - Comments explaining each symbolic choice

3. The harness should be FULLY COMPILABLE — no `<placeholder>` markers,
   no TODO comments. If you don't know the exact engine API for setting
   up state, choose the most plausible API based on the template + your
   reading of the source.

## Reporting format

Output ONLY the Rust source for the harness file. Wrap in a markdown
fence with the language tag `rust`. Do NOT include explanatory prose
outside the file content.

After the harness, on a new line, output:

```
## Notes
- Unwind bound chosen: <N> because <reason>
- Solver chosen: <name> because <reason>
- Most likely failure mode: <CEX shape> (if author thinks invariant is
  violated) OR <"property holds, expecting PASS"> (if author thinks
  invariant is correct)
```

Cap response at 1200 words including the harness. The harness itself is
typically 60-150 lines.
```
```
