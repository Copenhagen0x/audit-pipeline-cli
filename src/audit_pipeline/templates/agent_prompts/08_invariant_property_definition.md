# Prompt 08 — Invariant property definition

**Use when**: translating an English claim from the spec, code comments, or maintainer prose into a {FORMAL_TOOL}-checkable assertion.

This is the prompt that turns "the maintainer says X holds" into a machine-checked theorem.

---

## Prompt template

```
You are translating an English-language safety claim into a formal property
that can be encoded as a {FORMAL_TOOL} assertion.

## The English claim

Source: {SOURCE_OF_CLAIM}
  (e.g. "spec line 814", "issue #54 closure comment", "Twitter thread")

Quote: "{EXACT_PROSE}"

## Files to read

- {ENGINE_PATH}/{SRC_DIR_PATH} (for the engine state struct; all {SOURCE_EXTS} files)
- The exact source of the claim (spec section, comment, etc.)

## Method

1. Identify the variables/fields the claim references.
2. Identify the operation(s) the claim quantifies over (e.g., "after operation X")
3. Identify the timing of the claim:
   - Pre-condition: holds before operation
   - Post-condition: holds after operation
   - Invariant: holds at all times
4. Translate into a {LANGUAGE_DISPLAY} assertion that:
   - References engine state fields by their actual names
   - Uses this language's comparison / logical operators
   - Could appear inside a {FORMAL_TOOL} harness as an explicit assertion
     (using {ASSERTION_IDIOM})

## Output format

```
Original claim:    "{EXACT_PROSE}"
Source:            {SOURCE}

Variables referenced:
  - <field_name> (engine field at line N, type T)
  - ...

Quantification:
  - For all reachable engine states where {PRECONDITION}
  - After applying operation {OP}
  - The following holds: {POSTCONDITION}

{LANGUAGE_DISPLAY} translation: a short snippet (PSEUDO-CODE acceptable)
encoding the pre-condition, the operation under test, and the
post-condition. Use this language's assertion idiom ({ASSERTION_IDIOM})
and its native types / operators.

Suggested {FORMAL_TOOL} harness name: proof_<short_name>
Estimated harness complexity: LOW | MED | HIGH (in symbolic state size)
```

Cap at 400 words. Read-only.
```

---

## Why this is high-leverage

In the Percolator audit, this prompt produced 2 of the 10 SAFE proofs that
formally encoded the maintainer's own G3 closure statement at the wrapper
level. Quoting the maintainer's prose verbatim into the harness docstring
shows you read his words carefully and turned them into machine-checked
theorems — that's a strong signal of methodological rigor.

## Worked example

**Maintainer's prose (G3 closure)**:
> "CU exhaustion does not silently commit a partial Phase 2 sweep; the
> transaction aborts and rolls back. The engine loop advances the RR cursor
> only after the bounded sweep completes."

**Translation**:
- Variables: `rr_cursor_position` (engine field), `cursor_advanced` (boolean derived from pre/post)
- Operation: `keeper_crank_not_atomic` with CU exhaustion mid-sweep
- Quantification: For all reachable engine states + all CU exhaustion points,
  if the sweep does not complete, `rr_cursor_position` MUST equal its pre-call value

**Rust harness skeleton**:

```rust
let pre_cursor = engine.rr_cursor_position;
let pre_state_snapshot = clone_relevant_engine_state(&engine);

// Symbolic CU exhaustion: simulate by interrupting mid-loop
let result = engine.keeper_crank_not_atomic_with_cu_limit(symbolic_cu_limit);

if result.is_err() {
    // CU exhaustion (or other rollback) → cursor should NOT have advanced
    assert_eq!(engine.rr_cursor_position, pre_cursor);
}
```

Now Kani either PROVES this property (G3 closure is formally verified) or returns a CEX (the closure statement was wrong, and there's a bug).

In the Percolator audit, Kani proved it. The maintainer's prose became a machine-checked theorem.

## Customization

For claims that quantify over MULTIPLE operations (e.g., "across any sequence of N calls, X holds"), the harness becomes a small loop. Bound N aggressively (N=2 or 3) to keep the harness tractable.

For claims with implicit quantifiers ("normally" or "typically"), explicitly enumerate the conditions under which the claim is supposed to hold. Then encode those as `kani::assume()` constraints on the symbolic state.
