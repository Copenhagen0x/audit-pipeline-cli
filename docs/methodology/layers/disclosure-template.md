# Disclosure document template

This is the template structure for a disclosure document produced by the pipeline. Adapt as needed; the section ordering matters because reviewers read top-to-bottom.

## Structure

```
1. Title (under 70 chars, includes the bug class)
2. Header block
   - Auditor name + GitHub handle
   - Date
   - Engine pin (sha) + Wrapper pin (sha)
   - Method one-liner ("Multi-agent → Kani → LiteSVM → cross-platform")
3. What this disclosure delivers (one paragraph)
4. Direct response to maintainer's prior closure / invitation (if applicable)
5. Bug #1 — full write-up (see per-bug template below)
6. Bug #2 — full write-up
7. ...
8. Negative results / disclosed-but-not-a-bug findings
9. Prevention-class fix (if multiple bugs share a root cause)
10. Formal SAFE proofs section
11. Methodology + reproducibility (toolchain pins, file inventory)
12. Scope notes (what you didn't find, what's config-conditional)
```

## Per-bug write-up template

```markdown
## Bug #N — <one-line title summarizing the bug>

**Concrete state transition** (engine `src/<file>.rs`, line N):

```rust
<exact code from source>
```

**Why it commits partial progress incorrectly**:

<one paragraph explaining the bug semantics, NOT just the code>

**Public-API call chain** (verified empirically with LiteSVM PoC at `tests/wrapper/<file>.rs`):

1. <BPF instruction> 
2. → <wrapper function>
3. → <engine function (line N)>
4. → ... → PANIC at line N

**Engine math IS unsafe — Kani CEX confirms** (`tests/engine/<harness>.rs`):
- `proof_<name>` → **VERIFICATION FAILED** in N seconds — Kani returned CEX
- Native engine PoC (`tests/engine/<test>.rs`) panics with the expected message — passes local + VPS

**Reachability bound analysis** (`tests/wrapper/<bound>.rs`):

<table or paragraph quantifying reachability conditions>

**Reachability finding**: <code defect / exploitable / not-reachable-at-default-caps>

**Suggested fix**: <one-paragraph fix recommendation, with citation to the helper if reusing existing primitives>
```

## Tone guidance

| Do | Don't |
|---|---|
| State facts crisply | Editorialize ("clearly", "obviously") |
| Cite line numbers exactly | Approximate or generalize |
| Acknowledge config-conditionality | Inflate severity |
| Disclose negative results | Hide them to look more productive |
| Use "the code" / "the engine" | Use "your code" / "your engine" (subtly accusatory) |
| Defer to the maintainer on next steps | Tell the maintainer how to run their project |

## What NOT to put in a public disclosure

- Internal call-prep documents
- Speculation about other bugs you didn't verify
- Code snippets paraphrased to look more dramatic than the actual source
- Severity scores you can't justify with evidence
- Pricing, compensation, or business framing
- Adversarial language toward the maintainer or their prior work
- References to other auditors / vendors

## Sample headline finding (from Percolator audit)

> **Bug #1 — Cursor-wrap atomically resets consumption budget without absorbing volatility**
>
> **Concrete state transition** (engine `src/percolator.rs`, line 6155, inside atomic block lines 6149–6158 of `keeper_crank_not_atomic`):
>
> ```rust
> if sweep_end >= wrap_bound {
>     self.rr_cursor_position = 0;
>     self.sweep_generation = self.sweep_generation.checked_add(1)?;
>     self.price_move_consumed_bps_this_generation = 0;  // <-- BUG
> } else {
>     self.rr_cursor_position = sweep_end;
> }
> ```
>
> **Why it commits partial progress incorrectly**:
>
> The reset implies "this is a fresh generation — start a new consumption budget." The trigger (`sweep_end >= wrap_bound`) is purely call-count arithmetic. `KeeperCrank` is permissionless and unbounded — no rate-limit, no min-interval. ...

The full disclosure with this format is at: https://github.com/Copenhagen0x/percolator-audit-2026-04
