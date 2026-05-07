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
- Engine pin (sha):  04b854e
- Wrapper repo:      https://github.com/aeyakovenko/percolator-prog
- Wrapper pin (sha): 04b854e5718112f42ebba9c208335a22132075ad

Local clones (read-only):
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e

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

Read-only. Do NOT modify any files in /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e or /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e.
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

- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/src/ (all .rs files, focus on the main module)
- /tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/spec.md (if a spec doc exists)
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


---

# Specific hypothesis to investigate

ID:           R5-rpc-staleness-tolerance
Claim:        Off-chain components reading state via RPC tolerate up to N slots of staleness without acting on stale information.

Target file:  (see hypothesis brief above)
Target lines: (see hypothesis brief above)
Notes:        (none)

