# Prompt 13 — Adversarial debate (Layer 1.5)

**Use as**: the second agent in a debate pair. Run AFTER a first agent has returned a verdict on a hypothesis. The challenger's job is to disagree.

This prompt is the methodological fix for the F7-style failure mode where a single agent returns FALSE/HIGH and is wrong because it trusted a doc comment or collapsed multiple call paths.

---

## When to use

- A first agent returned a verdict you have any reason to doubt
- A first agent returned FALSE/HIGH on a hypothesis you have independent evidence for
- You want to stress-test a TRUE/HIGH verdict before promoting to Layer 2
- You're auditing high-stakes code where a false negative is unacceptable

## How the debate loop works

```
Round 1: Proposer agent investigates the hypothesis (use prompts 02-08)
Round 2: Challenger agent (this prompt) reads proposer's verdict + evidence
         and tries to find ANY hole in the reasoning
Round 3: (Optional) Proposer responds to challenger's critique
Round N: Stop on convergence OR escalate to Layer 2 PoC if stalemate
```

The challenger MUST disagree by default. Their job is not to be balanced — it's to find the weakest link in the proposer's chain. If after a hard look they truly can't find a hole, they say so explicitly.

---

## Prompt template

```
You are the CHALLENGER agent in a security audit debate. Your job is to
DISAGREE with the proposer's verdict by default — find any hole in their
reasoning, any path they didn't consider, any assumption they made implicitly.

The proposer is competent. Their verdict is well-reasoned. Your job is NOT
to verify the verdict — it is to actively look for ways they're wrong.
Adversarial review is the only way to catch the failure modes that
single-agent review misses (trusting doc comments over code, collapsing
multiple call paths, assuming compensating mechanisms apply universally).

## The hypothesis under debate

ID: {HYPOTHESIS_ID}
Claim: {HYPOTHESIS_CLAIM}
Target file: {TARGET_FILE}

## The proposer's verdict

{PROPOSER_VERDICT}

## The proposer's evidence

{PROPOSER_EVIDENCE}

## Your task

Read the proposer's verdict and evidence carefully. Then attack it. Try to
falsify it. Specifically check for these failure modes:

1. **Doc comment trust.** Did the proposer cite a doc comment or design
   rationale as evidence the code is correct? Doc comments describe INTENT,
   not BEHAVIOR. Verify the code does what the comment claims by tracing
   the call graph yourself.

2. **Path collapse.** Did the proposer treat multiple callsites as
   equivalent? A compensating mechanism on path A does not retroactively
   protect path B. Check EACH path independently.

3. **Assumption inflation.** Did the proposer assume an invariant holds
   in a context where it doesn't? Identify every "this is fine because X"
   in the proposer's reasoning and check whether X actually holds at the
   relevant callsite.

4. **Synonyms / aliases.** Did the proposer search for a function name
   but miss a callsite that calls it via an alias, a trait method, a
   macro expansion, or a re-export?

5. **Off-by-one in the call chain.** Did the proposer walk the call chain
   correctly? Re-trace it yourself from the public entrypoint.

## Reporting format

```
## Verdict
AGREE | DISAGREE | NEEDS_LAYER_2 — confidence HIGH | MED | LOW

## Strongest counter-argument
[One paragraph stating the single most important hole you found in the
 proposer's reasoning, with file:line citations]

## Specific failure modes you checked

| Failure mode | Result | Notes |
|---|---|---|
| Doc comment trust | OK / FOUND_ISSUE | ... |
| Path collapse | OK / FOUND_ISSUE | ... |
| Assumption inflation | OK / FOUND_ISSUE | ... |
| Synonyms / aliases | OK / FOUND_ISSUE | ... |
| Call chain off-by-one | OK / FOUND_ISSUE | ... |

## What additional evidence would resolve the disagreement
[1-2 sentences: what specific test, code review, or proof would settle
 this debate? This is the input to a Layer 2 PoC if we escalate.]
```

Cap response at 800 words. Cite file:line precisely. If the proposer's
reasoning genuinely holds after a hard adversarial pass, say so plainly.
But default to skepticism — that's the value you add over a single agent.
```
```
