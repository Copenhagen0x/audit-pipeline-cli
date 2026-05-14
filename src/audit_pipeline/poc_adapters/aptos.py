"""Layer-2 PoC adapter for Aptos Move source repos.

Test framework: ``aptos move test``. The "fired" signal is a Move
unit test that aborts when it should NOT (or runs to completion when
it shouldn't). Move's abort-on-violation discipline gives us a clean
fire detector: a `#[test]` annotated function aborts (= test fails)
when the bug is triggered.

Three idioms the LLM is steered toward:

  * Direct assertion: `assert!(actual == expected, ERR_CONSERVATION)`
    fires when the conservation invariant breaks.
  * Expected-but-absent abort: `#[expected_failure]` annotates a test
    that SHOULD abort; the test fires when the abort doesn't happen
    (Move treats "unexpected success" as a fail).
  * State-after-call: call the function under test, then read state
    + assert it matches the expected invariant.

Move catches: borrow_global without auth, signer-resource binding,
capability leak, u64 overflow as DoS, divide-by-zero, resource leak,
share math direction, oracle staleness, governance flash-loan vote.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.poc_adapters.base import LanguagePocAdapter, PocOutcome

_PSEUDO_PASS_MARKERS = (
    "CANNOT_TEST",
    "// placeholder",
    # NOTE: we intentionally DO NOT match bare "TODO" or "FIXME" — many
    # real Move tests have a `// TODO:` comment for follow-up work and are
    # still exercising the bug. See Phase 1d audit finding C-2.
)


def _detect_move_named_addresses(repo_root: Path) -> dict[str, str]:
    """Parse Move.toml `[addresses]` block and return {name -> hex}.

    Move modules are addressed by a NAMED address declared in the
    package's Move.toml (e.g. `program_b = "0x1"` or `osec = "_"` if
    deferred). Hard-coding `0x0` or `mutatis` in the PoC fails the
    compiler when the actual address has a different name. We read
    Move.toml so the LLM's prompt can reference the real names.

    Returns an empty dict if Move.toml is missing or unparseable —
    callers should fall back to `0x0` and treat that as best-effort.
    """
    manifest = repo_root / "Move.toml"
    if not manifest.is_file():
        return {}
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    # Find the [addresses] section + parse name="hex" or name='hex' pairs
    section = re.search(
        r"^\s*\[addresses\][^\[]*",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        return {}
    pairs = re.findall(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]",
        section.group(0),
        re.MULTILINE,
    )
    return dict(pairs)


class AptosAdapter(LanguagePocAdapter):
    """Aptos Move PoC adapter (`aptos move test`)."""

    language = "aptos"
    test_file_extension = ".move"
    framework = "aptos-cli"

    def build_author_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        target_file = hyp.get("target_file", "")
        engine_function = hyp.get("engine_function", "")
        relevant = hyp.get("relevant_instructions") or ""

        # Parse the target repo's Move.toml so the LLM uses the actual
        # named addresses (not a hardcoded `mutatis` / `0x0`).
        named_addrs = _detect_move_named_addresses(target_repo_root)
        if named_addrs:
            addr_lines = "\n".join(
                f"  {name} = \"{val}\""
                for name, val in named_addrs.items()
            )
            addr_block = (
                "The target package declares the following named addresses "
                "in Move.toml (use these in your `module <name>::...` "
                "declarations):\n\n"
                f"{addr_lines}\n"
            )
        else:
            addr_block = (
                "Could not read Move.toml — fall back to `0x0` for the test "
                "module address. If the compiler rejects it, the runtime "
                "harness will surface that as a compile error.\n"
            )

        return f"""You are authoring a Layer-2 Proof-of-Concept Aptos Move test for the Jelleo audit engine.

Your test will be run with:

  aptos move test --filter <test_function_name> --package-dir {target_repo_root}

The test FIRES (= bug confirmed) when ANY of these happen:
  * An explicit `assert!(...)` fails (abort with non-zero code)
  * A `#[test]` function is reached and aborts with an unexpected code
  * `#[expected_failure(abort_code = N)]` annotation is present but
    the test runs to completion without abort

The test PASSES (= bug NOT reachable from your witness state) when
the function runs to completion with all assertions satisfied AND no
abort.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Target file: {target_file}

**Engine function placeholder (DO NOT trust as a real function name):**
`{engine_function}`

⚠️  CRITICAL — read carefully. The `engine_function` value above is a
generic placeholder from a CLASS hypothesis library (e.g. the value
"transfer_admin" or "share_value" is a TEMPLATE name describing the
bug class, NOT a guarantee that a function with that exact name
exists in this codebase). On cycle 20260514-151541 the L2 author
wrote tests calling `vault::share_value(...)` and
`treasury::emergency_drain(...)` based on placeholder names — neither
function exists in the actual source, every test failed to compile,
~$10 of L2 spend was wasted.

**Rule:** before writing the test, scan the Grounded source below
and IDENTIFY the real function in the relevant module that matches
the hypothesis claim. Call THAT function, not the placeholder. If no
real function matches the claim, use the `CANNOT_TEST:` escape hatch
at the bottom of this prompt.

Relevant instructions: {relevant}

# Move-language semantics

* Resources are linear. You must `move` them, can't copy.
* `borrow_global<T>(addr)` reads a resource; `borrow_global_mut<T>(addr)`
  mutates. Both are auth points — check signer identity.
* `signer::address_of(&signer)` returns the caller's address.
* `assert!(cond, code)` aborts with `code` if `cond` is false.
* Integer arithmetic ABORTS on overflow (not wraps). Reachable abort
  on attacker-supplied input = severity High.

* **`acquires` is module-local only.** You CANNOT write
  `acquires <other_module>::<Resource>` in your test module's
  function signature — the Move bytecode verifier rejects it as
  "acquires a resource from another module". Just call the target
  module's `public entry fun` (e.g. `treasury::deposit`,
  `vault::withdraw`) — the framework handles `acquires` internally
  when the call crosses the module boundary. Cycle 20260514-151541
  saw L2 tests fail to compile on this exact mistake — don't repeat it.

* **Only import modules that exist in `sources/`.** Verify each
  `use <addr>::<module>;` you write against the Grounded source
  block below. If the module isn't there, you can't import it.
  Framework imports (`use aptos_framework::*`, `use std::*`) are
  always OK.

* **Addresses are HEX. Only 0-9, a-f, A-F.** Move addresses are
  hexadecimal literals. The lexer rejects ANY non-hex character.
  Cycle 20260514-151541 saw `@0xAT`, `@0xDEBT`, `@0xCOLL`,
  `@0xATTACKER` — all rejected because T, K, L, M, N, O, P, Q,
  R, S, T, U, V, W, X, Y, Z are NOT hex digits.
  ✓ valid:   `@0x42`, `@0xAA`, `@0xBEEF`, `@0xC0DE`, `@0xCAFE`,
             `@0xDEAD`, `@0xFADE`, `@0x100`, `@0x999`
  ✗ invalid: `@0xATTACKER`, `@0xVICTIM`, `@0xUSER`, `@0xDEBT`,
             `@0xCOLL`, `@0xATOKEN`, `@0xTOKEN`
  Use numeric or hex-friendly mnemonics: prefer `@0x42` /
  `@0xAA` / `@0x100` (host), `@0x999` / `@0xBE` / `@0xBAD` (attacker),
  `@0xC0` / `@0xC1` (collateral token), `@0xDE` / `@0xDF` (debt token).

# Grounded source

{source_context}

# Repo layout

* Package manifest: {target_repo_root}/Move.toml
* Source modules:   {target_repo_root}/sources/*.move
* Tests dir:        {target_repo_root}/tests/*.move

# Move.toml named addresses

{addr_block}

# 🔥 ANTI-BULLSHIT CONTRACT (read before authoring — non-negotiable)

The Layer-2 PoC layer exists to provide EMPIRICAL EVIDENCE the bug is
reachable. A test that compiles + runs but never exercises the buggy
code path is WORSE than no test — it inflates the "pass-no-bug" count
and gives downstream triage a false signal.

The following are AUTOMATIC REJECTIONS (the post-cycle gate flags
these as `weak_test` and your output is wasted spend):

1. **No exploration in comments.** If you need to find specific
   inputs that trigger the bug, do that reasoning in your head
   BEFORE writing the test. Code comments documenting the bug are
   fine (1-3 lines max). Pages of math working out divergent cases
   in `// ...` lines while the test body remains trivial = REJECT.
   Cycle 20260514-151541 APT12 wrote 80+ lines of math in comments,
   then `assert!(rate == 0, 0)` with zero borrows — the test passed
   trivially without ever triggering the precision loss. Don't.

2. **Non-trivial inputs required.** The hypothesis describes a bug
   that manifests under SPECIFIC conditions (large amounts, attacker
   signer, edge-case ratios). Your test MUST construct those exact
   conditions. A test that calls `withdraw(0)` from a balance-0
   account to "verify withdraw works" = REJECT. A test that checks
   `current_rate == 0` when there are no borrowers = REJECT.

3. **State-dependent assertion required.** The final `assert!(...)`
   in your test must depend on whether the bug actually fired. Good
   patterns:
     * `assert!(balance_after == balance_before, E_BUG)` — fails
       when an attacker drains funds.
     * `assert!(state_field_after != expected_under_bug)` — fails
       when the bug corrupted state.
     * For abort-on-DoS bugs: omit `#[expected_failure]`; the
       unexpected abort marks the test failed (= fired).
     * `assert!(false, E_NOT_TRIGGERED)` at the end of an overflow
       test — IF we reach this line, the overflow didn't fire,
       which means the protection held.
   Bad patterns (reject):
     * `assert!(rate == 0, 0)` after no state mutation.
     * `assert!(balance == initial_balance)` after zero operations.
     * `assert!(true, 0)` — tautology.

4. **Honesty escape hatch.** If you genuinely cannot construct the
   bug-triggering state from the available source (e.g. the function
   requires a coin transfer mechanism the module doesn't have, the
   admin path the bug needs is gated behind setup you can't
   replicate), output the `CANNOT_TEST:` form at the bottom of this
   prompt. That's a HONEST answer and counts as "pass-no-bug-tested"
   rather than "bullshit-test-that-trivially-passes".

The post-cycle gate scans your test body. If it finds:
  - the test body is <5 non-comment lines
  - OR the assertion has no link to mutated state
  - OR `#[expected_failure]` paired with a trivial assert
the test is marked `weak_test` and you'll know — your work was wasted.

# Your task

Write a single self-contained Move test module `test_<finding_name>.move`
that:

1. Declares `module <named_address>::test_<finding_name>` (use one of
   the named addresses listed above — DO NOT invent `mutatis` or `0x0`
   if the manifest declares different names).
2. `use`s the modules under test.
3. Defines a `#[test(signer = @<addr>)]` function `test_<finding_name>`
   that:
     (a) Sets up witness state (initial resources, balances, oracle prices)
     (b) Calls the function under test with the bug-triggering inputs
     (c) Either:
           * `assert!(invariant_holds, E_BUG_HIT)` — fails when the
             bug breaks the invariant.
           * Reads state after the call and `assert!(actual == expected)`.
           * For abort-on-DoS bugs: omit `#[expected_failure]` so the
             unexpected abort marks the test failed (= fired).
           * For "this should abort but doesn't": add
             `#[expected_failure(abort_code = X)]`.
4. Uses `aptos_framework::account` / `aptos_framework::coin` helpers
   to set up signers / balances. Use `account::create_account_for_test`
   or `account::create_signer_for_test` for ephemeral test signers.

# Output format

Output ONLY a single ```move ... ``` fenced code block containing
the COMPLETE test module. The contents MUST begin with
`module <named_address>::<test_module_name> {{` and end with the
matching `}}`. Do NOT paste raw `public entry fun` snippets from the
source — those compile only inside their original modules. Do NOT
output any prose, explanation, or markdown outside the fenced
block. Do NOT output multiple fenced blocks — only ONE.

Bad output (rejected):
  ```move
  public entry fun withdraw(...) {{ ... }}   // bare function — NOT a module
  ```

Good output (accepted):
  ```move
  module mutatis::test_apt_X {{
      use mutatis::token_vault;
      #[test]
      fun test_witness() {{
          // setup + call + assert
      }}
  }}
  ```

If you can't write a real PoC (e.g. the hypothesis is wrong, the bug
isn't reachable, or you don't have enough information), output:

  // CANNOT_TEST: <one-line reason>
  module 0x0::no_op_test {{
      #[test]
      fun test_no_op() {{ }}
  }}

The `CANNOT_TEST:` marker is recognized by the post-cycle gate as a
non-fire — it doesn't count as a passed test. Don't use it lightly.
"""

    def validate_test_body(
        self,
        body: str,
        engine_repo_root: Path | None = None,
    ) -> tuple[bool, str | None]:
        """Pre-compile validation of an LLM-authored Move test body.

        Catches the four failure modes the L2 author has hit on cycle
        20260514-151541 — each ate ~$0.50 of compile-cost before the
        Move toolchain rejected the test. Validating BEFORE compile
        lets the dispatch retry with a feedback message instead of
        wasting the compile/run.

        Returns (is_valid, error_message). When invalid, error_message
        is a single-line description suitable for re-prompting the LLM
        ("Your previous attempt had error X — fix and retry.").

        Checks (in order, returns first failure):

        1. Invalid hex addresses. Move addresses are 0x[0-9a-fA-F]+.
           Cute mnemonic addresses like @0xATTACKER / @0xDEBT / @0xCOLL
           contain non-hex characters and fail Move's lexer.

        2. Cross-module ``acquires`` annotations. Tests written in a
           test module cannot ``acquires <other_module>::<Resource>``
           — the bytecode verifier rejects it. Module-local acquires
           and same-module resource references are fine.

        3. Imports of non-existent modules. ``use mutatis::<X>;`` is
           only valid when ``module mutatis::<X>`` exists in the
           engine source. ``use aptos_framework::*`` and ``use std::*``
           are always allowed (framework). When engine_repo_root is
           given we scan sources/*.move for actual module names and
           reject imports for modules not present.

        4. Bare source-paste fallback. If the body has no `module`
           declaration but contains `public entry fun`, the LLM
           pasted source code instead of a test module. Reject.
        """
        # 4) bare source paste — quick check first
        if "module " not in body and "public entry fun " in body:
            return False, (
                "Output looks like a bare `public entry fun` paste, not a "
                "Move test module. Wrap the test in "
                "`module <addr>::test_<name> { #[test] fun test_<name>() { ... } }`."
            )

        # 1) hex address validation
        # Match @0x... and check every char is hex
        bad_addrs: list[str] = []
        for m in re.finditer(r"@0x([0-9a-zA-Z_]+)", body):
            hexpart = m.group(1)
            if not re.fullmatch(r"[0-9a-fA-F]+", hexpart):
                bad_addrs.append("@0x" + hexpart)
        if bad_addrs:
            uniq = sorted(set(bad_addrs))
            return False, (
                f"Invalid hex addresses: {', '.join(uniq[:5])}. Move "
                f"addresses must contain ONLY characters 0-9, a-f, A-F. "
                f"Cute mnemonic names like @0xATTACKER are not valid hex "
                f"(T, K, R are not hex digits). Use real hex like @0x100, "
                f"@0xAA, @0xBEEF, @0xC0DE."
            )

        # 2) cross-module acquires
        # Match `acquires <ident>::<Resource>` or
        # `acquires <ident>, <ident2>::<Resource>`
        # Note: bare `acquires <Type>` (same-module) is fine.
        m = re.search(r"\bacquires\s+(?:[^,{]+,\s*)*([A-Za-z_]\w*)::(\w+)", body)
        if m:
            other_mod = m.group(1)
            res = m.group(2)
            return False, (
                f"`acquires {other_mod}::{res}` is a cross-module acquires "
                f"clause. Move's bytecode verifier rejects this — a test "
                f"module can only declare `acquires` on resources defined "
                f"in the SAME module. Drop the `acquires` annotation from "
                f"your test fn signature and just call the target module's "
                f"`public entry fun` (e.g. `treasury::deposit(...)`) — the "
                f"framework handles acquires internally on cross-module "
                f"calls."
            )

        # 3) imports of non-existent modules
        if engine_repo_root is not None and engine_repo_root.is_dir():
            engine_modules: set[str] = set()
            for src in (engine_repo_root / "sources").glob("*.move"):
                try:
                    txt = src.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for mm in re.finditer(
                    r"^\s*module\s+\w+::(\w+)\s*\{", txt, re.MULTILINE
                ):
                    engine_modules.add(mm.group(1))

            # Allow framework + stdlib imports unconditionally
            ALLOWED_FRAMEWORK_PREFIXES = (
                "aptos_framework::",
                "aptos_std::",
                "aptos_token::",
                "std::",
                "0x1::",
                "0x2::",
                "0x3::",
                "0x4::",
            )
            bad_imports: list[str] = []
            for um in re.finditer(
                r"^\s*use\s+([\w:]+::\w+);", body, re.MULTILINE
            ):
                full = um.group(1)
                # Skip framework / stdlib
                if any(full.startswith(p) for p in ALLOWED_FRAMEWORK_PREFIXES):
                    continue
                # Engine modules: extract trailing module name and verify
                mod_name = full.rsplit("::", 1)[1]
                if mod_name not in engine_modules:
                    bad_imports.append(full)
            if bad_imports:
                uniq = sorted(set(bad_imports))[:5]
                avail = sorted(engine_modules)
                return False, (
                    f"Imports reference modules not present in engine "
                    f"source: {', '.join(uniq)}. Available engine modules "
                    f"(import these only): {', '.join(avail)}. (Framework "
                    f"imports like aptos_framework::account, std::signer "
                    f"are always OK.)"
                )

            # 4) function existence — call sites must reference real
            # public/entry funs in the imported modules. Catches APT12-
            # style `vault::total_shares` where the module exists but
            # the function the test wants doesn't (only a field of the
            # same name exists, no public getter).
            module_funs: dict[str, set[str]] = {}
            for src in (engine_repo_root / "sources").glob("*.move"):
                try:
                    txt = src.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                mm = re.search(
                    r"^\s*module\s+\w+::(\w+)\s*\{", txt, re.MULTILINE
                )
                if not mm:
                    continue
                mod = mm.group(1)
                funs: set[str] = set()
                for fm in re.finditer(
                    r"^\s*(?:public(?:\([^)]+\))?\s+)?(?:entry\s+)?fun\s+(\w+)",
                    txt,
                    re.MULTILINE,
                ):
                    funs.add(fm.group(1))
                module_funs[mod] = funs

            # Find all <module>::<function>(...) call sites in the test
            # body. Skip framework prefixes + ignore <module>::<Type>
            # references (struct types start uppercase by convention —
            # we only flag lowercase identifiers as function calls).
            ALLOWED_FRAMEWORK_PREFIXES = (
                "aptos_framework",
                "aptos_std",
                "aptos_token",
                "std",
            )
            bad_calls: list[str] = []
            for cm in re.finditer(
                r"\b(\w+)::(\w+)\s*\(", body
            ):
                mod = cm.group(1)
                fn = cm.group(2)
                if mod in ALLOWED_FRAMEWORK_PREFIXES:
                    continue
                if mod not in module_funs:
                    continue  # module-not-found already flagged above
                if not fn[0].islower():
                    continue  # type / struct constructor, not a function
                # Common Move literals/keywords that aren't fn calls
                if fn in ("borrow_global", "borrow_global_mut", "exists",
                          "move_from", "move_to"):
                    continue
                if fn not in module_funs[mod]:
                    bad_calls.append(f"{mod}::{fn}")
            if bad_calls:
                uniq = sorted(set(bad_calls))[:5]
                # Show the agent which functions actually exist in those
                # modules so the retry can call a real one.
                hints = []
                for bc in uniq:
                    mod = bc.split("::", 1)[0]
                    funs = sorted(module_funs.get(mod, set()))[:10]
                    hints.append(f"{mod}::* available: {', '.join(funs)}")
                return False, (
                    f"Test calls functions that don't exist: "
                    f"{', '.join(uniq)}. "
                    + " | ".join(hints)
                )

        return True, None

    def detect_weak_test(self, body: str) -> tuple[bool, str | None]:
        """Return (is_weak, reason) for an authored test body.

        Cycle 20260514-151541 anti-bullshit guard. APT12 wrote ~80 lines
        of precision-divergence math in `// ...` comments, then ended
        with `assert!(rate == 0, 0)` after no state mutation — the test
        "passed" trivially without ever triggering the bug.

        Heuristics (any one = weak):
          1. Comment-to-code ratio > 3:1 — bug exploration was offloaded
             into comments instead of into the test setup.
          2. < 5 non-comment, non-blank lines in the test function body.
          3. Sole `assert!` references only literals or unchanged
             initial state (heuristic: assertion includes `== 0`, `== 1`,
             `== initial_*`, or compares to a `let` defined immediately
             before with no intervening mutation).
        """
        # Strip the module wrapper to focus on the test fn body
        body_lines = body.splitlines()
        # Comment ratio across the whole body
        n_comment = sum(
            1 for ln in body_lines if ln.strip().startswith("//")
        )
        n_code = sum(
            1 for ln in body_lines
            if ln.strip() and not ln.strip().startswith("//")
        )
        if n_code and n_comment / n_code > 3:
            return True, (
                f"comment-to-code ratio {n_comment}/{n_code} > 3:1 — "
                "bug-exploration math was offloaded to comments instead "
                "of into the test setup (APT12-style)."
            )

        # Find the test function body
        fn_body = ""
        m = re.search(
            r"#\[test[^\]]*\]\s*\n\s*fun\s+\w+\s*\([^)]*\)\s*"
            r"(?:acquires[^{]+)?\{([\s\S]*?)\n\s*\}",
            body,
        )
        if m:
            fn_body = m.group(1)
        if not fn_body:
            return False, None  # can't parse — let compile catch it

        # Count non-comment, non-blank lines in the test fn body
        fn_lines = [
            ln for ln in fn_body.splitlines()
            if ln.strip() and not ln.strip().startswith("//")
        ]
        if len(fn_lines) < 5:
            return True, (
                f"test fn body has only {len(fn_lines)} non-comment lines — "
                "almost certainly not exercising the bug-triggering path."
            )

        # Pattern: ONLY assertion in body is `assert!(<trivial>)`
        asserts = re.findall(r"assert!\s*\(([^,]+)", fn_body)
        if len(asserts) == 1:
            expr = asserts[0].strip()
            # Strip whitespace, lowercase for matching
            low = expr.lower()
            trivial_patterns = (
                r"^true$",
                r"^0\s*==\s*0$",
                r"^\w+\s*==\s*0$",  # `rate == 0`, `bal == 0` — likely no mutation
                r"^\w+\s*==\s*1$",
            )
            for pat in trivial_patterns:
                if re.match(pat, low):
                    return True, (
                        f"sole assertion is trivial ({expr!r}) — the test "
                        "didn't actually exercise the bug-triggering "
                        "state mutation."
                    )

        return False, None

    def parse_test_body(self, llm_response: str) -> str:
        # Find ALL move/Move/rust/bare fenced blocks and pick the one
        # that LOOKS like a complete test module. Without this, the
        # parser used to grab the FIRST fence — which is often the
        # LLM showing "here's the source I'm testing against:" as a
        # quoted context block, NOT the actual test. The buggy
        # behavior was caught during the aptos-small dry-run where
        # APT11's first fence was bare `public entry fun withdraw...`
        # source code (no module wrapper) → compile failed.
        fences: list[str] = []
        fence_re = re.compile(
            r"```(?:move|Move|rust|Rust)?\s*\n([\s\S]*?)\n```"
        )
        for m in fence_re.finditer(llm_response):
            fences.append(m.group(1).strip())

        # Score each fence: prefer ones with BOTH module wrapper AND
        # #[test] annotation. Fall back to fences with just module,
        # then just #[test]. Reject bare source pastes.
        def _score(body: str) -> int:
            has_module = bool(re.search(r"\bmodule\s+\S+\s*\{", body))
            has_test = "#[test" in body
            has_cant = "CANNOT_TEST" in body
            if has_cant:
                # CANNOT_TEST stub — accept (caller's pseudo-pass
                # marker catches it). Score lower than a real test.
                return 1 if has_module else 0
            if has_module and has_test:
                return 100
            if has_module:
                return 50
            if has_test:
                return 30
            return -100  # bare source paste — REJECT

        best = -1000
        best_body = None
        for body in fences:
            s = _score(body)
            if s > best:
                best = s
                best_body = body

        if best_body and best > 0:
            return best_body + "\n"

        # Bare body fallback (LLM forgot fences)
        stripped = llm_response.strip()
        if stripped.startswith("module ") or "#[test" in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Move source block from the LLM response. "
            "Expected ```move ... ``` fenced code block."
        )

    def write_test_file(
        self,
        workspace: Path,
        test_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "tests" / "aptos"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"test_{test_name}{self.test_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_test(
        self,
        workspace: Path,
        test_name: str,
        target_repo_root: Path,
        timeout_s: int = 240,
    ) -> PocOutcome:
        test_path = (
            workspace / "tests" / "aptos"
            / f"test_{test_name}{self.test_file_extension}"
        )
        if not test_path.is_file():
            raise FileNotFoundError(
                f"PoC test file not found at {test_path}. Did write_test_file run?"
            )

        body = test_path.read_text(encoding="utf-8", errors="replace")
        for marker in _PSEUDO_PASS_MARKERS:
            if marker in body:
                return PocOutcome(
                    fired=False,
                    test_path=test_path,
                    stdout="",
                    stderr=f"pseudo-pass marker {marker!r} present",
                    returncode=-1,
                    duration_s=0.0,
                    framework=self.framework,
                    reason=f"pseudo-pass: contains {marker!r}",
                    metadata={"pseudo_pass": True, "marker": marker},
                )

        # Aptos move test expects tests in <package>/tests/. Deploy our
        # test there, run, then clean up.
        repo_tests_dir = target_repo_root / "tests"
        repo_tests_dir.mkdir(parents=True, exist_ok=True)
        deployed_test = repo_tests_dir / f"jelleo_l2_{test_name}.move"
        deployed_test.write_text(body, encoding="utf-8")

        # Filter name: extract the ACTUAL `fun test_X()` name from
        # the deployed test body, falling back to the slug-derived
        # name if extraction fails.
        #
        # The aptos-small dry-run caught the failure mode this fixes:
        # the LLM named the test `fun test_borrow_global_no_auth()`
        # (module-name based) while the adapter filtered for
        # `test_apt1_borrow_global_no_auth` (slug-prefixed). The
        # filter excluded our test → "Total tests: 0" → we never
        # learned whether the test would have fired. APT1's REAL
        # admin-hijack PoC was discarded as "0 PASS — bug not
        # reachable" when the test simply never ran.
        #
        # Now we parse `fun <name>(` patterns from the body,
        # preferring names that start with "test_" (per Move's
        # convention) and use that as the filter so we're matching
        # what the LLM actually wrote.
        fn_matches = re.findall(r"fun\s+(test_\w+)\s*\(", body)
        if fn_matches:
            filter_name = fn_matches[0]
        else:
            # Fallback: any fun, prefer ones containing 'test'
            all_funs = re.findall(r"fun\s+(\w+)\s*\(", body)
            test_funs = [f for f in all_funs if "test" in f.lower()]
            filter_name = (test_funs or all_funs or [f"test_{test_name}"])[0]

        t0 = time.time()
        try:
            run_proc = subprocess.run(
                [
                    "aptos", "move", "test",
                    "--filter", filter_name,
                    "--package-dir", str(target_repo_root),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="aptos CLI not installed — install via https://aptos.dev/tools/install-cli/",
                returncode=-3,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="toolchain missing: aptos CLI",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed_test.unlink(missing_ok=True)
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout="",
                stderr="aptos move test timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                framework=self.framework,
                reason="aptos move test timeout",
            )
        finally:
            deployed_test.unlink(missing_ok=True)

        duration = time.time() - t0
        # Strip ANSI color escapes that `aptos move test` emits with TTY
        # detection failing under capture_output=True on some platforms.
        # Without this strip, the PASS/FAIL regex below misses lines that
        # are wrapped in `\x1b[32m...\x1b[0m` etc.
        ansi_strip = re.compile(r"\x1b\[[0-9;]*m")
        stdout = ansi_strip.sub("", run_proc.stdout)[:8000]
        stderr = ansi_strip.sub("", run_proc.stderr)[:4000]

        # aptos move test prints lines like:
        #   [ PASS    ] 0x<addr>::test_<name>::test_func
        #   [ FAIL    ] 0x<addr>::test_<name>::test_func
        # FAIL lines mean the test demonstrated the bug (= fired).
        # We anchor on the EXACT test function name we authored so a
        # substring-match in --filter that pulled in `test_foo_bar` when
        # we asked for `test_foo` doesn't accidentally claim the wrong
        # bug fired.
        all_fail = re.findall(r"^\s*\[\s*FAIL\s*\]\s*(.+?)\s*$", stdout, re.MULTILINE)
        all_pass = re.findall(r"^\s*\[\s*PASS\s*\]\s*(.+?)\s*$", stdout, re.MULTILINE)
        # Filter to the test we actually authored. Pattern: <addr>::test_<test_name>::test_<test_name>
        # (or sometimes <addr>::test_<test_name>::<helper>). Require the
        # OUR test_name to appear as a `::test_<name>::` segment, not as
        # a substring of another test function name.
        anchor = f"::{filter_name}::"
        fail_lines = [line for line in all_fail if anchor in line or line.endswith(f"::{filter_name}")]
        pass_lines = [line for line in all_pass if anchor in line or line.endswith(f"::{filter_name}")]
        # Fallback: if the anchor filter is too strict (e.g. the CLI
        # printed a different format), accept the unfiltered set so we
        # don't lose signal entirely.
        if not fail_lines and not pass_lines and (all_fail or all_pass):
            fail_lines = all_fail
            pass_lines = all_pass
        # aptos move test also prints abort-code details on failure:
        abort_match = re.search(
            r"abort code\s*[:=]?\s*(\d+|0x[0-9a-fA-F]+)",
            stdout + stderr,
        )
        abort_code = abort_match.group(1) if abort_match else None

        if fail_lines:
            return PocOutcome(
                fired=True,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason=(
                    f"aptos move test failed: {fail_lines[0][:120]}"
                    + (f" (abort code {abort_code})" if abort_code else "")
                ),
                metadata={
                    "fail_lines": fail_lines[:5],
                    "abort_code": abort_code,
                },
            )

        if run_proc.returncode != 0 and not pass_lines:
            # Non-zero exit without any PASS/FAIL parsed → likely compile error
            return PocOutcome(
                fired=False,
                test_path=test_path,
                stdout=stdout,
                stderr=stderr,
                returncode=run_proc.returncode,
                duration_s=duration,
                framework=self.framework,
                reason="aptos move test exited non-zero without parseable PASS/FAIL (likely compile error)",
                metadata={"phase": "compile"},
            )

        return PocOutcome(
            fired=False,
            test_path=test_path,
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            duration_s=duration,
            framework=self.framework,
            reason=f"all tests passed ({len(pass_lines)} PASS) — bug not reachable from witness state",
            metadata={"pass_lines": pass_lines[:5]},
        )
