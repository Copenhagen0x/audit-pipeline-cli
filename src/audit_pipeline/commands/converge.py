"""`audit-pipeline converge` — WS1 autonomy: re-run hunt until it stops finding new bugs.

`hunt` is a single pass: dispatch a hypothesis library once, confirm what fires.
A confirmed finding has structural *siblings* — the same bug class on a neighbouring
surface — and those siblings are themselves un-audited hypotheses. `converge` closes
that loop: run `hunt`, derive the siblings of whatever it confirmed, dispatch those as
the next round's library, repeat until a round adds nothing new.

WHY THIS DERIVES SIBLINGS ITSELF (do not "simplify" this away). The auto-derivation
hook fires only from `db.transition_finding(..., CONFIRMED)`, and the only caller that
reaches CONFIRMED is the human triage UI (`triage.py`). `hunt` persists every verdict
through `db.upsert_finding(...)` (hunt.py:3768) — a direct INSERT that fires NO hooks.
So `<workspace>/derived/` is populated only after a HUMAN acts, and an unattended loop
that merely *reads* that directory finds it empty, prints "converged", and exits having
audited one round. converge therefore invokes `derive-siblings` itself between rounds.
The alternative — firing the hook from `upsert_finding` — would start LLM sibling
derivation on every confirmed finding of every existing hunt/watch/cron run; that is a
change to the engine's core persistence path and is deliberately NOT made here.

Reuses, never reimplements: `hunt` and `derive-siblings` (subprocesses, so this stays
decoupled from hunt's 4.5k-line internals and inherits every flag/gate unchanged),
`db.list_cycles`/`list_findings_by_cycle` (the net-new signal), `scoping.load_hypotheses`
(schema-validating loader), `gates.disclosure_history` (the cross-cycle seen-filter).

TRUST POSTURE. Sibling hypotheses are LLM-derived FROM the audited repo, so their text
is shaped by whoever controls that repo. The manual workflow puts a human at that
boundary (`triage-siblings`). converge is the AUTONOMOUS path and by default dispatches
un-reviewed siblings; that autonomy is bounded and announced, never silent:
  - `--approved-only`          restores the human gate (dispatch only triage-approved),
  - `--max-siblings-per-round` bounds round SIZE (round COUNT alone is not a cost bound),
  - `--max-total-usd`          bounds the WHOLE run — hunt-cycle spend (measured,
    off `cycles.total_cost_usd`) PLUS sibling derivation (estimated: derivation runs
    outside a cycle and the subprocess does not report its cost back). Checked before
    each derivation batch so it stops before spending. hunt's own `--budget-cap-usd` is
    PER CYCLE, so forwarding it to N rounds would otherwise allow N x the operator's cap,
  - every round that dispatches un-reviewed siblings says so on the console.

NEVER LIE ABOUT CONVERGENCE. "Converged" is the strongest claim this product makes, so
every path that cannot honestly earn it must fail loudly and exit non-zero instead:
a failed round, a round whose cycle converge cannot see (it is reading a different DB
than hunt writes), siblings that exist but were all gate-blocked, or a known
un-dispatched backlog. Silence here would tell a customer their code is clean when it
was never audited.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click
import yaml
from rich.console import Console

console = Console()

# hunt flags that SELECT the hypothesis source. converge owns the source per round
# (operator flags for round 1; derived siblings for rounds 2+). EVERY flag here takes a
# VALUE — including `--surface-scan`, which hunt defines as click.Path, NOT a boolean.
_HYP_SOURCE_FLAGS = ("--hypotheses", "-h", "--protocol-class", "--surface-scan")

# `--resume-cycle` pins hunt to a PRE-EXISTING cycle. converge identifies each round's
# work by diffing the cycle table, so a resumed cycle never registers as new and the
# round's real confirmed findings would silently count as 0. converge owns the cycle.
_CYCLE_PIN_FLAGS = ("--resume-cycle",)

_STRIPPED_FLAGS = (*_HYP_SOURCE_FLAGS, *_CYCLE_PIN_FLAGS)

# Bound the per-round dispatch batch. `--max-rounds` bounds how MANY rounds run, not how
# BIG one is: the audited repo shapes how many siblings get derived.
_DEFAULT_MAX_SIBLINGS_PER_ROUND = 50

# Stop deriving after this many CONSECUTIVE failures. Derivation failures are systemic
# (no ANTHROPIC_API_KEY, budget spent), not per-finding, so the whole doomed tail would
# otherwise spawn one ~2s subprocess each.
_DERIVE_FAILFAST_STREAK = 3

# Rough per-derivation LLM cost, used to pre-flight `--max-total-usd` BEFORE spending.
# Derivation runs outside a hunt cycle and the subprocess doesn't report its cost back,
# so this is the only number available — mirrors derive_siblings' own fallback when a
# response carries no cost. Estimated, never presented as measured.
_DERIVE_COST_ESTIMATE_USD = 0.30

# Round 2+ libraries + converge-derived siblings live here, NOT the workspace root:
# `lint-hypotheses` globs `<workspace>/*.yaml` as live libraries, so a leaked temp at the
# root gets re-linted as one. Cleaned at exit, mirroring hunt.py's `_cleanup_temp_yaml`.
_ROUND_LIB_DIR = ".converge"


def _cleanup_temp_yaml(path: str) -> None:
    """Best-effort unlink of a round's temp hypothesis library (atexit)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _audit_bin() -> str:
    """Resolve the audit-pipeline entrypoint (mirrors hunt._audit_pipeline_bin)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and Path(argv0).name in ("audit-pipeline", "audit-pipeline.exe") and Path(argv0).exists():
        return str(Path(argv0).resolve())
    return "audit-pipeline"


def _strip_flags(args: list[str], flags: tuple[str, ...]) -> list[str]:
    """Remove `flags` (and their values) from pass-through args.

    Every flag this is used with takes a value, INCLUDING `--surface-scan`. Treating it
    as a boolean leaves its path behind as a stray positional, which click rejects
    ("Got unexpected extra argument") — collapsing the loop to a single round on its
    flagship use case.
    """
    out: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in flags:
            skip_next = True
            continue
        if any(a.startswith(f + "=") for f in flags):
            continue
        out.append(a)
    return out


def _strip_converge_owned(args: list[str]) -> list[str]:
    """Rounds 2+: converge owns BOTH the hypothesis source and the cycle."""
    return _strip_flags(args, _STRIPPED_FLAGS)


def _round1_args(args: list[str]) -> list[str]:
    """Round 1: keep the operator's hypothesis source, but still drop the cycle pins.

    Round 1 must honour `--surface-scan` / `--hypotheses` (that IS the operator's
    round-1 library), but `--resume-cycle` is converge's to own in every round: hunt
    would reuse the pinned cycle, no new cycle would appear, and converge's own
    "no new cycle visible" net would then abort blaming DB divergence for a flag
    converge itself forwarded — after printing that it would not.
    """
    return _strip_flags(args, _CYCLE_PIN_FLAGS)


def _parse_hunt_args(hunt_args: tuple[str, ...]) -> dict:
    """Parse the operator's hunt args with CLICK ITSELF — never by hand.

    Hand-rolled flag scanning missed `-t` / `-tVALUE` (hunt's documented short form for
    `--target-name`), which silently mis-scoped every cycle snapshot into a false
    "converged". Asking the real hunt command to parse its own argv is the only version
    that cannot drift from hunt's option table. Strict (non-resilient) parsing doubles
    as a pre-flight: bad operator args fail HERE, once, with click's own message,
    instead of as N mystery "round exited 2" lines.
    """
    from audit_pipeline.commands.hunt import hunt_cmd

    ctx = hunt_cmd.make_context("hunt", list(hunt_args))
    try:
        params = dict(ctx.params)
        # Which params the OPERATOR actually typed, vs click's defaults. Without this,
        # `params["budget_cap_usd"]` is hunt's $1,000,000 default and reads as "the
        # operator set a cap" on every single run — a warning that always fires is a
        # warning that trains you to ignore warnings.
        params["_from_cmdline"] = {
            k for k in params
            if ctx.get_parameter_source(k) is not None
            and ctx.get_parameter_source(k).name == "COMMANDLINE"
        }
        return params
    finally:
        ctx.close()


def _load_config(workspace: Path) -> dict:
    try:
        return json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config just means "no hints"
        return {}


def _resolve_db_root(workspace: Path, config: dict) -> Path:
    """The workspace whose findings.db hunt will WRITE — mirrors hunt.py:420 + 944-955.

    Not cosmetic. hunt redirects its DB to the shared customer-eval root when
    workspace.json declares `customer_id`, using a LOOSER rule than
    `db._resolve_findings_db_path` applies to a plain `open_findings_db(workspace)`.
    Where they disagree, converge reads a DB hunt never writes: it sees no cycles, and
    reports "0 net-new confirmed / converged" for a run in which hunt confirmed real
    bugs. Mirroring hunt's own walk is what keeps the two on the same file.
    """
    if config.get("customer_id"):
        candidate = workspace.resolve().parent.parent
        if (candidate / "findings.db").exists() or candidate.name.endswith("-eval"):
            return candidate
    return workspace


def _resolve_target_id(db, name: str) -> int | None:
    """`name`'s target row id, READ-ONLY (never upserts).

    Cycle snapshots must be scoped to our target: the customer-eval layout shares one
    findings.db across every cell, so an unscoped list_cycles lets a concurrent hunt on
    a DIFFERENT target land a cycle between our before/after snapshot — its ids would be
    attributed to our round, inflating net-new and poisoning the dedup set. Resolved per
    round, not once: `init` never creates the target row (only hunt's upsert does), so a
    first-ever run resolves None before round 1 and would stay unscoped for the whole run.
    """
    try:
        row = db.get_target(name)
    except Exception:  # noqa: BLE001 — older DB without the helper -> unscoped
        return None
    if not row or row.get("id") is None:
        return None
    return int(row["id"])


def _cycle_rows(db, target_id: int | None) -> list[dict]:
    return db.list_cycles(target_id=target_id, limit=1000)


def _historically_dispatched(db, target_id: int | None) -> set[str]:
    """Hypothesis ids this target has had dispatched in ANY earlier cycle.

    Seeds the run's dedup set so "already audited" survives the process — see the call
    site. Returns empty when the target is unknown (a first-ever run has no history to
    miss anyway).
    """
    if target_id is None:
        return set()
    try:
        rows = db.list_findings(target_id=target_id, limit=100_000)
    except Exception:  # noqa: BLE001 — no history is a valid answer; never block the run
        return set()
    return {r["hypothesis_id"] for r in rows if r.get("hypothesis_id")}


def _cycle_hyp_ids(db, cycle_id: str) -> tuple[set[str], set[str], list[dict]]:
    """`(confirmed_hyp_ids, dispatched_hyp_ids, confirmed_findings)` for `cycle_id`.

    `dispatched` is EVERY hypothesis the round investigated, whatever the verdict. Dedup
    must key on dispatched, not confirmed: nothing removes a dispatched-but-unconfirmed
    sibling's YAML, so a confirmed-only key re-globs it as "fresh" and re-pays its full
    recon->debate->PoC->Kani cost every round.

    Confirmed findings come back as full rows, not ids: naming their sibling file the
    way `triage-siblings` expects needs the row's `hypothesis_id`, not just its `id`.
    """
    confirmed: set[str] = set()
    dispatched: set[str] = set()
    findings: list[dict] = []
    for f in db.list_findings_by_cycle(cycle_id):
        hid = f.get("hypothesis_id") or ""
        if hid:
            dispatched.add(hid)
        if (f.get("status") or "") == "confirmed":
            if hid:
                confirmed.add(hid)
            if f.get("id") is not None:
                findings.append(dict(f))
    return confirmed, dispatched, findings


def _derive_siblings(bin_: str, db_root: Path, findings: list[dict],
                     out_dir: Path, model: str | None = None) -> tuple[int, int]:
    """Derive siblings for each newly-confirmed finding. Returns `(ok, failed)`.

    This is the step that makes the loop a loop (see the module docstring): hunt's
    persistence fires no lifecycle hook, so nothing else produces siblings unattended.

    `-w db_root` (not the cell) so `derive-siblings` opens the same DB hunt wrote the
    finding into. `--output` uses triage-siblings' OWN slug helper so the file lands
    exactly where `triage-siblings list/approve/reject` looks for it — otherwise
    converge's output is unreviewable and `--approved-only` can never pass.

    Fail-fast: derivation failures are almost always systemic (no ANTHROPIC_API_KEY,
    daily cap spent), not per-finding. Without this, 50 confirmed findings meant 50
    doomed subprocesses (~2s of interpreter start each) to produce nothing.
    """
    from audit_pipeline.commands.triage_siblings import _find_sibling_file, _slug_for_finding

    out_dir.mkdir(parents=True, exist_ok=True)
    ok = failed = consecutive_failures = 0
    for f in findings:
        fid = f.get("id")

        # Never re-derive over a human's REJECTION. converge writes into the same
        # `derived/` dir `triage-siblings` manages (it must, or its output would be
        # unreviewable), and the slug is stable across runs — so without this, a
        # sibling the operator rejected is silently re-created as `pending` and
        # re-dispatched on the next run, while `triage-siblings show` still reports it
        # as rejected. The reject button has to mean something.
        try:
            existing = _find_sibling_file(Path(db_root), int(fid))
        except Exception:  # noqa: BLE001 — a lookup failure must not block derivation
            existing = None
        if existing and existing[1] == "rejected":
            console.print(f"  [yellow]finding {fid}: siblings were REJECTED by an operator — "
                          f"not re-deriving[/yellow]")
            continue

        slug = _slug_for_finding(f)
        argv = [bin_, "-w", str(db_root), "derive-siblings", str(fid),
                "--output", str(out_dir / f"{slug}-siblings.yaml")]
        if model:
            argv += ["--model", model]
        try:
            cp = subprocess.run(argv, check=False)
        except Exception as e:  # noqa: BLE001 — report, don't crash the loop
            console.print(f"  [yellow]sibling derivation for finding {fid} could not start: {e}[/yellow]")
            failed += 1
            break
        if cp.returncode == 0:
            ok += 1
            consecutive_failures = 0
            continue
        failed += 1
        consecutive_failures += 1
        console.print(f"  [yellow]sibling derivation for finding {fid} exited "
                      f"{cp.returncode}[/yellow]")
        # Bail on a RUN of failures, not just on ok==0. Derivation failures are
        # systemic (no ANTHROPIC_API_KEY, daily budget spent), and budget exhaustion in
        # particular happens MID-round — by then ok > 0, so an `ok == 0` guard never
        # fires and the whole doomed tail still spawns one subprocess each.
        if consecutive_failures >= _DERIVE_FAILFAST_STREAK:
            console.print(f"  [yellow]{consecutive_failures} derivations failed in a row — "
                          f"stopping derivation for this round (check ANTHROPIC_API_KEY and "
                          f"the derive-siblings daily budget)[/yellow]")
            break
    return ok, failed


def _sibling_roots(workspace: Path) -> list[Path]:
    """Directories converge reads sibling YAML from — the CELL's own, and only its own.

    Deliberately NOT `<db_root>/derived`. The lifecycle hook roots itself at the DB
    FILE's parent (`db.py:720` `ws = self.path.parent`), which under the customer-eval
    rollup is the eval root SHARED BY EVERY CELL — reading it makes converge on cell A
    ingest and dispatch siblings derived from cell B's findings, against cell A's
    target. Nothing downstream catches that: `scoping.filter_hypotheses` has no callers
    (hunt never scopes a `--hypotheses` library), and an LLM that omits `applies_to`
    gets the `["*"]` default, which matches everything.

    Reading it is also unnecessary now: converge derives its own siblings into this
    cell's `derived/` with triage's naming, so the loop no longer depends on wherever
    the hook happened to root itself.
    """
    return [workspace / "derived"]


def _load_new_siblings(
    roots: list[Path],
    dispatched_hyp_ids: set[str],
    *,
    target_name: str | None = None,
    approved_only: bool = False,
    max_batch: int = _DEFAULT_MAX_SIBLINGS_PER_ROUND,
) -> tuple[list[dict], int, list[tuple[str, str]]]:
    """Collect sibling hypotheses not yet dispatched this run.

    Returns `(fresh, dropped_by_cap, gate_blocked)`. `gate_blocked` is returned rather
    than discarded: `filter_hypotheses_by_disclosure_history` rejects any hyp carrying a
    malformed/rejected `prior_disclosure`, and that block is attacker-reachable (the
    field round-trips verbatim from LLM output). Swallowing it lets the audited repo
    empty the queue and have converge call that "converged" — hunt reports the same
    filter's skips explicitly (hunt.py:679-687) and converge must not regress against it.
    """
    from audit_pipeline.gates.disclosure_history import (
        filter_hypotheses_by_disclosure_history,
    )
    from audit_pipeline.scoping import load_hypotheses

    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        files.extend(sorted(root.glob("approved/*-siblings.yaml")))
        if not approved_only:
            files.extend(sorted(root.glob("*-siblings.yaml")))
    fresh: list[dict] = []
    seen_local = set(dispatched_hyp_ids)
    for yml in files:
        try:
            hyps = load_hypotheses(yml)
        except Exception as e:  # noqa: BLE001 — a malformed derived file must not crash the loop
            # Report the REASON: these files are LLM-generated, so a schema rejection is
            # the operator's main signal that sibling derivation is emitting something off.
            console.print(f"  [yellow]skipping unusable derived file {yml.name}: {e}[/yellow]")
            continue
        for h in hyps:
            hid = h.get("id") or ""
            if not hid or hid in seen_local:
                continue
            seen_local.add(hid)
            fresh.append(h)
    if not fresh:
        return [], 0, []

    # Target scoping. `derived/` is per-workspace, NOT per-target: run converge against
    # target A and later target B in the same workspace, and B's rounds would otherwise
    # ingest A's siblings. Nothing downstream catches it — hunt never scopes a
    # `--hypotheses` library, and an LLM that omits `applies_to` gets the `["*"]`
    # default, which matches everything. `scoping.filter_hypotheses` is the control
    # written for exactly this; it simply had no callers.
    if target_name:
        from audit_pipeline.scoping import filter_hypotheses

        scoped = filter_hypotheses(fresh, target_name)
        foreign = len(fresh) - len(scoped.applicable)
        if foreign:
            console.print(f"  [yellow]{foreign} sibling hyp(s) do not apply to target "
                          f"{target_name!r} — not dispatched (derived/ is shared across "
                          f"targets in one workspace)[/yellow]")
        fresh = scoped.applicable
        if not fresh:
            return [], 0, []

    # Real shape (verified against the gate): list[tuple[dict, GateResult]], where
    # GateResult.passed is True / False / None — None==skip, False==block; hunt
    # excludes both and logs each with its reason. Mirror that, never swallow it.
    kept, blocked_raw = filter_hypotheses_by_disclosure_history(fresh)
    blocked = [(str(h.get("id") or "?"), str(gr.reason or "")[:140]) for h, gr in blocked_raw]
    if max_batch > 0 and len(kept) > max_batch:
        return kept[:max_batch], len(kept) - max_batch, blocked
    return kept, 0, blocked


@click.command(name="converge", context_settings={"ignore_unknown_options": True})
@click.option("--max-rounds", type=int, default=4, show_default=True,
              help="Hard cap on hunt rounds (bounds cost if convergence never triggers).")
@click.option("--max-siblings-per-round", type=int, default=_DEFAULT_MAX_SIBLINGS_PER_ROUND,
              show_default=True,
              help="Cap sibling hypotheses dispatched per round (bounds round SIZE, not "
                   "just round count). 0 = no cap.")
@click.option("--max-total-usd", type=float, default=0.0, show_default=True,
              help="Bound the WHOLE run: hunt-cycle spend (measured) plus sibling "
                   "derivation (ESTIMATED at ~$0.30/call, since the derive subprocess "
                   "does not report its cost back). Checked BEFORE each derivation "
                   "batch, so it stops before spending, not after. 0 = no cap. NOTE: "
                   "hunt's own --budget-cap-usd is PER CYCLE, so forwarding it to N "
                   "rounds would allow N x that cap; this is the only whole-run bound.")
@click.option("--approved-only", is_flag=True,
              help="Dispatch ONLY triage-approved siblings (derived/approved/), keeping the "
                   "human review gate. The default also dispatches the pending queue.")
@click.option("--no-derive", is_flag=True,
              help="Do not derive siblings; only dispatch ones already on disk. The loop "
                   "cannot progress unattended without derivation (hunt fires no "
                   "derivation hook) — for operators driving triage by hand.")
@click.option("--dry-run", is_flag=True,
              help="Print the plan (round 1 command + loop policy) without running hunt.")
@click.argument("hunt_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def converge_cmd(ctx: click.Context, max_rounds: int, max_siblings_per_round: int,
                 max_total_usd: float, approved_only: bool, no_derive: bool,
                 dry_run: bool, hunt_args: tuple[str, ...]) -> None:
    """Re-run `hunt` until it stops finding new bugs (WS1 autonomy core).

    Round 1 runs `hunt` with YOUR arguments. Each round's confirmed findings are used to
    derive sibling hypotheses, which the next round audits — until a round yields 0
    net-new confirmed findings with nothing left to dispatch.

    Exits non-zero if it cannot honestly claim convergence.

    \b
    Example:
      audit-pipeline -w /abs/ws converge --surface-scan ./engine-src --source-repo o/r
    """
    # Absolute: the child hunt is given `-w <workspace>`, and a RELATIVE value would be
    # re-resolved against wherever the child runs. Never pass a relative -w onward.
    workspace = Path(ctx.obj["workspace"]).resolve()
    if not (workspace / "workspace.json").exists():
        raise click.ClickException("No workspace.json. Run `audit-pipeline init` first.")
    if max_rounds < 1:
        raise click.ClickException("--max-rounds must be >= 1.")
    if max_siblings_per_round < 0:
        raise click.ClickException("--max-siblings-per-round must be >= 0 (0 = no cap).")
    if max_total_usd < 0:
        raise click.ClickException("--max-total-usd must be >= 0 (0 = no cap).")

    from audit_pipeline.db import open_findings_db

    # Pre-flight: let hunt's own parser validate the operator's args ONCE, here.
    try:
        hunt_params = _parse_hunt_args(hunt_args)
    except click.UsageError as e:
        raise click.ClickException(f"hunt rejected these arguments: {e.format_message()}")

    config = _load_config(workspace)
    db_root = _resolve_db_root(workspace, config)
    target_name = (hunt_params.get("target_name")
                   or config.get("target_name") or config.get("name") or "default")
    bin_ = _audit_bin()
    base = [bin_, "-w", str(workspace), "hunt"]
    round1 = _round1_args(list(hunt_args))
    passthrough = _strip_converge_owned(list(hunt_args))
    # Derive into the workspace's REAL derived/ dir, using triage-siblings' own naming
    # (`_slug_for_finding`). A private `.converge/derived/<fid>-siblings.yaml` was
    # structurally unreviewable: `triage-siblings list/approve/reject` resolves only
    # `<workspace>/derived/<slug>-siblings.yaml`, so nothing converge derived could
    # ever be approved — which made --approved-only a guaranteed failure rather than
    # the human gate it advertises.
    own_derived = workspace / "derived"

    if dry_run:
        console.print("[bold]converge plan[/bold]")
        console.print(f"  round 1:  {' '.join([*base, *hunt_args])}")
        console.print(f"  rounds 2..{max_rounds}: hunt on newly-derived siblings "
                      f"(pass-through: {' '.join(passthrough) or '(none)'})")
        console.print(f"  target:   {target_name}   db root: {db_root}")
        console.print("  sibling source: "
                      f"{'triage-APPROVED only' if approved_only else 'approved + PENDING (un-reviewed)'}")
        derive_plan = "NO (--no-derive)" if no_derive else f"yes -> {own_derived}"
        console.print(f"  derive siblings between rounds: {derive_plan}")
        run_cap = f"${max_total_usd:.2f}" if max_total_usd else "none"
        console.print(f"  per-round cap: {max_siblings_per_round or 'none'} sibling hyp(s)"
                      f"   whole-run cap: {run_cap} (cycles + estimated derivation)")
        console.print("  stop when: 0 net-new confirmed AND nothing left to dispatch, "
                      f"or {max_rounds} rounds reached")
        return

    if "--resume-cycle" in hunt_args or any(a.startswith("--resume-cycle=") for a in hunt_args):
        console.print("  [yellow]--resume-cycle is not forwarded: converge identifies each "
                      "round by its NEW cycle, and a resumed cycle would count as 0 "
                      "confirmed.[/yellow]")
    if "budget_cap_usd" in hunt_params.get("_from_cmdline", set()) and not max_total_usd:
        console.print(f"  [yellow]--budget-cap-usd is PER CYCLE: across {max_rounds} rounds this "
                      f"run may spend up to {max_rounds}x it. Use --max-total-usd to bound "
                      f"the whole run.[/yellow]")

    db = open_findings_db(db_root)
    target_id = _resolve_target_id(db, target_name)   # re-resolved each round below
    # Seed from history, not from an empty set. `derived/` is triage's pending queue, so
    # converge must never delete from it — which means every sibling YAML ever written
    # is still on disk next run. With a per-process set, a scheduled converge re-globs
    # and re-dispatches the union of every prior run's siblings, re-paying full
    # recon->debate->PoC->Kani on all of them before reaching anything new: cost grows
    # monotonically forever. Seeding from what this target has ALREADY had dispatched
    # makes "already audited" survive the process.
    dispatched_hyp_ids: set[str] = _historically_dispatched(db, target_id)
    if dispatched_hyp_ids:
        console.print(f"  [cyan]{len(dispatched_hyp_ids)} hypothesis id(s) already audited "
                      f"for this target in earlier runs — not re-dispatching[/cyan]")
    confirmed_hyp_ids: set[str] = set()    # every hyp confirmed this run (net-new signal)
    derived_finding_ids: set[int] = set()  # findings we already derived siblings from
    total_confirmed = 0
    spent_usd = 0.0
    fresh: list[dict] = []
    outcome = "converged"
    detail = ""
    rnd = 0

    for rnd in range(1, max_rounds + 1):
        target_id = _resolve_target_id(db, target_name)   # per round: see the docstring
        before = {c.get("cycle_id") for c in _cycle_rows(db, target_id)}

        if rnd == 1:
            argv = [*base, *round1]
        else:
            # Pin fresh ids as dispatched BEFORE the run: a sibling that errors out
            # produces no finding row and would otherwise be re-globbed every round.
            dispatched_hyp_ids |= {h["id"] for h in fresh if h.get("id")}
            lib_dir = workspace / _ROUND_LIB_DIR
            lib_dir.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8", dir=str(lib_dir))
            yaml.safe_dump({"hypotheses": fresh}, tmp, sort_keys=False, allow_unicode=True)
            tmp.close()
            atexit.register(_cleanup_temp_yaml, tmp.name)
            argv = [*base, *passthrough, "--hypotheses", tmp.name]

        console.rule(f"[bold]converge round {rnd}/{max_rounds}[/bold]")
        console.print(f"  [cyan]{' '.join(argv)}[/cyan]")
        try:
            # No cwd= override: hunt must resolve the operator's relative paths
            # (e.g. --surface-scan ./src) against the OPERATOR's cwd, exactly as it
            # would if they had typed the hunt command themselves.
            cp = subprocess.run(argv, check=False)
        except Exception as e:  # noqa: BLE001
            raise click.ClickException(f"round {rnd} could not start hunt: {e}")
        if cp.returncode != 0:
            outcome, detail = "aborted", (f"round {rnd} hunt exited {cp.returncode}")
            console.print(f"  [red]{detail} — stopping (a failed round's siblings are NOT "
                          f"fed forward)[/red]")
            break

        after = _cycle_rows(db, _resolve_target_id(db, target_name) or target_id)
        new_cycles = [c for c in after if c.get("cycle_id") not in before]
        if not new_cycles:
            # hunt exited 0, so a cycle MUST exist. Not seeing it means we are reading a
            # different findings.db than hunt wrote (customer-eval layouts diverge), or
            # the cycle landed under another target. Reporting "0 confirmed" here would
            # be a false clean bill of health.
            outcome = "aborted"
            detail = (f"round {rnd}: hunt exited 0 but no new cycle is visible to converge "
                      f"(reading {getattr(db, 'path', db_root)}, target={target_name!r}). "
                      f"converge is not seeing hunt's output — refusing to report a result.")
            console.print(f"  [red]{detail}[/red]")
            break

        round_confirmed_ids: set[str] = set()
        round_findings: list[dict] = []
        round_dispatched: set[str] = set()
        for c in new_cycles:
            cid = c.get("cycle_id")
            c_ids, d_ids, f_ids = _cycle_hyp_ids(db, cid)
            round_confirmed_ids |= c_ids
            round_dispatched |= d_ids
            dispatched_hyp_ids |= d_ids
            round_findings.extend(f_ids)
            spent_usd += float(c.get("total_cost_usd") or 0.0)

        # A cycle that dispatched NOTHING is evidence the round did not run — never
        # evidence of convergence. hunt's Layer-1 failure path (hunt.py:1266-1269 —
        # recon timeout / crash / API outage) finishes the cycle with n_dispatched=0
        # and RETURNS EXIT 0, so rc==0 and a new cycle both look healthy. Without this
        # check converge reports "converged, 0 findings, exit 0" for an audit that
        # never happened: the exact lie this module exists to refuse.
        n_dispatched = sum(int(c.get("n_dispatched") or 0) for c in new_cycles)
        if n_dispatched == 0 and not round_dispatched:
            # Do NOT assert a cause here. From the DB alone converge cannot tell an
            # outage from a gate legitimately emptying the library, and there are real
            # instances of both:
            #   - Layer 1 produced no summary (timeout / crash / API outage), or
            #   - every hypothesis was filtered OUT OF SCOPE before dispatch — e.g.
            #     `--diff-since-sha` on a docs-only commit whose changed files match no
            #     hypothesis's target_file, or the disclosure gate emptying the library.
            # The second is the gate working, not a failure. Either way the round
            # audited nothing, so it still cannot be a convergence — but blaming an
            # "outage" for a README commit is a false diagnosis, and a red nightly run
            # that cries outage is one an operator learns to ignore.
            outcome = "incomplete"
            detail = (f"round {rnd}: hunt exited 0 but its cycle dispatched 0 hypotheses. "
                      f"Either Layer 1 produced no summary (timeout/crash/outage), or every "
                      f"hypothesis was filtered out of scope before dispatch (e.g. a "
                      f"--diff-since-sha commit touching no hypothesis's target_file). "
                      f"converge cannot tell which from the DB — reporting neither a "
                      f"convergence nor a crash.")
            console.print(f"  [yellow]{detail}[/yellow]")
            break
        net_new = round_confirmed_ids - confirmed_hyp_ids
        confirmed_hyp_ids |= round_confirmed_ids
        total_confirmed += len(net_new)
        console.print(f"  [green]round {rnd}: {len(net_new)} net-new confirmed "
                      f"({len(round_confirmed_ids)} confirmed this round)"
                      f"{f' · ${spent_usd:.2f} run total' if spent_usd else ''}[/green]")

        if max_total_usd and spent_usd >= max_total_usd:
            outcome, detail = "incomplete", (f"--max-total-usd ${max_total_usd:.2f} reached "
                                             f"(${spent_usd:.2f} spent)")
            console.print(f"  [yellow]{detail} — stopping[/yellow]")
            break

        # Derive siblings from THIS round's confirmed findings (hunt fires no hook).
        to_derive = [f for f in round_findings if int(f["id"]) not in derived_finding_ids]
        derive_ok = derive_failed = 0
        if to_derive and not no_derive:
            # Pre-flight the derivation spend against the run budget with a REAL number,
            # before spending it. Derivation happens outside a hunt cycle, so it is not
            # in `cycles.total_cost_usd` — counting it here (at an estimate, since the
            # subprocess doesn't report cost back) is what makes --max-total-usd an
            # honest whole-run bound rather than a cycles-only one wearing that name.
            est = len(to_derive) * _DERIVE_COST_ESTIMATE_USD
            if max_total_usd and spent_usd + est > max_total_usd:
                outcome = "incomplete"
                detail = (f"deriving from {len(to_derive)} finding(s) would cost ~${est:.2f}, "
                          f"which exceeds --max-total-usd ${max_total_usd:.2f} "
                          f"(${spent_usd:.2f} already spent)")
                console.print(f"  [yellow]{detail} — stopping before spending it[/yellow]")
                break
            derived_finding_ids |= {int(f["id"]) for f in to_derive}
            console.print(f"  [cyan]deriving siblings from {len(to_derive)} confirmed "
                          f"finding(s) (~${est:.2f})...[/cyan]")
            derive_ok, derive_failed = _derive_siblings(
                bin_, db_root, to_derive, own_derived,
                model=(hunt_params.get("model")
                       if "model" in hunt_params.get("_from_cmdline", set()) else None))
            # Estimated, not measured — say so wherever this total is printed.
            spent_usd += derive_ok * _DERIVE_COST_ESTIMATE_USD

        fresh, dropped, blocked = _load_new_siblings(
            _sibling_roots(workspace), dispatched_hyp_ids, target_name=target_name,
            approved_only=approved_only, max_batch=max_siblings_per_round)

        if blocked:
            # Mirror hunt.py:679-687 — never let a gate silently empty the queue.
            console.print(f"  [yellow]disclosure-history gate blocked {len(blocked)} sibling "
                          f"hyp(s): {', '.join(i for i, _ in blocked[:5])}"
                          f"{' ...' if len(blocked) > 5 else ''}[/yellow]")
        if not fresh:
            if blocked:
                outcome = "aborted"
                detail = (f"round {rnd}: every derived sibling was blocked by the "
                          f"disclosure-history gate — the queue was emptied by a gate, not "
                          f"by convergence. Refusing to report convergence.")
                console.print(f"  [red]{detail}[/red]")
            elif to_derive and no_derive:
                # --no-derive is the operator driving triage by hand. Reporting
                # "converged" after one round with confirmed findings never expanded is
                # the exact overclaim this command exists to refuse.
                outcome = "incomplete"
                detail = (f"--no-derive: {len(to_derive)} confirmed finding(s) were never "
                          f"expanded into siblings, so this is not a convergence")
                console.print(f"  [yellow]{detail}[/yellow]")
            elif to_derive and derive_failed and not derive_ok:
                outcome = "aborted"
                detail = (f"round {rnd}: every sibling derivation failed "
                          f"({derive_failed} finding(s)) — check ANTHROPIC_API_KEY and the "
                          f"derive-siblings daily budget. Refusing to report convergence.")
                console.print(f"  [red]{detail}[/red]")
            elif to_derive and approved_only:
                # Derivation worked; the siblings are parked in derived/ awaiting
                # `triage-siblings approve`. That is the gate doing its job, not a failure.
                outcome = "incomplete"
                detail = (f"--approved-only: {derive_ok} finding(s) derived siblings that are "
                          f"awaiting review — run `audit-pipeline -w {workspace} "
                          f"triage-siblings list` to approve, then re-run converge")
                console.print(f"  [yellow]{detail}[/yellow]")
            elif to_derive and not no_derive:
                outcome = "aborted"
                detail = (f"round {rnd}: derived siblings from {len(to_derive)} confirmed "
                          f"finding(s) but none are readable/new — derivation produced "
                          f"nothing. Refusing to report convergence.")
                console.print(f"  [red]{detail}[/red]")
            else:
                console.print("  [bold green]converged — nothing left to audit "
                              f"({len(net_new)} net-new confirmed this round)[/bold green]")
            break

        if dropped:
            console.print(f"  [yellow]{dropped} sibling hyp(s) over the "
                          f"--max-siblings-per-round={max_siblings_per_round} cap were NOT "
                          f"dispatched this round (they carry to the next round)[/yellow]")
        if not approved_only:
            console.print("  [yellow]dispatching UN-REVIEWED derived siblings (autonomous "
                          "mode; --approved-only keeps the triage-siblings gate)[/yellow]")
        if rnd < max_rounds:
            console.print(f"  [cyan]{len(fresh)} new sibling hyp(s) queued for round "
                          f"{rnd + 1}[/cyan]")
    else:
        outcome = "incomplete"
        detail = (f"reached --max-rounds={max_rounds} with {len(fresh)} sibling hyp(s) still "
                  f"un-dispatched")
        console.print(f"  [yellow]{detail} — NOT a convergence[/yellow]")

    console.print(f"\n[bold]converge {outcome}[/bold] — {total_confirmed} net-new confirmed "
                  f"finding(s) across {rnd} round(s)"
                  f"{f' · ${spent_usd:.2f}' if spent_usd else ''}.")
    if outcome != "converged":
        # Exit non-zero so an unattended harness can tell "clean" from "audited nothing".
        # ctx.exit (not raise SystemExit) so this stays correct if converge is ever
        # invoked programmatically with standalone_mode=False.
        ctx.exit(1)
