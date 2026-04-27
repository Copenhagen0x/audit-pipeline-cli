# Prompt 01 — Codebase orientation

**Use when**: starting a fresh audit on an unfamiliar Solana program. Spawn this agent FIRST to enumerate structure before any hypothesis-driven work.

---

## Prompt template

```
You are doing a structural orientation on a Solana program codebase before
the security audit begins. Your job is to enumerate the codebase shape so
subsequent agents have a map to work from.

## Files to read

- {ENGINE_PATH}/src/ (all .rs files)
- {WRAPPER_PATH}/src/ (all .rs files)
- {ENGINE_PATH}/Cargo.toml
- {WRAPPER_PATH}/Cargo.toml
- Any README, ARCHITECTURE.md, SPEC.md at the repo root

## What to enumerate

### 1. Engine surface
- Public functions exposed by the engine library (the `pub fn` entries)
- Internal helper functions worth knowing about
- Engine state struct field map (what fields, what types)
- Engine constants (max-cap values, default values, magic numbers)
- Test-visible methods (those exposed via `test_visible!` macro or `#[cfg(feature = "test")]`)

### 2. Wrapper surface
- BPF instruction enum + handler table
- For each instruction: name, accounts required, signature requirements
- Permission model (admin-only, signer-required, permissionless)

### 3. Engine-wrapper interface
- Which engine functions does the wrapper call?
- What state mutations happen at the wrapper layer vs. engine layer?

### 4. Existing test coverage
- How many test files? In what directories?
- Existing Kani harnesses (if any) — list with names
- Existing fuzz harnesses (proptest, etc.) — list with names

### 5. Documentation surface
- Spec or design doc location?
- Inline doc-comments on public functions?
- Whitepaper or blog posts?

## Output format

```
## Engine surface
- Functions: <count public, count internal>
- State struct: <name>, <field count>
- Notable constants: <list with values>

## Wrapper surface
- BPF instructions: <count>
  - <name>: <permission> | <accounts required>
  - ...

## Engine-wrapper interface
- Engine functions called by wrapper: <list>
- Wrapper-only state: <list>

## Test coverage
- Test files: <count>
- Existing Kani harnesses: <count>, names: <list>
- Existing fuzz harnesses: <count>, names: <list>

## Documentation
- Spec doc: <path or "none">
- Inline coverage: <"thorough" | "sparse" | "none">

## Audit hypothesis seeds
- 5-10 candidate hypotheses worth investigating, derived from the
  enumeration above. Each should be a question framed as: "Does X hold?"
```

Cap at 1000 words. Read-only.
```

---

## Why this matters

Layer 1 (multi-agent review) is only as good as the hypotheses you spawn agents on. Without first orienting on the codebase shape, you'll spawn agents on hypotheses that don't fit the architecture (e.g., looking for instruction-level bugs in a codebase that does most work in helpers).

The "audit hypothesis seeds" output from this prompt feeds directly into the next 5-10 agents you spawn.

## Output use

Save the orientation report somewhere you'll re-read frequently during the audit:

```
audit_workspace/
├── 00_orientation_report.md   ← from this agent
├── 01_hypothesis_X_report.md  ← from the next agents
├── ...
```

Each subsequent hypothesis-driven agent starts by reading `00_orientation_report.md` for context.
