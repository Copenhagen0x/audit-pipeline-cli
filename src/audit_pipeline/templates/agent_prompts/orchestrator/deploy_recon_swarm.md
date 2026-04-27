# Orchestrator — Deploy recon swarm

**Use when**: starting Layer 1 of a new audit. You want to spawn 5-10 agents in parallel on disjoint hypotheses.

This is a META-prompt: it tells YOU (the orchestrator) how to set up the swarm, not an agent prompt itself.

---

## When to use

- After running prompt 01 (codebase orientation) so you know the codebase shape
- After identifying 5-10 distinct hypotheses worth investigating
- When you have agent dispatch capability (Claude with subagents, or equivalent)

## The orchestration pattern

### Step 1 — Hypothesis list

Write down 5-10 disjoint hypotheses. Each should:
- Be framed as a question ("Does X hold?")
- Cover a non-overlapping slice of the attack surface
- Be answerable with file:line citations

Example list (from Percolator audit's insurance-balance hunt):

```
H1: Does any code path call `use_insurance_buffer` without a corresponding
    vault debit (F7-class)?
H2: Does any path call `withdraw_resolved_insurance_not_atomic` before all
    accounts are settled?
H3: Can `insurance_withdraw_deposit_remaining` desync from
    `insurance_fund.balance` such that withdrawal exceeds actual balance?
H4: Does `sweep_empty_market_surplus_to_insurance` ever produce a
    NEGATIVE surplus that decreases insurance?
H5: Is there a 3+ actor liquidation cascade that touches insurance even
    when the global accounting nets to zero?
```

### Step 2 — Per-hypothesis agent prompt

For each hypothesis, build a prompt by combining:
- Prompt 00 (orientation) at the top
- One of prompts 02-07 (matching the hypothesis class) in the middle
- The specific hypothesis at the bottom

This produces ~5-10 individualized prompts.

### Step 3 — Parallel dispatch

Send all prompts in a single message with multiple agent invocations. The agents run concurrently; you wait for all to return.

### Step 4 — Synthesis

Read each agent's report. Build a candidates table:

```
| H | hypothesis | verdict | evidence | layer-2 priority |
|---|---|---|---|---|
| 1 | use_insurance_buffer w/o vault debit | REFUTED (F7 fix in place) | line citation | none |
| 2 | withdraw_resolved before settlement | NEEDS_LAYER_2 | line citation | HIGH |
| 3 | tracker desync | REFUTED (wrapper clamps to balance at line 8109) | line citation | none |
| ... | | | | |
```

### Step 5 — Promote winners to Layer 2

For each NEEDS_LAYER_2 candidate, write an empirical PoC using the Layer-2 template. For each REFUTED candidate, log the negative result for the disclosure.

## Common orchestration mistakes

### Mistake 1: hypotheses overlap

If H1 and H2 both ask about the same code path, you're wasting agent budget. Spread the hypotheses across distinct surfaces.

### Mistake 2: hypotheses too vague

"Find bugs in the engine" is not a hypothesis. "Does any caller of `set_position_basis_q` skip the cap check at line 2263?" is a hypothesis.

### Mistake 3: trusting agent verdicts without verification

An agent may say "VERIFICATION FAILED with CEX" when the harness didn't actually compile. Always cross-check claimed verdicts against raw artifacts before promoting them into a finding.

### Mistake 4: spawning too many at once

Beyond ~10 in parallel, the orchestration cost (your attention) exceeds the benefit. Use sequential batches of 5-10 if you need more breadth.

### Mistake 5: not using prompt 00 (orientation) as a prefix

Without orientation context, agents cite imagined line numbers and confuse engine vs wrapper layers. Always prefix.

## Template for the per-agent prompt assembly

```python
# Pseudocode
for hypothesis in hypothesis_list:
    prompt = orientation_prompt(target_repos, target_constants)
    prompt += hypothesis_class_template(hypothesis.class_)  # e.g., 02_implicit_invariant_hunt
    prompt += specific_hypothesis_brief(hypothesis)
    spawn_agent(prompt)
```

## Cost estimate

Per agent: ~5-15 minutes of agent-side work, ~0-5 minutes of your attention.

For 10 agents in parallel: ~15 min real time, ~30-60 min of your attention to read and synthesize their reports.

This is a high-yield use of agent budget. The Percolator audit's most productive single hour was a swarm of 5 recon agents on disjoint hypotheses for the insurance-drain bounty.
