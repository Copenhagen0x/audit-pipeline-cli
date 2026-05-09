"""Per-bug-class bundle templates.

Templates structure the LLM patch-authorship task: instead of "write a
fix for this bug," the LLM gets a much narrower task ("fill in this
auth-bypass template"). Narrower task = lower failure rate = safer
patches.

Each template defines:

  * `headline`         — short phrase used in PR title / commit subject
  * `writeup_skeleton` — markdown sections the writeup must contain
  * `patch_intent`     — sentence describing the *shape* of the fix the
                         LLM should produce (NOT the actual fix — that's
                         drawn from the PoC + bug description)
  * `verification_hints` — extra checks the verifier should run for this
                         class (e.g. "rerun the K/F invariant proof")

A bug class without a registered template falls back to GENERIC_TEMPLATE.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BundleTemplate:
    headline: str
    writeup_skeleton: str
    patch_intent: str
    verification_hints: tuple[str, ...]


GENERIC_TEMPLATE = BundleTemplate(
    headline="Fix for {finding_id}: {title}",
    writeup_skeleton=(
        "# Root cause\n\n"
        "<2-4 paragraph description of the structural defect>\n\n"
        "# Reproducer\n\n"
        "<reference to the PoC test that triggers the bug>\n\n"
        "# Fix\n\n"
        "<one-paragraph description of how the patch resolves the defect>\n\n"
        "# Verification\n\n"
        "<list of checks: PoC fails pre-patch, passes post-patch, existing "
        "tests still pass, Kani re-proves any relevant invariant>\n"
    ),
    patch_intent=(
        "Apply the minimal scoped change that makes the PoC test stop "
        "triggering the assertion. Do not modify functions other than "
        "the one the PoC exercises. Do not add new dependencies. Do "
        "not change function signatures."
    ),
    verification_hints=(
        "rerun PoC pre-patch (must fail)",
        "rerun PoC post-patch (must pass)",
        "rerun full test suite (must pass)",
    ),
)


# Per-class templates. Add entries as new bug classes ship a verified
# fix shape. The class name MUST match BUG_CLASS_SIGNATURES keys.
BUNDLE_TEMPLATES: dict[str, BundleTemplate] = {
    "insurance-counter-vault-divergence": BundleTemplate(
        headline="Fix vault-counter divergence on insurance absorption ({finding_id})",
        writeup_skeleton=(
            "# Root cause: counter-vault decoupling\n\n"
            "The insurance-fund counter and the underlying vault counter "
            "become decoupled when {trigger}. Specifically, "
            "`{handler_function}` mutates `insurance_fund.balance` without "
            "the paired vault debit, so the residual `vault - c_tot - "
            "insurance` grows by the absorbed amount.\n\n"
            "# Reproducer\n\n"
            "PoC at `{poc_path}` constructs the precondition state, fires "
            "the entrypoint that triggers the divergence, and asserts "
            "`post_residual == pre_residual`. Pre-patch: assertion fails "
            "by `{absorbed_amount}`. Post-patch: assertion holds.\n\n"
            "# Fix\n\n"
            "Add the paired vault debit inside `{handler_function}` so "
            "`insurance_fund.balance -= delta` is mirrored by "
            "`vault.set(vault.get() - delta)` in the same instruction. "
            "Order matters: vault debit must occur after the insurance "
            "decrement and before any risk gate.\n\n"
            "# Verification\n\n"
            "* PoC at `{poc_path}` fails pre-patch by `{absorbed_amount}`\n"
            "* PoC passes post-patch (residual conserved)\n"
            "* Existing test suite passes (no regression)\n"
            "* Kani harness `prove_residual_conservation` (if registered) "
            "  re-verifies the invariant\n"
        ),
        patch_intent=(
            "Inside the handler function the PoC exercises, after the "
            "insurance balance decrement and before any risk-gate return, "
            "add a matching debit to the vault counter so vault delta == "
            "insurance delta. Do not modify the insurance decrement itself. "
            "Do not change any function signature."
        ),
        verification_hints=(
            "rerun PoC: residual conservation must hold post-patch",
            "rerun K/F invariant proofs if registered for this engine",
            "rerun full cargo test suite",
        ),
    ),

    "vault-balance-divergence": BundleTemplate(
        headline="Fix vault balance divergence ({finding_id})",
        writeup_skeleton=(
            "# Root cause\n\nVault counter mutated without paired user-balance "
            "update (or vice versa).\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` shows the asymmetric mutation.\n\n"
            "# Fix\n\nAdd the missing paired update inside the same instruction.\n\n"
            "# Verification\n\nPoC fails pre-patch, passes post-patch, full test "
            "suite passes.\n"
        ),
        patch_intent=(
            "Add the missing paired counter update inside the same instruction "
            "the PoC exercises. Scope: only the function the PoC fires."
        ),
        verification_hints=(
            "rerun PoC",
            "rerun full test suite",
        ),
    ),

    "authorization-bypass": BundleTemplate(
        headline="Fix authorization bypass ({finding_id})",
        writeup_skeleton=(
            "# Root cause: missing signer check\n\n"
            "Privileged path `{handler_function}` reachable without the "
            "expected signer / authority check. PoC demonstrates a "
            "non-privileged caller successfully invoking the path.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nAdd the missing signer / authority check at the entry "
            "of `{handler_function}`, before any state mutation. The check "
            "should match the canonical signer pattern used elsewhere in "
            "the program.\n\n"
            "# Verification\n\nPoC's unprivileged-caller flow now returns an "
            "error variant. Existing tests continue to pass.\n"
        ),
        patch_intent=(
            "Add the missing signer / authority check at function entry. "
            "Use the same pattern the rest of the program uses for "
            "privileged paths. Don't widen or narrow the privilege "
            "model — match existing convention exactly."
        ),
        verification_hints=(
            "rerun PoC: unprivileged caller must now fail",
            "rerun privileged-caller test (must still succeed)",
            "rerun full test suite",
        ),
    ),

    "arithmetic-overflow-pnl-mark": BundleTemplate(
        headline="Fix arithmetic overflow on PnL/mark ({finding_id})",
        writeup_skeleton=(
            "# Root cause: unchecked arithmetic\n\n"
            "Function `{handler_function}` performs unchecked arithmetic on "
            "`{value_type}` accumulators that can overflow under "
            "{trigger_conditions}.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` constructs the overflow "
            "boundary case.\n\n"
            "# Fix\n\nReplace the unchecked operation with `checked_*` "
            "(or saturating, depending on the engine's documented "
            "behavior). Return an explicit error variant on overflow.\n\n"
            "# Verification\n\nPoC's overflow case now returns Err. "
            "Normal-range cases unaffected. Existing tests pass.\n"
        ),
        patch_intent=(
            "Replace the specific arithmetic operation the PoC exercises "
            "with `checked_add` / `checked_mul` / `checked_sub` (or the "
            "engine's existing safe-math wrapper if one exists). Return "
            "an error on None. Do not change unrelated arithmetic in the "
            "same function."
        ),
        verification_hints=(
            "rerun PoC: overflow case must error",
            "rerun normal-range tests (must still pass)",
            "rerun full test suite",
        ),
    ),
}


def template_for(bug_class: str) -> BundleTemplate:
    """Return the registered template for bug_class, or GENERIC_TEMPLATE."""
    return BUNDLE_TEMPLATES.get(bug_class, GENERIC_TEMPLATE)
