"""L3 formal-verification adapter for Aptos Move — Move Prover.

Aptos's Move Prover is a SMT-backed formal verifier for Move modules.
It reads `spec` blocks attached to functions + invariants on
resources, and either:

  * Proves the specification holds, OR
  * Returns a counterexample (concrete inputs violating the spec)

Move Prover is built into the aptos CLI:

  aptos move prove --package-dir <repo>

Spec block syntax (annotated alongside the function under test):

    spec withdraw {
        // Preconditions
        requires exists<Vault>(@mutatis);
        // Postconditions
        ensures global<Vault>(@mutatis).total_deposits ==
                old(global<Vault>(@mutatis).total_deposits) - amount;
        // Aborts conditions
        aborts_if amount > global<Vault>(@mutatis).total_deposits;
    }

The L3 harness for Aptos is a Move MODULE containing only `spec`
blocks attached to existing functions. The hunt deploys this spec
module alongside the target sources and runs the prover.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from audit_pipeline.formal_adapters.base import FormalOutcome, LanguageFormalAdapter


class AptosFormalAdapter(LanguageFormalAdapter):
    """Aptos Move formal-verification adapter (Move Prover)."""

    language = "aptos"
    harness_file_extension = ".move"
    verifier = "move-prover"

    def build_harness_prompt(
        self,
        hyp: dict[str, Any],
        source_context: str,
        target_repo_root: Path,
    ) -> str:
        hyp_id = hyp.get("id", "unknown")
        claim = hyp.get("claim", "(no claim)")
        engine_function = hyp.get("engine_function", "")
        target_file = hyp.get("target_file", "")

        return f"""You are authoring a Move Prover spec module for the Jelleo audit engine.

Move Prover is invoked via:

  aptos move prove --package-dir {target_repo_root}

It reads `spec` blocks attached to functions and resources, and either
PROVES the specification holds or returns a CONCRETE COUNTEREXAMPLE.

# Hypothesis under test

ID: {hyp_id}
Claim: {claim}
Function under test: {engine_function}
Target file: {target_file}

# Grounded source

{source_context}

# Move Prover output patterns

  * "verification successful" → spec proved
  * "specification failed" or "abort code N" or "counterexample" →
    spec violated; bug constructively proven

# Spec file syntax (CRITICAL — Move parser is strict)

The TOP-LEVEL form for a free-standing spec file is:

  spec <address>::<module_name> {{

      // ── INNER spec module block — for module-level invariants ──
      // (Optional. Only include if you have a true module-wide invariant.)
      spec module {{
          invariant <module_level_invariant>;
      }}

      // ── Per-function spec blocks ──
      spec <function_name> {{
          requires <precondition>;       // input constraints
          ensures <postcondition>;       // result + state guarantees
          aborts_if <abort_condition>;   // when this aborts
      }}
  }}

ABSOLUTE rules:

  ❌ DO NOT write `spec module <address>::<module_name>` at the top.
     INVALID — the parser rejects with "Unexpected 'module'".
     The keyword `module` at the top-level position is only used for
     a REAL Move module declaration, not a spec attachment.

  ❌ DO NOT put `pragma <name>;` at the outer `spec <addr>::<mod>` level.
     INVALID — the parser rejects with "Unexpected 'pragma'. Expected
     a module member: 'spec', 'use', 'friend', 'const', 'fun', 'inline',
     or 'struct'". Pragmas MUST be inside `spec module {{ ... }}` (for
     module-level pragmas like `aborts_if_is_strict`) OR inside an
     individual `spec <function_name> {{ ... }}` block (for function-
     scoped pragmas like `opaque`).

  ✓ `spec module {{ ... }}` IS valid INSIDE a `spec <addr>::<mod>` block,
     for both pragmas AND module-level invariants.

  ✓ Multiple `spec <addr>::<mod>` blocks in a single file are OK if you
     need to spec functions across multiple modules (e.g. attaching
     specs to both `mutatis::access_control` and `mutatis::token_vault`).

# Your task

Write a Move spec file that:

1. Top-level: `spec <address>::<module_name>` (NO `module` keyword).
   The address + name MUST match the target module under test exactly.
2. FIRST inner block: `spec module {{ pragma aborts_if_is_strict; ... }}`.
   This is where ALL pragmas go + any module-level `invariant` clauses.
3. Following inner blocks: `spec <function_name> {{ ... }}` per function.
4. Express the invariant as the OPPOSITE of the bug:
     * If bug = "function admits invalid input X" → write
       `aborts_if input_is_invalid_X;`
     * If bug = "balance can go negative" → write
       `invariant balance >= 0;` (inside spec module)
     * If bug = "auth bypassed" → write
       `requires signer::address_of(s) == admin_addr;` + ensure
       state mutation only happens under that precondition.

# Important

* Use `global<Resource>(addr)` and `exists<Resource>(addr)` to refer
  to global state.
* `old(expr)` refers to the pre-execution value.
* Parameter names in spec blocks MUST match the function signature
  in the target source (e.g. if `fun transfer_admin(_caller: &signer,
  new_admin: address)`, your spec uses `_caller` and `new_admin`).

# ❌ DO NOT call IMPURE Move functions in spec context

Spec blocks only accept PURE expressions. Calling an impure (state-
mutating or non-deterministic) function inside a spec expression
triggers:

  error: specification expression cannot call impure Move function `<name>`

WRONG (impure function call):
  spec emergency_drain {{
      aborts_if access_control::is_paused();
      requires access_control::is_admin(signer::address_of(invoker));
  }}

RIGHT (use direct global accessors instead):
  spec emergency_drain {{
      // Was: access_control::is_paused()
      aborts_if exists<access_control::PauseState>(@mutatis) &&
                global<access_control::PauseState>(@mutatis).paused;
      // Was: access_control::is_admin(signer::address_of(invoker))
      requires exists<access_control::AdminCap>(@mutatis);
      requires signer::address_of(invoker) ==
               global<access_control::AdminCap>(@mutatis).admin;
  }}

If a helper is genuinely PURE (e.g. `fun current_admin(): address`
that only reads global state without mutation), it can be re-declared
as a `spec fun` for use in spec context — but the simplest path is
always: inline the global accessor pattern.

# ❌ DO NOT use `pragma aborts_if_is_strict` unless you write specs
   for EVERY function in the module

That pragma flips the prover into "all abort paths must be declared"
mode for the WHOLE module — every other function (deposit, withdraw,
init_vault, …) will fail verification because their abort paths
weren't declared in your spec.

WRONG (single-function spec + strict pragma):
  spec mutatis::token_vault {{
      spec module {{ pragma aborts_if_is_strict; }}
      spec emergency_drain {{ ... }}
  }}
  // ↑ Prover complains about deposit, withdraw, init_vault, etc.

RIGHT (omit the pragma):
  spec mutatis::token_vault {{
      spec emergency_drain {{ ... }}
  }}

# Authoring the bug-spec to FIND the counterexample

The goal is for the prover to SHOW that the bug exists. Two patterns:

  Pattern A — abort-IF condition violated:
    spec emergency_drain {{
        // "Function MUST abort if invoker is not admin"
        aborts_if signer::address_of(invoker) !=
                  global<access_control::AdminCap>(@mutatis).admin;
        aborts_if !exists<Vault>(@mutatis);
        // ... other necessary abort conditions
    }}
  If the code does NOT abort for a non-admin invoker, the prover
  finds a counterexample where the function returns normally while
  invoker != admin → bug formally exposed.

  Pattern B — module-level invariant:
    spec module {{
        invariant exists<AdminCap>(@mutatis) ==>
            global<AdminCap>(@mutatis).admin != @0x0;
    }}
  If any function (e.g. transfer_admin accepting new_admin=@0x0)
  can violate this, the prover finds the offending function +
  parameter assignment.

Prefer Pattern A for auth-bypass bugs; Pattern B for invariant-
preservation bugs.

# Output format

Output ONLY a single ```move ... ``` fenced code block. If you can't
write a real spec:

  // CANNOT_VERIFY: <one-line reason>
  spec 0x0::noop {{ }}
"""

    def parse_harness_body(self, llm_response: str) -> str:
        m = re.search(r"```(?:move|Move|rust)\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            return m.group(1).strip() + "\n"
        m = re.search(r"```\s*\n([\s\S]*?)\n```", llm_response)
        if m:
            body = m.group(1).strip()
            if "spec " in body or "module " in body:
                return body + "\n"
        stripped = llm_response.strip()
        if "spec " in stripped or "module " in stripped:
            return stripped + "\n"
        raise ValueError(
            "Could not extract a Move spec from the LLM response."
        )

    def write_harness_file(
        self,
        workspace: Path,
        harness_name: str,
        body: str,
    ) -> Path:
        out_dir = workspace / "formal" / "aptos"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"spec_{harness_name}{self.harness_file_extension}"
        out_path.write_text(body, encoding="utf-8")
        return out_path

    def run_verifier(
        self,
        workspace: Path,
        harness_name: str,
        target_repo_root: Path,
        timeout_s: int = 900,
    ) -> FormalOutcome:
        harness_path = (
            workspace / "formal" / "aptos"
            / f"spec_{harness_name}{self.harness_file_extension}"
        )
        if not harness_path.is_file():
            raise FileNotFoundError(
                f"Move Prover spec not found at {harness_path}."
            )

        body = harness_path.read_text(encoding="utf-8", errors="replace")
        if "CANNOT_VERIFY" in body:
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="CANNOT_VERIFY marker — spec stubbed",
                returncode=-1,
                duration_s=0.0,
                verifier=self.verifier,
                reason="spec stub (CANNOT_VERIFY)",
            )

        # Deploy spec into the target repo's sources/ so the prover
        # picks it up
        deployed = target_repo_root / "sources" / f"jelleo_l3_spec_{harness_name}.move"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(body, encoding="utf-8")

        cmd = [
            "aptos", "move", "prove",
            "--package-dir", str(target_repo_root),
        ]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except FileNotFoundError:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="aptos CLI not installed",
                returncode=-3,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="toolchain missing: aptos CLI",
                metadata={"infra_error": True},
            )
        except subprocess.TimeoutExpired:
            deployed.unlink(missing_ok=True)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout="",
                stderr="Move Prover timed out",
                returncode=-5,
                duration_s=time.time() - t0,
                verifier=self.verifier,
                reason="Move Prover timeout",
            )
        finally:
            deployed.unlink(missing_ok=True)

        duration = time.time() - t0
        stdout = proc.stdout[:8000]
        stderr = proc.stderr[:4000]
        combined = stdout + "\n" + stderr

        # Move Prover output patterns.
        #
        # Scoping rationale: we deploy our spec as `jelleo_l3_spec_<name>.
        # move` and the prover runs over the WHOLE package. Pre-existing
        # specs on the engine source could (in theory) fail and falsely
        # register as OUR counterexample.
        #
        # OLD scoping (filter to lines containing spec_anchor): broke on
        # APT4 because Move Prover's primary FAILURE MARKER lines (e.g.
        # `error: global memory invariant does not hold`,
        # `error: abort not covered`) don't carry the filename — only the
        # subsequent context lines do. So the filter dropped the marker
        # → adapter saw no failure → returned "indeterminate" even
        # though the prover found a concrete counterexample.
        #
        # NEW scoping: if spec_anchor appears anywhere in the combined
        # output, accept the FULL output for failure detection. The
        # `finally: deployed.unlink()` clean-up means only OUR spec is
        # in the package at run-time, so any failure must be either
        # (a) our spec, OR (b) a pre-existing engine spec that fails
        # without our spec present (which would also fail without us
        # invoking — operator-visible as a separate issue, not ours
        # to suppress).
        spec_anchor = f"jelleo_l3_spec_{harness_name}"
        if spec_anchor in combined:
            scoped = combined  # our spec touched the run — use full output
        else:
            scoped = combined  # spec_anchor not even mentioned — full anyway

        # Tooling errors come FIRST — these are infra failures, not
        # verification outcomes. The Aptos CLI buries the actual cause
        # behind a generic "Move Prover failed: exiting with 1 error
        # in compilation" message on stdout (JSON-formatted), even
        # when the real issue is "No boogie executable set" or a
        # genuine spec-syntax error. Previously the adapter classified
        # all of these as "inconclusive" — silently masking the bug
        # from the dashboard + cycle report. Operator caught this on
        # cycle 20260513-191318: 4 L3 runs reported proved=false /
        # counterexample=false with no error event in the log.
        prover_compile_error_re = re.compile(
            r"Move Prover failed.*?compilation|"
            r"Move compilation failed|"
            r"unexpected token|"
            r"Expected an address or an identifier",
            re.IGNORECASE | re.DOTALL,
        )
        prover_infra_error_re = re.compile(
            r"No boogie executable set|"
            r"Z3 not found|"
            r"BOOGIE_EXE|Z3_EXE|CVC5_EXE|"
            r"Cannot find the (boogie|z3|cvc5) executable",
            re.IGNORECASE,
        )
        if prover_infra_error_re.search(combined):
            err = prover_infra_error_re.search(combined).group(0)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=(
                    f"Move Prover infra error: {err}. "
                    "Run `aptos update prover-dependencies` on the VPS "
                    "+ ensure BOOGIE_EXE / Z3_EXE / CVC5_EXE are exported "
                    "in /root/.audit-env."
                ),
                metadata={"infra_error": True, "failure_signal": err},
            )
        if prover_compile_error_re.search(combined):
            err = prover_compile_error_re.search(combined).group(0)
            return FormalOutcome(
                proved=False,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=(
                    f"Move Prover spec did not compile: {err}. "
                    "The auto-authored spec has a syntax error — "
                    "L2 PoC fire remains the authoritative bug signal."
                ),
                metadata={"compile_error": True, "failure_signal": err},
            )

        # Move Prover's actual failure marker lines. Each of these
        # indicates a verification counterexample (the bug-spec was
        # violated by some concrete state the SMT solver constructed):
        #   * "global memory invariant does not hold" — module-level
        #     `invariant` violated (e.g. APT4: admin != @0x0 broken)
        #   * "abort not covered by any of the `aborts_if` clauses" —
        #     function aborts in a path not declared in spec
        #   * "post-condition does not hold" — `ensures` clause failed
        #   * "specification failed" / "verification error" — generic
        #   * "abort code N" — runtime abort with bug-marker code
        #   * "did not verify" — verification did not succeed
        failure_match = re.search(
            r"(specification failed|abort code\s*\d+|counterexample|"
            r"verification error|did not verify|"
            r"global memory invariant does not hold|"
            r"abort not covered|"
            r"post-condition does not hold|"
            r"function does not abort|"
            r"the prover failed to verify)",
            scoped,
            re.IGNORECASE,
        )
        if failure_match:
            return FormalOutcome(
                proved=False,
                counterexample=True,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason=f"Move Prover found counterexample: {failure_match.group(0)}",
                metadata={
                    "failure_signal": failure_match.group(0),
                    "spec_anchor_present": spec_anchor in combined,
                },
            )

        # Newer aptos move prove prints `"Result": "Success"` (JSON) on
        # stdout when the spec verifies. Older versions printed the
        # human-readable "verification successful" line. Detect either.
        if (
            '"Result": "Success"' in combined
            or "verification successful" in combined.lower()
        ):
            return FormalOutcome(
                proved=True,
                counterexample=False,
                harness_path=harness_path,
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
                duration_s=duration,
                verifier=self.verifier,
                reason="Move Prover verified all specifications",
            )

        return FormalOutcome(
            proved=False,
            counterexample=False,
            harness_path=harness_path,
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode,
            duration_s=duration,
            verifier=self.verifier,
            reason=(
                "Move Prover output had no success / failure / infra "
                "marker — investigate stdout+stderr captures above."
            ),
        )
