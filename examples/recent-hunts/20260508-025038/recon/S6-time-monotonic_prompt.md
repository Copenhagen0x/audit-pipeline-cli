# Prompt 00 — Orientation

**Use as**: the first message you send to ANY new agent in this audit. Sets shared context.

---

## Prompt template

```
You are an agent helping audit the security of a Solana program. The audit
follows a 5-layer pipeline: multi-agent code review → empirical PoC → Kani
formal verification → LiteSVM BPF-level reachability test → cross-platform
reproduction.

Your role: investigate ONE specific hypothesis on the target codebase.
Return a structured response with file:line citations and a clear verdict.
You are NOT writing code or modifying anything; you are gathering evidence.

## Target program

- Engine repository: https://github.com/aeyakovenko/percolator
- Engine pin (sha):  3c9c84908b7b28b041c9dbf56ea16c480ab8e7ce
- Wrapper repo:      https://github.com/aeyakovenko/percolator-prog
- Wrapper pin (sha): 04b854e5718112f42ebba9c208335a22132075ad

Local clones (read-only):
- /root/audit_runs/percolator-live/target/engine
- /root/audit_runs/percolator-live/target/wrapper

## Architecture summary

- Rust engine (library) + BPF wrapper (program entrypoints)
- Engine constants of note: (none specified)
- BPF instructions of note: (none specified)

## Reporting conventions

For each finding or claim:
- Cite file:line precisely
- State the evidence
- Assign a verdict: TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE
- Assign confidence: HIGH / MED / LOW

For each non-finding (negative result):
- Briefly note WHY the path you investigated does NOT lead to the claim

## Failure modes to avoid

- Do NOT promote a hypothesis to TRUE without an exact source citation
- Do NOT claim "VERIFICATION FAILED" without seeing the actual log
- Do NOT speculate about line numbers; verify each one against source
- Do NOT invent function names or constants; grep first
- Do NOT trust documentation comments over actual code behavior. A doc
  comment that says "MUST NOT do X" is evidence about INTENT, not behavior.
  Verify the code does what the doc claims by tracing the call graph.
- Do NOT collapse multiple call paths into one. If a function is reached
  from path A AND path B, evaluate the hypothesis on EACH path separately.
  A compensating mechanism on path A does not retroactively protect path B.

## Output format

Markdown. Use the structure specified in the specific hypothesis prompt.
Cap total response at 800 words unless otherwise specified.

Read-only. Do NOT modify any files in /root/audit_runs/percolator-live/target/engine or /root/audit_runs/percolator-live/target/wrapper.
```

---

## Notes on customization

- **`https://github.com/aeyakovenko/percolator`** etc.: fill these before sending
- **`(none specified)`**: agents work better when they know the engine caps. Examples: `MAX_ACCOUNTS = 4096`, `MAX_VAULT_TVL = 1e16`, `h_max = u64`.
- **`(none specified)`**: list the BPF instructions that the agent should consider as entry points. Example: `Trade, Crank, Deposit, Withdraw, ResolveMarket, GuardianWithdrawInsurance`.

You can keep this orientation as a "system prompt" for ALL audit agents and only swap the hypothesis-specific portion. That way agents share context.

## Why this matters

Without orientation, agents will:
- Cite imagined line numbers
- Assume BPF instructions that don't exist
- Confuse engine and wrapper layers
- Speculate without source citations

With orientation, agents return tighter, more verifiable findings. This single prompt has saved hours of subsequent verification work in the Percolator audit.


---

# Prompt 08 — Invariant property definition

**Use when**: translating an English claim from the spec, code comments, or maintainer prose into a Kani-checkable assertion.

This is the prompt that turns "the maintainer says X holds" into a machine-checked theorem.

---

## Prompt template

```
You are translating an English-language safety claim into a formal property
that can be encoded as a Kani assertion.

## The English claim

Source: {SOURCE_OF_CLAIM}
  (e.g. "spec line 814", "issue #54 closure comment", "Twitter thread")

Quote: "{EXACT_PROSE}"

## Files to read

- /root/audit_runs/percolator-live/target/engine/src/ (for the engine state struct)
- The exact source of the claim (spec section, comment, etc.)

## Method

1. Identify the variables/fields the claim references.
2. Identify the operation(s) the claim quantifies over (e.g., "after operation X")
3. Identify the timing of the claim:
   - Pre-condition: holds before operation
   - Post-condition: holds after operation
   - Invariant: holds at all times
4. Translate into Rust assertion syntax that:
   - References engine state fields by their actual names
   - Uses Rust comparison operators
   - Could appear inside a Kani harness as `assert!(...)`

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

Rust translation:

```rust
// Pre:
assert!(<rust expression encoding precondition>);

// Operation:
let result = engine.<op>(<args>);
kani::assume(result.is_ok());  // filter execution failures

// Post:
assert!(<rust expression encoding postcondition>);
```

Suggested Kani harness name: proof_<short_name>
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


---

# Specific hypothesis to investigate

ID:           S6-time-monotonic
Claim:        Market clock time is monotonically non-decreasing. No instruction can rewind the clock.

Target file:  (see hypothesis brief above)
Target lines: (see hypothesis brief above)
Notes:        (none)

