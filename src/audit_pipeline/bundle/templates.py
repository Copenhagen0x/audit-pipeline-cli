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

    # ───────── Solana / Anchor templates ─────────

    "rounding-favors-user": BundleTemplate(
        headline="Fix rounding direction favoring user ({finding_id})",
        writeup_skeleton=(
            "# Root cause: rounding direction\n\n"
            "`{handler_function}` rounds in the direction that benefits the "
            "user (floor on amount-out / ceil on amount-in), allowing a "
            "drip extraction across repeated calls.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` shows N small operations "
            "extracting more than the fair-share total.\n\n"
            "# Fix\n\nReverse the rounding direction in the specific "
            "div/mul/shift that the PoC exercises so the protocol always "
            "rounds against the user (ceil on amount-in, floor on amount-out).\n\n"
            "# Verification\n\nPoC's drip-extraction case yields <= fair "
            "share post-patch. Normal-magnitude swaps unaffected.\n"
        ),
        patch_intent=(
            "Reverse the rounding direction of the specific div/mul the PoC "
            "exercises. Use `div_ceil` / explicit `(a + b - 1) / b` for "
            "amounts the user pays; floor for amounts the user receives. "
            "Do not change unrelated math in the same function."
        ),
        verification_hints=(
            "rerun PoC: drip-extraction must net <= fair share",
            "rerun standard swap tests (slippage unchanged)",
            "rerun full test suite",
        ),
    ),

    "oracle-staleness": BundleTemplate(
        headline="Reject stale oracle prices ({finding_id})",
        writeup_skeleton=(
            "# Root cause: oracle staleness not enforced\n\n"
            "`{handler_function}` consumes an oracle price without checking "
            "the publish slot/timestamp against the current clock. Stale "
            "prices from prior epochs feed into liquidation / mark / borrow "
            "decisions.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` advances the clock past the "
            "oracle's last publish slot and demonstrates the handler still "
            "accepts the stale price.\n\n"
            "# Fix\n\nBefore consuming the price, compare "
            "`clock.slot - oracle.last_publish_slot` to a documented "
            "`MAX_ORACLE_AGE_SLOTS` constant and return `OracleStale` if "
            "exceeded.\n\n"
            "# Verification\n\nStaleness boundary now rejects. Fresh-price "
            "tests unchanged.\n"
        ),
        patch_intent=(
            "Add a slot-age check at the read site: `require!(clock.slot - "
            "oracle.last_publish_slot <= MAX_ORACLE_AGE_SLOTS, "
            "OracleStale)`. Reuse any existing staleness constant in the "
            "program. Do not change the oracle struct or downstream math."
        ),
        verification_hints=(
            "rerun PoC: stale-slot path must error with OracleStale",
            "rerun fresh-price tests (must still pass)",
            "rerun full test suite",
        ),
    ),

    "clock-decrease": BundleTemplate(
        headline="Enforce monotonic clock on time-windowed state ({finding_id})",
        writeup_skeleton=(
            "# Root cause: non-monotonic clock\n\n"
            "`{handler_function}` writes `clock.unix_timestamp` (or slot) "
            "into a stored last-touch field without rejecting decreases, "
            "allowing a regressed clock value to reset cooldowns / windows.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` shows a stored timestamp "
            "going backwards.\n\n"
            "# Fix\n\nReplace direct assignment with `state.last_ts = "
            "max(state.last_ts, clock.unix_timestamp)` and reject any "
            "decrease with `ClockRegression` if the protocol semantics "
            "require strict monotonicity.\n\n"
            "# Verification\n\nRegression case is rejected. Normal "
            "forward-clock cases unchanged.\n"
        ),
        patch_intent=(
            "At the line that writes the timestamp/slot field, replace "
            "direct assignment with a `max` guard or an explicit "
            "require!(new >= old) check. Do not change downstream "
            "computation that uses the field."
        ),
        verification_hints=(
            "rerun PoC: clock regression must be rejected",
            "rerun forward-clock tests (must still pass)",
            "rerun full test suite",
        ),
    ),

    "wrapping-counter": BundleTemplate(
        headline="Replace wrapping arithmetic on security counter ({finding_id})",
        writeup_skeleton=(
            "# Root cause: wrapping_* on security-relevant counter\n\n"
            "`{handler_function}` uses `wrapping_add` / `wrapping_sub` on a "
            "counter that gates a security invariant (cap, reuse-guard, "
            "rate-limit). Wraparound silently rolls the counter to zero and "
            "voids the gate.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` saturates the counter to "
            "u64::MAX (or the relevant width) and demonstrates the next "
            "increment wraps to zero, allowing the gated action to fire "
            "again.\n\n"
            "# Fix\n\nReplace `wrapping_*` with `checked_*` and return "
            "`CounterOverflow` (or the engine's existing overflow error) "
            "on `None`.\n\n"
            "# Verification\n\nBoundary case errors post-patch. Normal "
            "increments unchanged.\n"
        ),
        patch_intent=(
            "At the specific `wrapping_add` / `wrapping_sub` call the PoC "
            "saturates, replace with `checked_add` / `checked_sub` and "
            "`.ok_or(error)`. Do not touch arithmetic elsewhere in the "
            "function."
        ),
        verification_hints=(
            "rerun PoC: counter saturation must now error",
            "rerun under-cap tests (must still pass)",
            "rerun full test suite",
        ),
    ),

    "lazy-init-race": BundleTemplate(
        headline="Initialize state before mutation, not lazily ({finding_id})",
        writeup_skeleton=(
            "# Root cause: lazy init under concurrent mutation\n\n"
            "`{handler_function}` lazily initializes a state field on first "
            "mutation. Two concurrent mutators each see uninitialized state "
            "and double-init, racing on the initial value.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` invokes two paths whose "
            "lazy-init writes the field with different starting points.\n\n"
            "# Fix\n\nMove initialization to the canonical `initialize` "
            "instruction (or add an idempotent `init-if-not-set` guard "
            "using a stored discriminator). Mutators MUST find the field "
            "already initialized.\n\n"
            "# Verification\n\nConcurrent-mutator PoC now produces a "
            "deterministic post-state. Normal-init path unchanged.\n"
        ),
        patch_intent=(
            "Remove the lazy-init branch from the mutator. Either: "
            "(a) move the init to the canonical initialize instruction, "
            "or (b) gate it on a stored discriminator field so only the "
            "first writer initializes. Do not change the mutator's "
            "non-init logic."
        ),
        verification_hints=(
            "rerun PoC: lazy-race must produce deterministic result",
            "rerun first-write tests (must still pass)",
            "rerun full test suite",
        ),
    ),

    "share-rate-missing": BundleTemplate(
        headline="Use exchange rate when minting/burning shares ({finding_id})",
        writeup_skeleton=(
            "# Root cause: shares-assets conflation\n\n"
            "`{handler_function}` mints / burns shares using raw asset "
            "amounts without applying the current `total_assets / "
            "total_shares` exchange rate. Inflation attack possible.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` donates to the vault and "
            "then mints shares 1:1 with assets, capturing the donation.\n\n"
            "# Fix\n\nCompute shares = `assets * total_shares / "
            "total_assets` (rounding down on mint, up on burn). Handle the "
            "initial-deposit edge case (when total_shares == 0) with a "
            "shares := assets fallback (or virtual-shares pattern).\n\n"
            "# Verification\n\nDonation attack neutralized. First-deposit "
            "path still mints assets-equal shares.\n"
        ),
        patch_intent=(
            "Replace the 1:1 share computation with an exchange-rate-aware "
            "version. Round in the protocol's favor (down on user mint, up "
            "on user redeem). Handle total_shares==0 explicitly."
        ),
        verification_hints=(
            "rerun PoC: donation attack must net <= 0 for attacker",
            "rerun first-deposit tests (still 1:1)",
            "rerun full test suite",
        ),
    ),

    "daily-cap-bypass": BundleTemplate(
        headline="Anchor daily cap to absolute slot/day, not relative ({finding_id})",
        writeup_skeleton=(
            "# Root cause: epoch-boundary cap reset\n\n"
            "`{handler_function}` resets a daily cap counter based on a "
            "relative window (last-reset + 86400), letting an attacker "
            "fire near the boundary and again immediately after.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` fires at t=86399 and "
            "t=86400, doubling the cap.\n\n"
            "# Fix\n\nKey the cap by absolute day (`clock.unix_timestamp / "
            "86400`) and reset only when the day index changes. The "
            "counter is bound to the day, not to elapsed-since-last-reset.\n\n"
            "# Verification\n\nBoundary doubling neutralized. Normal "
            "single-day usage unchanged.\n"
        ),
        patch_intent=(
            "Replace the `if now - last_reset > 86400` reset logic with "
            "`day_index = now / 86400; if day_index != stored_day { reset; "
            "stored_day = day_index }`. Do not change the cap value itself."
        ),
        verification_hints=(
            "rerun PoC: boundary-doubling must fail",
            "rerun within-day usage tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "buyer-inversion": BundleTemplate(
        headline="Correct buyer/seller direction in settle ({finding_id})",
        writeup_skeleton=(
            "# Root cause: inverted role assignment\n\n"
            "`{handler_function}` credits the buyer with the seller's "
            "asset (or vice versa) due to a swapped operand on settlement.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` settles a trade and "
            "observes the asset arriving at the wrong party.\n\n"
            "# Fix\n\nSwap the operands at the credit/debit lines so the "
            "buyer receives the asset and the seller receives the quote "
            "(or whichever role mapping the protocol documents).\n\n"
            "# Verification\n\nPost-trade balances match expected role "
            "mapping. Existing inverse-direction tests still pass.\n"
        ),
        patch_intent=(
            "Identify the swapped operands at the specific credit/debit "
            "lines the PoC exercises and correct the direction. Do not "
            "change settlement amounts or fee calculation."
        ),
        verification_hints=(
            "rerun PoC: settlement must credit correct parties",
            "rerun matching-engine tests (still pass)",
            "rerun full test suite",
        ),
    ),

    # ───────── High-frequency Solana classes (broad coverage) ─────────

    "missing-signer-check": BundleTemplate(
        headline="Add missing Signer check ({finding_id})",
        writeup_skeleton=(
            "# Root cause: privileged path without Signer constraint\n\n"
            "`{handler_function}` accepts an `AccountInfo` where it should "
            "require a `Signer`. PoC shows a non-signing caller executing "
            "the privileged action.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nChange the account type in the `#[derive(Accounts)]` "
            "struct from `AccountInfo<'info>` to `Signer<'info>` (or add a "
            "`#[account(signer)]` constraint). The PoC's non-signing flow "
            "now errors at deserialization.\n\n"
            "# Verification\n\nNon-signer path rejected. Signer path "
            "unchanged.\n"
        ),
        patch_intent=(
            "In the Accounts struct, replace `AccountInfo<'info>` with "
            "`Signer<'info>` on the account the PoC controls. Do not "
            "change other accounts or business logic."
        ),
        verification_hints=(
            "rerun PoC: non-signer must fail at account deserialization",
            "rerun signer-flow tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "missing-account-binding": BundleTemplate(
        headline="Bind related accounts via has_one ({finding_id})",
        writeup_skeleton=(
            "# Root cause: missing has_one / address binding\n\n"
            "`{handler_function}` accepts a child account whose parent "
            "field is not constrained, allowing an attacker to substitute "
            "a child account belonging to a different parent.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` shows the substitution.\n\n"
            "# Fix\n\nAdd `has_one = parent` (or "
            "`constraint = child.parent == parent.key()`) to the Anchor "
            "Accounts struct so the child must reference the supplied "
            "parent.\n\n"
            "# Verification\n\nSubstitution rejected. Legitimate "
            "parent-child pair unchanged.\n"
        ),
        patch_intent=(
            "Add the missing `has_one` / address constraint on the "
            "Accounts struct. Use the existing parent-field name. Do not "
            "modify business logic in the handler body."
        ),
        verification_hints=(
            "rerun PoC: mismatched parent must error",
            "rerun matched-pair tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "pda-not-canonical": BundleTemplate(
        headline="Validate PDA seeds + canonical bump ({finding_id})",
        writeup_skeleton=(
            "# Root cause: PDA accepted without canonical-bump validation\n\n"
            "`{handler_function}` derives or accepts a PDA without "
            "validating the seeds and canonical bump, allowing a foreign "
            "PDA at a non-canonical bump.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` supplies a PDA with a "
            "non-canonical bump.\n\n"
            "# Fix\n\nAdd `seeds = [...], bump` to the Anchor Accounts "
            "struct (Anchor verifies canonical bump automatically) OR for "
            "native Rust, call `Pubkey::find_program_address` and assert "
            "equality.\n\n"
            "# Verification\n\nNon-canonical PDA rejected. Canonical "
            "PDA path unchanged.\n"
        ),
        patch_intent=(
            "Add `seeds = [...], bump` to the PDA account in the "
            "`#[derive(Accounts)]` struct. Use the program's existing "
            "seed convention. Do not change handler logic."
        ),
        verification_hints=(
            "rerun PoC: non-canonical PDA must fail",
            "rerun canonical-PDA tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "missing-program-ownership": BundleTemplate(
        headline="Enforce program ownership on account ({finding_id})",
        writeup_skeleton=(
            "# Root cause: account ownership not checked\n\n"
            "`{handler_function}` reads / writes an account without "
            "verifying its `owner` matches the expected program, "
            "permitting accounts owned by malicious programs.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nDeclare the account as a typed Anchor `Account<T>` "
            "(which auto-checks ownership) or, in native Rust, add "
            "`assert_eq!(account.owner, &expected_program_id)`.\n\n"
            "# Verification\n\nForeign-owner account rejected.\n"
        ),
        patch_intent=(
            "Replace `UncheckedAccount` / `AccountInfo` with the typed "
            "`Account<'info, T>` wrapper, or add explicit owner check. "
            "Use the program's existing owner-check pattern."
        ),
        verification_hints=(
            "rerun PoC: foreign-owner account must error",
            "rerun typed-owner tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "auth-from-untrusted-source": BundleTemplate(
        headline="Move auth check from instruction data to Signer ({finding_id})",
        writeup_skeleton=(
            "# Root cause: auth derived from instruction args\n\n"
            "`{handler_function}` reads an admin pubkey or role flag "
            "from instruction data (or an attacker-writable account "
            "field) and trusts it. PoC supplies arbitrary auth claim.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nRemove the instruction-data auth path. Require the "
            "admin signer via `Signer<'info>` + an address constraint "
            "(`constraint = admin.key() == config.admin`).\n\n"
            "# Verification\n\nAttacker-supplied admin claim rejected.\n"
        ),
        patch_intent=(
            "Replace instruction-data auth with a signer-based check. "
            "The signer must satisfy a stored-admin constraint."
        ),
        verification_hints=(
            "rerun PoC: spoofed-admin call must fail",
            "rerun real-admin tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "predictable-pda": BundleTemplate(
        headline="Bind PDA seeds to identity, not attacker data ({finding_id})",
        writeup_skeleton=(
            "# Root cause: PDA seed includes attacker-controlled value\n\n"
            "`{handler_function}` derives a PDA whose seeds include a "
            "field the attacker writes (nonce, ID, etc.), letting them "
            "collide with other users' PDAs.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nSeed the PDA with a value the attacker cannot "
            "control: the user's pubkey, an authority pubkey, or a "
            "protocol-issued unique ID stored at init time.\n\n"
            "# Verification\n\nCollision attempt fails. Existing "
            "single-user PDA usage unchanged.\n"
        ),
        patch_intent=(
            "Change the seeds vector to include identity-bound bytes "
            "(authority key, etc.) and exclude attacker-controlled "
            "fields. Update the canonical seed convention consistently."
        ),
        verification_hints=(
            "rerun PoC: collision attack must fail",
            "rerun benign-user PDA tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "oracle-zero": BundleTemplate(
        headline="Reject zero / negative oracle price ({finding_id})",
        writeup_skeleton=(
            "# Root cause: zero price accepted\n\n"
            "`{handler_function}` does not reject `price == 0` from the "
            "oracle, leading to division-by-zero, unbounded mint amounts, "
            "or free borrows.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nAdd `require!(price > 0, OracleZero)` (and `price > "
            "MIN_PRICE` if the protocol documents a floor) at the price "
            "read site.\n\n"
            "# Verification\n\nZero/negative path rejected.\n"
        ),
        patch_intent=(
            "Insert price-floor check immediately after the oracle read. "
            "Reject with explicit error variant."
        ),
        verification_hints=(
            "rerun PoC: zero-price path must fail",
            "rerun normal-price tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "cast-truncation": BundleTemplate(
        headline="Use checked cast, not `as` ({finding_id})",
        writeup_skeleton=(
            "# Root cause: silent truncating cast\n\n"
            "`{handler_function}` casts a wider integer to a narrower "
            "type with `as`, silently truncating high bits. PoC "
            "constructs the high-bit value.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}`.\n\n"
            "# Fix\n\nReplace the `as` cast with `u64::try_from(value)"
            ".map_err(|_| CastOverflow)?` (or the analogous typed "
            "conversion).\n\n"
            "# Verification\n\nTruncation case rejected.\n"
        ),
        patch_intent=(
            "Replace the specific `as` cast the PoC exercises with a "
            "`try_from` / `try_into` and propagate the error."
        ),
        verification_hints=(
            "rerun PoC: truncation must now error",
            "rerun in-range tests (still pass)",
            "rerun full test suite",
        ),
    ),

    # ───────── Solidity / EVM templates ─────────

    "reentrancy-cei-violation": BundleTemplate(
        headline="Apply CEI ordering ({finding_id})",
        writeup_skeleton=(
            "# Root cause: state writes after external call\n\n"
            "`{handler_function}` performs an external token transfer "
            "BEFORE decrementing the caller's accounting state. A "
            "malicious token (ERC777 with `_callTokensReceived`, fee-"
            "on-transfer, rebase with hooks) can re-enter the function "
            "while the caller's balance is still full and drain the "
            "contract.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` deploys a malicious "
            "token that re-enters during transfer.\n\n"
            "# Fix\n\nMove ALL state writes (balance / supply / nonce) "
            "BEFORE the external transfer. Optionally add OpenZeppelin's "
            "`ReentrancyGuard` `nonReentrant` modifier as defense in "
            "depth.\n\n"
            "# Verification\n\nReentrant call sees decremented state, "
            "fails the balance check.\n"
        ),
        patch_intent=(
            "Reorder the function body so state mutations on caller / "
            "totals run before any external `transfer` / `call` / "
            "`approve`. Do not change the transfer logic itself."
        ),
        verification_hints=(
            "rerun PoC: reentrant attack must fail",
            "rerun benign-token tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "share-inflation": BundleTemplate(
        headline="Block first-depositor share inflation ({finding_id})",
        writeup_skeleton=(
            "# Root cause: floor division on donation-inflatable balance\n\n"
            "`{handler_function}` computes shares as "
            "`(totalShares * amount) / asset.balanceOf(this)`. An "
            "attacker who is the first depositor donates a large "
            "amount directly to the vault address, then any victim "
            "deposit smaller than the donation rounds to 0 shares — "
            "the victim's principal is confiscated.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` performs the donation "
            "+ victim-deposit sequence.\n\n"
            "# Fix\n\nApply one of the canonical mitigations: (a) "
            "permanent virtual shares burned at deployment (OZ ERC4626 "
            "decimalsOffset pattern), (b) revert when computed shares "
            "round to 0, or (c) bootstrap with a permanently-locked "
            "first deposit. Option (b) is the smallest scoped fix.\n\n"
            "# Verification\n\nDonation attack now reverts on victim "
            "deposit.\n"
        ),
        patch_intent=(
            "Add a `require(shares != 0, ZeroShares)` check immediately "
            "after the `toShares` computation. Keep the rest of deposit "
            "untouched."
        ),
        verification_hints=(
            "rerun PoC: donation attack must revert",
            "rerun normal-deposit tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "missing-access-control": BundleTemplate(
        headline="Add missing access-control modifier ({finding_id})",
        writeup_skeleton=(
            "# Root cause: privileged setter callable by anyone\n\n"
            "`{handler_function}` mutates state that gates "
            "authorization or pricing decisions but lacks the "
            "`onlyOwner` (or equivalent role) modifier used by every "
            "other setter on the contract.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` calls the function as "
            "an unprivileged signer.\n\n"
            "# Fix\n\nAdd the `onlyOwner` (or `onlyRole(ADMIN_ROLE)`) "
            "modifier to the function declaration, matching the "
            "pattern used by sibling setters on the same contract.\n\n"
            "# Verification\n\nUnprivileged caller now reverts with "
            "`Unauthorized`.\n"
        ),
        patch_intent=(
            "Add the canonical access-control modifier to the function "
            "signature. Do not change the body."
        ),
        verification_hints=(
            "rerun PoC: unprivileged caller must revert",
            "rerun owner-flow tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "overcharge-on-cap": BundleTemplate(
        headline="Use capped amount for transfer ({finding_id})",
        writeup_skeleton=(
            "# Root cause: cap computed but uncapped value transferred\n\n"
            "`{handler_function}` computes a capped `applied = "
            "min(amount, max)` for accounting, but the subsequent "
            "`transferFrom(msg.sender, …, amount)` pulls the UNCAPPED "
            "amount. The excess is stuck in the contract with no "
            "recovery path; the user overpays.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` repays with an amount "
            "greater than outstanding debt.\n\n"
            "# Fix\n\nReplace the uncapped `amount` argument in the "
            "transfer with the capped `applied` variable already "
            "computed two lines above.\n\n"
            "# Verification\n\nUser pays exactly `min(amount, debt)`; "
            "excess is no longer pulled.\n"
        ),
        patch_intent=(
            "Change the `transferFrom` (or `transfer`) argument from "
            "the raw input amount to the locally-capped variable. Only "
            "the one transfer call should change."
        ),
        verification_hints=(
            "rerun PoC: overpayment case must transfer exactly `min`",
            "rerun exact-amount tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "tx-origin-auth": BundleTemplate(
        headline="Replace `tx.origin` with `msg.sender` ({finding_id})",
        writeup_skeleton=(
            "# Root cause: tx.origin used for authentication\n\n"
            "`{handler_function}` authenticates with `tx.origin == "
            "owner`. An attacker who tricks the owner into calling ANY "
            "function on a malicious contract can have that contract "
            "invoke this function — `tx.origin` is still the owner.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` deploys a malicious "
            "proxy contract and has the victim-as-owner call it.\n\n"
            "# Fix\n\nReplace `tx.origin == owner` with `msg.sender == "
            "owner` (or apply the contract's existing `onlyOwner` "
            "modifier). Never use `tx.origin` for auth.\n\n"
            "# Verification\n\nMalicious-proxy attack now reverts.\n"
        ),
        patch_intent=(
            "Replace `tx.origin` with `msg.sender` in the auth check. "
            "If an `onlyOwner` modifier exists on sibling functions, "
            "use it for consistency."
        ),
        verification_hints=(
            "rerun PoC: proxy-attack must revert",
            "rerun direct-owner-call tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "flash-loan-governance": BundleTemplate(
        headline="Snapshot vote weight at proposal-creation ({finding_id})",
        writeup_skeleton=(
            "# Root cause: vote weight read live from balanceOf\n\n"
            "`{handler_function}` reads voter weight as "
            "`token.balanceOf(msg.sender)` at vote time. An attacker "
            "flash-loans the governance token, votes with arbitrary "
            "weight, and repays in the same transaction — passing any "
            "proposal regardless of legitimate holdings.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` exercises the flash-"
            "loan / vote / repay sequence in a single tx.\n\n"
            "# Fix\n\nSnapshot the token balance at proposal creation "
            "(or use OpenZeppelin's `ERC20Votes` with checkpoint-based "
            "`getPastVotes(proposalSnapshot)`). The vote weight read "
            "must come from a state SNAPSHOT BEFORE the proposal "
            "exists.\n\n"
            "# Verification\n\nFlash-loaned tokens now provide zero "
            "voting weight.\n"
        ),
        patch_intent=(
            "Replace the `balanceOf(msg.sender)` read with a "
            "checkpoint lookup keyed by the proposal's "
            "`snapshotBlock`. Add the checkpointing on the token "
            "contract if it doesn't exist."
        ),
        verification_hints=(
            "rerun PoC: flash-loan vote must accrue 0 weight",
            "rerun legitimate-holder vote tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "signature-replay": BundleTemplate(
        headline="Bind signed digest to nonce + chainId + deadline ({finding_id})",
        writeup_skeleton=(
            "# Root cause: signed digest lacks nonce / chainId / deadline\n\n"
            "`{handler_function}` recovers a signer from a digest that "
            "does NOT include a per-account nonce, the chainId, or a "
            "deadline. Any valid signature can be replayed forever AND "
            "replayed on any forked chain with the same contract "
            "address (CREATE2 / L2 / hard-fork risk).\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` submits the same "
            "signed message twice.\n\n"
            "# Fix\n\nAdopt EIP-712 with a domain separator that "
            "includes `block.chainid` + this contract's address, a "
            "per-account `nonces[account]` mapping incremented on "
            "use, and an explicit `deadline` parameter checked "
            "against `block.timestamp`.\n\n"
            "# Verification\n\nReplay attempt reverts on the nonce "
            "check; cross-chain replay reverts on the domain mismatch.\n"
        ),
        patch_intent=(
            "Replace the `keccak256(abi.encodePacked(...))` digest "
            "with an EIP-712 typed-data hash including chainId, "
            "nonce, and deadline. Bump nonce on successful verify."
        ),
        verification_hints=(
            "rerun PoC: second use of same sig must revert",
            "rerun fresh-sig tests (still pass)",
            "rerun full test suite",
        ),
    ),

    "dos-on-batch-transfer": BundleTemplate(
        headline="Use pull-based claim, not push batch ({finding_id})",
        writeup_skeleton=(
            "# Root cause: batch transfer reverts on single failure\n\n"
            "`{handler_function}` loops over recipients and reverts "
            "the entire batch if any single `transfer` returns false "
            "(or a recipient contract reverts on receive). One "
            "griefing recipient — denylisted address, contract with "
            "reverting fallback, or token with blocklist — freezes "
            "the whole distribution.\n\n"
            "# Reproducer\n\nPoC at `{poc_path}` adds a malicious "
            "recipient and shows the batch reverts.\n\n"
            "# Fix\n\nReplace push-based distribution with a pull-"
            "based claim: store per-recipient pending balances in a "
            "mapping; recipients call `claim()` themselves. A failed "
            "claim affects only that recipient, not the batch.\n\n"
            "# Verification\n\nGriefer no longer DoSes other "
            "recipients.\n"
        ),
        patch_intent=(
            "Replace the loop with `pending[recipient] += amount` "
            "assignments + a new `claim()` function recipients call. "
            "Keep the funder-side semantics identical."
        ),
        verification_hints=(
            "rerun PoC: griefer no longer blocks other claims",
            "rerun normal-claim tests (still pass)",
            "rerun full test suite",
        ),
    ),
}


def template_for(bug_class: str) -> BundleTemplate:
    """Return the registered template for bug_class, or GENERIC_TEMPLATE."""
    return BUNDLE_TEMPLATES.get(bug_class, GENERIC_TEMPLATE)
