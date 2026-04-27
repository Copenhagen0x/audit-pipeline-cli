# Prompt 09 — Kani harness authoring

**Use when**: you have a target finding or invariant property and need to author the Kani harness file.

This prompt produces the actual `.rs` file content for a Kani harness, ready to drop into `tests/`.

---

## Prompt template

```
You are writing a Kani harness file for a specific verification target.

## Target

Type: {CEX_HARNESS | SAFE_HARNESS}
  (CEX_HARNESS: assert that an unsafe path does NOT panic; Kani returning
   CEX confirms the panic class.
   SAFE_HARNESS: assert a desired property holds; Kani returning SUCCESSFUL
   formally verifies the property.)

Finding/invariant name: {SHORT_NAME}

Property to verify (from prompt 08 output OR from spec):
{RUST_ASSERTION_OR_PROPERTY_PROSE}

## Files to read

- {ENGINE_PATH}/src/percolator.rs (engine, for state struct + function signatures)
- {ENGINE_PATH}/Cargo.toml (for the `[features.test]` declaration and
  `[features.kani]` if present)
- Existing harnesses in {ENGINE_PATH}/tests/proofs_*.rs (for conventions)
- {TEMPLATE_FILE} (templates/kani_cex_panic_class.rs.template OR
  templates/kani_safe_invariant.rs.template)

## Method

1. Read the template that matches your target type
2. Read the engine source to understand:
   - The state struct fields you need to set up
   - The function signature you'll call
   - Existing test_visible! exposures
   - Engine constants (max caps) for use in kani::assume bounds
3. Read 1-2 existing harnesses to match the codebase's conventions
4. Author the harness file, replacing template placeholders with specific
   values:
   - <FINDING_NAME> → {SHORT_NAME}
   - <engine_function_name> → actual function
   - Witness state setup → the specific fields the bug needs
   - kani::any() bounds → the engine caps for those fields

5. Verify your harness:
   - Has a single #[kani::proof] function
   - Has #[kani::unwind(N)] with N >= MAX_ACCOUNTS_KANI + 2
   - Bounds every kani::any() with kani::assume()
   - Uses kani::assume(result.is_ok()) to filter setup failures
   - Has a docstring explaining what the property is and what each
     assertion means

## Output format

Return the complete harness file content, ready to save as
tests/proofs_<short_name>.rs.

Plus:
- 1-paragraph explanation of the symbolic state space the harness covers
- Estimated solver time (LOW < 30s, MED 30s-5min, HIGH > 5min)
- Any kani::assume() simplifications you made and why
- The exact `cargo kani` command to run it

Cap at 1000 words. Read-only on source files; ONLY new file is the harness.
```

---

## Why a separate prompt for this

Authoring a Kani harness is its own skill. The agent needs to:
- Match the codebase's existing conventions
- Bound kani::any() appropriately (too narrow = misses CEX; too broad = solver doesn't converge)
- Set up engine state correctly (subtle: forgetting one field can cause spurious results)
- Write a clear assertion that encodes the property exactly

Splitting this into a dedicated prompt keeps the agent focused and produces higher-quality harnesses than asking a generalist agent to "write a Kani test."

## Tips for the orchestrator (you)

- Save the engine constants list separately and inject it into every prompt — agents work better with explicit caps than inferring from source
- Provide 1-2 worked example harnesses in the prompt (or in the orientation prompt 00) so the agent has a concrete style guide
- For complex properties, decompose into 2-3 simpler harnesses (each verifying one aspect) rather than one mega-harness

## Common Kani harness anti-patterns to flag

- `kani::any::<i128>()` with no `kani::assume(...)` bounds → solver explodes
- Setup that uses `unwrap()` instead of `kani::assume(result.is_ok())` → causes spurious panic CEXes
- Asserting properties that depend on the engine's INPUT being well-formed without `kani::assume`-ing them → CEXes against malformed inputs
- Using `#[kani::unwind(2)]` when the engine init loop runs ≥3 iterations → noise FAIL

The prompt produces harnesses that avoid these patterns.
