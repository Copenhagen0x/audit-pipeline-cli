# Prompt 04 — State transition completeness

**Use when**: auditing atomic blocks, state-machine commits, and operations that mutate multiple state fields together.

The maintainer's specific question often comes back to this: "is there a state transition that commits partial progress incorrectly?" This prompt formalizes the hunt for exactly that.

---

## Prompt template

```
You are auditing the engine's state transitions for completeness. A state
transition COMMITS PARTIAL PROGRESS INCORRECTLY if:

- It writes some but not all of a logically related set of fields
- It writes a counter to "0" or "max" without absorbing the work that
  counter was supposed to track
- It crosses an atomic-block boundary in a way that leaves the engine in
  a state that no individual function intended

## Files to read

- {ENGINE_PATH}/src/ (focus on functions that mutate multiple fields)

## Method

1. Identify atomic blocks: code regions where multiple state fields are
   updated as a unit. Look for:
   - Functions named with `_atomic` suffix
   - Code blocks separated by `// === atomic ===` comments or similar
   - Branch arms that write multiple fields before returning

2. For each atomic block, enumerate:
   - WHAT fields are written
   - WHAT condition triggers the block
   - WHAT precondition the spec/comments assume holds at entry
   - WHAT postcondition the spec/comments assert holds at exit

3. For each block, ask:
   - Can the trigger condition fire WITHOUT the precondition holding?
     (e.g., trigger is "call count" but precondition is "real work was done")
   - Does the block write a "reset to zero" without the work being done?
   - Are there caller paths where the precondition is implicit (not checked)?

## Output format

For each suspicious atomic block:

```
- ID: state_transition_<short_name>
  Block: file:line-line
  Function: <function name>
  Trigger: <what causes this block to execute>
  Precondition (per spec/comments): <what should be true at entry>
  Precondition enforced by code: <line:line, or "NONE">
  Fields written: <list>
  Risk: <what the partial commit could cause>
  Confidence the precondition is bypassable: <HIGH | MED | LOW>
  Suggested PoC: <Layer-2 test pattern>
```

Aim for 3-7 candidates. Cap at 800 words.
Read-only.
```

---

## Why this is critical

In the Percolator audit, this prompt produced Bug #1 (cursor-wrap consumption reset). The pattern was:

- Atomic block at engine:6149-6158: `if sweep_end >= wrap_bound { rr_cursor=0; sweep_generation+=1; consumption=0; }`
- Trigger: cursor-wrap arithmetic (call count)
- Spec precondition: "wrap = real volatility window expired"
- Code does NOT enforce that wrap implies real volatility absorption
- Permissionless cranks at fixed (slot, price) can advance the cursor without absorbing volatility
- Atomic block fires → consumption resets without the work being done

This finding was the maintainer's most-requested type ("a concrete state transition that commits partial progress incorrectly"). The prompt formalizes the hunt.

## What the agent should NOT do

- Should NOT just list every atomic block; only flag the ones where there's a real precondition gap
- Should NOT speculate without grep-verifying the trigger condition and precondition enforcement

## Customization tips

- For codebases with very large atomic blocks (e.g., crank handlers): split this prompt into multiple agents, one per block
- For codebases with no atomic-block convention: search for functions that write 3+ state fields and treat each as a candidate atomic block
