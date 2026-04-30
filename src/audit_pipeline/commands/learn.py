"""`audit-pipeline learn-from-disclosures` — generate hypotheses from public bug reports.

Takes a list of GitHub issue URLs (Percolator disclosures, F7-class
findings, etc.), uses Claude to extract the structural attack pattern
from each, and emits a hypothesis YAML file targeting siblings of those
disclosed bugs.

This is the "find next bug in the same family" loop:
  qedbot's #60 was a sibling of #33.
  Dark Cobra's #62 was a sibling of #60.
  Jelleo's job is to find the next sibling autonomously.

Usage:
  audit-pipeline learn-from-disclosures \\
      --url https://github.com/aeyakovenko/percolator-prog/issues/60 \\
      --url https://github.com/aeyakovenko/percolator-prog/issues/62 \\
      --output disclosure_derived.yaml
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import click
import yaml
from rich.console import Console

from audit_pipeline.utils import complete, is_available

console = Console()


EXTRACTION_PROMPT = """You are a senior security researcher. The Markdown below is a public
disclosure of a confirmed bug in a Solana perpetual DEX (Percolator).
Extract the STRUCTURAL ATTACK PATTERN so we can generate hypotheses
for siblings of this bug.

# Disclosure body

<DISCLOSURE_BODY>

# Extract the following — return as YAML

Return EXACTLY this YAML schema (no surrounding prose):

```yaml
disclosure_id: "<short-id-from-issue-title>"
issue_url: "<URL>"
severity: "<Critical|High|Medium|Low>"
root_cause_class: "<one-line family — e.g. accrual-helper-asymmetry, integer-overflow-on-funding, lazy-MTM-bypass>"
affected_functions:
  - "<engine_or_wrapper_function_name>"
  - "<another>"
attack_preconditions:
  - "<precondition like 'self-matched paired position open'>"
  - "<another>"
attack_sequence:
  - "<step 1: instruction + state required>"
  - "<step 2>"
  - "<step 3>"
violated_invariant: "<one-line description of the invariant broken>"
fix_landed_in: "<commit SHA or 'unfixed'>"
sibling_hypotheses:
  # 3-5 NEW hypotheses targeting structural siblings of this bug.
  # Each is framed as a falsifiable invariant claim — not a restatement
  # of the disclosed bug, but a NEW invariant in the same family that
  # might be violated by an unexamined code path.
  - id: "DISC-<id>-S1"
    class: "<state_transition|invariant_property|authorization|arithmetic_overflow|implicit_invariant>"
    severity: "<Critical|High|Medium|Low>"
    claim: "<one-paragraph falsifiable claim about a sibling invariant>"
    relevant_constants: |
      <function1>
      <function2>
    relevant_instructions: |
      <wrapper instruction names>
  - id: "DISC-<id>-S2"
    ...
```

Rules:
- The sibling hypotheses MUST be NEW invariants, not the disclosed bug
  itself. Imagine: "This same bug class with a different door."
- Reference REAL function names from the disclosure where possible
- Be precise — avoid vague claims like "all paths must be safe"
- Each sibling should be testable by reading the affected functions
"""


@click.command(name="learn-from-disclosures")
@click.option(
    "--url", "-u", multiple=True, required=True,
    help="GitHub issue URL of a public disclosure (repeatable)",
)
@click.option(
    "--output", "-o", type=click.Path(path_type=Path), required=True,
    help="Output YAML path for generated hypotheses",
)
@click.option(
    "--max-tokens", type=int, default=8192, show_default=True,
)
@click.pass_context
def learn_cmd(
    ctx: click.Context,
    url: tuple[str, ...],
    output: Path,
    max_tokens: int,
) -> None:
    """Generate hypotheses from public bug-disclosure issues."""
    if not is_available():
        raise click.ClickException("ANTHROPIC_API_KEY required.")

    output.parent.mkdir(parents=True, exist_ok=True)

    all_disclosures: list[dict] = []
    all_hypotheses: list[dict] = []

    for u in url:
        console.print(f"[bold]Fetching[/bold] {u}")
        body = _fetch_issue_body(u)
        if not body:
            console.print(f"  [red]✗[/red] could not fetch {u}")
            continue
        console.print(f"  fetched {len(body):,} chars")

        prompt = EXTRACTION_PROMPT.replace("<DISCLOSURE_BODY>", body[:15000])
        console.print(f"  extracting structural pattern...")
        try:
            resp = complete(prompt, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✗[/red] LLM error: {e}")
            continue

        # Parse YAML out of the response (between code fences if present)
        yaml_text = _extract_yaml(resp.text)
        try:
            parsed = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            console.print(f"  [red]✗[/red] YAML parse error: {e}")
            (output.parent / f"raw_{_slug(u)}.txt").write_text(resp.text, encoding="utf-8")
            continue

        if not isinstance(parsed, dict):
            console.print(f"  [red]✗[/red] expected dict, got {type(parsed).__name__}")
            continue

        sibs = parsed.pop("sibling_hypotheses", [])
        all_disclosures.append({"url": u, **parsed})
        if isinstance(sibs, list):
            for h in sibs:
                if isinstance(h, dict) and "id" in h:
                    h["source_disclosure"] = parsed.get("disclosure_id", u)
                    all_hypotheses.append(h)

        console.print(f"  [green]✓[/green] extracted {len(sibs)} sibling hypotheses")

    out_doc = {
        "schema": "audit-pipeline.disclosure-derived.v1",
        "source_disclosures": all_disclosures,
        "hypotheses": all_hypotheses,
    }
    output.write_text(yaml.safe_dump(out_doc, sort_keys=False), encoding="utf-8")

    console.print()
    console.print(f"[bold green]Wrote[/bold green] {len(all_hypotheses)} hypotheses to {output}")
    console.print(f"  source disclosures: {len(all_disclosures)}")


def _fetch_issue_body(url: str) -> str:
    """Fetch a GitHub issue body. Tries gh CLI, falls back to unauth'd public API."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    if not m:
        return ""
    owner, repo, num = m.group(1), m.group(2), m.group(3)

    # Try gh CLI first (works if authenticated)
    for cmd in (
        ["gh", "api", f"repos/{owner}/{repo}/issues/{num}", "--jq", ".body"],
        ["gh", "issue", "view", num, "--repo", f"{owner}/{repo}", "--json", "body", "-q", ".body"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
            if result.stdout.strip():
                return result.stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    # Fallback: unauth'd public API via curl (60 req/hr per IP — fine for our use)
    try:
        import requests
        api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
        r = requests.get(api_url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "audit-pipeline-jelleo",
        }, timeout=30)
        if r.status_code == 200:
            data = r.json()
            return data.get("body") or ""
    except Exception:  # noqa: BLE001
        pass

    return ""


def _extract_yaml(text: str) -> str:
    """Extract YAML from a response, looking for ```yaml ... ``` fences first."""
    m = re.search(r"```ya?ml\n(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Fallback: assume the whole text is YAML
    return text


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text)[:40]
