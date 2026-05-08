# §03 · Hypothesis library schema

The hypothesis library is the heart of the methodology. Each entry is a falsifiable claim about an invariant that should hold for a class of protocol — phrased so a clean negative result strengthens the disclosure, not undermines it.

This section is the canonical schema reference for entries in `hypotheses.yaml` and bundled class libraries (`perp_dex_class.yaml`, `amm_cp_class.yaml`, `clmm_class.yaml`, `lending_class.yaml`, `lst_class.yaml`).

---

## Per-hypothesis schema

### Required fields

| Field      | Type     | Description |
|------------|----------|-------------|
| `id`       | string   | Unique short identifier, stable across cycles. Used as the cross-DB key. Convention: `<prefix><n>-<kebab-slug>` (e.g. `H1-residual-conservation`, `PD7-pnl-mark-bound`, `CLMM3-fee-growth-monotone`). |
| `class`    | enum     | One of: `invariant_property`, `state_transition`, `authorization`, `arithmetic_overflow`, `implicit_invariant`. Determines the Layer-2/3 dispatch path. |
| `claim`    | string   | The falsifiable claim in plain English. Phrased so a clean negative result strengthens the disclosure. |

### Severity field

| Field      | Type     | Description |
|------------|----------|-------------|
| `severity` | enum     | Optional. One of `Critical`, `High`, `Medium`, `Low`, `Info`. Sets the floor on auto-derived severity. Per-cycle severity may auto-promote when a PoC fires (see §05). |

### Scoping fields (heart of v1)

These three fields are what makes the library safe to grow at scale. They control which hyps load against which target, and how the propagation engine generalizes a confirmed finding across the cluster.

| Field              | Type            | Default  | Description |
|--------------------|-----------------|----------|-------------|
| `applies_to`       | list of strings | `['*']`  | Protocol names this hypothesis applies to. `['*']` = all protocols (back-compat). Examples: `[percolator]`, `[percolator, drift, mango]`, `[orca-whirlpools, kamino-liquidity]`. |
| `scope_conditions` | list of strings | `[]`     | Predicates that must be true under target conditions (workspace.json `conditions:` mapping). E.g. `has_insurance_pool`, `uses_pyth_oracle`, `clob_orderbook`. |
| `bug_class`        | string          | `null`   | Generalized class identifier for cross-protocol propagation. Stable across protocols. E.g. `insurance-counter-vault-divergence`, `oracle-staleness-bypass`. |

### Anchor fields (agent prompt context)

These help the recon agent focus its initial reading window. Loader does not filter on them.

| Field                   | Type   | Description |
|-------------------------|--------|-------------|
| `target_file`           | string | Anchor file path within the target's repo. Drives diff-aware hunting (§08): only hyps whose `target_file` is in a commit's diff fire on a watch-triggered cycle. |
| `relevant_constants`    | string | Free-form text listing bound constants the agent should consider. |
| `relevant_instructions` | string | Free-form list of public instructions / helpers the hypothesis touches. |

---

## Enumerations

### `class`

| Value                  | Layer-2 path                            | Use when |
|------------------------|------------------------------------------|----------|
| `invariant_property`   | Empirical PoC (cargo test against helper) | A property that should hold at every state transition |
| `state_transition`     | Empirical PoC + LiteSVM reachability      | A specific transition / instruction has a property |
| `authorization`        | Empirical PoC (signer-bypass class)       | A privileged path is gated correctly |
| `arithmetic_overflow`  | Empirical PoC + Kani synthesis            | A math expression is bounded |
| `implicit_invariant`   | Empirical PoC (state-comparison class)    | An invariant the code does not assert explicitly but the engine relies on |

### `severity`

`Critical` > `High` > `Medium` > `Low` > `Info`. Final severity of a finding is the maximum of (declared severity floor, severity auto-derived from class + PoC outcome). See §05.

### Scope predicates (vocabulary)

Predicates declared in `scope_conditions` must resolve to a boolean in the target's `workspace.json` `conditions:` mapping. Unknown predicates default to `false` and surface a warning in the loader log.

Standard vocabulary:

```
has_insurance_pool          uses_pyth_oracle
has_haircut_accounting      uses_switchboard_oracle
perpetual_funding           liquidation_engine
clob_orderbook              multi_market
amm_constant_product        flash_loan
multi_collateral            cross_program_invocation_heavy
```

Adding a new predicate is additive: declare it in any hyp, add `<name>: true` to the target's `workspace.json` conditions, and the loader matches.

---

## Loader behavior

The loader (`audit_pipeline.scoping`) applies three filters before dispatch:

1. **`applies_to` filter** — hypothesis loads only if the target's name is in `applies_to` OR `applies_to` includes `'*'`.
2. **`scope_conditions` filter** — hypothesis loads only if every predicate in `scope_conditions` is `true` for the target's workspace.json conditions mapping.
3. **Severity floor filter** — hypothesis loads only if its declared `severity` is at or above the cycle's `--min-severity` (if any).

Filter order is fixed: applies_to → scope_conditions → severity. Skipped hypotheses surface in the cycle log with the reason (`scope_applies_to`, `scope_conditions`, or `min_severity`) so misconfigurations are obvious.

---

## Class libraries

Bundled libraries live at `src/audit_pipeline/templates/hypotheses/` in the implementation repo. As of v1:

| File                          | Hyps | Class focus |
|-------------------------------|------|-------------|
| `percolator.yaml`             |  12  | Percolator engine (cluster-applicable + protocol-specific) |
| `percolator_deep.yaml`        | 101  | Deep-protocol Percolator (engine internals) |
| `percolator_strict_helper_class.yaml` | 12 | Sibling class derived from F7 (helper-strictness) |
| `percolator_bounty_regression.yaml`   | 18 | Bounty-2 negative-result regression suite |
| `perp_dex_class.yaml`         |  43  | Perp-DEX cluster (Drift, Mango, Jupiter Perps, Percolator) |
| `amm_cp_class.yaml`           |  58  | Constant-product AMM (Raydium, Orca CP, Saber) |
| `clmm_class.yaml`             | 102  | Concentrated-liquidity AMM (Orca Whirlpools, Kamino Liquidity, Meteora DLMM) |
| `lending_class.yaml`          |  94  | Lending markets (Marginfi, Kamino Lend, Solend, Save) |
| `lst_class.yaml`              |  68  | Liquid staking (Marinade, Sanctum, JitoSOL) |
| **TOTAL**                     | **508** | distinct hypotheses |

Adding a new class:
1. Drop `<class>_class.yaml` in `templates/hypotheses/`.
2. Add `<class>` to `PROTOCOL_CLASSES` in `audit_pipeline/scoping.py`.
3. The loader picks it up automatically (`audit-pipeline hunt --protocol-class <class>`).

---

## Auto-derivation

When a finding transitions to `confirmed`, the engine asks Claude (with the parent finding as context) to emit N **structural siblings** — hypotheses targeting variations of the same root cause across adjacent code paths. Output lands in `<workspace>/derived/<hyp-id>-siblings.yaml` and can be optionally appended into the appropriate class library.

This is how the library compounds without manual work. F7's confirmed → 12 siblings auto-derived → 5 confirmed across the F7-class — that pattern repeats per finding.

See [`audit-pipeline derive-siblings`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/commands/derive_siblings.py) in the implementation repo.

---

**Live reference:** [jelleo.com/methodology.html#scoping](https://jelleo.com/methodology.html#scoping)
**Implementation:** [`audit_pipeline/scoping.py`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/scoping.py)
