"""`audit-pipeline confirm` — empirical confirmation layer.

Takes a finding's agent-reasoning response.md, generates a CUSTOM Rust
test that attempts to demonstrate the claimed violation, compiles it
into the engine's tests/ directory, and runs `cargo test`.

Outcomes:
  - test compiles + assertion fires (cargo test fails) -> CONFIRMED bug
  - test compiles + assertion holds (cargo test passes) -> safety attestation
  - test fails to compile -> NEEDS_HUMAN (harness needs manual fix)

This is the layer that converts NEEDS_LAYER_2 leads into confirmed/refuted
verdicts. Without it, recon stays as triage forever.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import click
from rich.console import Console

from audit_pipeline.utils import complete, is_available

console = Console()


SYSTEM_PROMPT = """You are writing a Rust integration test for the Percolator perpetual DEX engine.
The test goes into `<engine>/tests/<test_name>.rs` and runs via:

  cargo test --features test --test <test_name>

# Test structure constraints

- File MUST start with `#![cfg(feature = "test")]` (so it only compiles in test mode)
- Use `use percolator::*;` and `use percolator::i128::U128;`
- Make the test fail (panic / assert!) when the claimed violation triggers
- Make the test pass when the invariant actually holds

# Available helpers from existing tests

```rust
fn default_params() -> RiskParams { ... }
fn add_user_test(engine: &mut RiskEngine, fee_payment: u128) -> Result<u16> { ... }
fn add_lp_test(engine: &mut RiskEngine, matcher_program: [u8; 32], matcher_context: [u8; 32], fee_payment: u128) -> Result<u16> { ... }
```

You can copy these into your test file or write them inline.

# Example test pattern

```rust
#![cfg(feature = "test")]

use percolator::*;
use percolator::i128::U128;

fn default_params() -> RiskParams {
    // ... copy from existing tests
}

#[test]
fn test_<finding_id>_invariant() {
    let params = default_params();
    let mut engine = RiskEngine::init_with_params(params).unwrap();

    // 1. Set up the precondition state described in the finding
    // 2. Capture pre-state (vault, insurance, c_tot, etc.)
    let pre_residual = engine.vault.get() - engine.c_tot.get() - engine.insurance_fund.balance.get();

    // 3. Execute the instruction sequence the finding claims violates the invariant
    // ...

    // 4. Capture post-state
    let post_residual = engine.vault.get() - engine.c_tot.get() - engine.insurance_fund.balance.get();

    // 5. Assert the invariant
    assert_eq!(pre_residual, post_residual, "residual conservation violated");
}
```

# Your job

Write the COMPLETE test file. Output ONLY Rust code (no markdown fences, no
prose). The test must:
- Be self-contained (include any helpers it needs)
- Be deterministic (no random, no timestamp)
- Run in <10 seconds
- Either ASSERT the invariant claim from the finding (test passes if
  invariant holds, fails if violated) OR demonstrate the attack sequence
  ending with an assertion that fires on the claimed bug

If the finding's claim cannot be expressed as a deterministic test (e.g.
requires Solana runtime context), output:

  // CANNOT_TEST: <one-line reason>

as the only line.
"""


@click.command(name="confirm")
@click.option("--response-md", "-r", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="Path to the hunt-deep response.md for this finding")
@click.option("--hyp-id", required=True, help="Short hypothesis identifier (used as test name)")
@click.option("--hypotheses-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Original hypotheses YAML to lookup the claim text")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Where to write the test (default: <workspace>/confirm/)")
@click.option("--no-run", is_flag=True, help="Generate but don't compile/run")
@click.option("--timeout", type=int, default=180, show_default=True,
              help="Cargo test timeout in seconds")
@click.option("--tool-using/--single-shot", default=True, show_default=True,
              help="Use tool-using agent (read engine tests + source iteratively) vs single-shot LLM")
@click.option("--max-turns", type=int, default=18, show_default=True,
              help="Tool-using max turns")
@click.pass_context
def confirm_cmd(
    ctx: click.Context,
    response_md: Path,
    hyp_id: str,
    hypotheses_file: Path | None,
    output_dir: Path | None,
    no_run: bool,
    timeout: int,
    tool_using: bool,
    max_turns: int,
) -> None:
    """Generate + compile + run a custom PoC for a finding."""
    if not is_available():
        raise click.ClickException("ANTHROPIC_API_KEY required.")

    workspace = Path(ctx.obj["workspace"])
    config = json.loads((workspace / "workspace.json").read_text())
    engine_dir = workspace / config["engine"]["local"]

    output_dir = output_dir or (workspace / "confirm")
    output_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Confirming[/bold] {hyp_id}")
    console.print(f"  Response: {response_md}")
    console.print(f"  Engine:   {engine_dir}")

    # Load finding context
    response_text = response_md.read_text(encoding="utf-8")
    claim_text = ""
    if hypotheses_file:
        try:
            import yaml
            hyp_data = yaml.safe_load(hypotheses_file.read_text())
            for h in hyp_data.get("hypotheses", []):
                if h.get("id") == hyp_id:
                    claim_text = h.get("claim", "")
                    break
        except Exception:  # noqa: BLE001
            pass

    # Read 1 example test for context
    example_test = ""
    for example_path in (
        engine_dir / "tests" / "amm_tests.rs",
        engine_dir / "tests" / "proofs_invariants.rs",
    ):
        if example_path.exists():
            example_test = example_path.read_text(encoding="utf-8")[:8000]
            break

    # Build the prompt
    prompt = f"""# Finding to confirm

## Hypothesis ID
{hyp_id}

## Original claim
{claim_text or "(see analysis below)"}

## Agent analysis (line-cited reasoning from hunt-deep)

{response_text}

# Example existing test for reference (DO NOT copy whole, use as style guide)

```rust
{example_test[:6000]}
```

Now write the complete custom Rust test file for `{hyp_id}`. Output ONLY
the .rs file contents (no markdown fences). The test name should be
`test_confirm_{_slug(hyp_id)}`.
"""

    console.print(f"  Generating test ({'tool-using' if tool_using else 'single-shot'})...")
    if tool_using:
        from audit_pipeline.utils.llm_tools import run_tool_using_agent
        tool_system = (
            "You write Rust integration tests for the Percolator perpetual DEX engine. "
            "Use `read_file`, `grep`, `find_function` to STUDY the existing tests in "
            f"`{engine_dir}/tests/` (especially amm_tests.rs, proofs_invariants.rs, "
            "proofs_safety.rs) and the engine source at "
            f"`{engine_dir}/src/percolator.rs`. Learn:\n"
            "1. The exact `default_params()` and `add_user_test()` helper signatures used\n"
            "2. The RiskEngine constructor (`RiskEngine::new(params)` or `new_with_market`)\n"
            "3. The actual public methods on RiskEngine — do NOT invent method names\n"
            "4. Import order: `use percolator::*;` and `use percolator::i128::U128;`\n"
            "5. The `#[cfg(feature = \"test\")]` gate at the top of every test file\n\n"
            "Write a test that COMPILES against the actual codebase and either "
            "asserts the invariant from the finding (passes if invariant holds) "
            "or demonstrates the violation (fails on the assertion). "
            "When ready, output ONLY the complete .rs file content as your final "
            "message (no markdown fences, no explanation). The test fn name should "
            f"be `test_confirm_{_slug(hyp_id)}`. If the finding cannot be expressed "
            "as a deterministic Rust test against the engine, output exactly "
            "`// CANNOT_TEST: <reason>` as your only line."
        )
        tool_user_msg = (
            f"# Finding to confirm\n\n"
            f"**Hypothesis ID:** {hyp_id}\n"
            f"**Original claim:** {claim_text}\n\n"
            f"# Agent reasoning to validate (already produced by hunt-deep, line-cited)\n\n"
            f"{response_text[:8000]}\n\n"
            f"Now use the tools to study the existing tests, then write the "
            f"complete custom Rust test as your final answer."
        )
        try:
            tu_result = run_tool_using_agent(
                workspace=workspace,
                system_prompt=tool_system,
                initial_user_message=tool_user_msg,
                max_turns=max_turns,
                max_tokens_per_turn=8192,
            )
            test_code = _strip_code_fences(tu_result.text)
            console.print(
                f"  ({tu_result.n_turns} turns, {len(tu_result.tool_calls)} tool calls, "
                f"{tu_result.input_tokens:,}in/{tu_result.output_tokens:,}out)"
            )
            class _Resp:
                input_tokens = tu_result.input_tokens
                output_tokens = tu_result.output_tokens
            resp = _Resp()
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(f"tool-using agent error: {e}")
    else:
        try:
            resp = complete(prompt, max_tokens=8192)
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(f"LLM error: {e}")
        test_code = _strip_code_fences(resp.text)
    if test_code.strip().startswith("// CANNOT_TEST"):
        console.print(f"  [yellow]CANNOT_TEST[/yellow]: {test_code.strip()[:120]}")
        (output_dir / f"{hyp_id}.cannot_test.txt").write_text(test_code)
        return

    test_name = f"test_confirm_{_slug(hyp_id)}"
    saved_path = output_dir / f"{test_name}.rs"
    saved_path.write_text(test_code, encoding="utf-8")
    console.print(f"  [green]wrote[/green] {saved_path} ({len(test_code)} bytes)")

    if no_run:
        return

    # Copy into engine tests/ and compile
    test_dest = engine_dir / "tests" / f"{test_name}.rs"
    try:
        test_dest.write_text(test_code, encoding="utf-8")
    except OSError as e:
        raise click.ClickException(f"failed to install test: {e}")

    # Ensure cargo is findable — fall back to common install locations
    import os as _os
    cargo_bin = "cargo"
    for candidate in ("cargo", "/root/.cargo/bin/cargo", _os.path.expanduser("~/.cargo/bin/cargo")):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=5, check=True)
            cargo_bin = candidate
            break
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue

    cargo_env = _os.environ.copy()
    cargo_env["PATH"] = "/root/.cargo/bin:" + cargo_env.get("PATH", "")

    console.print(f"  Compiling: {cargo_bin} test --features test --test {test_name}")
    started = time.time()
    try:
        result = subprocess.run(
            [cargo_bin, "test", "--features", "test", "--test", test_name, "--",
             "--nocapture"],
            cwd=str(engine_dir),
            capture_output=True, text=True, timeout=timeout, env=cargo_env,
        )
    except subprocess.TimeoutExpired:
        console.print(f"  [red]✗ timeout[/red]")
        return
    except FileNotFoundError as e:
        console.print(f"  [red]✗ cargo not found:[/red] {e}")
        return
    elapsed = time.time() - started

    rc = result.returncode
    log = (output_dir / f"{test_name}.cargo.log")
    log.write_text(f"=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}\n",
                   encoding="utf-8")

    # Classify outcome
    if rc == 0:
        console.print(f"  [green]✅ PASS[/green] in {elapsed:.1f}s — invariant HOLDS (safety attestation)")
        outcome = "safety_attestation"
    elif "panicked" in result.stdout or "test result: FAILED" in result.stdout:
        console.print(f"  [red]🚨 FIRED[/red] in {elapsed:.1f}s — assertion failed (potential confirmed bug)")
        outcome = "fired"
    elif "error[E" in result.stderr:
        console.print(f"  [yellow]⚠ COMPILE ERROR[/yellow] — manual harness fix needed (rc={rc})")
        # Show first compile error
        for line in result.stderr.splitlines()[:20]:
            if line.strip().startswith("error"):
                console.print(f"      {line}")
                break
        outcome = "compile_error"
    else:
        console.print(f"  [yellow]? unknown outcome (rc={rc})[/yellow]")
        outcome = "unknown"

    # Write JSON summary
    summary = {
        "hyp_id": hyp_id,
        "test_name": test_name,
        "test_path": str(saved_path),
        "outcome": outcome,
        "cargo_rc": rc,
        "cargo_elapsed_s": round(elapsed, 1),
        "cargo_log": str(log),
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
    }
    (output_dir / f"{test_name}.summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    console.print(f"  Summary: {output_dir / f'{test_name}.summary.json'}")


def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:50] or "finding"


def _strip_code_fences(text: str) -> str:
    """If the model wrapped the code in ```rust ... ```, extract the inside."""
    import re
    m = re.search(r"```(?:rust|rs)?\n(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text
