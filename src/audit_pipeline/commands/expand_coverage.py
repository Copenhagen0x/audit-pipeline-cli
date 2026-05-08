"""`audit-pipeline expand-coverage` — generate hypotheses from non-disclosure sources.

Three sources, one command:

  1. spec.md            — every numbered design claim becomes one hypothesis
  2. kani coverage gaps — functions WITHOUT a Kani harness become candidates
  3. wrapper public handlers — every BPF instruction handler becomes a hypothesis

Together with `learn-from-disclosures`, this gives us coverage along
multiple axes: known-bug-class siblings (disclosures), explicit design
contract (spec), formally-unverified terrain (Kani gaps), and attack
surface (wrapper).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
import yaml
from rich.console import Console

from audit_pipeline.utils import complete, is_available

console = Console()


SPEC_PROMPT = """You are extracting testable security hypotheses from a design specification
for a Solana perpetual DEX engine ("Percolator"). The spec text below uses numbered
section headers like `§1.4`, `§4.11`, `§5.5`, etc. Each numbered section asserts a
design property.

Your job: for every numbered section, generate ONE testable hypothesis that, if
violated by the actual code, would indicate a bug.

# Spec text

<SPEC_TEXT>

# Output

Return ONLY YAML in this schema (no surrounding prose):

```yaml
hypotheses:
  - id: SPEC-<section-number>-<short-name>
    class: <state_transition|invariant_property|authorization|arithmetic_overflow|implicit_invariant>
    severity: <Critical|High|Medium|Low>
    claim: |
      <one paragraph: state the spec claim, then state how violation would manifest in code>
    spec_reference: "§<section_number>"
    relevant_constants: |
      <function_name_1>
      <function_name_2>
    relevant_instructions: |
      <wrapper instruction names that touch this property>
  - id: SPEC-<next-section>-...
    ...
```

Rules:
- Generate ONE hypothesis per numbered section that asserts a non-trivial property
  (skip definitional sections that don't have a falsifiable claim).
- Use REAL function names from the spec where it cites them.
- The `claim` must be falsifiable: phrase it as "X holds at all reachable states"
  or "every call to Y satisfies Z", not vague aspirations.
- Severity: assign based on what an exploit would cost. Spec violations on
  vault/insurance/PnL accounting are usually Critical or High.
- Generate up to 40 hypotheses. Prefer depth over breadth — a few precise
  hypotheses beat many vague ones.
"""


KANI_GAP_PROMPT = """You're identifying engine functions that lack Kani harness coverage.
For each gap function, generate a hypothesis Kani could verify if a harness existed.

# Audited functions list (TSV-style)

<KANI_LIST>

# Engine source preview

<ENGINE_PREVIEW>

# Output

Return ONLY YAML — for the top 15 most security-critical functions WITHOUT
Kani coverage, generate one hypothesis each. Schema:

```yaml
hypotheses:
  - id: KGAP-<short-fn-name>
    class: <invariant_property|state_transition|arithmetic_overflow|authorization>
    severity: <Critical|High|Medium|Low>
    claim: |
      <invariant the function should preserve, phrased as a falsifiable claim>
    relevant_constants: |
      <function_name>
    notes: "Kani-uncovered function (no proof harness exists in current corpus)"
```

Rules:
- Skip read-only / accessor / debug functions
- Focus on state-mutating, vault-touching, insurance-touching, PnL-touching paths
"""


WRAPPER_PROMPT = """You're auditing the BPF wrapper for a Solana perpetual DEX engine. Each
public instruction handler is a potential attack surface. The wrapper source
preview below shows the instruction-decode block and several handler bodies.

# Wrapper source preview

<WRAPPER_PREVIEW>

# Output

Return ONLY YAML — generate one hypothesis per public instruction handler
you can identify. Schema:

```yaml
hypotheses:
  - id: WRAP-<instruction-name>
    class: <authorization|state_transition|invariant_property>
    severity: <Critical|High|Medium|Low>
    claim: |
      <falsifiable claim about what the handler must enforce —
      e.g. signer checks, state preconditions, invariant preservation>
    relevant_constants: |
      <handler function or relevant helpers>
    relevant_instructions: |
      <instruction enum variant>
```

Rules:
- One hypothesis per instruction
- Severity: based on what the instruction can do (drain insurance / mutate
  another user's account / change config = Critical; read-only or self-only = Low)
- Cite real instruction names from the InstructionTag enum or match arms
"""


@click.command(name="expand-coverage")
@click.option(
    "--spec", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None, help="Path to spec.md (default: <workspace>/<engine>/spec.md)",
)
@click.option(
    "--kani-list", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None, help="Path to kani-list.json or kani_audit.tsv",
)
@click.option(
    "--wrapper-src", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None, help="Path to wrapper percolator.rs",
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), required=True,
)
@click.option(
    "--sources", default="spec,kani,wrapper", show_default=True,
    help="Comma-separated sources to use (spec/kani/wrapper)",
)
@click.pass_context
def expand_coverage_cmd(
    ctx: click.Context,
    spec: Path | None,
    kani_list: Path | None,
    wrapper_src: Path | None,
    output: Path,
    sources: str,
) -> None:
    """Generate hypotheses from spec, Kani-coverage gaps, and wrapper handlers."""
    if not is_available():
        raise click.ClickException("ANTHROPIC_API_KEY required.")

    workspace = Path(ctx.obj["workspace"])
    config = json.loads((workspace / "workspace.json").read_text())
    engine_dir = workspace / config["engine"]["local"]
    wrapper_dir = workspace / config["wrapper"]["local"]

    spec = spec or (engine_dir / "spec.md")
    kani_list = kani_list or (engine_dir / "kani_audit_final.tsv")
    wrapper_src = wrapper_src or (wrapper_dir / "src" / "percolator.rs")
    enabled = {s.strip() for s in sources.split(",")}

    output.parent.mkdir(parents=True, exist_ok=True)
    all_hyps: list[dict] = []

    if "spec" in enabled and spec.exists():
        console.print(f"[bold]Spec[/bold] — reading {spec}")
        spec_text = spec.read_text(encoding="utf-8", errors="replace")[:60000]
        prompt = SPEC_PROMPT.replace("<SPEC_TEXT>", spec_text)
        try:
            resp = complete(prompt, max_tokens=8192)
            parsed = _parse_yaml_block(resp.text)
            if parsed and isinstance(parsed.get("hypotheses"), list):
                for h in parsed["hypotheses"]:
                    h["source"] = "spec"
                    all_hyps.append(h)
                console.print(f"  [green]✓[/green] {len(parsed['hypotheses'])} spec-derived hypotheses")
            else:
                console.print("  [red]✗[/red] no hypotheses parsed from spec")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✗[/red] {e}")

    if "kani" in enabled and kani_list.exists():
        console.print(f"[bold]Kani gaps[/bold] — reading {kani_list}")
        list_text = kani_list.read_text(encoding="utf-8", errors="replace")[:30000]
        engine_preview = (engine_dir / "src" / "percolator.rs").read_text(
            encoding="utf-8", errors="replace"
        )[:15000] if (engine_dir / "src" / "percolator.rs").exists() else ""
        prompt = (
            KANI_GAP_PROMPT
            .replace("<KANI_LIST>", list_text)
            .replace("<ENGINE_PREVIEW>", engine_preview)
        )
        try:
            resp = complete(prompt, max_tokens=8192)
            parsed = _parse_yaml_block(resp.text)
            if parsed and isinstance(parsed.get("hypotheses"), list):
                for h in parsed["hypotheses"]:
                    h["source"] = "kani-gap"
                    all_hyps.append(h)
                console.print(f"  [green]✓[/green] {len(parsed['hypotheses'])} Kani-gap hypotheses")
            else:
                console.print("  [red]✗[/red] no hypotheses parsed")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✗[/red] {e}")

    if "wrapper" in enabled and wrapper_src.exists():
        console.print(f"[bold]Wrapper[/bold] — reading {wrapper_src}")
        wrapper_text = wrapper_src.read_text(encoding="utf-8", errors="replace")[:60000]
        prompt = WRAPPER_PROMPT.replace("<WRAPPER_PREVIEW>", wrapper_text)
        try:
            resp = complete(prompt, max_tokens=8192)
            parsed = _parse_yaml_block(resp.text)
            if parsed and isinstance(parsed.get("hypotheses"), list):
                for h in parsed["hypotheses"]:
                    h["source"] = "wrapper"
                    all_hyps.append(h)
                console.print(f"  [green]✓[/green] {len(parsed['hypotheses'])} wrapper-derived hypotheses")
            else:
                console.print("  [red]✗[/red] no hypotheses parsed")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✗[/red] {e}")

    out_doc = {
        "schema": "audit-pipeline.expand-coverage.v1",
        "hypotheses": all_hyps,
    }
    output.write_text(yaml.safe_dump(out_doc, sort_keys=False), encoding="utf-8")
    console.print()
    console.print(f"[bold green]Wrote {len(all_hyps)} total hypotheses[/bold green] -> {output}")
    by_source = {}
    for h in all_hyps:
        s = h.get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1
    for s, n in by_source.items():
        console.print(f"  {s}: {n}")


def _parse_yaml_block(text: str):
    m = re.search(r"```ya?ml\n(.+?)```", text, re.DOTALL)
    raw = m.group(1) if m else text
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
