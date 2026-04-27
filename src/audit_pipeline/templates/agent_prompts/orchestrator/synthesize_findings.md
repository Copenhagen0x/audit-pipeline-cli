# Orchestrator — Synthesize findings

**Use when**: you have multiple agent reports (from a recon swarm or self-audit) and need to merge them into a single findings table.

This is a META-prompt. You either do this synthesis yourself OR spawn an additional agent dedicated to merging.

---

## When to use

- After a recon swarm (5-10 agents) returns
- After a self-audit pass (multiple agents reviewing different files)
- Whenever you need to consolidate distributed findings into one document

## Pattern: the candidates table

Build a single markdown table consolidating all findings:

```
| ID  | Hypothesis | Verdict | Evidence | Layer-2 Priority | Owner |
|-----|------------|---------|----------|------------------|-------|
| H1  | <claim>    | TRUE    | engine:6155, test passes | HIGH | you |
| H2  | <claim>    | REFUTED | engine:7065 has guard | none | (negative result) |
| H3  | <claim>    | NEEDS_LAYER_2 | suspicious pattern at engine:3915 | MED | you |
| ... | ... | ... | ... | ... | ... |
```

Columns:
- **ID**: H1, H2, ... matching the hypothesis list
- **Hypothesis**: the question framed
- **Verdict**: TRUE | REFUTED | NEEDS_LAYER_2 | INCONCLUSIVE
- **Evidence**: file:line citations from the agent's report
- **Layer-2 priority**: HIGH | MED | LOW | none (refuted)
- **Owner**: who's writing the Layer-2 PoC

## Synthesis-as-an-agent prompt

If you want to spawn an agent to do the synthesis, use this:

```
You are merging multiple independent agent reports into a single findings table.

## Reports to merge

{REPORT_1}

---

{REPORT_2}

---

(... up to N reports ...)

## Method

1. Read all reports.
2. Identify findings that appear in multiple reports (potential overlap).
3. Identify findings unique to one report.
4. Identify CONTRADICTIONS between reports — when agent A says X is TRUE
   and agent B says X is REFUTED. Flag explicitly; do NOT pick a side.

## Output format

```
# Consolidated findings

## Findings table

| ID | Hypothesis | Verdict | Evidence | Priority | Notes |
|----|------------|---------|----------|----------|-------|
| ... |

## Contradictions to resolve

(List any case where two agents disagree. State which agent said what.
 Recommend which one to trust based on evidence quality.)

## Recommended next actions

For each TRUE / NEEDS_LAYER_2 finding:
- What Layer-2 PoC to write
- What Kani harness to author
- What LiteSVM test to design
```

Cap at 1000 words. Read-only.
```

## Common synthesis pitfalls

### Pitfall 1: Aggregating wrong line numbers

If agent A says "engine line 5715" and agent B says "engine line 3944", they may both be correct (one cites the call site, the other cites the function definition). Disambiguate before consolidating.

### Pitfall 2: Treating "NEEDS_LAYER_2" as a finding

NEEDS_LAYER_2 means "I couldn't decide." It's NOT a finding. Don't put it in the disclosure as if it were. Either promote it to TRUE via Layer-2 PoC, or refute it.

### Pitfall 3: Burying negative results

Refuted hypotheses are still valuable for the disclosure. They show the audit was thorough. Include them in a "negative results" appendix.

## Worked example: insurance-drain bounty recon

Recon swarm of 3 agents on the Percolator insurance-drain bounty produced:

```
Agent A (3-actor liquidation):
  Verdict: PROMISING
  Evidence: enqueue_adl Step 2 calls use_insurance_buffer

Agent B (post-resolve race):
  Verdict: SPECULATIVE
  Evidence: theoretical race window, requires multi-tx ordering

Agent C (deposits_only tracker desync):
  Verdict: CONFIRMED EXPLOITABLE
  Evidence: claimed wrapper doesn't clamp to balance
```

Verification by hand-reading source:
- Agent A's "PROMISING" → REFUTED on detailed read (K/F adjustment absorbs matched losses, insurance only fires on legitimate uninsured deficits — spec-compliant)
- Agent B's "SPECULATIVE" → still SPECULATIVE (would need more time)
- Agent C's "CONFIRMED EXPLOITABLE" → REFUTED (wrapper does clamp at line 8109; agent missed it)

Consolidated finding: 0 of 3 candidates were real. Honest result; useful for the disclosure as "we hunted, here's what we ruled out."

This kind of honest synthesis is more valuable than an inflated finding count. Maintainers respect honest negative results; they distrust inflated positive ones.
