"""audit-pipeline cold-verify  —  Layer 1.5 → Layer 2 pre-gate.

Cycle 20260514-151541 exposed three failure modes that the existing
recon → debate → poc flow can't catch on its own:

  1. Phantom hyps — proposer correctly concludes "the hypothesis does
     not apply to this codebase" (e.g. APT29 auction-settle-no-winner
     against a Move codebase that has no auction module), but the
     verdict parser glitches and records TRUE. The phantom enters L2
     and wastes ~$0.50 on a PoC test for code that doesn't exist.

  2. Duplicate clusters — multiple hyps converge on the same
     underlying bug (e.g. APT38 + APTM2 both target
     treasury::emergency_withdraw permissionless drain). Without
     dedup, each dispatches its own L2 PoC test. The pre-existing
     cluster dedup runs at L2.5 (AFTER the L2 spend), not before.

  3. False-negative demotions — hunt's L1.5 logic auto-demotes any
     TRUE proposer verdict where the challenger response text
     contains the word "DISAGREE". But "DISAGREE" semantically means
     either "the bug doesn't exist" (refute) OR "the bug exists AND
     my view is even stronger" (amplify). The current code conflates
     these, silently dropping hyps the challenger actually amplified
     (APT7, APT10, APT12 on cycle 20260514-151541).

This module runs AFTER L1.5 and BEFORE L2 dispatch. It writes a
``l2_candidates.json`` artifact the operator approves before any L2
PoC author spend is committed.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

_console = Console()

# Phrases that, when found in the proposer body, indicate the hypothesis
# was not applicable to the codebase — the agent correctly identified
# the precondition doesn't hold. These should NEVER reach L2 even if the
# verdict parser glitches and records them as TRUE.
PHANTOM_PHRASES = (
    "does not apply",
    "presupposes the existence of",
    "no such module exists",
    "no auction module",
    "preconditioned attack surface does not exist",
    "hypothesis simply does not apply",
    "prerequisite feature simply does not exist",
    "construct does not exist anywhere",
    "the attack surface does not exist",
)


# Phrases in a challenger response that indicate AMPLIFICATION (not
# refutation) when paired with a "DISAGREE" verdict. If any of these
# appear, the auto-demote logic should be SUPPRESSED — the bug is
# still real, the challenger just has a stronger view.
AMPLIFY_PHRASES = (
    "stronger than stated",
    "understated",
    "additional finding",
    "additional bug",
    "additional issue",
    "additional confirmed",
    "the proposer missed",
    "missed by the proposer",
    "cannot find a meaningful hole",
    "cannot find a hole",
    "cannot find any hole",
    "more critically",
    "amplification",
    "amplifies",
    "expanded attack surface",
    "wider than described",
    "more accessible than stated",
    "easier than described",
    "easier to trigger",
    "even more certain",
    "more severe",
    "the vulnerability is real",
    "the verdict stands",
    "the conclusion holds",
    "the finding is correct",
)


@dataclass
class CandidateDecision:
    hyp_id: str
    final_status: str  # "keep" | "drop"
    drop_reason: str | None = None
    cluster_id: str | None = None
    is_cluster_rep: bool = False
    body_verdict: str | None = None
    recon_verdict: str | None = None
    bug_class: str | None = None
    target_file: str | None = None
    engine_function: str | None = None
    severity: str | None = None
    notes: list[str] = field(default_factory=list)


def _read_response(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _is_phantom_body(text: str) -> tuple[bool, str | None]:
    """Return (is_phantom, matched_phrase). Case-insensitive substring."""
    low = text.lower()
    for phrase in PHANTOM_PHRASES:
        if phrase in low:
            return True, phrase
    return False, None


def _is_amplification(challenger_text: str) -> tuple[bool, str | None]:
    """Detect 'challenger says DISAGREE but actually agrees the bug is real'."""
    if not challenger_text:
        return False, None
    low = challenger_text.lower()
    for phrase in AMPLIFY_PHRASES:
        if phrase in low:
            return True, phrase
    return False, None


def _claim_canon(claim: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation, take first 80 chars."""
    s = re.sub(r"[^a-z0-9 ]", " ", (claim or "").lower())
    s = " ".join(s.split())
    return s[:80]


_FILE_LINE_RE = re.compile(
    # `sources/foo.move:42`, `foo.move:42-44`, `foo.move:42–44` (em-dash),
    # `foo.move line 42`, `foo.move:42:`. Captures (filename, primary line).
    r"\b(?:sources/)?([a-zA-Z_][\w.-]*\.(?:move|rs|sol|c|h))[:\s]*"
    r"(?:line\s+)?(\d{1,5})(?:\s*[-–to ]+\s*\d{1,5})?",
    re.IGNORECASE,
)


def _extract_code_sites(text: str, top_k: int = 3) -> list[tuple[str, int]]:
    """Pull the most-cited (file, line) pairs from a recon/debate body.

    Two hyps that cite overlapping (file, line) sites are almost
    certainly targeting the same bug regardless of how their
    bug_class / target_file / claim wording differ at the surface.
    Returns up to top_k unique sites, sorted by citation frequency.
    """
    if not text:
        return []
    counts: dict[tuple[str, int], int] = defaultdict(int)
    for m in _FILE_LINE_RE.finditer(text):
        fname = m.group(1).lower()
        try:
            line = int(m.group(2))
        except (TypeError, ValueError):
            continue
        # Snap to nearest 5-line bucket so 61 and 64 collapse to the
        # same site (proposers cite slightly different anchor lines
        # for the same function body).
        bucket = (line // 5) * 5
        counts[(fname, bucket)] += 1
    return [site for site, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:top_k]


def cluster_candidates(
    candidates: list[dict[str, Any]],
    response_texts: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Group candidate hyps by underlying bug. Returns cluster_id -> [hyp_ids].

    Two-pass clustering:
      1. SURFACE cluster — exact match on
         (bug_class, target_file, claim-canon). Catches identical
         hyp instances (rare in practice — different libraries usually
         have small surface differences).
      2. CODE-SITE cluster — when response_texts are provided, extract
         the top (file, line-bucket) citations from each proposer
         body and cluster hyps whose top-cited sites overlap. This is
         the real dedup signal: two hyps that both name
         `treasury.move:60-65` as the bug location are testing the
         same code path no matter how their YAML metadata diverges.
         Cycle 20260514-151541 caught APT38↔APTM2, APT36↔APTM1,
         APT9↔APTM13 — all 3 had different target_file (glob vs
         specific) and different engine_function (template vs exact
         function name), but cited the same code site at L1.

    Representative selection: APTM* (medium-specific) over APT*
    (class-library template), then shorter id (more direct), then
    alphabetic.
    """
    # Pass 1 — surface cluster
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for c in candidates:
        key = (
            (c.get("bug_class") or "").lower().strip(),
            (c.get("target_file") or "").lower().strip(),
            _claim_canon(c.get("claim", "")),
        )
        groups[key].append(c["id"])

    out: dict[str, list[str]] = {}
    used_ids: set[str] = set()

    def _rep_sort_key(hid: str) -> tuple[int, int, str]:
        is_aptm = 0 if hid.startswith("APTM") else 1
        return (is_aptm, len(hid), hid)

    for i, (key, ids) in enumerate(sorted(groups.items())):
        if len(ids) < 2:
            continue
        cluster_id = f"surface-{i:03d}-{key[0] or 'na'}"
        out[cluster_id] = sorted(ids, key=_rep_sort_key)
        used_ids.update(ids)

    # Pass 2 — code-site cluster (only when we have response bodies)
    #
    # Fix from the first cold-verify run (cycle 20260514-151541): clustering on
    # "any-of-top-3 sites overlap" over-merged. Many proposers cite multiple
    # files in their response — e.g. an oracle-staleness response cites
    # oracle.move (primary) AND lending_pool.move + vault.move (where the
    # stale price flows). Union-find on ANY shared site then chained every
    # hyp that touched a popular file like vault.move into one giant cluster.
    # Result: 51 dropped, 10 kept — collapsed 16+ distinct bugs to a single
    # "vault.move:125" cluster representative.
    #
    # New rule: each hyp gets a SINGLE primary code site (its most-cited
    # location). Two hyps cluster IFF their primary sites are identical
    # AND they share the same bug_class (or one has a glob target_file
    # and the other a specific match for the same module).
    if response_texts:
        primary_by_hyp: dict[str, tuple[str, int]] = {}
        bug_class_by_hyp: dict[str, str] = {}
        for c in candidates:
            hid = c["id"]
            if hid in used_ids:
                continue
            text = response_texts.get(hid, "")
            top = _extract_code_sites(text, top_k=1)
            if top:
                primary_by_hyp[hid] = top[0]
                bug_class_by_hyp[hid] = (c.get("bug_class") or "").lower().strip()

        site_to_hyps: dict[tuple[str, int], list[str]] = defaultdict(list)
        for hid, site in primary_by_hyp.items():
            site_to_hyps[site].append(hid)

        for j, (site, hyps) in enumerate(sorted(site_to_hyps.items())):
            if len(hyps) < 2:
                continue
            # Within hyps that share the same primary site, only cluster
            # those whose bug_class is compatible. This blocks the
            # "oracle-staleness" + "u64-overflow" co-citation collapse:
            # they happen to both cite oracle.move but test different
            # invariants. Treat bug_classes as compatible when they're
            # the same string OR one is a substring of the other
            # (e.g. "treasury-drain" ⊆ "missing-auth-treasury-drain").
            from itertools import groupby
            # Sort by bug_class so groupby works, then cluster within
            # each compatibility group
            hyps_sorted = sorted(hyps, key=lambda h: bug_class_by_hyp.get(h, ""))
            for k, group in groupby(hyps_sorted, key=lambda h: bug_class_by_hyp.get(h, "")):
                gids = list(group)
                if len(gids) < 2:
                    continue
                cluster_id = f"site-{j:03d}-{site[0]}-L{site[1]:04d}-{k or 'na'}"
                out[cluster_id] = sorted(gids, key=_rep_sort_key)

    return out


def cold_verify_cycle(
    cycle_dir: Path,
    hyp_lib_path: Path,
) -> dict[str, Any]:
    """Run the full cold-verification pre-gate. Writes l2_candidates.json
    under cycle_dir and returns the decisions dict.
    """
    import yaml

    recon_summary = json.loads((cycle_dir / "recon" / "recon_summary.json").read_text())
    hyp_by_id = {
        h["id"]: h
        for h in yaml.safe_load(hyp_lib_path.read_text())["hypotheses"]
    }

    debate_dir = cycle_dir / "debate"
    decisions: dict[str, CandidateDecision] = {}

    # Stage 1: For every TRUE recon verdict, run phantom-body + body-verdict-mismatch checks.
    for v in recon_summary["verdicts"]:
        hid = v["hypothesis_id"]
        h = hyp_by_id.get(hid, {})
        recon_verdict = v.get("verdict")
        d = CandidateDecision(
            hyp_id=hid,
            final_status="keep" if recon_verdict == "TRUE" else "drop",
            drop_reason=None if recon_verdict == "TRUE" else f"L1 verdict {recon_verdict}",
            recon_verdict=recon_verdict,
            bug_class=h.get("bug_class"),
            target_file=h.get("target_file"),
            engine_function=h.get("engine_function"),
            severity=h.get("severity"),
        )
        if recon_verdict != "TRUE":
            decisions[hid] = d
            continue

        # Read proposer body
        proposer_text = _read_response(cycle_dir / "recon" / f"{hid}_response.md")
        if proposer_text:
            is_phantom, phrase = _is_phantom_body(proposer_text)
            if is_phantom:
                d.final_status = "drop"
                d.drop_reason = f"phantom-hyp (body contains: {phrase!r})"
                d.notes.append(
                    "Proposer correctly identified hypothesis precondition "
                    "doesn't apply; verdict parser glitch recorded as TRUE."
                )

        decisions[hid] = d

    # Stage 2: Re-promote L1.5-promoted FALSE candidates (those whose
    # challenger DISAGREEd and pushed them to L2). These currently live
    # in hunt's in-memory candidate list and are lost when hunt exits.
    # Recover them by reading challenger responses directly.
    promoted_from_false: list[str] = []
    for v in recon_summary["verdicts"]:
        hid = v["hypothesis_id"]
        if v.get("verdict") != "FALSE":
            continue
        challenger_path = debate_dir / f"{hid}_challenger_response.md"
        if not challenger_path.is_file():
            continue
        chal = challenger_path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bDISAGREE\b", chal, re.IGNORECASE):
            promoted_from_false.append(hid)
            d = decisions.get(hid)
            if d is None:
                h = hyp_by_id.get(hid, {})
                d = CandidateDecision(
                    hyp_id=hid,
                    final_status="keep",
                    recon_verdict=v.get("verdict"),
                    bug_class=h.get("bug_class"),
                    target_file=h.get("target_file"),
                    engine_function=h.get("engine_function"),
                    severity=h.get("severity"),
                )
                decisions[hid] = d
            else:
                d.final_status = "keep"
                d.drop_reason = None
            d.notes.append("L1.5 challenger DISAGREEd with FALSE proposer; promoted to L2.")

    # Stage 3: Detect false-negative demotions. For every TRUE proposer +
    # DISAGREE challenger, check if the challenger AMPLIFIED (=keep) vs
    # REFUTED (=drop).
    for hid, d in list(decisions.items()):
        if d.recon_verdict != "TRUE" or d.final_status == "drop":
            continue
        challenger_path = debate_dir / f"{hid}_challenger_response.md"
        if not challenger_path.is_file():
            continue
        chal = challenger_path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bDISAGREE\b", chal, re.IGNORECASE):
            is_amp, phrase = _is_amplification(chal)
            if is_amp:
                d.notes.append(
                    f"L1.5 challenger said DISAGREE but actually AMPLIFIES "
                    f"(matched: {phrase!r}). Keep — hunt's naive demote "
                    "rule would have wrongly dropped this."
                )
            else:
                d.notes.append(
                    "L1.5 challenger DISAGREE without amplification markers; "
                    "may be a genuine refutation. Keep but flag for review."
                )

    # Stage 4: Cluster dedup across all kept candidates
    kept = [
        {"id": d.hyp_id, **{k: v for k, v in asdict(d).items() if k != "notes"}}
        for d in decisions.values()
        if d.final_status == "keep"
    ]
    # Merge in original hyp fields needed for clustering
    for c in kept:
        h = hyp_by_id.get(c["id"], {})
        c.setdefault("claim", h.get("claim", ""))
        c.setdefault("bug_class", h.get("bug_class"))
        c.setdefault("target_file", h.get("target_file"))

    # Load proposer + challenger bodies for code-site clustering.
    # Pass 2 of cluster_candidates uses these to find hyps that cite
    # overlapping (file, line-bucket) sites — the real signal that two
    # hyps test the same underlying bug regardless of YAML metadata.
    response_texts: dict[str, str] = {}
    for c in kept:
        hid = c["id"]
        proposer = _read_response(cycle_dir / "recon" / f"{hid}_response.md")
        challenger = _read_response(debate_dir / f"{hid}_challenger_response.md")
        response_texts[hid] = (proposer or "") + "\n" + (challenger or "")
    clusters = cluster_candidates(kept, response_texts=response_texts)
    for cluster_id, ids in clusters.items():
        rep = ids[0]
        for hid in ids:
            d = decisions[hid]
            d.cluster_id = cluster_id
            if hid == rep:
                d.is_cluster_rep = True
            else:
                d.final_status = "drop"
                d.drop_reason = f"duplicate of {rep} (cluster {cluster_id})"

    # Build summary
    keep_list = sorted([d.hyp_id for d in decisions.values() if d.final_status == "keep"])
    drop_list = sorted([(d.hyp_id, d.drop_reason) for d in decisions.values() if d.final_status == "drop"])

    out_path = cycle_dir / "l2_candidates.json"
    payload = {
        "schema": "jelleo.cold-verify.v1",
        "cycle_id": recon_summary.get("cycle_id") or cycle_dir.name,
        "n_keep": len(keep_list),
        "n_drop": len(drop_list),
        "n_clusters_collapsed": len(clusters),
        "keep": keep_list,
        "drop": drop_list,
        "decisions": [asdict(d) for d in sorted(decisions.values(), key=lambda d: d.hyp_id)],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


# ─────────────────────────── CLI ───────────────────────────


@click.command(name="cold-verify")
@click.option(
    "--cycle-id",
    required=True,
    help="Cycle id (e.g. 20260514-151541) under <workspace>/hunts/<cycle-id>/",
)
@click.option(
    "--hyp-library",
    type=click.Path(),
    default=None,
    help="Path to the hyp YAML library used for the cycle. Defaults to "
         "workspace.json `hyp_library`.",
)
@click.option(
    "--approve",
    is_flag=True,
    default=False,
    help="Mark the l2_candidates.json as operator-approved (writes "
         "approved_at + approved_by fields). Without this, L2 dispatch "
         "should refuse to run on the candidate list.",
)
@click.pass_context
def cold_verify_cmd(
    ctx: click.Context,
    cycle_id: str,
    hyp_library: str | None,
    approve: bool,
) -> None:
    """Pre-L2 gate: phantom-hyp filter + duplicate cluster dedup + L1.5
    false-negative-demote rescue.

    Reads <workspace>/hunts/<cycle-id>/recon/recon_summary.json and the
    debate/ challenger responses. Writes
    <workspace>/hunts/<cycle-id>/l2_candidates.json with a verified
    candidate set. The operator reviews + approves before L2 fires.
    """
    workspace = Path(ctx.parent.params.get("workspace", "."))
    cycle_dir = workspace / "hunts" / cycle_id
    if not cycle_dir.is_dir():
        raise click.ClickException(f"cycle dir not found: {cycle_dir}")

    if hyp_library is None:
        ws_json = workspace / "workspace.json"
        if ws_json.is_file():
            import json as _j
            cfg = _j.loads(ws_json.read_text(encoding="utf-8"))
            hl_str = cfg.get("hyp_library", "")
            if hl_str:
                # Resolve relative to the installed package (templates) first
                try:
                    from audit_pipeline import __file__ as _pkg
                    pkg_root = Path(_pkg).resolve().parent.parent
                    if (pkg_root / hl_str).exists():
                        hyp_library = str(pkg_root / hl_str)
                    elif (pkg_root.parent / hl_str).exists():
                        hyp_library = str(pkg_root.parent / hl_str)
                    elif (workspace / hl_str).exists():
                        hyp_library = str(workspace / hl_str)
                except Exception:
                    pass
    if not hyp_library or not Path(hyp_library).is_file():
        raise click.ClickException(
            f"hyp library not found (--hyp-library or workspace.json): {hyp_library!r}"
        )

    payload = cold_verify_cycle(cycle_dir, Path(hyp_library))

    # Render the per-hyp decision table
    table = Table(title=f"Cold-verify · {cycle_id} · keep={payload['n_keep']} drop={payload['n_drop']}")
    table.add_column("hyp_id")
    table.add_column("status")
    table.add_column("reason / cluster")
    table.add_column("severity")
    for d in payload["decisions"]:
        if d["final_status"] == "drop":
            color = "red"
        elif d.get("is_cluster_rep"):
            color = "yellow"
        else:
            color = "green"
        reason = d.get("drop_reason") or (
            f"cluster-rep ({d['cluster_id']})" if d.get("is_cluster_rep") else ""
        )
        table.add_row(
            f"[{color}]{d['hyp_id']}[/{color}]",
            d["final_status"],
            reason or "",
            d.get("severity") or "",
        )
    _console.print(table)

    out_path = cycle_dir / "l2_candidates.json"
    if approve:
        import datetime as _dt
        import os as _os
        data = json.loads(out_path.read_text(encoding="utf-8"))
        data["approved_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        data["approved_by"] = _os.environ.get("USER", "operator")
        out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _console.print(f"[green]✓ approved — {out_path}[/green]")
    else:
        _console.print(
            f"\n[cyan]Wrote {out_path}.[/cyan]\n"
            f"[yellow]NOT approved.[/yellow] Re-run with --approve "
            "to mark the candidate list ready for L2 dispatch.\n"
            f"keep={payload['n_keep']} · drop={payload['n_drop']} · "
            f"clusters_collapsed={payload['n_clusters_collapsed']}"
        )
