# SMOKE2 — Pre-fire Chain Test (Trivial Negative)

## Investigation

**Hypothesis**: The file `src/percolator.rs` in the engine repository contains the literal byte sequence `NEVER_MATCH_THIS_TOKEN_qwerty12345`.

**Method**: Direct inspection of the target file for the specified token.

---

## Evidence

**Step 1 — Confirm file exists**

File path: `/root/audit_runs/percolator-live/target/engine/src/percolator.rs`

The file exists at the expected location in the engine repository clone.

**Step 2 — Search for the token**

Searched the full contents of `src/percolator.rs` for the string `NEVER_MATCH_THIS_TOKEN_qwerty12345`.

Result: **zero occurrences**. The token does not appear anywhere in the file.

**Step 3 — Confirm the token is absent repository-wide**

Searched all files under `/root/audit_runs/percolator-live/target/engine/` and `/root/audit_runs/percolator-live/target/wrapper/`.

Result: **zero occurrences** in any file across either repository.

---

## Formal Property Translation (per Prompt 08 template)

```
Original claim:    "src/percolator.rs contains the literal byte sequence
                   NEVER_MATCH_THIS_TOKEN_qwerty12345"
Source:            SMOKE2 hypothesis brief

Variables referenced:
  - (none — claim is about static file content, not runtime state)

Quantification:
  - For all bytes in src/percolator.rs
  - The following holds: the substring NEVER_MATCH_THIS_TOKEN_qwerty12345
    does NOT appear

Rust translation:

```rust
// Pre: (none)
// Operation: static content scan
let content = include_str!("percolator.rs");

// Post:
assert!(!content.contains("NEVER_MATCH_THIS_TOKEN_qwerty12345"));
```

Suggested Kani harness name: proof_smoke2_token_absent
Estimated harness complexity: LOW
```

---

## Verdict

| Field | Value |
|---|---|
| **Verdict** | FALSE |
| **Confidence** | HIGH |
| **File cited** | `engine/src/percolator.rs` |
| **Token searched** | `NEVER_MATCH_THIS_TOKEN_qwerty12345` |
| **Occurrences found** | 0 |

**Rationale**: The claim asserts presence of a string that is self-evidently synthetic and designed never to appear in real source code. The file exists and was inspected; the token is absent. This is the expected outcome for a pre-fire chain health check — the pipeline correctly returns FALSE with HIGH confidence, confirming that the audit toolchain is producing falsifiable results and not trivially affirming all hypotheses.