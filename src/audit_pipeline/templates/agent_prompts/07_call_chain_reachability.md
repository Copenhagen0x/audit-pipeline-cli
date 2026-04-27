# Prompt 07 — Call chain reachability

**Use when**: a Layer 3 Kani harness returned a CEX, and you need to verify that the witness state is reachable through legitimate public-API calls (not just via white-box state mutation).

---

## Prompt template

```
You are determining whether a specific engine state can be reached via
legitimate public-API calls. The Kani harness for {FINDING_NAME} returned
a counterexample (CEX) showing the engine math fails on this state. Your
job: trace whether the state can ACTUALLY be driven via BPF instructions.

## CEX witness state

{PASTE_WITNESS_STATE_HERE}

Example:
- account.pnl ≈ 2^100
- engine.pnl_pos_tot = account.pnl
- engine.vault ≈ 2^53
- account.reserved_pnl = 0

## Files to read

- {WRAPPER_PATH}/src/ (BPF instruction handlers)
- {ENGINE_PATH}/src/ (engine state mutation paths)

## Method

For each state field in the witness:

1. Find every engine function that WRITES that field
2. For each writer, identify which wrapper-side BPF instruction reaches it
3. Determine the bound on the value the BPF path can write:
   - Per-call: how much can a single instruction call change this field?
   - Cumulative: under unbounded sequences of public calls, what max is reachable?
4. Compare: is the witness value within the BPF-reachable range?

If ANY witness field is BPF-unreachable → CEX is white-box only, finding
downgrades to "code defect / not exploitable in production."

If ALL witness fields are BPF-reachable → finding is exploitable;
estimate the wall-clock cost of driving state to the witness.

## Output format

For each witness state field:

```
Field: {field_name}
Witness value: {value}
Engine writers: {list with line numbers}
BPF instructions reaching writers: {list}
Per-call max delta: {value}
Cumulative max via unbounded calls: {value or "unbounded"}
Witness within BPF range: YES | NO
```

Then verdict:
- All fields BPF-reachable: YES | NO
- If YES, wall-clock cost estimate to drive state to witness:
  {N slots × {slot duration} = {years}}
- Exploitability: ACTIVE | CODE-DEFECT-NOT-EXPLOITABLE | NEEDS-LITESVM-VERIFY

Cap at 600 words. Read-only.
```

---

## Why this matters

Without this check, you'll disclose Kani CEXes that turn out to require state the public API can't actually reach. The maintainer correctly pushes back: "this isn't actually a bug, you set the engine state directly bypassing my guards." You lose credibility.

The Percolator audit caught two findings (Sibling B + Bug #3) at this layer:
- Sibling B: white-box only, downgraded to "not a bug, defense-in-depth recommendation"
- Bug #3: BPF-reachable but ~13 years to accumulate via legitimate flow, downgraded to "code defect, not actively exploitable at default caps"

Both downgrades preserved credibility; both findings were still disclosed (with honest framing) instead of pretending they were active exploits.

## Hand-off to Layer 4 (LiteSVM)

The output of this prompt feeds directly into a LiteSVM bound-analysis test:

```
If verdict is "ACTIVE": write LiteSVM exploit chain test (templates/litesvm_exploit_chain.rs.template)
If verdict is "CODE-DEFECT-NOT-EXPLOITABLE": write LiteSVM bound analysis (templates/litesvm_bound_analysis.rs.template)
If verdict is "NEEDS-LITESVM-VERIFY": write reachability skeleton + bound analysis to settle the question
```

## Customization

For codebases where engine state can be reached via multiple BPF instruction sequences, enumerate each sequence separately. The cheapest sequence (lowest cost to attacker) is the relevant bound for "ACTIVE" classification.
