"""Static-source tests asserting hunt.py's multi-language wiring.

Phase 1h rewired the hunt orchestrator so workspace.json's `language`
field drives which adapter package each layer uses:

    Solana   → cargo test           / synth-kani / LiteSVM      (legacy path)
    C        → poc_adapters.CAdapter / formal_adapters / AFL++
    Solidity → SolidityAdapter      / SMTChecker      / forge-fuzz
    Aptos    → AptosAdapter         / Move Prover     / aptos move test

The dispatch happens inside `_hunt_run` via three `get_adapter(language)`
calls. These tests pin those wires so a future refactor can't silently
revert them (and re-break the OSec eval the way the original audit
caught).
"""

from __future__ import annotations

from pathlib import Path


def _hunt_src() -> str:
    """Return hunt.py's source as a single string."""
    import audit_pipeline.commands.hunt as hunt_mod
    return Path(hunt_mod.__file__).read_text(encoding="utf-8")


# ─────────────────── workspace.json field reads ───────────────────


def test_hunt_reads_language_from_workspace_json() -> None:
    """REGRESSION: hunt.py must read workspace.json["language"] so the
    OSec eval cells (each tagged with a different language) get
    dispatched correctly."""
    src = _hunt_src()
    assert 'config.get("language")' in src, (
        "hunt.py must read workspace.json['language']"
    )
    # Validation of the language value
    assert '"solana", "c", "solidity", "aptos"' in src or \
           "{solana, c, solidity, aptos}" in src, (
        "hunt.py must restrict language to the four supported values"
    )


def test_hunt_reads_hyp_library_from_workspace_json() -> None:
    """REGRESSION: hunt.py must read workspace.json["hyp_library"] so
    each language cell defaults to its own class library."""
    src = _hunt_src()
    assert 'config.get("hyp_library")' in src, (
        "hunt.py must read workspace.json['hyp_library']"
    )


def test_hunt_reads_customer_id_from_workspace_json() -> None:
    """REGRESSION: hunt.py must read workspace.json["customer_id"] so
    every cell of a multi-target customer flows findings into the
    SHARED parent eval DB the dashboard reads."""
    src = _hunt_src()
    assert 'config.get("customer_id")' in src, (
        "hunt.py must read workspace.json['customer_id']"
    )


# ─────────────────── adapter dispatch wiring ───────────────────


def test_hunt_imports_poc_adapter_package() -> None:
    """REGRESSION: L2 must dispatch to the poc_adapters package for
    non-Solana languages. Without this import, every OSec cell would
    fall back to the Solana cargo test path and produce nonsense."""
    src = _hunt_src()
    assert "from audit_pipeline.poc_adapters import get_adapter" in src, (
        "hunt.py must import poc_adapters.get_adapter for L2 dispatch"
    )


def test_hunt_imports_formal_adapter_package() -> None:
    """REGRESSION: L3 must dispatch to formal_adapters for non-Solana."""
    src = _hunt_src()
    assert "from audit_pipeline.formal_adapters import" in src, (
        "hunt.py must import formal_adapters for L3 dispatch"
    )


def test_hunt_imports_runtime_adapter_package() -> None:
    """REGRESSION: L4 must dispatch to runtime_adapters for non-Solana."""
    src = _hunt_src()
    assert "from audit_pipeline.runtime_adapters import" in src, (
        "hunt.py must import runtime_adapters for L4 dispatch"
    )


# ─────────────────── subprocess --language passthrough ───────────────────


def test_hunt_passes_language_to_recon_subprocess() -> None:
    """REGRESSION: recon subprocess must get --language so its system
    prompt picks the right language-specific framing."""
    src = _hunt_src()
    # Find the recon_argv block and verify --language appears in it.
    idx = src.find("recon_argv = [")
    assert idx > 0
    chunk = src[idx:idx + 2000]
    assert '"--language", language' in chunk or \
           "'--language', language" in chunk, (
        "hunt.py must pass --language to the recon subprocess"
    )


# ─────────────────── Patch #16 R2 — taint-refused outcome preservation ───────────────────


def test_hunt_outcome_classifier_includes_tainted_filter_branch() -> None:
    """REGRESSION (Patch #16 R1 F-1): the outcome ternary at hunt.py
    must have a `tainted_filter_rejected` branch keyed on
    `metadata.get("phase") == "taint_refused"`. Without it, the aptos
    adapter's tainted-filter refusal silently reclassifies as
    `test_passed_no_bug` — reintroducing the false-negative bug
    discovery the audit aimed to close.

    R3 hardening (code-reviewer): anchor the check to the
    `poc_results[hyp_id] = {` assignment block so a future refactor
    that moves the branch INTO a comment (string-match would still
    pass on a whole-file grep) is caught.
    """
    src = _hunt_src()
    # Locate the relevant poc_results assignment block. There are
    # multiple poc_results writes in hunt.py — pick the one that
    # immediately precedes the `cargo_log_path` key (the live-run
    # outcome ternary lives there).
    idx = src.find('"outcome": (\n                        "test_failed_bug_reproduced"')
    assert idx > 0, "live-run poc_results outcome ternary not found"
    chunk = src[idx:idx + 1500]
    assert '"tainted_filter_rejected"' in chunk, (
        f"hunt.py outcome ternary must produce 'tainted_filter_rejected' "
        f"in the live-run poc_results assignment block; got: {chunk!r}"
    )
    assert '"taint_refused"' in chunk, (
        f"hunt.py outcome ternary must check metadata.get('phase') == "
        f"'taint_refused' in the same block; got: {chunk!r}"
    )


def test_hunt_retry_guard_skips_taint_refused() -> None:
    """REGRESSION (Patch #16 R1 F-2): the L2 passed-no-fire retry loop
    must NOT fire when the outcome was refused by the validator
    (phase == 'taint_refused'). Re-prompting the LLM with the same
    tainted source would echo back the same rejected harness."""
    src = _hunt_src()
    # Locate the retry-guard `while (` block and verify both phases
    # are listed in the skip tuple.
    idx = src.find("MAX_RUN_RETRIES = 2 if _debate_context else 0")
    assert idx > 0, "retry guard sentinel not found in hunt.py"
    chunk = src[idx:idx + 1500]
    assert '"compile"' in chunk and '"taint_refused"' in chunk, (
        f"retry guard at hunt.py must skip BOTH 'compile' and "
        f"'taint_refused' phases; got chunk: {chunk!r}"
    )


def test_hunt_poc_adapter_done_logs_outcome_and_phase() -> None:
    """REGRESSION (Patch #16 R2 fast-resume bypass): the
    `poc_adapter_done` log event must emit `outcome` and `phase` so
    the fast-resume reconstructions (lines ~1517 and ~1631) can
    preserve `tainted_filter_rejected` across `--resume-cycle`.
    Without these fields, the resume path re-derives outcome from
    only `fired` → reclassifies refused PoCs as clean passes."""
    src = _hunt_src()
    idx = src.find('log("poc_adapter_done"')
    assert idx > 0, "poc_adapter_done log call not found"
    chunk = src[idx:idx + 700]
    assert "outcome=" in chunk, (
        "poc_adapter_done log event must include `outcome=...`"
    )
    assert "phase=" in chunk, (
        "poc_adapter_done log event must include `phase=...`"
    )


def test_hunt_fast_resume_prefers_logged_outcome() -> None:
    """REGRESSION (Patch #16 R2 fast-resume bypass): both fast-resume
    reconstruction paths must consult `_evt.get('outcome')` /
    `prior_event.get('outcome')` BEFORE falling back to the binary
    fired-→-outcome derivation."""
    src = _hunt_src()
    # Both reconstruction blocks compute an `_evt_outcome` / `_prior_outcome`
    # local then assign it to `poc_results[...]["outcome"]`.
    assert "_evt_outcome = _evt.get(\"outcome\")" in src, (
        "fast-resume #1 must read outcome from the prior log event"
    )
    assert "_prior_outcome = prior_event.get(\"outcome\")" in src, (
        "fast-resume #2 must read outcome from the prior log event"
    )


def test_hunt_resume_uses_is_none_guard_not_truthy() -> None:
    """REGRESSION (Patch #16 R3 threat-modeler MEDIUM): the fallback to
    the binary-derived outcome MUST gate on `is None`, NOT on falsiness.
    An empty string `""` in the `outcome` field is suspicious (legitimate
    writers never emit it) and falling through to the binary derivation
    would silently restore the false-negative `test_passed_no_bug` for a
    refused PoC whose outcome was tampered to empty.
    """
    src = _hunt_src()
    # Both fast-resume paths must use `is None`, not `not _evt_outcome`.
    assert "if _evt_outcome is None:" in src, (
        "fast-resume #1 must gate fallback on `is None`, not truthiness"
    )
    assert "if _prior_outcome is None:" in src, (
        "fast-resume #2 must gate fallback on `is None`, not truthiness"
    )
    # Negative assertion: no truthy `if not _evt_outcome` form survives.
    assert "if not _evt_outcome" not in src, (
        "stale truthy guard found — would trigger fallback on empty string"
    )
    assert "if not _prior_outcome" not in src, (
        "stale truthy guard found — would trigger fallback on empty string"
    )


def test_hunt_pre_resume_summary_normalizes_missing_outcome() -> None:
    """REGRESSION (Patch #16 R3 threat-modeler LOW Finding 1): the
    `hunt_summary.json.pre-resume` loader at hunt.py:~1454 is the THIRD
    resume path. Without normalization, a pre-P16-R2 summary entry with
    no `outcome` field would surface as `outcome=None` downstream. Apply
    the same is-None backward-compat fallback the JSONL paths use so all
    three resume paths produce consistent outcome strings."""
    src = _hunt_src()
    idx = src.find("poc_results[_hyp_id] = dict(_entry)")
    assert idx > 0, "pre-resume loader sentinel not found in hunt.py"
    chunk = src[idx:idx + 800]
    assert "poc_results[_hyp_id].get(\"outcome\") is None" in chunk, (
        f"pre-resume loader must normalize missing outcome via is-None "
        f"guard; chunk: {chunk!r}"
    )
    # Negative: don't accidentally use truthy guard here either.
    # Patch #16 R5 (paranoid-goober): catch both bare-dict and
    # `.get("outcome")` truthy variants (`if not d[..]` and
    # `if not d[..].get("outcome")`). Both would silently restore
    # the binary-derivation fallback on an empty-string outcome.
    assert "if not poc_results[_hyp_id]" not in chunk, (
        "pre-resume loader must NOT use truthy guard on outcome"
    )
    assert "if not poc_results[_hyp_id].get" not in chunk, (
        "pre-resume loader must NOT use truthy `.get()` form either — "
        "use `is None` instead"
    )


def test_hunt_passes_language_to_debate_subprocess() -> None:
    """REGRESSION: debate subprocess must get --language so the challenger
    uses the right adversarial frame."""
    src = _hunt_src()
    idx = src.find("debate_argv = [")
    assert idx > 0
    chunk = src[idx:idx + 1500]
    assert '"--language", language' in chunk or \
           "'--language', language" in chunk, (
        "hunt.py must pass --language to the debate subprocess"
    )


# ─────────────────── customer DB redirect ───────────────────


def test_hunt_redirects_db_when_customer_id_set() -> None:
    """REGRESSION: when workspace.json has customer_id, the findings DB
    must be written to the SHARED parent eval dir, not per-workspace.
    Without this, every OSec eval cell writes to its own isolated DB
    and the customer dashboard sees zero findings."""
    src = _hunt_src()
    # The redirect is implemented by walking up from the workspace to
    # the parent eval dir. Check the key marker comment + code.
    assert "shared customer-level findings.db" in src.lower() or \
           "shared DB" in src or \
           "B1 fix" in src, (
        "hunt.py must redirect findings DB to parent eval dir when "
        "customer_id is set"
    )
    assert "db_workspace" in src, (
        "hunt.py must use a separate db_workspace variable for the redirect"
    )
