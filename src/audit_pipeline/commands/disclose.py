"""`audit-pipeline disclose` — generate disclosure documents from findings."""

import json
from pathlib import Path

import click
import yaml
from jinja2 import Template
from rich.console import Console

console = Console()


DISCLOSURE_README_TEMPLATE = """# {{ target_name }} audit — {{ audit_date }}

Independent security audit of {{ target_name }}.

**Audit window**: {{ audit_window }}
**Engine pin**: [`{{ engine_repo_slug }}`]({{ engine_repo_url }}) @ `{{ engine_branch }}` sha `{{ engine_sha }}`
**Wrapper pin**: [`{{ wrapper_repo_slug }}`]({{ wrapper_repo_url }}) @ `{{ wrapper_branch }}` sha `{{ wrapper_sha }}`
**Auditor**: {{ auditor_name }} ([@{{ auditor_handle }}](https://github.com/{{ auditor_handle }}))

## TL;DR

| # | Finding | Class |
|---|---|---|
{% for f in findings -%}
| {{ f.id }} | {{ f.title }} (`engine:{{ f.engine_line }}`) | **{{ f.class }}** |
{% endfor %}

## How to read this repo

| Path | What it is |
|---|---|
| [`DISCLOSURE.md`](./DISCLOSURE.md) | Canonical disclosure document |
| [`EXEC_BRIEF.md`](./EXEC_BRIEF.md) | One-page reference |
| [`RECOMMENDED_PATCH.md`](./RECOMMENDED_PATCH.md) | Exact diff for the recommended fix |
| [`RAW_KANI_RESULTS.md`](./RAW_KANI_RESULTS.md) | Verification times for the SAFE proofs |
| [`tests/`](./tests/) | All PoC test files |
| [`baseline/`](./baseline/) | Re-run of maintainer's existing Kani baseline |
| [`LICENSE`](./LICENSE) | CC BY 4.0 + Apache-2.0 |
"""


@click.command(name="disclose")
@click.option(
    "--findings",
    "-f",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file describing findings",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Output directory for disclosure docs",
)
@click.pass_context
def disclose_cmd(ctx: click.Context, findings: str, output: Path) -> None:
    """Generate disclosure documents (README, DISCLOSURE.md, etc.) from findings.yaml."""
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException("No workspace.json. Run `audit-pipeline init` first.")

    config = json.loads(config_path.read_text())

    with open(findings) as f:
        findings_data = yaml.safe_load(f)

    output.mkdir(parents=True, exist_ok=True)

    # Render the README
    template = Template(DISCLOSURE_README_TEMPLATE)
    rendered = template.render(
        target_name=config.get("target_name", "Target"),
        audit_date=findings_data.get("audit_date", "TBD"),
        audit_window=findings_data.get("audit_window", "TBD"),
        engine_repo_url=config["engine"]["repo"],
        engine_repo_slug=config["engine"]["repo"].replace("https://github.com/", ""),
        engine_branch=findings_data.get("engine_branch", "master"),
        engine_sha=config["engine"]["sha"],
        wrapper_repo_url=config["wrapper"]["repo"],
        wrapper_repo_slug=config["wrapper"]["repo"].replace("https://github.com/", ""),
        wrapper_branch=findings_data.get("wrapper_branch", "main"),
        wrapper_sha=config["wrapper"]["sha"],
        auditor_name=findings_data.get("auditor_name", "Anonymous"),
        auditor_handle=findings_data.get("auditor_handle", "anonymous"),
        findings=findings_data.get("findings", []),
    )

    readme_path = output / "README.md"
    readme_path.write_text(rendered)
    console.print(f"[green]Wrote {readme_path}[/green]")

    # Stub the other expected docs (user fills in details from per-finding agent prompts)
    for stub_filename, stub_content in [
        ("DISCLOSURE.md", "# Full disclosure\n\nUse `audit-pipeline disclose --finding <id>` per finding to generate sections.\n"),
        ("EXEC_BRIEF.md", "# Executive brief\n\nOne-page summary. Generated from findings.yaml.\n"),
        ("RECOMMENDED_PATCH.md", "# Recommended patch\n\nExact diffs for each finding's fix.\n"),
        ("RAW_KANI_RESULTS.md", "# Raw Kani results\n\nTable of SAFE proof verification times.\n"),
    ]:
        stub_path = output / stub_filename
        if not stub_path.exists():
            stub_path.write_text(stub_content)

    console.print()
    console.print(f"[bold green]Disclosure scaffold ready at {output}[/bold green]")
    console.print("  Fill in DISCLOSURE.md per-finding sections using prompt 11 (disclosure_documentation)")
