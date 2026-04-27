# Prompt 05 — Authorization chain trace

**Use when**: a Layer-1/2 candidate finding involves an engine function that mutates sensitive state. You need to know: which BPF instructions can reach this function, and what authorization gates them?

---

## Prompt template

```
You are tracing the public-API authorization chain to a specific engine
function. The goal is to determine whether the function is:
- PERMISSIONLESS (anyone can call it)
- SIGNER-REQUIRED (any signer can call it)
- ADMIN-GATED (only an admin/authority PDA can call it)
- UNREACHABLE (no public-API path exists)

## Target function

Function: {ENGINE_FUNCTION_NAME}
Engine line: {ENGINE_LINE}
File: {ENGINE_PATH}/src/percolator.rs

## Files to read

- {WRAPPER_PATH}/src/ (to find BPF instruction handlers)
- {ENGINE_PATH}/src/ (to follow internal callers)

## Method

1. Find every wrapper-side caller of {ENGINE_FUNCTION_NAME} (or its
   wrapping helpers). For each:
   - What BPF instruction handles this caller's flow?
   - What signature checks does the wrapper enforce?
   - What permission checks (admin / authority / config flag) gate the path?
   - Are there cooldown / rate-limit / amount-cap guards?

2. For each authorization gate, identify bypass conditions:
   - Are there config-conditional bypasses (e.g., if max_bps == 0)?
   - Are there permissionless-mode special values (e.g., caller_idx == u16::MAX)?
   - Are there race windows where state changes between check and use?

## Output format

For each path that reaches {ENGINE_FUNCTION_NAME}:

```
Path #N
- BPF instruction: {name}
- Wrapper handler line: {file:line}
- Engine call line:    {file:line}
- Signature requirements: {list of accounts that must sign}
- Authority requirements: {admin PDA? authority PDA? none?}
- Other guards: {cooldown? amount cap? config flag?}
- Reachability verdict: PERMISSIONLESS | SIGNER | ADMIN | UNREACHABLE
- Bypass conditions: {list, or "none identified"}
- Severity if reachable improperly: {assessment}
```

Then summary:
- Total reachable paths: N
- PERMISSIONLESS paths: M (these are the highest-risk)
- ADMIN-only paths: K (these are lower-risk but still relevant)
- Strongest bypass candidate (if any)

Cap at 700 words. Read-only.
```

---

## When to use

This prompt is the bridge between Layer 3 (Kani CEX showing engine math is unsafe) and Layer 4 (LiteSVM showing whether the public API can actually reach that math).

If the answer is UNREACHABLE → the finding downgrades to "code defect, not exploitable."
If the answer is PERMISSIONLESS with no bypass conditions → the finding is exploitable.
If the answer is ADMIN-GATED → the finding is mitigated by admin trust assumptions.
If the answer reveals a bypass condition → the finding is exploitable AND potentially urgent.

## Example output (Percolator audit, Bug #3)

```
Path #1
- BPF instruction: TradeNoCpi
- Wrapper handler line: percolator-prog/src/percolator.rs:5811
- Engine call line:    percolator/src/percolator.rs:3915
- Signature requirements: user signer + LP signer (both required)
- Authority requirements: none
- Other guards: pre-IM check at engine:5715
- Reachability verdict: PERMISSIONLESS (any user + LP pair)
- Bypass conditions: none identified
- Severity if reachable improperly: HIGH (engine math overflow)
```

This output told us that Bug #3's panic site WAS reachable from public API, even though the bound analysis later showed it required prohibitive state accumulation to actually fire.

## Customization

For codebases with multiple admin roles (e.g., Percolator has `insurance_authority` and `insurance_operator`), enumerate each role separately and explain which role gates which path.
