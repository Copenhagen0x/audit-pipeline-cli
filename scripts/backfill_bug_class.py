#!/usr/bin/env python3
"""One-shot migration: backfill `bug_class` field on every hypothesis YAML
that's missing it. Only touches hyps that don't already have the field.

Class assignments are based on the hypothesis ID prefix, validated against
each hyp's claim text. Adds `bug_class: <value>` immediately after the
`severity:` line (or after `class:` if no severity).

Run from repo root:

    python3 scripts/backfill_bug_class.py

The script is idempotent: running it twice produces the same result as
running it once.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Hypothesis ID -> bug_class mapping
# ---------------------------------------------------------------------------
# Specific IDs win over prefix rules; prefix rules apply when no specific
# match. Class names match the BUG_CLASS_SIGNATURES catalog in
# audit_pipeline/commands/propagate.py.

ID_TO_CLASS: dict[str, str] = {
    # ------- percolator_deep.yaml — vault/insurance section (V1-V10) -------
    "V1-vault-residual-conservation":         "vault-balance-divergence",
    "V2-vault-balance-equation":              "vault-balance-divergence",
    "V3-vault-monotonic-on-deposit":          "vault-balance-divergence",
    "V4-vault-cap-respect":                   "vault-balance-divergence",
    "V5-haircut-direction":                   "haircut-direction-violation",
    "V6-insurance-floor":                     "insurance-counter-vault-divergence",
    "V7-insurance-counter-vault-coupling":    "insurance-counter-vault-divergence",
    "V8-cash-locked-conservation":            "vault-balance-divergence",
    "V9-rebate-accumulation-bounded":         "fee-accounting-rounding-asymmetry",
    "V10-claimable-pnl-conservation":         "resolved-state-pnl-leak",

    # ------- PnL / funding / mark (P1-P10) -------
    "P1-pnl-zero-sum":                        "token-balance-conservation-violation",
    "P2-pnl-pos-tot-monotonic":               "arithmetic-overflow-pnl-mark",
    "P3-pnl-matured-bound":                   "arithmetic-overflow-pnl-mark",
    "P4-funding-rate-mark-bias":              "funding-rate-self-bias",
    "P5-funding-payment-zero-sum":            "token-balance-conservation-violation",
    "P6-mark-ewma-bound":                     "funding-rate-self-bias",
    "P7-pnl-on-side-flip":                    "arithmetic-overflow-pnl-mark",
    "P8-self-trade-cash-flow":                "self-trade-cash-flow-violation",
    "P9-pnl-arithmetic-bounds":               "arithmetic-overflow-pnl-mark",
    "P10-funding-index-monotonic-modulo-direction": "funding-rate-self-bias",

    # ------- Position / OI / orders (O1-O10) -------
    "O1-position-q-bound":                    "arithmetic-overflow-pnl-mark",
    "O2-oi-conservation":                     "token-balance-conservation-violation",
    "O3-position-authority-binding":          "authorization-bypass",
    "O4-im-respect-on-open":                  "init-state-invariant-violation",
    "O5-mm-trigger-correctness":              "liquidation-incentive-overpayment",
    "O6-side-flip-atomicity":                 "arithmetic-overflow-pnl-mark",
    "O7-position-zero-clears-basis":          "init-state-invariant-violation",
    "O8-cross-margin-equity":                 "arithmetic-overflow-pnl-mark",
    "O9-position-bedge-correct":              "arithmetic-overflow-pnl-mark",
    "O10-orderbook-side-balance":             "token-balance-conservation-violation",

    # ------- Liquidation / keeper crank (L1-L10) -------
    "L1-liquidation-discount-bounded":        "liquidation-incentive-overpayment",
    "L2-liquidation-only-on-mm-breach":       "liquidation-incentive-overpayment",
    "L3-keeper-crank-cursor-budget":          "keeper-cursor-budget-bypass",
    "L4-keeper-authorization-surface":        "authorization-bypass",
    "L5-liquidation-no-fee-enrichment":       "liquidation-incentive-overpayment",
    "L6-force-closure-conditions":            "liquidation-incentive-overpayment",
    "L7-keeper-crank-progress":               "keeper-cursor-budget-bypass",
    "L8-partial-liquidation-correctness":     "liquidation-incentive-overpayment",
    "L9-cascade-liquidation-bound":           "liquidation-incentive-overpayment",
    "L10-liquidation-touch-pairing":          "clock-advance-without-touch",

    # ------- Authorization / admin / CPI (A1-A10) -------
    "A1-permissionless-no-drain":             "authorization-bypass",
    "A2-admin-instructions-signer-check":     "authorization-bypass",
    "A3-cpi-safety":                          "authorization-bypass",
    "A4-token-authority-validation":          "authorization-bypass",
    "A5-pda-derivation-canonicality":         "init-state-invariant-violation",
    "A6-account-discriminator-check":         "init-state-invariant-violation",
    "A7-wrapper-instruction-signer-routing":  "authorization-bypass",
    "A8-multisig-threshold":                  "authorization-bypass",
    "A9-pause-gate-coverage":                 "authorization-bypass",
    "A10-upgrade-authority-frozen":           "authorization-bypass",

    # ------- State / settlement / time (S1-S10) -------
    "S1-init-state-invariants":               "init-state-invariant-violation",
    "S2-resolved-mode-mature-claim":          "resolved-state-pnl-leak",
    "S3-settle-after-close":                  "resolved-state-pnl-leak",
    "S4-touch-account-live-pairing":          "clock-advance-without-touch",
    "S5-market-mode-transitions":             "init-state-invariant-violation",
    "S6-time-monotonic":                      "clock-advance-without-touch",
    "S7-epoch-staleness-gate":                "clock-advance-without-touch",
    "S8-deposit-withdraw-atomicity":          "vault-balance-divergence",
    "S9-cancel-correctness":                  "init-state-invariant-violation",
    "S10-rebate-claim-correctness":           "fee-accounting-rounding-asymmetry",

    # ------- Account GC (AC1-AC8) -------
    "AC1-account-gc-state-leak":              "account-gc-state-leak",
    "AC2-materialize-fresh-state":            "clock-advance-without-touch",
    "AC3-touch-idempotent":                   "clock-advance-without-touch",
    "AC4-free-only-on-zero-position":         "account-gc-state-leak",
    "AC5-account-capital-conservation":       "token-balance-conservation-violation",
    "AC6-slot-reuse-no-aliasing":             "account-gc-state-leak",
    "AC7-account-bound-authority":            "authorization-bypass",
    "AC8-account-zeroing-on-close":           "account-gc-state-leak",

    # ------- Arithmetic (AR1-AR8) -------
    "AR1-mul-div-floor-no-overflow":          "arithmetic-overflow-pnl-mark",
    "AR2-pnl-delta-i128-bound":               "arithmetic-overflow-pnl-mark",
    "AR3-funding-rate-bounds":                "arithmetic-overflow-pnl-mark",
    "AR4-catchup-no-overflow":                "arithmetic-overflow-pnl-mark",
    "AR5-fee-calc-overflow":                  "arithmetic-overflow-pnl-mark",
    "AR6-square-root-bounds":                 "arithmetic-overflow-pnl-mark",
    "AR7-saturating-arithmetic-correctness":  "arithmetic-overflow-pnl-mark",
    "AR8-rounding-direction":                 "fee-accounting-rounding-asymmetry",

    # ------- Instruction validation (IX1-IX10) -------
    "IX1-ix-data-validation":                 "init-state-invariant-violation",
    "IX2-account-list-length-check":          "init-state-invariant-violation",
    "IX3-rent-exemption-check":               "init-state-invariant-violation",
    "IX4-clock-sysvar-required":              "init-state-invariant-violation",
    "IX5-no-arbitrary-cpi":                   "authorization-bypass",
    "IX6-account-owner-check":                "authorization-bypass",
    "IX7-readonly-vs-writable-correctness":   "authorization-bypass",
    "IX8-replay-protection":                  "init-state-invariant-violation",
    "IX9-compute-budget-respect":             "init-state-invariant-violation",
    "IX10-error-codes-distinct":              "init-state-invariant-violation",

    # ------- Cross-instruction (CI1-CI10) -------
    "CI1-deposit-then-withdraw-zero":         "init-state-invariant-violation",
    "CI2-double-touch-no-drift":              "clock-advance-without-touch",
    "CI3-fill-then-cancel-impossible":        "init-state-invariant-violation",
    "CI4-self-trade-net-zero":                "self-trade-cash-flow-violation",
    "CI5-cross-market-isolation":             "init-state-invariant-violation",
    "CI6-batch-instruction-atomicity":        "init-state-invariant-violation",
    "CI7-wrapper-instruction-equivalence":    "init-state-invariant-violation",
    "CI8-flash-fill-impossible":              "flash-loan-repayment-bypass",
    "CI9-orderbook-depth-bound":              "init-state-invariant-violation",
    "CI10-resolution-final":                  "resolved-state-pnl-leak",

    # ------- Reorg / consensus (R1-R5) -------
    "R1-reorg-resilience":                    "init-state-invariant-violation",
    "R2-deterministic-fill-matching":         "init-state-invariant-violation",
    "R3-finality-gate":                       "init-state-invariant-violation",
    "R4-leader-rotation-safety":              "init-state-invariant-violation",
    "R5-rpc-staleness-tolerance":             "clock-advance-without-touch",

    # ------- percolator_bounty_regression.yaml (BR-* prefix) -------
    "BR-F7-helper-conservation":              "insurance-counter-vault-divergence",
    "BR-cascade-bypass-tradenocpi":           "clock-advance-without-touch",
    "BR-cascade-bypass-tradecpi-zerofill":    "clock-advance-without-touch",
    "BR-cascade-bypass-keeper-crank-elsebranch": "clock-advance-without-touch",
    "BR-cascade-bypass-liquidate-decoy":      "clock-advance-without-touch",
    "BR-cascade-bypass-withdrawal":           "clock-advance-without-touch",
    "BR-sweepgap-k-drift":                    "funding-rate-self-bias",
    "BR-funding-k-walk-no-oracle":            "funding-rate-self-bias",
    "BR-cursor-wrap-consumption-reset":       "keeper-cursor-budget-bypass",
    "BR-keeper-reward-redirect-attacker":     "liquidation-incentive-overpayment",
    "BR-keeper-reward-zero-on-populated":     "keeper-cursor-budget-bypass",
    "BR-catchup-zero-funding-skip-envelope":  "clock-advance-without-touch",
    "BR-catchup-partial-rollback-monotonicity": "clock-advance-without-touch",
    "BR-no-oracle-wrapper-paths":             "init-state-invariant-violation",
    "BR-wrapper-stair-step-divergence":       "funding-rate-self-bias",
    "BR-trade-oversized-size-panic":          "arithmetic-overflow-pnl-mark",
    "BR-defer-phase2-rr-denial":              "keeper-cursor-budget-bypass",
    "BR-tradecpi-is-signer-forward":          "authorization-bypass",

    # ------- percolator_strict_helper_class.yaml (SH-* prefix) -------
    # F7-derived siblings — the strict-helper coverage class.
    # SH1+SH2 introduce the new class `accrual-helper-asymmetry`;
    # SH3+SH4 introduce `k-walk-accumulation`. Both new classes are
    # registered in BUG_CLASS_SIGNATURES via a parallel patch.
    "SH1-strict-helper-coverage":             "accrual-helper-asymmetry",
    "SH2-withdraw-collateral-helper-choice":  "accrual-helper-asymmetry",
    "SH3-k-walk-via-oracle-rejected":         "k-walk-accumulation",
    "SH4-k-walk-via-funding-rejected":        "k-walk-accumulation",
    "SH5-keeper-crank-touching-completeness": "clock-advance-without-touch",
    "SH6-resolve-flat-negative-gate":         "insurance-counter-vault-divergence",
    "SH7-mark-ewma-update-rate-cap":          "funding-rate-self-bias",
    "SH8-trade-cpi-band-check-tightness":     "init-state-invariant-violation",
    "SH9-stuck-target-accrual-rejection":     "clock-advance-without-touch",
    "SH10-cpi-matcher-state-writes-isolated": "authorization-bypass",
    "SH11-self-matched-pair-cannot-walk-K":   "self-trade-cash-flow-violation",
    "SH12-insurance-drain-via-resolve-flat-negative": "insurance-counter-vault-divergence",
}


# ---------------------------------------------------------------------------
# YAML surgery — string-based, preserves comments + formatting
# ---------------------------------------------------------------------------

# Pattern: an entry block starts with "  - id: <X>", continues until the
# next "  - id:" or end of file. We find the entry's `severity:` line (or
# `class:` if no severity), and insert `    bug_class: <value>\n` after it.

ID_LINE_RE     = re.compile(r"^  - id: (\S+)\s*$")
BUG_CLASS_RE   = re.compile(r"^    bug_class:")
SEVERITY_RE    = re.compile(r"^    severity:")
CLASS_RE       = re.compile(r"^    class:")


def patch_file(path: Path) -> tuple[int, int, list[str]]:
    """Return (n_patched, n_already_set, unmapped_ids)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n_patched = 0
    n_already = 0
    unmapped: list[str] = []

    while i < len(lines):
        line = lines[i]
        m = ID_LINE_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue

        # Found an id line. Walk forward to the boundary (next id line or EOF).
        hyp_id = m.group(1)
        out.append(line)
        i += 1

        block_start = i
        # Find the boundary
        j = i
        while j < len(lines) and not ID_LINE_RE.match(lines[j]):
            j += 1
        block = lines[block_start:j]

        # Already has bug_class?
        if any(BUG_CLASS_RE.match(ln) for ln in block):
            n_already += 1
            out.extend(block)
            i = j
            continue

        # Look up bug_class assignment
        bug_class = ID_TO_CLASS.get(hyp_id)
        if not bug_class:
            unmapped.append(hyp_id)
            out.extend(block)
            i = j
            continue

        # Find insertion point: after `severity:` or after `class:`.
        # Insert as `    bug_class: <value>\n`
        inserted = False
        new_block: list[str] = []
        for ln in block:
            new_block.append(ln)
            if not inserted and SEVERITY_RE.match(ln):
                new_block.append(f"    bug_class: {bug_class}\n")
                inserted = True
        if not inserted:
            # Fall through: insert after `class:` if no severity
            new_block = []
            for ln in block:
                new_block.append(ln)
                if not inserted and CLASS_RE.match(ln):
                    new_block.append(f"    bug_class: {bug_class}\n")
                    inserted = True

        if inserted:
            n_patched += 1
            out.extend(new_block)
        else:
            # Couldn't find an anchor — skip
            unmapped.append(f"{hyp_id} (no anchor)")
            out.extend(block)
        i = j

    if n_patched > 0:
        path.write_text("".join(out), encoding="utf-8")

    return n_patched, n_already, unmapped


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    yaml_dir = repo / "src" / "audit_pipeline" / "templates" / "hypotheses"
    if not yaml_dir.is_dir():
        print(f"missing dir: {yaml_dir}", file=sys.stderr)
        return 2

    targets = sorted(yaml_dir.glob("*.yaml"))
    grand_patched = 0
    grand_unmapped: list[tuple[str, list[str]]] = []
    for path in targets:
        n, already, unmapped = patch_file(path)
        if n or already or unmapped:
            print(f"{path.name}: patched={n}, already_set={already}, unmapped={len(unmapped)}")
            if unmapped:
                grand_unmapped.append((path.name, unmapped))
        grand_patched += n

    print(f"\nTotal patched: {grand_patched}")
    if grand_unmapped:
        print("\n--- Unmapped IDs (need entries in ID_TO_CLASS) ---")
        for fname, ids in grand_unmapped:
            print(f"\n{fname}:")
            for hid in ids:
                print(f"  {hid}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
