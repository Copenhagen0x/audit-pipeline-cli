# Agent prompts library

A library of multi-agent review prompts that produced verified findings on the Percolator audit. Each prompt is parameterized for re-use on a new target.

## How to use

These prompts are designed for an LLM with multi-agent dispatch capability (Claude with subagent dispatch, or equivalent). Each prompt is a self-contained brief for ONE agent.

For each new audit:
1. Read `00_orientation.md` first — sets context for ALL subsequent prompts
2. Pick one or more prompt categories from the list below based on what you're investigating
3. Replace the `{TARGET}` placeholders with your specific values
4. Spawn agents in parallel; one prompt = one agent

## Prompt categories

| File | Use when... |
|---|---|
| [`00_orientation.md`](./00_orientation.md) | Starting a new audit; baseline orientation for all subsequent agents |
| [`01_codebase_orientation.md`](./01_codebase_orientation.md) | Spawning agents on a fresh codebase to enumerate structure |
| [`02_implicit_invariant_hunt.md`](./02_implicit_invariant_hunt.md) | Hunting unstated assumptions in the spec or comments |
| [`03_arithmetic_overflow_class_audit.md`](./03_arithmetic_overflow_class_audit.md) | Enumerating panic-class arithmetic sites (one bug class at a time) |
| [`04_state_transition_completeness.md`](./04_state_transition_completeness.md) | Auditing atomic blocks and state-machine commits |
| [`05_authorization_chain_trace.md`](./05_authorization_chain_trace.md) | Tracing public-API callers down to sensitive engine functions |
| [`06_panic_site_enumeration.md`](./06_panic_site_enumeration.md) | Listing every `.expect()` / `.unwrap()` / `assert!` that could panic |
| [`07_call_chain_reachability.md`](./07_call_chain_reachability.md) | Determining if a Kani-CEX site is reachable from public API |
| [`08_invariant_property_definition.md`](./08_invariant_property_definition.md) | Translating English claims into Kani-checkable assertions |
| [`09_kani_harness_authoring.md`](./09_kani_harness_authoring.md) | Writing a Kani harness for a specific finding or invariant |
| [`10_litesvm_bound_analysis_design.md`](./10_litesvm_bound_analysis_design.md) | Designing a numeric bound analysis for a Kani-CEX |
| [`11_disclosure_documentation.md`](./11_disclosure_documentation.md) | Drafting the per-bug write-up for the disclosure |
| [`12_self_audit_critic.md`](./12_self_audit_critic.md) | Reviewing your own audit for errors before publication |

## Orchestration patterns

| Pattern | When to use |
|---|---|
| [`orchestrator/deploy_recon_swarm.md`](./orchestrator/deploy_recon_swarm.md) | Spawning 5-10 agents in parallel on disjoint hypotheses |
| [`orchestrator/synthesize_findings.md`](./orchestrator/synthesize_findings.md) | Merging multiple agent reports into a single findings table |

## Prompt design principles

The prompts in this library follow conventions tested during the Percolator audit:

1. **Hypothesis framed as a question** ("does X hold?") — produces clean negatives
2. **Files cited with absolute paths** — agents read what you tell them, not what they assume
3. **Output format specified** (table / structured list / verdict) — no rambling
4. **Word cap stated explicitly** (typically 600-800 words) — keeps responses scannable
5. **Examples or anti-patterns in the prompt** — prevents agent over-confidence

If you write a new prompt that doesn't follow these conventions, expect lower-quality output. If you have to deviate, document why.

## Customizing for non-Solana targets

The prompts assume a Solana program with engine + BPF wrapper architecture. For other targets:

- Replace "engine" / "wrapper" with the equivalent layer terminology
- Replace Solana-specific tool names (cargo-build-sbf, LiteSVM) with the equivalent
- Keep the structural pattern (hypothesis → enumeration → verification → reachability)

The CONTENT of the prompts is Solana-flavored; the SHAPE is general-purpose.

## License

All prompts in this directory are CC BY 4.0 (see repo `LICENSE`). Adapt freely for your audits; attribution appreciated but not required.
