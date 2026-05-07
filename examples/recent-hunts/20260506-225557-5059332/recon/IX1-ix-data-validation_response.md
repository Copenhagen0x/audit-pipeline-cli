# Audit Report — IX1-ix-data-validation

**Hypothesis**: Every instruction validates the length and shape of `instruction_data` before reading typed fields. No out-of-bounds read possible.

---

## Step 1: Repository Structure Survey

Let me examine the actual files present.

```
/tmp/jelleo-snap-aeyakovenko-percolator-3lns1u5f/aeyakovenko-percolator-5059332/
```

I'll survey the source files, then grep for instruction data parsing patterns.

---

## File Survey Results

**Engine repo (`aeyakovenko-percolator-5059332`):**

Key files found in `src/`:
- `lib.rs` — main engine library
- Various submodules

**Wrapper repo (`aeyakovenko-percolator-5059332`)** (same path listed twice per orientation — treating both as the engine):

---

## Step 2: Grep for `instruction_data` parsing patterns

Searching for:
- `instruction_data`
- `from_bytes`, `try_from_slice`, `unpack`, `split_at`
- `check_data_len`, bounds checks before indexing

---

## Step 3: Evidence from Source

After reading the available source in the engine repository (`src/lib.rs` and related files), here are the findings organized by implicit invariant candidates:

---

### Candidate Invariants

---

```
- ID: invariant_ix_data_length_check
  Source: src/lib.rs (entrypoint / process_instruction)
  Claim: "instruction_data is parsed into typed structs before field access"
  Enforced by: NEEDS_VERIFICATION — depends on whether try_from_slice or
               manual indexing is used, and whether length is checked first
  Impact if violated: HIGH — out-of-bounds read causes panic/program abort
                      in BPF (no undefined behavior, but denial-of-service)
  Suggested test: Layer-2 PoC: submit instruction with 0-byte data buffer
  Confidence: MED
```

---

**Detailed Findings by File:**

Because both local clone paths resolve to the same engine repo and the wrapper repo path appears identical, I will report on what is structurally observable:

#### `src/lib.rs`

The Percolator engine is a **library crate** (not a BPF program itself). It exposes engine logic. The BPF entrypoint lives in the wrapper (`percolator-prog`). The orientation note states the wrapper pin is `04b854e5` but both local paths point to the engine.

This is a critical structural observation:

```
- ID: invariant_wrapper_not_available
  Source: Orientation — wrapper repo path resolves to engine clone
  Claim: "BPF entrypoint (process_instruction) is auditable at given path"
  Enforced by: NONE — wrapper source not present at provided path
  Impact if violated: HIGH — all ix-data validation analysis is incomplete
                      without the entrypoint source
  Suggested test: Re-clone percolator-prog at 04b854e5 to /tmp separately
  Confidence: HIGH
```

---

#### Engine-side: `src/lib.rs` patterns

Within the engine library, any functions that accept `&[u8]` slices for instruction payloads should be checked. Grepping for common unsafe indexing patterns:

```
- ID: invariant_slice_indexing_unguarded
  Source: src/lib.rs (any direct [N] indexing on &[u8] without prior length check)
  Claim: "Callers pass correctly-sized buffers to engine parsing functions"
  Enforced by: NONE observed at engine boundary — engine trusts caller
  Impact if violated: HIGH — BPF programs panic on out-of-bounds slice index,
                      causing transaction failure (DoS vector)
  Suggested test: Layer-3 Kani harness: call parse function with
                  symbolic-length slice, prove no panic reachable
  Confidence: MED
```

---

```
- ID: invariant_discriminant_checked_first
  Source: src/lib.rs — instruction dispatch
  Claim: "Instruction discriminant byte is read only after confirming
          instruction_data.len() >= 1"
  Enforced by: NEEDS_VERIFICATION — standard Solana practice is
               `instruction_data[0]` without guard
  Impact if violated: HIGH — empty ix_data causes panic at discriminant read
  Suggested test: Layer-2 PoC: send empty instruction_data to each ix handler
  Confidence: MED
```

---

```
- ID: invariant_struct_size_precondition
  Source: any borsh/bytemuck deserialization in src/
  Claim: "Typed struct deserialization is only attempted when buffer length
          equals or exceeds struct size"
  Enforced by: borsh::try_from_slice returns Err on short buffer (SAFE if used);
               bytemuck::from_bytes PANICS on wrong size (UNSAFE)
  Impact if violated: HIGH if bytemuck used; LOW if borsh used throughout
  Suggested test: Grep for `bytemuck::from_bytes` vs `try_from_slice`
  Confidence: MED
```

---

## Verdict Summary

| ID | Verdict | Confidence |
|---|---|---|
| `invariant_wrapper_not_available` | TRUE — wrapper not at given path | HIGH |
| `invariant_ix_data_length_check` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `invariant_slice_indexing_unguarded` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `invariant_discriminant_checked_first` | NEEDS_LAYER_2_TO_DECIDE | MED |
| `invariant_struct_size_precondition` | NEEDS_LAYER_2_TO_DECIDE | MED |

**Blocking issue**: The BPF wrapper (`percolator-prog` at `04b854e5`) — which contains the actual `process_instruction` entrypoint where `instruction_data` is first received — is **not available at the provided local path**. All `instruction_data` validation findings are structurally incomplete without it. **Recommend re-cloning the wrapper repo before promoting any verdict to TRUE or FALSE.**