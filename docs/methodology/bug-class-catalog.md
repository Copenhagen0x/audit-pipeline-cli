# Bug-class catalog

> Single source of truth for the `bug_class` taxonomy. Every hypothesis YAML
> and every confirmed finding carries a `bug_class` value drawn from this
> document. The propagation engine pivots on `bug_class` to decide which
> regex / AST signatures to apply to the corpus.

This document is the **operator-facing reference** for the taxonomy. The
runtime mapping from `bug_class` → regex signatures lives in
[`src/audit_pipeline/commands/propagate.py:BUG_CLASS_SIGNATURES`](../../src/audit_pipeline/commands/propagate.py)
— that's the source of truth for the engine. This doc is the source of
truth for humans.

---

## Naming convention

`<noun-or-mechanism>-<failure-mode>` — kebab-case, lowercase only, no
acronyms unless universal (`pnl`, `pda`, `cpi`, `oi`, `mm`, `tvl`, `bps`).

Good: `insurance-counter-vault-divergence`
Good: `funding-rate-self-bias`
Bad:  `INSURANCE_COUNTER_VAULT_DIV` (uppercase + underscore)
Bad:  `f7-class` (project-specific; not portable across protocols)

A bug-class identifier should describe a **structural pattern** that can
appear across protocols, not a specific finding ID. F7's class is
`insurance-counter-vault-divergence`, not `F7-class`. That makes propagation
meaningful: when F7 confirms in Percolator, the engine searches every
indexed protocol for the same *pattern*, not the same protocol's bug.

---

## Catalog (current — 19 classes with signatures registered)

Each entry below has registered regex signatures in
`BUG_CLASS_SIGNATURES`. All other `bug_class` values declared in YAMLs
without signatures yet are tracked under "Pending signatures" below.

### Operational invariants

#### `insurance-counter-vault-divergence`
The insurance-fund counter and the underlying vault counter become
decoupled. The bug shows up as a haircut residual that grows on
absorption: when insurance pays out, the vault is left intact, so a
formula computing `vault − c_tot − insurance` *grows* by the absorbed
amount. **F7's class.** Inaugural disclosure: [aeyakovenko/percolator-prog#39](https://github.com/aeyakovenko/percolator-prog/pull/39).

#### `vault-balance-divergence`
General vault-counter mismatches: deposits/withdrawals not perfectly
mirrored in the vault accumulator. Less specific than the insurance-
counter case; covers any mutation that updates the user-visible state
without a paired vault update.

#### `haircut-direction-violation`
The haircut formula is supposed to cap a side's claim at the residual.
If the cap is implemented in the wrong direction (e.g., applying the
ceiling to the loser instead of the winner), invariant violated.

### Cross-instruction state

#### `clock-advance-without-touch`
Market clock advances (via `accrue_market_to`, `catchup_accrue`, etc.)
without first touching all open accounts whose lazy MTM the clock
advancement is about to materialize. Reaching the new state without
the per-account touch creates a window where risk gates fire on stale
balances. Class behind the disclosed cascade-bypass bugs (#54-#69).

#### `keeper-cursor-budget-bypass`
Round-robin cursor in `KeeperCrank` either advances when it shouldn't
(missing accounts) or fails to advance when it should (DOS via stuck
candidate). Includes the "reward zero on populated buffer" failure mode.

#### `account-gc-state-leak`
Account marked free / reclaimable while still holding live state
(non-zero position, pending fees, etc.). Subsequent reuse aliases
old state into new account.

#### `resolved-state-pnl-leak`
Findings reachable only when a market is in `Resolved` state.
Settlement / matured-claim paths that should have been gated by mode
remain reachable, leaking PnL.

### PnL / funding / mark

#### `funding-rate-self-bias`
Funding rate is computed off `mark_ewma` which itself can be moved by
attacker-controlled trades. The attacker pays themselves funding via
their own mark divergence.

#### `arithmetic-overflow-pnl-mark`
Integer overflow on `i128` PnL accumulators or mark prices. Includes
saturating-vs-wrapping arithmetic correctness, square-root bounds,
checked vs unchecked.

#### `self-trade-cash-flow-violation`
Self-matched trade primitive (one operator controlling both sides)
that bypasses the PnL zero-sum invariant. F7 was a special case of
this combined with the residual class.

#### `liquidation-incentive-overpayment`
Liquidation discount / bonus exceeds the configured cap, or stacks
across cascade rounds to overpay the liquidator.

### Authorization

#### `authorization-bypass`
Permissionless instruction reaches a privileged path; signer check
missing or weakened; admin authority delegated incorrectly; PDA
authority bypass.

### State / lifecycle

#### `init-state-invariant-violation`
A market / account / pool can be initialized in a state that violates
a documented invariant: zero reserves, mismatched discriminator, missing
authority binding, etc. Includes replay protection failures.

#### `account-close-state-leak`
Closing path doesn't fully zero state before reclaiming the account
slot. Subsequent re-init can read residual bytes.

### Token math

#### `token-balance-conservation-violation`
SPL-token transfers don't conserve total supply across protocol-internal
state — money created or destroyed somewhere.

#### `constant-product-invariant-violation`
For AMMs: `x*y = k` violated by a swap or liquidity event. Includes
rounding-direction bugs that let the invariant strictly decrease.

#### `fee-accounting-rounding-asymmetry`
Fees rounded asymmetrically vs the trade direction — attacker can
extract micro-margin via repeated calls in the favorable direction.

#### `flash-loan-repayment-bypass`
Flash-borrow path can complete without a paired repayment, or the
repayment check is loose enough to permit short-rebay attacks.

### F7-derived (added 2026-05-08)

#### `accrual-helper-asymmetry`
Wrapper exposes two accrual helpers — one weak (no per-account-touch
gate), one strict (calls `reject_account_limited_market_progress`).
Bug: a permissionless instruction handler routes through the weak
helper when it should route through the strict one. SH1, SH2 in the
strict-helper sibling library.

#### `k-walk-accumulation`
Multi-step state advancement (oracle observations, funding rate
integration, etc.) that accumulates into engine state without per-step
account-touch gating. SH3, SH4 cover this class.

---

## Pending signatures (declared in YAMLs, not yet in `BUG_CLASS_SIGNATURES`)

When a hyp declares a `bug_class` not in the catalog, propagation
returns `no_signatures_registered` for findings of that class — the
hook fires but the corpus sweep is a no-op. **This is the C9 backlog
in P2's buildout.**

Today: ~200 distinct `bug_class` values declared across the 5 protocol-
class libraries (perp_dex, amm_cp, clmm, lending, lst). Adding
signatures is incremental: when a class confirms for the first time,
write its signatures, add the entry, and the next propagation cycle
will pick it up.

To list current gaps:

```bash
python3 -c "
from pathlib import Path
import yaml
from audit_pipeline.commands.propagate import BUG_CLASS_SIGNATURES
declared = set()
for p in Path('src/audit_pipeline/templates/hypotheses/').glob('*.yaml'):
    raw = yaml.safe_load(p.read_text())
    for h in raw.get('hypotheses', []):
        if h.get('bug_class'):
            declared.add(h['bug_class'])
gaps = sorted(declared - set(BUG_CLASS_SIGNATURES.keys()))
print(f'{len(gaps)} bug_class values lack signatures:')
for g in gaps:
    print(f'  - {g}')
"
```

---

## How to add a new bug class

1. **Pick a name** — `<noun>-<failure-mode>`, kebab-case, protocol-agnostic.
2. **Add YAML hyps** that declare `bug_class: <new-name>` (any class
   library where it applies).
3. **Add to `BUG_CLASS_SIGNATURES`** in `propagate.py` with 2–4 regex
   signatures that identify candidate code in any protocol.
4. **Document here** under the appropriate section above with a
   2–3 sentence description.
5. **(Optional) Add a fixture-based unit test** under
   `tests/test_propagation_signatures.py` (item C11) confirming the
   signature matches a known-vulnerable file and doesn't match a
   patched version.

The CI rule in [`tests/test_class_libraries.py`](../../tests/test_class_libraries.py)
(`test_signature_catalog_no_regression`) prevents accidental signature
removal — the catalog can grow but cannot regress below the current 19.
