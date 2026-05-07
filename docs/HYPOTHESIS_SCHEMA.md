# Hypothesis Library Schema

**Status:** v1 spec · 2026-05-07
**Implementation:** Sprint 2.2 (loader filter + schema validation in `audit_pipeline.commands.hunt`)
**Public reference:** [jelleo.com/methodology.html#scoping](https://jelleo.com/methodology.html#scoping)

This is the canonical schema for entries in `hypotheses.yaml`. Every entry in the `hypotheses:` list is a single hypothesis. The schema is additive: optional fields default to permissive values that match v0.1 behaviour.

---

## Schema · per-hypothesis fields

### Required fields

| Field      | Type   | Description                                                                                         |
|------------|--------|-----------------------------------------------------------------------------------------------------|
| `id`       | string | Unique short identifier. Stable across cycles. Used as the cross-DB key. Convention: `H<n>-<slug>`. |
| `class`    | enum   | One of: `invariant_property`, `state_transition`, `authorization`, `arithmetic_overflow`, `implicit_invariant`. Determines Layer-2/3 dispatch path. |
| `claim`    | string | The falsifiable claim in plain English. Phrased so a clean negative result strengthens the disclosure. |

### Severity field

| Field      | Type   | Description                                                                                         |
|------------|--------|-----------------------------------------------------------------------------------------------------|
| `severity` | enum   | Optional. One of: `Critical`, `High`, `Medium`, `Low`, `Info`. Sets the floor on auto-derived severity. Per-cycle severity may auto-promote when a PoC fires (see [methodology §05](https://jelleo.com/methodology.html#severity)). |

### Scoping fields (NEW in v1)

These three fields are the heart of the v1 schema. They control which hypotheses load against which target, and how the propagation engine generalizes a confirmed finding across the cluster.

| Field              | Type             | Default     | Description                                                                                |
|--------------------|------------------|-------------|--------------------------------------------------------------------------------------------|
| `applies_to`       | list of strings  | `['*']`     | Protocol names this hypothesis applies to. `['*']` = all protocols (back-compat default).  |
| `scope_conditions` | list of strings  | `[]`        | Predicates that must be true under target conditions. E.g. `has_insurance_pool`, `uses_pyth_oracle`. |
| `bug_class`        | string           | `null`      | Generalized class identifier for cross-protocol propagation. E.g. `insurance-counter-vault-divergence`. |

### Anchor fields

These help the recon agent focus its initial reading window. Optional; loader does not filter on them.

| Field                   | Type   | Description                                                                |
|-------------------------|--------|----------------------------------------------------------------------------|
| `target_file`           | string | Anchor file path within the target's engine repo.                          |
| `relevant_constants`    | string | Free-form text listing bound constants the agent should consider.          |
| `relevant_instructions` | string | Free-form list of public instructions / helpers the hypothesis touches.    |

---

## Enumerations

### `class`

| Value                | Layer-2 path                               | Use when                                                                       |
|----------------------|--------------------------------------------|--------------------------------------------------------------------------------|
| `invariant_property` | Empirical PoC (cargo test against helper)  | A property that should hold at every state transition                          |
| `state_transition`   | Empirical PoC + LiteSVM reachability       | A specific transition / instruction has a property                             |
| `authorization`      | Empirical PoC (signer-bypass class)        | A privileged path is gated correctly                                           |
| `arithmetic_overflow`| Empirical PoC + Kani synthesis             | A math expression is bounded                                                   |
| `implicit_invariant` | Empirical PoC (state-comparison class)     | An invariant the code does not assert explicitly but the engine relies on      |

### `severity`

| Value      | Color  | Definition                                                                                              |
|------------|--------|---------------------------------------------------------------------------------------------------------|
| `Critical` | red    | Direct loss of funds or full takeover with no meaningful preconditions. Permissionless reach.            |
| `High`     | orange | Significant loss under realistic preconditions.                                                          |
| `Medium`   | yellow | Hardening issue or invariant violation requiring privileged signer or improbable state.                  |
| `Low`      | blue   | Minor issue with no plausible path to fund loss.                                                         |
| `Info`     | gray   | Informational. No security impact.                                                                       |

### `scope_conditions` — predicate vocabulary

The loader evaluates each predicate against the target's `workspace.json` config. These are the recognized predicates as of v1; the list grows as we onboard more protocol shapes.

| Predicate               | True when target's config indicates...                                                  |
|-------------------------|-----------------------------------------------------------------------------------------|
| `has_insurance_pool`    | The protocol maintains a separate insurance fund balance                                |
| `has_haircut_accounting`| The protocol applies a haircut to claimable PnL on shortfall                            |
| `perpetual_funding`     | The protocol charges/credits funding rate on open positions                             |
| `uses_pyth_oracle`      | The protocol consumes Pyth price feeds                                                  |
| `uses_switchboard_oracle` | The protocol consumes Switchboard price feeds                                          |
| `liquidation_engine`    | The protocol has a liquidation path (lending or perp)                                  |
| `multi_market`          | The protocol supports multiple markets in a single program account                      |
| `clob_orderbook`        | The protocol uses a central limit order book                                            |
| `amm_constant_product`  | The protocol uses constant-product AMM math                                             |
| `flash_loan`            | The protocol exposes a flash-loan instruction                                           |
| `multi_collateral`      | The protocol accepts multiple collateral asset types                                    |
| `cross_program_invocation_heavy` | The protocol delegates significant logic via CPI                                |

Predicates are matched case-insensitively. A hypothesis with `scope_conditions: [has_insurance_pool, has_haircut_accounting]` loads only against targets whose `workspace.json` declares both.

### `bug_class` — propagation namespace

Bug classes are the cross-protocol generalization of confirmed findings. Each confirmed finding's `bug_class` becomes the propagation key. A non-exhaustive starter set:

| `bug_class`                                  | Origin                                | Pattern                                                                  |
|----------------------------------------------|---------------------------------------|--------------------------------------------------------------------------|
| `insurance-counter-vault-divergence`         | F7 / Percolator / 2026-04             | Insurance counter shrinks without corresponding vault debit               |
| `oracle-effective-price-staleness`           | Public disclosures (Solana 2024–25)   | Oracle price used past staleness window                                  |
| `liquidation-incentive-overpayment`          | Public class                          | Liquidation bonus exceeds protocol-configured incentive                   |
| `funding-rate-self-bias`                     | Perp DEX class                        | Funding rate captured after mark-price mutation in same tx                |
| `flash-loan-reentrancy`                      | DeFi class                            | Flash-loan instruction permits reentry into protocol state               |
| `cpi-account-substitution`                   | Solana-specific class                 | CPI accepts an account that does not match its expected program owner    |
| `clock-advance-without-touch`                | Engine class                          | A market clock advances without all materialized accounts being touched   |

This list is open. A hypothesis may declare a `bug_class` that does not yet appear here; on confirmation, the propagation engine catalogs it.

---

## Loader semantics

When `audit-pipeline hunt` runs against target `T` with conditions `C` (derived from `T`'s `workspace.json`), the loader applies a 3-step filter to every hypothesis in the loaded `hypotheses.yaml` files:

```
for each hypothesis H in library:
  if T not in H.applies_to and '*' not in H.applies_to:
      skip H (record as scoped-out)
      continue

  if any predicate p in H.scope_conditions where C[p] is False:
      skip H (record as scoped-out)
      continue

  if cli_min_severity is set and H.severity < cli_min_severity:
      skip H (record as severity-filtered)
      continue

  emit H for dispatch
```

**Recording skipped hypotheses.** The cycle's metadata records every skipped hypothesis with its skip reason (`scope_applies_to`, `scope_conditions`, `min_severity`). This shows up in the report's "Hypothesis library coverage" table — customers see exactly how many hypotheses applied to their cycle and how many were correctly filtered out.

---

## Validation rules

The schema validator (Sprint 2.2 implementation) enforces:

1. `id` matches `^H\d+-[a-z][a-z0-9-]*$` (e.g. `H1-residual-conservation`).
2. `class` is in the enumeration above.
3. `severity` (if present) is in the enumeration above.
4. `applies_to` is a list of strings; each string is either `*` or a known protocol slug.
5. `scope_conditions` is a list of strings; each string is in the predicate vocabulary above (warns, does not error, on unknown predicates — keeps the schema additive as new shapes onboard).
6. `bug_class` (if present) matches `^[a-z][a-z0-9-]*$` and is at most 64 chars.
7. `claim` is non-empty and at least 20 characters (catches accidentally-empty entries).

Validation errors fail the hunt cycle with a non-zero exit code; warnings are logged but do not block.

---

## Example · F7 hypothesis with full v1 scoping

```yaml
- id: H1-residual-conservation
  class: invariant_property
  claim: >
    The post-haircut residual cash on a market
    (vault - cash_locked_in_orderbook - claimable_pnl - insurance_counter)
    is conserved by every internal accounting helper. If any helper shrinks
    the insurance counter, it MUST also debit the vault by the same amount,
    otherwise the residual grows without a corresponding credit obligation.
  severity: Critical
  applies_to: [percolator, drift, mango, marginfi]
  scope_conditions: [has_insurance_pool, has_haircut_accounting]
  bug_class: insurance-counter-vault-divergence
  target_file: src/percolator.rs
  relevant_constants: |
    MAX_VAULT_TVL = 1e16
    MAX_ACCOUNT_POSITIVE_PNL = 1e32
  relevant_instructions: |
    settle_after_close, fill_match, use_insurance_buffer
```

---

## Backward compatibility

v0 hypotheses (no `applies_to`, no `scope_conditions`, no `bug_class`) load with permissive defaults:

- `applies_to` defaults to `['*']` — hypothesis applies to every target.
- `scope_conditions` defaults to `[]` — no predicate filtering.
- `bug_class` defaults to `null` — propagation is not triggered on this hypothesis's confirmation.

The migration path is incremental: each protocol's hypothesis library can be retroactively tagged at any time without breaking existing cycles.

---

## Migration plan for the Percolator library (Sprint 2.2)

The current `templates/hypotheses/percolator.yaml` has 12 hypotheses without scoping fields. The Sprint 2.2 migration:

1. Tag every Percolator hypothesis with `applies_to: [percolator]` (preserves current behavior — runs only against Percolator).
2. Tag F-class hypotheses (those that produced F7's family) with `applies_to: [percolator, drift, mango, marginfi]` and the appropriate `scope_conditions` so propagation can fan out.
3. Assign `bug_class` to every hypothesis. Hypotheses without an obvious class get `bug_class: null` initially; classes are added as findings confirm.
4. Run a baseline cycle to verify zero regressions versus the unscoped library.

The migration is non-destructive: the original library is preserved; the v1 fields are additive.

---

## Validation against the JSON Schema

A formal JSON Schema lives at `src/audit_pipeline/schemas/hypothesis.schema.json` (Sprint 2.2). It is consumed by:

- The CLI's `audit-pipeline hunt` (validates before dispatch)
- The `audit-pipeline onboard` command (validates the stub it generates)
- CI on the platform repo (validates every committed hypothesis YAML)

Tooling-level integration (editor schema hints, GitHub PR checks) lands in Sprint 3.

---

*Maintained by Jelleo. Apache-2.0. Last updated: 2026-05-07.*
