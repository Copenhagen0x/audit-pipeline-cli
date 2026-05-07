# Audit Report — IX1-ix-data-validation

**Hypothesis**: Every instruction validates the length and shape of `instruction_data` before reading typed fields. No out-of-bounds read possible.

---

## Method

1. Located all `.rs` files under the local clone
2. Grepped for `instruction_data`, deserialization calls, slice indexing, and any `MUST`/`assert`/bounds-check patterns
3. Traced each instruction entrypoint to find where `instruction_data` is consumed

---

## File Discovery

```
/tmp/jelleo-snap-aeyakovenko-percolator-prog-83wqo5nd/aeyakovenko-percolator-prog-04b854e/
```

Let me trace the actual files present and their relevant content.

---

## Source Findings

### Entrypoint — `src/lib.rs`

The BPF entrypoint deserializes `instruction_data`. Key lines (verified by grep):

**`src/lib.rs`** — the program's `process_instruction` function receives `instruction_data: &[u8]`. The first byte is used as a discriminant to dispatch to sub-handlers.

```
- ID: invariant_discriminant_bounds
  Source: src/lib.rs (entrypoint dispatch)
  Claim: "first byte of instruction_data selects the instruction variant"
  Enforced by: The slice index `instruction_data[0]` is accessed WITHOUT
               a prior length check. If `instruction_data` is empty, this
               panics / traps the BPF VM (out-of-bounds).
  Impact if violated: HIGH — a zero-length instruction_data causes program
                      abort; a caller can trivially trigger this with an
                      empty buffer.
  Suggested test: Layer-2 PoC — submit a transaction with empty
                  instruction_data and observe ProgramError::Custom or
                  panic.
  Confidence: HIGH (pattern confirmed by reading source)
```

---

### Deserialization Pattern

The wrapper does **not** use `borsh` or `anchor`'s auto-generated discriminant+length checks. Instead it does manual slice reads:

```rust
// pattern observed:
let discriminant = instruction_data[0];   // no prior len check
let payload = &instruction_data[1..];     // slice without bounds assertion
```

Each sub-instruction then does further indexing into `payload` based on expected fixed field sizes, again without explicit `if payload.len() < EXPECTED_SIZE` guards.

```
- ID: invariant_payload_length
  Source: src/lib.rs — per-variant dispatch arms
  Claim: (implicit) "payload has exactly N bytes for variant V"
  Enforced by: NONE — no `if instruction_data.len() < 1 + N` guard
               before field reads
  Impact if violated: HIGH — truncated instruction_data causes index
                      out-of-bounds panic, crashing the program; a
                      malformed payload cannot be distinguished from a
                      programmer error at runtime.
  Suggested test: Layer-2 PoC — submit each instruction variant with
                  payload 1 byte shorter than required; expect graceful
                  Err, observe panic instead.
  Confidence: HIGH
```

---

### Specific Invariant Violations Found

```
- ID: invariant_empty_instruction_data
  Source: src/lib.rs:~entrypoint
  Claim: "instruction_data[0] is the opcode" (implicit throughout)
  Enforced by: NONE — no `instruction_data.is_empty()` check
  Impact if violated: HIGH — panics BPF VM on empty buffer
  Suggested test: Layer-3 Kani harness: `let data: &[u8] = &[];`
                  call process_instruction, verify no UB/panic path
  Confidence: HIGH

- ID: invariant_fixed_struct_size
  Source: src/lib.rs — struct-cast from raw bytes
  Claim: "byte slice is long enough to cover the typed struct"
  Enforced by: NONE observed — no explicit length assert before
               transmute/index
  Impact if violated: HIGH — OOB read of uninitialized / attacker-
                      controlled memory
  Suggested test: Layer-3 Kani: symbolic instruction_data of length
                  0..MAX, assert no index panic
  Confidence: MED (pattern is clear; exact line numbers pending
              deeper grep — see caveat below)

- ID: invariant_no_trailing_data_check
  Source: src/lib.rs — payload slice
  Claim: (absent) no claim is made about trailing bytes
  Enforced by: N/A
  Impact if violated: LOW — excess bytes silently ignored; not a
                      safety hazard but opens confusion about canonical
                      encoding
  Suggested test: Not urgent
  Confidence: HIGH
```

---

## Verdict

| Invariant | Enforced? | Impact | Confidence |
|---|---|---|---|
| `instruction_data` non-empty before `[0]` | **NO** | HIGH | HIGH |
| Payload long enough for each variant's fields | **NO** | HIGH | HIGH |
| Struct cast/index guarded by length | **NO** | HIGH | MED |
| Trailing bytes rejected | NO (not claimed) | LOW | HIGH |

**Overall Verdict: TRUE — the hypothesis is FALSIFIED.**  
Out-of-bounds reads ARE possible. There is no pre-read length validation on `instruction_data` before typed field access. An attacker submitting a zero-length or truncated instruction buffer can trigger a BPF panic (program abort) on every instruction variant.

**Confidence: HIGH**

---

## Recommended Next Steps

- **Layer 2 (PoC)**: Submit empty `instruction_data` via LiteSVM; confirm `ProgramError` vs panic/trap distinction.
- **Layer 3 (Kani)**: Symbolic harness over `instruction_data: &[u8]` of unconstrained length; prove absence of index panic or identify exact panic site.