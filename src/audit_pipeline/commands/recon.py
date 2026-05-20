"""`audit-pipeline recon` — Layer-1 multi-agent code review.

Two modes:

  render mode (default): writes one prompt file per hypothesis for the
    user to feed to their LLM manually.

  --auto mode: actually dispatches N parallel Claude agents, one per
    hypothesis, saves each response to disk, parses verdicts, and emits
    a structured summary. Requires ANTHROPIC_API_KEY.

When --auto is on, this becomes the entry point of the autonomous hunt
loop: the watch daemon's --on-update can fire `recon --auto` on every
new commit, the synthesis script processes the verdicts, and downstream
PoC + Kani + disclosure flows handle whatever's confirmed.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import click
import yaml
from rich.console import Console
from rich.table import Table

from audit_pipeline.utils import (
    complete,
    is_available,
    render_placeholders,
)
from audit_pipeline.utils.language_profile import (
    profile_for as _profile_for,
)
from audit_pipeline.utils.language_profile import (
    render_template as _render_language_template,
)

# POST-AUDIT FIX (2026-05-12 re-audit catch): hoist emit_event to module
# scope. Previously imported INSIDE a try block at line 490 — on a cold
# install where the event_log module is broken (eg syntax error during
# pre-prod deploy, missing optional dep) the lazy import raised inside
# the try; the bare `except Exception: pass` swallowed it, but the name
# `_emit` was never bound, and any subsequent reference (eg in a later
# refactor that pulled the try outside) would raise UnboundLocalError.
# Belt-and-braces: make the import explicit and module-level, with a
# safe no-op fallback the rest of the file can call unconditionally.
try:
    from audit_pipeline.utils.event_log import emit_event as _emit
except Exception:  # noqa: BLE001 — never crash recon over telemetry
    def _emit(*_a, **_kw) -> None:  # type: ignore[misc]
        return None
from audit_pipeline.utils.github_snapshot import GitHubSnapshot, SnapshotDownloadError

console = Console()


# Per-language system prompts for the tool-using recon agent.
#
# Each prompt frames the agent as an expert auditor in the SPECIFIC
# language under test — vocabulary, tooling references, and the
# canonical bug catalogue. The tool-using loop (read_file / grep /
# find_function) is itself language-agnostic — these prompts just
# steer the agent's analysis to the right bug classes.
#
# CRITICAL: every prompt ends with the same `## Verdict` discipline so
# downstream parsing (`_parse_verdict`) and the L1.5 debate stage work
# uniformly across languages.
_BASE_VERDICT_DISCIPLINE = (
    "Use read_file / grep / find_function to verify every claim before "
    "rendering a verdict — do NOT speculate. Cite concrete file paths "
    "and line numbers. End your final answer with a `## Verdict` "
    "section containing one of TRUE / FALSE / INCONCLUSIVE and a "
    "confidence (HIGH / MEDIUM / LOW)."
)

LANGUAGE_SYSTEM_PROMPTS: dict[str, str] = {
    "solana": (
        "You are an expert Solana / Anchor / Rust security auditor "
        "running Layer 1 recon. The codebase under test is a Solana "
        "program (Rust + Anchor framework, sometimes with a wrapper "
        "BPF entrypoint). Familiar bug classes: insufficient account "
        "validation, missing signer / owner / authority checks, "
        "arithmetic overflow on u64/u128 balances, account confusion "
        "between mints + token accounts, account-data deserialization "
        "mismatch, cross-program invocation trust, PDA seed collisions, "
        "missing CPI program-id verification, lamports drain via "
        "rent-exempt edge cases, invariant violations in vault / "
        "settlement / liquidation accounting. "
        + _BASE_VERDICT_DISCIPLINE
    ),
    "c": (
        "You are an expert C / systems-software security auditor "
        "running Layer 1 recon. The codebase under test is a plain C "
        "program — no smart-contract VM, just classic systems code. "
        "Familiar bug classes: buffer overflow (off-by-one, memcpy "
        "with attacker-controlled length, sprintf/strcpy), use-after-"
        "free, double-free, integer overflow in size calculations "
        "(malloc(a*b)), signed/unsigned comparison bugs, sign "
        "conversion on input lengths, format-string injection "
        "(printf(user_input)), null-pointer dereference after malloc, "
        "uninitialized-stack-variable read, TOCTOU races on files / "
        "sessions / state, path traversal in file opens, symlink "
        "races, unchecked realloc return, unbounded loops/recursion "
        "on untrusted input, IV/nonce reuse, MAC-after-decrypt, "
        "padding oracles. Be precise: distinguish a real exploit "
        "primitive from a hardening suggestion. "
        + _BASE_VERDICT_DISCIPLINE
    ),
    "solidity": (
        "You are an expert Solidity / EVM smart-contract security "
        "auditor running Layer 1 recon. The codebase under test is "
        "Solidity 0.8+ contracts (Foundry framework). Familiar bug "
        "classes: reentrancy (classic CEI violation, read-only "
        "reentrancy, cross-function, ERC777 fallback), access control "
        "bypass (missing onlyOwner, tx.origin auth, front-runnable "
        "initialize, uninitialized implementation), oracle manipulation "
        "(stale price, zero/negative answer, decimals mismatch, TWAP "
        "flash-loan), share/vault math (ERC4626 first-depositor "
        "inflation, donation attack, precision loss divide-first, "
        "rounding direction), missing slippage / deadline, unchecked "
        "block arithmetic (`unchecked` + overflow), cast truncation, "
        "approval race, permit front-run, non-standard ERC20 return, "
        "flash-loan composition, signature replay (no nonce, no "
        "chainid in EIP-712 domain), signature malleability "
        "(high-s), governance flash-loan vote / proposal replay, "
        "delegatecall storage collision, delegatecall-to-untrusted, "
        "selfdestruct misuse, withdrawal-delay bypass via readyAt=0, "
        "returndata bomb, unbounded loop DoS, missing/asymmetric pause, "
        "fee-on-transfer + rebase token handling, total-supply / "
        "share-balance conservation, packed-encode collision, "
        "msg.value-in-loop free mint, block.timestamp/number misuse, "
        "module trust-boundary, library storage discipline. The OSec "
        "synthetic repos use obfuscated parameter names "
        "(`_v_2a84a346` etc) — read SEMANTICS, not identifiers. "
        + _BASE_VERDICT_DISCIPLINE
    ),
    "aptos": (
        "You are an expert Aptos Move security auditor running Layer 1 "
        "recon. The codebase under test is Aptos Move (sources/*.move "
        "files, Move.toml manifest). Move-specific properties: "
        "resources are linear (no copy/drop, must move); "
        "borrow_global / borrow_global_mut is an explicit auth point; "
        "capabilities (key/store abilities) gate privileged ops; "
        "&signer carries identity (assert signer::address_of(&s) == "
        "expected_owner). Familiar bug classes: borrow_global without "
        "auth check, missing signer-address binding to resource, "
        "capability leak via public storage, ACL bypass via direct "
        "entry function, friend module trust, resource leak / "
        "double-move, u64 overflow + underflow as reachable DoS "
        "(Move aborts, not wraps), precision drop in fixed-point math, "
        "divide-by-zero abort, rounding direction in share math, "
        "oracle staleness / zero-price / decimals, missing/asymmetric "
        "pause, ERC4626-style share inflation, total-supply divergence, "
        "missing slippage, withdraw-delay bypass, stake double-claim, "
        "auction post-end bid / no-winner settle, module publisher "
        "confusion, type-argument confusion via phantom types, "
        "liquidation min-amount / bonus overflow, lending interest "
        "accrual overflow, treasury drain, governance flash-loan vote, "
        "proposal replay. "
        + _BASE_VERDICT_DISCIPLINE
    ),
}


def _system_prompt_for(language: str) -> str:
    """Pick the right Layer-1 recon system prompt for a language.

    Unknown languages fall back to the Solana prompt (the engine's
    original calibration) with a notice. The CLI should reject
    unknown languages upstream so this fallback only fires under
    operator typos.
    """
    return LANGUAGE_SYSTEM_PROMPTS.get(
        language.lower().strip(),
        LANGUAGE_SYSTEM_PROMPTS["solana"],
    )


SUPPORTED_LANGUAGES = sorted(LANGUAGE_SYSTEM_PROMPTS.keys())


HYPOTHESIS_CLASS_TO_TEMPLATE = {
    "implicit_invariant": "02_implicit_invariant_hunt.md",
    "arithmetic_overflow": "03_arithmetic_overflow_class_audit.md",
    "state_transition": "04_state_transition_completeness.md",
    "authorization": "05_authorization_chain_trace.md",
    "panic_site": "06_panic_site_enumeration.md",
    "reachability": "07_call_chain_reachability.md",
    "invariant_property": "08_invariant_property_definition.md",
}


# Approximate Sonnet 4.7 pricing as of build time. If pricing changes,
# update here. Used only for cost estimation in --auto mode.
COST_PER_INPUT_TOKEN = 3.0e-6   # $3 per 1M input tokens
COST_PER_OUTPUT_TOKEN = 15.0e-6  # $15 per 1M output tokens


@click.command(name="recon")
@click.option(
    "--hypotheses",
    "-h",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file describing hypotheses to investigate",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory for prompt files (defaults to <workspace>/recon/)",
)
@click.option(
    "--auto",
    is_flag=True,
    help=(
        "Actually dispatch Claude agents in parallel, save responses, "
        "parse verdicts. Requires ANTHROPIC_API_KEY in env."
    ),
)
@click.option(
    "--max-concurrent",
    type=int,
    default=4,
    show_default=True,
    help="Max parallel Claude API calls in --auto mode",
)
@click.option(
    "--budget-cap-usd",
    type=float,
    default=5.0,
    show_default=True,
    help=(
        "Hard cap on total Claude spend for this run (--auto mode). "
        "If estimated spend would exceed, agents skip silently."
    ),
)
@click.option(
    "--refinement-rounds",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Adversarial refinement rounds per hypothesis. 0=single-pass, "
        "1=challenge round (devil's-advocate self-critique then reconsider), "
        "2=double-challenge. Each round ~doubles per-hyp API spend."
    ),
)
@click.option(
    "--ground-code/--no-ground-code",
    default=True,
    show_default=True,
    help=(
        "Inject actual Rust source for the functions / patterns named in each "
        "hypothesis's `relevant_constants` and `relevant_instructions` fields. "
        "Defeats the agent-hallucinates-code-reads failure mode at the cost of "
        "more input tokens per call."
    ),
)
@click.option(
    "--code-max-lines",
    type=int, default=120, show_default=True,
    help="Max lines per extracted function in --ground-code mode",
)
@click.option(
    "--source-repo",
    default=None,
    help=(
        "GitHub repo (owner/repo) to read source from via an ephemeral "
        "snapshot. If set, source code is downloaded fresh per run instead "
        "of reading from a local clone. Mutually exclusive with the "
        "default local-clone path resolved via workspace.json."
    ),
)
@click.option(
    "--source-sha",
    default=None,
    help=(
        "Specific commit SHA to pin the snapshot to (requires --source-repo). "
        "If omitted, uses the default branch HEAD at download time. Pinning "
        "is preferred for reproducibility."
    ),
)
@click.option(
    "--use-tools/--no-use-tools",
    default=False,
    show_default=True,
    help=(
        "L1 recon Defect 01 fix: run the tool-using agent loop instead of "
        "single-shot complete(). Agent gets read_file/grep/find_function "
        "tools and iterates until it has concrete line-cited evidence. "
        "Defeats the speculation-based-recon hallucination mode that "
        "underwrote cycle 20260511's retraction. Requires "
        "ANTHROPIC_API_KEY (already required for --auto)."
    ),
)
@click.option(
    "--tool-max-turns",
    type=int,
    default=12,
    show_default=True,
    help="Max tool-using turns per hypothesis (only used with --use-tools).",
)
@click.option(
    "--language",
    type=click.Choice(SUPPORTED_LANGUAGES, case_sensitive=False),
    default="solana",
    show_default=True,
    help=(
        "Target language under audit. Selects the language-specific "
        "system prompt for the tool-using recon agent — bug vocabulary, "
        "tooling references, and known bug classes match the language. "
        "Use 'c' for C system-software repos, 'solidity' for EVM "
        "smart contracts, 'aptos' for Aptos Move modules. Default is "
        "'solana' (Rust + Anchor) to preserve existing Percolator "
        "workflows."
    ),
)
@click.pass_context
def recon_cmd(
    ctx: click.Context,
    hypotheses: str,
    output: Path | None,
    auto: bool,
    max_concurrent: int,
    budget_cap_usd: float,
    refinement_rounds: int,
    ground_code: bool,
    code_max_lines: int,
    source_repo: str | None,
    source_sha: str | None,
    use_tools: bool,
    tool_max_turns: int,
    language: str,
) -> None:
    """Layer-1 multi-agent recon. Render prompts (default) or dispatch agents (--auto).

    The hypotheses YAML file should have:

      hypotheses:
        - id: H1
          class: implicit_invariant
          claim: "..."
          target_file: "..."
          target_lines: "..."
        - id: H2
          ...

    In default mode: writes <hyp-id>_prompt.md per hypothesis.
    In --auto mode: also writes <hyp-id>_response.md with the agent's
    output, plus a recon_summary.json aggregating all verdicts for the
    synthesis pipeline to consume.
    """
    workspace = Path(ctx.obj["workspace"])
    config_path = workspace / "workspace.json"

    if not config_path.exists():
        raise click.ClickException(
            f"No workspace.json at {config_path}. Run `audit-pipeline init` first."
        )

    config = json.loads(config_path.read_text())

    # Validate source-mode args
    if source_sha and not source_repo:
        raise click.ClickException(
            "--source-sha requires --source-repo to be set."
        )

    if output is None:
        output = workspace / "recon"
    output.mkdir(parents=True, exist_ok=True)

    with open(hypotheses) as f:
        hyp_data = yaml.safe_load(f)

    if "hypotheses" not in hyp_data:
        raise click.ClickException(
            f"{hypotheses} must contain a top-level 'hypotheses' key with a list."
        )

    # Pre-flight check for --auto mode
    if auto and not is_available():
        raise click.ClickException(
            "--auto mode requires ANTHROPIC_API_KEY in your environment. "
            "Either set the key or omit --auto."
        )

    # ---------- Source-mode resolution ----------
    # If --source-repo is given, download an ephemeral snapshot for the
    # lifetime of this command and read source code from it. Otherwise
    # fall back to the local-clone path resolved via workspace.json
    # (legacy mode, preserves backward compat).
    if source_repo:
        try:
            snap_cm = GitHubSnapshot(source_repo, source_sha)
        except SnapshotDownloadError as e:
            raise click.ClickException(f"snapshot init failed: {e}")
    else:
        snap_cm = None

    with (snap_cm if snap_cm is not None else _NoSnapshot()) as snap:
        if snap_cm is not None:
            engine_root = snap.workspace
            wrapper_root = snap.workspace  # snapshot is a single repo; wrapper unused
            console.print(
                f"  [cyan]Reading from snapshot {snap.repo_slug}@"
                f"{snap.resolved_sha[:7]}[/cyan] ({engine_root})"
            )
        else:
            engine_root = workspace / config["engine"]["local"]
            wrapper_root = workspace / config["wrapper"]["local"]
            console.print(
                f"  [cyan]Reading from local workspace {engine_root}[/cyan]"
            )

        return _recon_body(
            ctx=ctx,
            workspace=workspace,
            config=config,
            engine_root=engine_root,
            wrapper_root=wrapper_root,
            hypotheses=hypotheses,
            hyp_data=hyp_data,
            output=output,
            auto=auto,
            max_concurrent=max_concurrent,
            budget_cap_usd=budget_cap_usd,
            refinement_rounds=refinement_rounds,
            ground_code=ground_code,
            code_max_lines=code_max_lines,
            snap_sha=snap.resolved_sha if snap_cm is not None else None,
            use_tools=use_tools,
            tool_max_turns=tool_max_turns,
            language=language,
        )


class _NoSnapshot:
    """No-op context manager so the snapshot/non-snapshot branches share a body."""
    def __enter__(self):
        return None
    def __exit__(self, *a):
        return False


def _recon_body(
    *,
    ctx: click.Context,
    workspace: Path,
    config: dict,
    engine_root: Path,
    wrapper_root: Path,
    hypotheses: str,
    hyp_data: dict,
    output: Path,
    auto: bool,
    max_concurrent: int,
    budget_cap_usd: float,
    refinement_rounds: int,
    ground_code: bool,
    code_max_lines: int,
    snap_sha: str | None,
    use_tools: bool = False,
    tool_max_turns: int = 12,
    language: str = "solana",
) -> None:
    """Body of recon, factored out so snapshot vs local-clone modes share it.

    ``language`` selects the per-language system prompt for the
    tool-using recon agent. Defaults to ``solana`` to preserve
    existing Percolator workflows. See ``LANGUAGE_SYSTEM_PROMPTS``
    for supported languages and prompt content.
    """

    # Locate orientation prompt + class-specific prompts
    from audit_pipeline import __file__ as pkg_init
    prompts_dir = Path(pkg_init).parent / "templates" / "agent_prompts"

    orientation_path = prompts_dir / "00_orientation.md"
    if not orientation_path.exists():
        raise click.ClickException(f"Orientation prompt missing at {orientation_path}")

    orientation = orientation_path.read_text(encoding="utf-8", errors="replace")
    # Phase 2 (language-aware templates): substitute language placeholders
    # ({PROGRAM_KIND}, {SOURCE_EXTS}, {FORMAL_TOOL}, ...) BEFORE the per-hyp
    # render_placeholders pass so the orientation reflects the target
    # language regardless of which class template fires below.
    _lang_profile = _profile_for(language)
    orientation = _render_language_template(orientation, language)

    # Render prompts for every hypothesis (mode-independent)
    rendered_prompts: list[tuple[str, str]] = []  # (hyp_id, full_prompt_text)
    for hyp in hyp_data["hypotheses"]:
        hyp_id = hyp["id"]
        hyp_class = hyp.get("class", "implicit_invariant")

        if hyp_class not in HYPOTHESIS_CLASS_TO_TEMPLATE:
            console.print(
                f"[yellow]Warning:[/yellow] {hyp_id} has unknown class "
                f"'{hyp_class}'; using implicit_invariant template."
            )
            hyp_class = "implicit_invariant"

        template_filename = HYPOTHESIS_CLASS_TO_TEMPLATE[hyp_class]
        template_path = prompts_dir / template_filename
        if not template_path.exists():
            raise click.ClickException(f"Template missing: {template_path}")

        template_content = template_path.read_text(encoding="utf-8", errors="replace")
        # Same language placeholder pass as orientation — fills
        # {PROGRAM_KIND}, {SOURCE_EXTS}, {FORMAL_TOOL}, {ASSERTION_IDIOM},
        # {ENTRY_POINT_LABEL}, etc. before the per-hyp engine paths.
        template_content = _render_language_template(template_content, language)

        local_engine_path = str(engine_root)
        local_wrapper_path = str(wrapper_root)
        substitutions = {
            "ENGINE_REPO_URL": config["engine"]["repo"],
            "ENGINE_SHA": snap_sha or config["engine"]["sha"],
            "WRAPPER_REPO_URL": config["wrapper"]["repo"],
            "WRAPPER_SHA": config["wrapper"]["sha"],
            "LOCAL_ENGINE_PATH": local_engine_path,
            "LOCAL_WRAPPER_PATH": local_wrapper_path,
            "ENGINE_PATH": local_engine_path,
            "WRAPPER_PATH": local_wrapper_path,
            "SPEC_PATH": str(Path(local_engine_path) / "spec.md"),
            "LIST_RELEVANT_CONSTANTS": hyp.get(
                "relevant_constants", "(none specified)"
            ),
            "LIST_RELEVANT_INSTRUCTIONS": hyp.get(
                "relevant_instructions", "(none specified)"
            ),
        }

        rendered_orientation = render_placeholders(orientation, **substitutions)
        rendered_class = render_placeholders(template_content, **substitutions)

        # Optional code-grounding: pull actual Rust source for the named
        # constants / instructions and inject into the prompt. Defeats the
        # agent-hallucinates-code-reads failure mode.
        code_section = ""
        if ground_code:
            from audit_pipeline.utils.code_extract import collect_grounded_code

            # Phase 2 (language-aware grounding): walk the canonical src
            # dir for THIS language, not a hardcoded "src/*.rs". Pulls the
            # ext set from the active LanguageProfile (e.g. .c / .h for c,
            # .move for aptos, .sol for solidity, .rs for solana). For c
            # repos we rglob (subdirs: auth/, common/, vendor/...).
            _src_subdir = _lang_profile.src_dir_path.rstrip("/")
            engine_src_dir = Path(local_engine_path) / _src_subdir
            wrapper_src_dir = Path(local_wrapper_path) / _src_subdir
            rs_files: list[Path] = []
            for d in (engine_src_dir, wrapper_src_dir):
                if not d.exists():
                    continue
                for ext in _lang_profile.source_exts:
                    if language == "c":
                        rs_files.extend(sorted(d.rglob(f"*{ext}")))
                    else:
                        rs_files.extend(sorted(d.glob(f"*{ext}")))

            targets: list[str] = []
            for raw_block in (hyp.get("relevant_constants", ""), hyp.get("relevant_instructions", "")):
                if not isinstance(raw_block, str):
                    continue
                for line in raw_block.splitlines():
                    for token in line.replace(",", " ").split():
                        token = token.strip(" `'\"():")
                        if token and not token.startswith("("):
                            targets.append(token)

            grounded = collect_grounded_code(targets, rs_files, max_lines=code_max_lines)
            blocks = [v for v in grounded.values() if v]
            if blocks:
                code_section = (
                    "\n\n# CODE-GROUNDED CONTEXT (actual source bytes)\n\n"
                    "These are real source-code excerpts pulled from the workspace. "
                    "Cite specific line numbers from these excerpts in your verdict — "
                    "DO NOT invent code that is not in this section.\n\n"
                    + "\n\n".join(blocks)
                )

        # L1 recon audit Defect 07 (MED): render prior_disclosure context
        # into the prompt so the model knows when a claim has been
        # explicitly considered + declined upstream. Gate 5 already
        # filters these out at cycle-start, but if a hyp slips through
        # (e.g. revisit_justification present), the model should still
        # know to consult the prior decision rationale instead of
        # re-deriving the surface from scratch.
        prior_disclosure_block = ""
        prior = hyp.get("prior_disclosure")
        if isinstance(prior, dict):
            prior_disclosure_block = (
                "\n\n# PRIOR DISCLOSURE HISTORY (DO NOT RE-DERIVE BLINDLY)\n\n"
                f"This hypothesis was previously disclosed at "
                f"{prior.get('pr', '(unspecified PR)')} with decision = "
                f"**{prior.get('decision', '?')}**.\n\n"
                f"Upstream rationale:\n> {(prior.get('rationale') or '(none recorded)')[:600]}\n\n"
                f"Regression test: `{prior.get('regression_test', '(none)')}`\n\n"
                "If your verdict aligns with the prior rationale, mark the "
                "hypothesis FALSE / LOW confidence and cite the prior decision. "
                "Only mark TRUE if you find concrete NEW evidence the prior "
                "rationale no longer applies (e.g. a commit changed the "
                "guard the team relied on)."
            )

        full_prompt = f"""{rendered_orientation}

---

{rendered_class}

---

# Specific hypothesis to investigate

ID:           {hyp_id}
Claim:        {hyp.get("claim", "(see hypothesis brief above)")}
Target file:  {hyp.get("target_file", "(see hypothesis brief above)")}
Target lines: {hyp.get("target_lines", "(see hypothesis brief above)")}
Notes:        {hyp.get("notes", "(none)")}
{prior_disclosure_block}
{code_section}
"""

        out_path = output / f"{hyp_id}_prompt.md"
        out_path.write_text(full_prompt, encoding="utf-8")
        rendered_prompts.append((hyp_id, full_prompt))
        console.print(f"  [green]wrote[/green] {out_path}")

    console.print()
    console.print(
        f"[bold green]Built {len(rendered_prompts)} prompts in {output}/[/bold green]"
    )

    if not auto:
        console.print(
            "  Send each prompt to your LLM. Save responses as "
            "<hyp-id>_response.md in the same directory for synthesis."
        )
        console.print(
            "  Tip: pass [cyan]--auto[/cyan] to dispatch agents in parallel "
            "and skip the manual paste."
        )
        return

    # ---------- AUTO MODE ----------
    console.print()
    console.print(
        f"[bold]Dispatching {len(rendered_prompts)} agents (max {max_concurrent} "
        f"concurrent, budget cap ${budget_cap_usd:.2f})...[/bold]"
    )

    results: dict[str, dict[str, Any]] = {}
    total_in_tokens = 0
    total_out_tokens = 0
    total_cost = 0.0
    started = time.time()
    skipped_for_budget: list[str] = []

    def _dispatch_one(hyp_id: str, prompt_text: str) -> tuple[str, dict[str, Any]]:
        """Worker: dispatch one hypothesis to Claude with optional refinement rounds.

        Two paths:
          (a) tool-using agent (when use_tools=True): closes L1 recon
              Defect 01. Agent gets read_file/grep/find_function tools
              and iterates against the actual source tree. Refinement
              rounds are skipped — the tool loop already iterates.
          (b) single-shot complete() with optional refinement rounds
              (legacy path, kept so --use-tools is opt-in).
        """
        try:
            # Bridge dashboard event: hyp dispatch starts
            # POST-AUDIT FIX (2026-05-12): use module-level _emit
            # alias so cold-install / partial-import failures can't bind
            # the name to nothing at runtime.
            _emit("recon_hyp_start", hyp_id=hyp_id, use_tools=bool(use_tools))
            if use_tools:
                # L1 recon Defect 01 fix: source-grounded tool loop.
                # POST-AUDIT FIX: previously stashed the active hyp_id in
                # the process env var JELLEO_ACTIVE_HYP_ID for
                # llm_tools.emit_event to read. That raced across
                # concurrent ThreadPoolExecutor workers — tool_call events
                # got cross-attributed. Now passed in as a per-call kwarg.
                #
                # PHASE 1b: system prompt is now LANGUAGE-AWARE. The
                # tool-using agent loop itself (read_file / grep /
                # find_function) is language-agnostic — only the
                # framing changes per language. Falls back to the
                # Solana prompt for unknown languages.
                from audit_pipeline.utils.llm_tools import run_tool_using_agent
                system_prompt = _system_prompt_for(language)
                tu_result = run_tool_using_agent(
                    workspace,
                    system_prompt,
                    prompt_text,
                    max_turns=tool_max_turns,
                    hyp_id=hyp_id,
                )
                # Emit verdict event for the dashboard's hypothesis grid
                try:
                    _verdict_match = _parse_verdict(tu_result.text)
                    _emit("recon_hyp_done",
                          hyp_id=hyp_id,
                          verdict=_verdict_match[0],
                          confidence=_verdict_match[1],
                          input_tokens=tu_result.input_tokens,
                          output_tokens=tu_result.output_tokens,
                          n_tool_turns=tu_result.n_turns)
                except Exception:  # noqa: BLE001
                    pass
                return hyp_id, {
                    "ok": True,
                    "text": tu_result.text,
                    "input_tokens": tu_result.input_tokens,
                    "output_tokens": tu_result.output_tokens,
                    "n_rounds": 1,
                    "n_tool_turns": tu_result.n_turns,
                    "tool_calls": tu_result.tool_calls,
                    "model": "tool-using-agent",
                    "stop_reason": tu_result.stop_reason,
                }

            resp = complete(prompt_text)
            rounds_data = [{
                "text": resp.text,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
            }]

            for r in range(refinement_rounds):
                challenge_prompt = _build_challenge_prompt(
                    hyp_id, prompt_text, rounds_data[-1]["text"], r + 1,
                )
                challenge_resp = complete(challenge_prompt)
                rounds_data.append({
                    "text": challenge_resp.text,
                    "input_tokens": challenge_resp.input_tokens,
                    "output_tokens": challenge_resp.output_tokens,
                })
                resp = challenge_resp  # last round becomes the final verdict

            try:
                _v = _parse_verdict(rounds_data[-1]["text"])
                _emit("recon_hyp_done",
                      hyp_id=hyp_id,
                      verdict=_v[0],
                      confidence=_v[1],
                      input_tokens=sum(r["input_tokens"] for r in rounds_data),
                      output_tokens=sum(r["output_tokens"] for r in rounds_data),
                      n_tool_turns=0)
            except Exception:  # noqa: BLE001
                pass
            return hyp_id, {
                "ok": True,
                "text": rounds_data[-1]["text"],
                "input_tokens": sum(r["input_tokens"] for r in rounds_data),
                "output_tokens": sum(r["output_tokens"] for r in rounds_data),
                "n_rounds": len(rounds_data),
                "rounds": rounds_data,
                "model": resp.model,
                "stop_reason": resp.stop_reason,
            }
        except Exception as e:  # noqa: BLE001 — surface every failure as data
            try:
                _emit("recon_hyp_done", hyp_id=hyp_id, verdict="ERROR",
                      confidence="LOW", error=str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
            return hyp_id, {"ok": False, "error": str(e)}

    # Resume support: if a previous recon left behind <hyp_id>_response.md in
    # the output dir (e.g. cycle hit a timeout), load that response into the
    # results dict and skip the paid API call. Cost is preserved across the
    # stop/fix/resume protocol.
    n_resumed = 0
    for hyp_id, _prompt_text in rendered_prompts:
        existing = output / f"{hyp_id}_response.md"
        if existing.is_file() and existing.stat().st_size > 0:
            try:
                results[hyp_id] = {
                    "ok": True,
                    "text": existing.read_text(encoding="utf-8"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "n_rounds": 1,
                    "model": "resumed-from-disk",
                    "stop_reason": "resumed",
                }
                n_resumed += 1
            except OSError:
                pass  # fall through; will re-dispatch
    if n_resumed:
        console.print(
            f"[cyan]resume[/cyan] reloaded {n_resumed} existing response.md files; "
            f"will dispatch only the {len(rendered_prompts) - n_resumed} missing hyps"
        )

    # Spend audit Defect 02 (HIGH): previously this submitted ALL hyps
    # to the pool synchronously before any future returned. ``total_cost``
    # was 0 for every gate check, making ``--budget-cap-usd`` a no-op
    # within a single batch. Now: a lock-guarded "reserved" counter
    # tracks in-flight estimated cost, and submissions block at the
    # gate when reserved + actual would exceed cap. Real cost replaces
    # the reservation on completion.
    import threading
    budget_lock = threading.Lock()
    reserved_cost = 0.0   # estimated cost for in-flight calls

    def _try_reserve(est: float) -> bool:
        nonlocal reserved_cost
        with budget_lock:
            if total_cost + reserved_cost + est > budget_cap_usd:
                return False
            reserved_cost += est
            return True

    def _release_reservation(est: float, actual: float) -> float:
        """Subtract reservation, add real cost, return new total."""
        nonlocal reserved_cost, total_cost
        with budget_lock:
            reserved_cost -= est
            total_cost += actual
            return total_cost

    with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
        future_map: dict = {}
        future_est: dict = {}  # future → (hyp_id, est_cost) for release
        # Submit in two phases: first reserve under lock; only submit if
        # reservation succeeded. If cap is exhausted, mark for skip.
        for hyp_id, prompt_text in rendered_prompts:
            if hyp_id in results:
                continue  # resumed from disk
            # Tool-using mode iterates: ~3-5× the single-shot input + a bit
            # more output. Use empirical AVERAGES, not the worst-case max
            # turn count — otherwise the cap mass-skips hyps even when
            # there's real budget left.
            #
            # POST-AUDIT FIX: previously estimated using `tool_max_turns // 3`
            # for input multiplier AND the full `tool_max_turns` for output
            # — at default tool_max_turns=12 that meant 4× input + 12 ×
            # 8192-token output = ~$1.50/hyp, overshooting real cost ~3×.
            # Result: hyps got silently skipped on small budgets. Empirical
            # averages from cycle 20260511 + smoke tests: ~3 turns avg,
            # ~3500 output tokens per turn.
            AVG_TOOL_TURNS = 3
            AVG_OUTPUT_PER_TURN_TOOL = 3500
            AVG_OUTPUT_SINGLE_SHOT = 20_000
            input_mult = AVG_TOOL_TURNS if use_tools else 1
            output_total = (
                AVG_OUTPUT_PER_TURN_TOOL * AVG_TOOL_TURNS
                if use_tools else AVG_OUTPUT_SINGLE_SHOT
            )
            est_cost = (
                input_mult * len(prompt_text) / 4 * COST_PER_INPUT_TOKEN
                + output_total * COST_PER_OUTPUT_TOKEN
            )
            if not _try_reserve(est_cost):
                skipped_for_budget.append(hyp_id)
                continue
            fut = ex.submit(_dispatch_one, hyp_id, prompt_text)
            future_map[fut] = hyp_id
            future_est[fut] = est_cost

        for future in as_completed(future_map):
            est_cost = future_est.get(future, 0.0)
            hyp_id, result = future.result()
            results[hyp_id] = result
            if result.get("ok"):
                total_in_tokens += result["input_tokens"]
                total_out_tokens += result["output_tokens"]
                cost = (
                    result["input_tokens"] * COST_PER_INPUT_TOKEN
                    + result["output_tokens"] * COST_PER_OUTPUT_TOKEN
                )
                _release_reservation(est_cost, cost)
            else:
                # Failed call — still release the reservation (no real cost incurred)
                _release_reservation(est_cost, 0.0)
            if result.get("ok"):
                # Write response to disk
                resp_path = output / f"{hyp_id}_response.md"
                resp_path.write_text(result["text"], encoding="utf-8")
                console.print(
                    f"  [green]✓[/green] {hyp_id}: "
                    f"{result['input_tokens']:,}in / {result['output_tokens']:,}out "
                    f"(${cost:.3f})"
                )
            else:
                console.print(
                    f"  [red]✗[/red] {hyp_id}: {result.get('error', 'unknown error')}"
                )

    elapsed = time.time() - started

    # Parse verdicts from each response
    verdict_summary: list[dict[str, Any]] = []
    for hyp_id, result in sorted(results.items()):
        record: dict[str, Any] = {"hypothesis_id": hyp_id}
        if not result.get("ok"):
            record["status"] = "ERROR"
            record["error"] = result.get("error")
        else:
            verdict, confidence = _parse_verdict(result["text"])
            record["status"] = "OK"
            record["verdict"] = verdict
            record["confidence"] = confidence
            record["input_tokens"] = result["input_tokens"]
            record["output_tokens"] = result["output_tokens"]
        verdict_summary.append(record)

    summary = {
        "schema": "audit-pipeline.recon.v1",
        "workspace": str(workspace),
        "engine_sha": snap_sha or config.get("engine", {}).get("sha"),
        "wrapper_sha": config.get("wrapper", {}).get("sha"),
        "n_hypotheses": len(rendered_prompts),
        "n_dispatched": len(results),
        "n_skipped_budget": len(skipped_for_budget),
        "skipped_for_budget": skipped_for_budget,
        "n_errors": sum(1 for r in results.values() if not r.get("ok")),
        "n_ok": sum(1 for r in results.values() if r.get("ok")),
        "total_input_tokens": total_in_tokens,
        "total_output_tokens": total_out_tokens,
        "total_cost_usd": round(total_cost, 4),
        "elapsed_seconds": round(elapsed, 1),
        "use_tools": bool(use_tools),  # records source-grounded vs single-shot
        "verdicts": verdict_summary,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    summary_path = output / "recon_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Print summary table
    console.print()
    table = Table(title=f"Recon dispatch complete in {elapsed:.0f}s")
    table.add_column("Hypothesis", style="cyan")
    table.add_column("Verdict", style="bold")
    table.add_column("Confidence")
    table.add_column("Tokens")
    for r in verdict_summary:
        if r["status"] == "OK":
            verdict_color = {
                "TRUE": "[red]TRUE[/red]",
                "FALSE": "[green]FALSE[/green]",
                "NEEDS_LAYER_2_TO_DECIDE": "[yellow]NEEDS_L2[/yellow]",
                "UNKNOWN": "[dim]UNKNOWN[/dim]",
            }.get(r["verdict"], r["verdict"])
            table.add_row(
                r["hypothesis_id"],
                verdict_color,
                r["confidence"],
                f"{r['input_tokens']:,}/{r['output_tokens']:,}",
            )
        else:
            table.add_row(
                r["hypothesis_id"],
                "[red]ERROR[/red]",
                "-",
                r.get("error", "")[:30],
            )
    console.print(table)

    console.print()
    console.print(
        f"[bold]Spent:[/bold] ${total_cost:.3f} "
        f"({total_in_tokens:,}in / {total_out_tokens:,}out)"
    )
    if skipped_for_budget:
        console.print(
            f"[yellow]Skipped {len(skipped_for_budget)} for budget:[/yellow] "
            f"{', '.join(skipped_for_budget)}"
        )
    console.print(f"Summary: [cyan]{summary_path}[/cyan]")


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _build_challenge_prompt(
    hyp_id: str, original_prompt: str, prior_response: str, round_num: int,
) -> str:
    """Construct the devil's-advocate challenge prompt for a refinement round.

    The agent is asked to identify the strongest counter-argument against
    its own prior verdict, surface missed code paths, and then produce a
    final verdict that takes both perspectives into account.
    """
    return f"""You previously analyzed hypothesis `{hyp_id}` and produced this verdict + analysis:

---BEGIN PRIOR ANALYSIS---
{prior_response}
---END PRIOR ANALYSIS---

This is refinement round {round_num}. Now play devil's advocate against your own verdict.

Identify, specifically:
1. Code paths or call sites you may not have examined that could flip the conclusion.
2. Invariants asserted elsewhere in the codebase that contradict (or weaken) your prior reasoning.
3. Edge cases — adversarial inputs, race conditions, atomicity gaps, integer-edge values, replay sequences — that your prior analysis didn't address.
4. Assumptions in your prior reasoning that may not hold under all reachable states.

After surfacing the strongest counter-argument, reconsider the original hypothesis and produce your FINAL verdict. Be intellectually honest: if the devil's-advocate analysis flips your conclusion, change the verdict. If it strengthens your conclusion, raise the confidence.

Use the SAME output format as before, including a `## Verdict` section that ends with TRUE / FALSE / NEEDS_LAYER_2_TO_DECIDE plus HIGH / MED / LOW confidence.

Original hypothesis context (for reference, do not repeat in your output):
{original_prompt[-2000:]}
"""


def _parse_verdict(text: str) -> tuple[str, str]:
    """Extract (verdict, confidence) from an agent response.

    Strategy:
      1. Find the LAST verdict-header heading (`## Verdict`, `### Verdict`,
         etc — any heading level). Agents often write multiple verdict
         blocks while reasoning; only the FINAL one is the actual verdict.
      2. Bound the section by the next heading AT THE SAME OR HIGHER LEVEL
         (fewer-or-equal `#`). FIX: previously used `##+` which also
         matched sub-headings (`### Claim sub-parts:`), truncating the
         verdict section to empty and defaulting the parse to UNKNOWN.
         The original regression buried 21 likely-TRUE and 22
         NEEDS_LAYER_2_TO_DECIDE verdicts as UNKNOWN on a 492-hyp Step 3
         cycle (2026-05-11). Same-level matching keeps subheadings
         inside the section so the verdict body is parsed in full.
      3. Within the section, parse the FIRST 2000 chars (enough for tables
         and per-claim breakdowns).
      4. Use word-boundary regexes for TRUE/FALSE/NEEDS_LAYER_2.
      5. FALLBACK: scan the full text for `**Overall verdict: X**` /
         `Final verdict: X` patterns — many agents render the conclusion
         as a bolded sentence rather than a heading.
    """
    import re

    matches = list(re.finditer(
        r"(?im)^(?P<hashes>#+)\s*(?:final\s+)?verdict\b.*$",
        text,
    ))
    verdict = "UNKNOWN"
    block = ""
    if matches:
        last = matches[-1]
        n_hashes = len(last.group("hashes"))
        section_start = last.end()
        next_header_re = re.compile(
            rf"(?im)^#{{1,{n_hashes}}}\s+\S",
        )
        nm = next_header_re.search(text[section_start:])
        section_end = section_start + nm.start() if nm else len(text)
        block = text[section_start:section_end][:2000].upper()

        block_head = block[:200]
        # Cycle 20260514-151541 fix: extract verdict from the FIRST
        # non-empty line of the block (the actual verdict declaration)
        # before falling back to naive substring scans. The previous
        # logic's last-resort `\bTRUE\b in block_head` matched any
        # occurrence of "TRUE" anywhere in the first 200 chars —
        # including phrases like "cannot be TRUE or violated" in
        # FALSE-verdict bodies (APT29-auction-settle-no-winner: body
        # said `**FALSE** — Confidence: **HIGH**` but the very next
        # sentence said "cannot be TRUE", so the parser flipped the
        # verdict to TRUE, sending a phantom-hyp into L2 candidates).
        first_line = next(
            (ln.strip() for ln in block.splitlines() if ln.strip()),
            "",
        )
        # Match `**TRUE**`, `TRUE`, `**FALSE** — Confidence: ...`, etc.
        # at the START of the first non-empty line.
        m_lead = re.match(
            r"^\*{0,2}\s*(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|"
            r"TRUE|FALSE|UNKNOWN|INCONCLUSIVE)\b\*{0,2}",
            first_line,
        )
        if re.search(r"NEEDS[_\s]LAYER[_\s]2", block):
            verdict = "NEEDS_LAYER_2_TO_DECIDE"
        elif re.search(r"\bVERDICT\s*[:=\-]\s*TRUE\b", block):
            verdict = "TRUE"
        elif re.search(r"\bVERDICT\s*[:=\-]\s*FALSE\b", block):
            verdict = "FALSE"
        elif re.search(r"(?m)^\s*\*{0,2}TRUE\*{0,2}\s*$", block):
            verdict = "TRUE"
        elif re.search(r"(?m)^\s*\*{0,2}FALSE\*{0,2}\s*$", block):
            verdict = "FALSE"
        elif m_lead:
            tok = m_lead.group(1).upper().replace(" ", "_")
            if "NEEDS" in tok:
                verdict = "NEEDS_LAYER_2_TO_DECIDE"
            elif tok == "INCONCLUSIVE":
                verdict = "UNKNOWN"
            else:
                verdict = tok
        # NOTE: the old `\bTRUE\b in block_head` / `\bFALSE\b in
        # block_head` fallbacks were removed — they false-positive on
        # bodies that mention TRUE/FALSE in passing. The lead-line
        # match above handles every well-formed case; truly malformed
        # verdicts now fall through to the FALLBACKS list below and
        # only match strong patterns like `**Verdict: X**`.

    # Fallback: scan the whole text for strong verdict-declaration patterns.
    # Tries from most specific (least likely to false-positive) to broadest.
    if verdict == "UNKNOWN":
        FALLBACKS = (
            # **Verdict: X** with bold emphasis on both sides (strongest signal)
            r"(?im)\*{2}\s*verdict\s*[:=\-]\s*(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|TRUE|FALSE)\b\s*\*{2}",
            # Overall/Final verdict: X
            r"(?im)\*{0,2}(?:overall|final)\s+verdict\*{0,2}\s*[:=\-]\s*\*{0,2}\s*(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|TRUE|FALSE)\b",
            # Verdict: X (Confidence: ...) — parenthetical confidence next to it
            r"(?i)\bverdict\s*[:=\-]\s*(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|TRUE|FALSE)\b\s*\(?\s*confidence",
            # Recommendation: ... is/should be ... TRUE/FALSE (within a sentence)
            r"(?im)\brecommendation\b[^.]{0,200}?\b(?:is|should\s+be)\b[^.]{0,80}?\b(TRUE|FALSE)\b",
            # FIX 2026-05-19: VERDICT: TOKEN (anything-in-parens) more leniently.
            # CMED21 ended with `**VERDICT: FALSE (as-shipped) / TRUE (latent...)**`
            # — the closing `**` was after the parens, defeating the strongest
            # fallback above. Accept the FIRST verdict token after `VERDICT:` /
            # `Verdict:` regardless of what comes after.
            r"(?im)\*{0,2}\s*verdict\s*[:=\-]\s*\*{0,2}(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|TRUE|FALSE)\b",
            # FIX 2026-05-19: Markdown table row verdicts. CMED23/CMED25 ended
            # with rows like `| **Overall claim** | **TRUE** |` or
            # `| Confidence | **HIGH** |` — verdict only appears as a bolded
            # table cell, no `## Verdict` heading. Scan the LAST 800 chars of
            # the text for `| **(TRUE|FALSE|NEEDS_LAYER_2)** |` patterns.
            # Last-chars window prevents false-positives from in-narrative
            # tables earlier in the response.
        )
        for pat in FALLBACKS:
            m = re.search(pat, text)
            if m:
                tok = m.group(1).upper().replace(" ", "_")
                if "NEEDS" in tok:
                    verdict = "NEEDS_LAYER_2_TO_DECIDE"
                else:
                    verdict = tok
                break

        # Table-cell fallback — only scan the final 800 chars to avoid
        # false-positives from analysis tables earlier in the response.
        if verdict == "UNKNOWN":
            tail = text[-800:]
            table_pat = (
                r"\|\s*\*{0,2}\s*(?:overall\s+claim|verdict|conclusion|"
                r"final\s+verdict|UAF\s+exists)\s*\*{0,2}\s*\|"
                r"\s*\*{0,2}\s*(NEEDS[_\s]LAYER[_\s]2(?:[_\s]TO[_\s]DECIDE)?|TRUE|FALSE)\b"
            )
            m = re.search(table_pat, tail, re.I)
            if m:
                tok = m.group(1).upper().replace(" ", "_")
                if "NEEDS" in tok:
                    verdict = "NEEDS_LAYER_2_TO_DECIDE"
                else:
                    verdict = tok

    # Confidence — search block first (typed near the verdict), else
    # whole text as fallback.
    confidence = "UNKNOWN"
    haystack = block if block else text.upper()
    for c in ("HIGH", "MED", "LOW"):
        if re.search(rf"\b{c}\b", haystack):
            confidence = c
            break

    return verdict, confidence
