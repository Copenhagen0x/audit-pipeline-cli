"""`audit-pipeline derive-siblings` — auto-derive sibling hypotheses from a confirmed finding.

Tier 2 #8.

When a finding crosses into the `confirmed` lifecycle state, its bug
class often generalizes — there are typically N "sibling" attack
patterns sharing the same structural shape across (a) other call paths
in the same protocol and (b) other protocols of the same class. This
command reads the confirmed finding from findings.db, asks Claude to
extract the structural pattern + emit N sibling hypotheses, and writes
those siblings to a yaml file in the workspace.

The output yaml is loadable by the existing scoping pipeline as a regular
hypothesis library — no schema fork. The user can then merge it into the
appropriate class library by hand, or just leave it as a standalone file
for the next hunt cycle to pick up.

Usage:
    audit-pipeline derive-siblings 378                     # by finding id
    audit-pipeline derive-siblings 378 --num 8 --output -  # 8 siblings to stdout
    audit-pipeline derive-siblings 378 --append-to perp_dex_class.yaml

Lifecycle hook:
    When `lifecycle.transition(... to_status='confirmed')` fires, the
    transition module schedules `derive_siblings_async(finding_id)` as
    a fire-and-forget. The hook never blocks the transition — if the
    LLM is unreachable, the transition still succeeds; the siblings are
    just not generated this cycle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console

from audit_pipeline.db import FindingsDB
from audit_pipeline.utils import LLMUnavailable, complete, is_available

console = Console()


SIBLING_PROMPT = """You are a senior Solana protocol security researcher. A confirmed
finding has been disclosed in a {protocol_class} protocol. Your job is
to derive {num} STRUCTURAL SIBLINGS of this finding — additional
hypotheses that would catch the same root-cause class in adjacent code
paths or other protocols of the same class.

CONFIRMED FINDING
─────────────────
Hypothesis ID:   {hypothesis_id}
Title:           {title}
Severity:        {severity}
Bug class:       {bug_class}
Target file:     {target_file}
Claim:
{claim}

YOUR TASK
─────────
Emit a YAML document of exactly {num} sibling hypotheses targeting variations
of the same root-cause structure. Each sibling must be a falsifiable claim
about an invariant that should hold — phrased so a clean negative result
strengthens the disclosure.

OUTPUT SCHEMA (per hypothesis)
─────────────────────────────
- id: <prefix><n>-<kebab-slug>     # short, stable; convention: SIB-<parent>-<n>
  class: invariant_property | state_transition | authorization | arithmetic_overflow | implicit_invariant
  severity: Critical | High | Medium | Low
  claim: >
    <falsifiable claim, 2-4 sentences>
  applies_to: [<protocol-name>, ...]   # cluster names ok
  scope_conditions: [<predicate>, ...]   # see the protocol's scope vocab
  bug_class: <stable cross-protocol class id, kebab-case>

CONSTRAINTS
───────────
- Each sibling MUST share the parent's bug_class root (same prefix), but
  may extend it (e.g. `insurance-counter-vault-divergence-on-resolve`).
- Each sibling MUST target a distinct attack surface from the parent.
- DO NOT restate the parent finding.
- DO NOT invent protocol features that don't exist.
- Output MUST be valid YAML, top-level key `hypotheses:`, no markdown
  fence, no commentary outside the yaml.
"""


@click.command(name="derive-siblings")
@click.argument("finding_id", type=int)
@click.option(
    "--num", "-n",
    type=int, default=6, show_default=True,
    help="Number of sibling hypotheses to derive",
)
@click.option(
    "--protocol-class",
    default=None,
    help=(
        "Protocol class context for the LLM (default: inferred from the "
        "finding's target). One of: perp_dex, amm_cp, clmm, lending, lst."
    ),
)
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path), default=None,
    help=(
        "Output file path. Default: <workspace>/derived/<finding-id>-siblings.yaml. "
        "Use '-' to write to stdout."
    ),
)
@click.option(
    "--append-to",
    type=click.Path(path_type=Path), default=None,
    help=(
        "Append the siblings to an existing yaml library file (in-place "
        "merge under the `hypotheses:` key). Skips conflicting ids."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Override LLM model (default: configured DEFAULT_MODEL)",
)
@click.pass_context
def derive_siblings_cmd(
    ctx: click.Context,
    finding_id: int,
    num: int,
    protocol_class: str | None,
    output: Path | None,
    append_to: Path | None,
    model: str | None,
) -> None:
    """Derive structural sibling hypotheses from a confirmed finding."""
    workspace = Path(ctx.obj["workspace"])
    db = FindingsDB(workspace / "findings.db")

    finding = _get_finding(db, finding_id)
    if not finding:
        raise click.ClickException(f"finding {finding_id} not found in DB")

    if (finding.get("status") or "").lower() not in ("confirmed", "disclosed", "fixed", "verified"):
        console.print(
            f"  [yellow]warning: finding {finding_id} status is "
            f"{finding.get('status')!r} — sibling derivation usually fires on "
            f"'confirmed' or later.[/yellow]"
        )

    inferred_class = protocol_class or _infer_protocol_class(db, finding) or "perp_dex"
    console.print(
        f"  [cyan]Deriving {num} siblings of finding {finding_id} "
        f"(class={inferred_class}, bug_class={finding.get('bug_class')!r})[/cyan]"
    )

    if not is_available():
        raise click.ClickException(
            "ANTHROPIC_API_KEY required to derive siblings. Set it and re-run."
        )

    prompt = SIBLING_PROMPT.format(
        protocol_class=inferred_class,
        hypothesis_id=finding.get("hypothesis_id") or f"#{finding_id}",
        title=finding.get("title") or "(no title)",
        severity=finding.get("severity") or "Unknown",
        bug_class=finding.get("bug_class") or "unspecified",
        target_file=finding.get("target_file") or "(unknown)",
        claim=(finding.get("claim") or finding.get("title") or "")[:2000],
        num=num,
    )

    kwargs = {}
    if model:
        kwargs["model"] = model
    response = complete(prompt, **kwargs)
    raw = response.text

    # Strip optional markdown fence ```yaml ... ```
    raw = _strip_md_fence(raw)
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise click.ClickException(
            f"LLM emitted invalid YAML: {e}\n\nRaw response:\n{raw[:600]}"
        )
    siblings = parsed.get("hypotheses") if isinstance(parsed, dict) else None
    if not isinstance(siblings, list) or not siblings:
        raise click.ClickException(
            f"LLM response did not contain a `hypotheses` list. Got: {parsed!r}"
        )

    # Tag each sibling with its parent finding for traceability
    for s in siblings:
        if isinstance(s, dict):
            s.setdefault("derived_from", finding.get("hypothesis_id") or f"finding-{finding_id}")

    out_payload = yaml.safe_dump({"hypotheses": siblings}, sort_keys=False, allow_unicode=True)

    # Output / append
    if str(output) == "-":
        click.echo(out_payload)
    else:
        out_path = output
        if out_path is None:
            derived_dir = workspace / "derived"
            derived_dir.mkdir(parents=True, exist_ok=True)
            slug = (finding.get("hypothesis_id") or f"finding-{finding_id}").replace("/", "-")
            out_path = derived_dir / f"{slug}-siblings.yaml"
        out_path.write_text(out_payload, encoding="utf-8")
        console.print(f"  [green]wrote[/green] {out_path} ({len(siblings)} siblings)")

    if append_to:
        appended = _append_siblings(append_to, siblings)
        console.print(
            f"  [green]appended[/green] {appended} new siblings to {append_to} "
            f"(skipped {len(siblings) - appended} duplicates)"
        )


def derive_siblings_async(workspace: Path, finding_id: int, num: int = 6) -> None:
    """Fire-and-forget hook target. Used by lifecycle.transition().

    Never raises. Logs failures to stderr and returns. The finding's
    lifecycle transition is independent — sibling derivation is a
    best-effort augmentation.
    """
    try:
        db = FindingsDB(workspace / "findings.db")
        finding = _get_finding(db, finding_id)
        if not finding:
            return
        if not is_available():
            return
        inferred_class = _infer_protocol_class(db, finding) or "perp_dex"
        prompt = SIBLING_PROMPT.format(
            protocol_class=inferred_class,
            hypothesis_id=finding.get("hypothesis_id") or f"#{finding_id}",
            title=finding.get("title") or "(no title)",
            severity=finding.get("severity") or "Unknown",
            bug_class=finding.get("bug_class") or "unspecified",
            target_file=finding.get("target_file") or "(unknown)",
            claim=(finding.get("claim") or finding.get("title") or "")[:2000],
            num=num,
        )
        try:
            response = complete(prompt)
        except LLMUnavailable:
            return
        raw = _strip_md_fence(response.text)
        try:
            parsed = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return
        siblings = parsed.get("hypotheses") if isinstance(parsed, dict) else None
        if not isinstance(siblings, list) or not siblings:
            return
        for s in siblings:
            if isinstance(s, dict):
                s.setdefault("derived_from", finding.get("hypothesis_id") or f"finding-{finding_id}")
        derived_dir = workspace / "derived"
        derived_dir.mkdir(parents=True, exist_ok=True)
        slug = (finding.get("hypothesis_id") or f"finding-{finding_id}").replace("/", "-")
        out_path = derived_dir / f"{slug}-siblings.yaml"
        out_path.write_text(
            yaml.safe_dump({"hypotheses": siblings}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        # Never block the lifecycle transition on derivation failure
        return


# ─────────────────────────── Internal helpers ──────────────────────────────


def _strip_md_fence(text: str) -> str:
    """Strip markdown ```yaml ... ``` fences (and bare ``` ... ```) if present."""
    s = text.strip()
    m = re.match(r"^```(?:yaml|yml)?\s*\n(.*?)```\s*$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _get_finding(db: FindingsDB, finding_id: int) -> dict[str, Any] | None:
    """Look up a finding by id."""
    for f in db.list_findings(limit=10000):
        if f.get("id") == finding_id:
            return f
    return None


def _infer_protocol_class(db: FindingsDB, finding: dict[str, Any]) -> str | None:
    """Infer a protocol class from the finding's target (best effort)."""
    from audit_pipeline.scoping import PROTOCOL_CLASSES
    target_id = finding.get("target_id")
    if target_id is None:
        return None
    targets = db.list_targets()
    target = next((t for t in targets if t.get("id") == target_id), None)
    if not target:
        return None
    name = (target.get("name") or "").lower()
    for cid, cdef in PROTOCOL_CLASSES.items():
        for proto in cdef.get("protocols") or []:
            if proto.lower() in name or name in proto.lower():
                return cid
    return None


def _append_siblings(library_path: Path, siblings: list[dict[str, Any]]) -> int:
    """Append new siblings to an existing library yaml. Returns # appended."""
    if library_path.exists():
        existing = yaml.safe_load(library_path.read_text(encoding="utf-8")) or {}
    else:
        existing = {}
    existing_hyps = existing.get("hypotheses") or []
    seen_ids = {h.get("id") for h in existing_hyps if isinstance(h, dict)}
    appended = 0
    for s in siblings:
        if not isinstance(s, dict):
            continue
        if s.get("id") in seen_ids:
            continue
        existing_hyps.append(s)
        seen_ids.add(s["id"])
        appended += 1
    existing["hypotheses"] = existing_hyps
    library_path.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return appended
