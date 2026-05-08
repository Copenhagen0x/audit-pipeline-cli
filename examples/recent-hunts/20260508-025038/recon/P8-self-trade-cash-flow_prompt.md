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

- /root/audit_runs/percolator-live/target/engine/src/ (focus on functions that mutate multiple fields)

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


---

# Specific hypothesis to investigate

ID:           P8-self-trade-cash-flow
Claim:        A self-trade (same authority on both sides of a fill) is cash-flow neutral up to fees + IM transitions. No fund extraction via self-trades.

Target file:  (see hypothesis brief above)
Target lines: (see hypothesis brief above)
Notes:        (none)

