"""Tests for Layer 2.5 fire triage (audit-pipeline triage-fires + hunt wire-up).

Productized form of the manual STRONG/SOFT/FALSE bucket-sort that
collapsed cycle 20260511's 64 PoC fires down to 7 STRONG / 4 root causes.

Coverage:
  - Fast-path FALSE patterns (no LLM cost) catch the dominant
    "params_for_*() factory panic" failure mode + sibling patterns
  - LLM judge contract (system prompt structure, JSON parse robustness)
  - Root-cause clustering by (bug_class, engine_function, claim Jaccard)
  - End-to-end triage_cycle on a synthetic cycle dir
  - Hunt wire-up: triage_fires flag default ON, filter narrows Layer 3
    dispatch set, summary contains triage block
"""

from __future__ import annotations

import json
from pathlib import Path

from audit_pipeline.layer25_triage import (
    FALSE_PATTERNS,
    _parse_judge_response,
    build_judge_user_prompt,
    classify_by_pattern,
    cluster_strong_fires,
    extract_panic_line,
    triage_cycle,
)

# ─────────────────── Fast-path FALSE patterns ───────────────────


def test_riskparams_overflow_classified_false() -> None:
    """The dominant cycle-20260511 false-fire signature must short-circuit
    to FALSE without an LLM call."""
    line = (
        "thread 'tests::test_x' panicked at src/percolator.rs:1684:43: "
        "invalid RiskParams: Overflow"
    )
    result = classify_by_pattern(line)
    assert result is not None
    cls, reason = result
    assert cls == "FALSE"
    assert "RiskParams" in reason


def test_engine_unwrap_classified_false() -> None:
    line = (
        "thread 'tests::y' panicked at src/wrapper.rs:42:13: "
        "called `Result::unwrap()` on an `Err` value: EngineInsufficientBalance"
    )
    result = classify_by_pattern(line)
    assert result is not None
    assert result[0] == "FALSE"


def test_subtract_overflow_in_setup_classified_false() -> None:
    line = (
        "thread 'tests::z' panicked at tests/setup.rs:11:5: "
        "attempt to subtract with overflow"
    )
    result = classify_by_pattern(line)
    assert result is not None
    assert result[0] == "FALSE"


def test_test_file_index_oob_classified_false() -> None:
    line = (
        "thread 'tests::w' panicked at tests/test_h17.rs:88:20: "
        "index out of bounds: the len is 3 but the index is 5"
    )
    result = classify_by_pattern(line)
    assert result is not None
    assert result[0] == "FALSE"


def test_setup_assertion_classified_false() -> None:
    line = (
        "thread 'tests::a' panicked at tests/test_v1.rs:55:5: "
        "assertion `left == right` failed in setup phase"
    )
    result = classify_by_pattern(line)
    assert result is not None
    assert result[0] == "FALSE"


def test_unknown_panic_returns_none() -> None:
    """Genuinely novel panics must fall through to the LLM judge,
    not silently classify as FALSE."""
    line = (
        "thread 'tests::b' panicked at src/engine.rs:200:5: "
        "haircut residual exceeded vault balance by 12345 units"
    )
    assert classify_by_pattern(line) is None


def test_empty_panic_line_returns_none() -> None:
    assert classify_by_pattern("") is None


def test_extract_panic_line_finds_first_panic() -> None:
    log = """
test::foo ... FAILED
note: some context
thread 'foo' panicked at src/x.rs:1:1: assertion failed
note: run with `RUST_BACKTRACE=1` for a backtrace
"""
    line = extract_panic_line(log)
    assert "panicked at" in line
    assert "src/x.rs" in line


def test_extract_panic_line_falls_back_to_assertion() -> None:
    log = "ok 1 test\nassertion `left == right` failed at tests/t.rs:5\n"
    line = extract_panic_line(log)
    assert "assertion" in line.lower()


def test_extract_panic_line_returns_empty_when_no_panic() -> None:
    assert extract_panic_line("test result: ok. 1 passed; 0 failed") == ""


def test_false_patterns_list_immutable_shape() -> None:
    """Each entry must be (Pattern, str) — schema lock for the registry."""
    import re as _re
    for entry in FALSE_PATTERNS:
        assert len(entry) == 2
        assert isinstance(entry[0], _re.Pattern)
        assert isinstance(entry[1], str)
        assert entry[1]  # non-empty reason


# ─────────────────── LLM judge response parsing ───────────────────


def test_parse_judge_response_strong() -> None:
    cls, reason = _parse_judge_response(
        '{"classification": "STRONG", "reason": "vault drain matches claim"}'
    )
    assert cls == "STRONG"
    assert "vault" in reason


def test_parse_judge_response_with_prose_around_json() -> None:
    text = (
        "Sure, here's my judgment:\n\n"
        '{"classification": "SOFT", "reason": "test asserts wrong invariant"}\n\n'
        "Let me know if you need more detail."
    )
    cls, reason = _parse_judge_response(text)
    assert cls == "SOFT"


def test_parse_judge_response_lowercase_classification() -> None:
    cls, _ = _parse_judge_response(
        '{"classification": "strong", "reason": "x"}'
    )
    assert cls == "STRONG"


def test_parse_judge_response_unknown_classification_defaults_to_soft() -> None:
    cls, reason = _parse_judge_response(
        '{"classification": "MAYBE", "reason": "x"}'
    )
    assert cls == "SOFT"
    assert "unknown" in reason.lower()


def test_parse_judge_response_invalid_json_defaults_to_soft() -> None:
    cls, _ = _parse_judge_response("not json at all")
    assert cls == "SOFT"


def test_build_judge_user_prompt_includes_all_inputs() -> None:
    prompt = build_judge_user_prompt(
        hyp_id="H_TEST",
        claim="vault balance is conserved",
        bug_class="implicit_invariant",
        engine_function="absorb_loss",
        test_body="fn test_x() { /* ... */ }",
        panic_line="panicked at: residual mismatch",
        engine_source="fn absorb_loss(state: &mut State) { ... }",
    )
    assert "H_TEST" in prompt
    assert "vault balance is conserved" in prompt
    assert "implicit_invariant" in prompt
    assert "absorb_loss" in prompt
    assert "residual mismatch" in prompt


def test_build_judge_user_prompt_truncates_long_inputs() -> None:
    """Test bodies + engine sources can be huge. Confirm we cap each
    block so the prompt doesn't blow context."""
    huge = "X" * 100_000
    prompt = build_judge_user_prompt(
        "H", "c", "bc", "ef", huge, "p", huge,
    )
    # Test body capped at 4000, engine source at 3000, panic line at 1500
    assert len(prompt) < 10_000


# ─────────────────── Root-cause clustering ───────────────────


def test_clusters_same_bugclass_function_and_claim_together() -> None:
    """F7-family: 4 hyps with same bug_class + engine_function + similar
    claim wording must collapse to ONE cluster with ONE representative."""
    strong = [
        {"hyp_id": "F7a", "bug_class": "implicit_invariant",
         "engine_function": "use_insurance_buffer",
         "claim": "insurance counter shrinks without vault debit"},
        {"hyp_id": "F7b", "bug_class": "implicit_invariant",
         "engine_function": "use_insurance_buffer",
         "claim": "insurance counter shrinks without vault debit accounting"},
        {"hyp_id": "F7c", "bug_class": "implicit_invariant",
         "engine_function": "use_insurance_buffer",
         "claim": "insurance counter shrinks without proper vault debit"},
    ]
    clusters = cluster_strong_fires(strong)
    assert len(clusters) == 1
    cid = next(iter(clusters))
    assert set(clusters[cid]) == {"F7a", "F7b", "F7c"}


def test_clusters_split_on_different_engine_function() -> None:
    """Same bug_class but different engine_function = different cluster
    (different root cause even if surface symptoms rhyme)."""
    strong = [
        {"hyp_id": "X", "bug_class": "invariant_property",
         "engine_function": "absorb_loss",
         "claim": "the residual grows unboundedly"},
        {"hyp_id": "Y", "bug_class": "invariant_property",
         "engine_function": "settle_negative_pnl",
         "claim": "the residual grows unboundedly"},
    ]
    clusters = cluster_strong_fires(strong)
    assert len(clusters) == 2


def test_clusters_singletons_when_claims_diverge() -> None:
    """Different bug_class + different claims = 3 singleton clusters
    (mirrors cycle 20260511: AR7 / CI10 / L3 each in own cluster)."""
    strong = [
        {"hyp_id": "AR7", "bug_class": "arithmetic_overflow",
         "engine_function": "fee_math", "claim": "fee accrual overflows i128"},
        {"hyp_id": "CI10", "bug_class": "state_transition",
         "engine_function": "resolve", "claim": "resolved market still allows trades"},
        {"hyp_id": "L3", "bug_class": "authorization",
         "engine_function": "liquidate", "claim": "liquidation bypasses keeper auth"},
    ]
    clusters = cluster_strong_fires(strong)
    assert len(clusters) == 3
    assert all(len(members) == 1 for members in clusters.values())


def test_cluster_first_member_is_representative() -> None:
    strong = [
        {"hyp_id": "A", "bug_class": "x", "engine_function": "f",
         "claim": "alpha beta gamma delta"},
        {"hyp_id": "B", "bug_class": "x", "engine_function": "f",
         "claim": "alpha beta gamma delta epsilon"},
    ]
    clusters = cluster_strong_fires(strong)
    # Cluster id should be the FIRST hyp_id added
    assert "A" in clusters
    assert "B" in clusters["A"]


# ─────────────────── triage_cycle end-to-end ───────────────────


def _seed_cycle(tmp_path: Path, fires: list[dict]) -> tuple[Path, dict, dict]:
    """Build a synthetic cycle dir with PoC test files + cargo logs.

    fires: list of {"hyp_id", "panic_line", "test_body", "fired" (default True)}
    Returns (cycle_dir, poc_results, hyp_meta).
    """
    cycle_dir = tmp_path / "hunts" / "C-TRIAGE-TEST"
    cycle_dir.mkdir(parents=True)
    poc_dir = cycle_dir / "poc"
    poc_dir.mkdir()
    logs_dir = cycle_dir / "logs"
    logs_dir.mkdir()
    poc_results = {}
    hyp_meta = {}
    for f in fires:
        hid = f["hyp_id"]
        test_path = poc_dir / f"test_{hid.lower()}.rs"
        test_path.write_text(f.get("test_body", "fn test_x() {}"), encoding="utf-8")
        log_path = logs_dir / f"{hid}.log"
        log_text = f"test::test_{hid} ... FAILED\n"
        if f.get("panic_line"):
            log_text += f"thread 'test_{hid}' panicked at {f['panic_line']}\n"
        log_path.write_text(log_text, encoding="utf-8")
        poc_results[hid] = {
            "scaffold_path": str(test_path),
            "cargo_log_path": str(log_path),
            "fired": f.get("fired", True),
            "outcome": "test_failed_bug_reproduced",
        }
        hyp_meta[hid] = {
            "id": hid,
            "claim": f.get("claim", f"claim for {hid}"),
            "bug_class": f.get("bug_class", "x"),
            "engine_function": f.get("engine_function", "fn_x"),
        }
    return cycle_dir, poc_results, hyp_meta


def test_triage_cycle_fast_path_classifies_riskparams_as_false(tmp_path: Path) -> None:
    """41 of cycle 20260511's 45 FALSE fires were this exact pattern. The
    fast path must catch them WITHOUT calling the LLM."""
    cycle_dir, poc_results, hyp_meta = _seed_cycle(tmp_path, [
        {"hyp_id": "F1", "panic_line":
            "src/percolator.rs:1684:43: invalid RiskParams: Overflow"},
        {"hyp_id": "F2", "panic_line":
            "src/percolator.rs:1684:43: invalid RiskParams: Overflow"},
    ])

    def boom_if_called(*a, **kw):
        raise AssertionError("LLM judge should NOT be called for fast-path FALSE")

    out = triage_cycle(
        cycle_dir,
        poc_results=poc_results,
        hyp_meta=hyp_meta,
        complete_fn=boom_if_called,
    )
    assert out["counts"]["FALSE"] == 2
    assert out["counts"]["STRONG"] == 0
    assert out["n_llm_calls"] == 0
    assert out["layer3_dispatch_set"] == []


def test_triage_cycle_llm_judge_invoked_for_unknown_panics(tmp_path: Path) -> None:
    cycle_dir, poc_results, hyp_meta = _seed_cycle(tmp_path, [
        {"hyp_id": "S1", "panic_line":
            "src/engine.rs:200:5: residual exceeded vault by 1234 lamports"},
    ])

    calls = {"n": 0}

    def fake_complete(prompt, **kwargs):
        calls["n"] += 1
        class R:
            text = '{"classification": "STRONG", "reason": "real bug"}'
        return R()

    out = triage_cycle(
        cycle_dir,
        poc_results=poc_results,
        hyp_meta=hyp_meta,
        complete_fn=fake_complete,
    )
    assert calls["n"] == 1
    assert out["counts"]["STRONG"] == 1
    assert out["n_llm_calls"] == 1


def test_triage_cycle_writes_triage_jsonl(tmp_path: Path) -> None:
    cycle_dir, poc_results, hyp_meta = _seed_cycle(tmp_path, [
        {"hyp_id": "F1", "panic_line":
            "src/percolator.rs:1684:43: invalid RiskParams: Overflow"},
    ])
    out = triage_cycle(cycle_dir, poc_results=poc_results, hyp_meta=hyp_meta)
    p = Path(out["triage_jsonl_path"])
    assert p.is_file()
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["hyp_id"] == "F1"
    assert row["classification"] == "FALSE"


def test_triage_cycle_missing_files_classified_lost(tmp_path: Path) -> None:
    """If the test scaffold file or cargo log was wiped (U23 in cycle
    20260511), the fire must classify as LOST not silently SOFT."""
    cycle_dir = tmp_path / "hunts" / "C-LOST"
    cycle_dir.mkdir(parents=True)
    poc_results = {"U23": {
        "scaffold_path": str(cycle_dir / "poc" / "test_u23.rs"),  # doesn't exist
        "cargo_log_path": str(cycle_dir / "logs" / "u23.log"),    # doesn't exist
        "fired": True,
    }}
    hyp_meta = {"U23": {"claim": "x", "bug_class": "y", "engine_function": "z"}}
    out = triage_cycle(cycle_dir, poc_results=poc_results, hyp_meta=hyp_meta)
    assert out["counts"]["LOST"] == 1
    assert out["counts"]["STRONG"] == 0


def test_triage_cycle_skips_non_fired_pocs(tmp_path: Path) -> None:
    """PoC tests that ran but didn't fire (test_passed_no_bug) must NOT
    be triaged — there's nothing to classify."""
    cycle_dir, poc_results, hyp_meta = _seed_cycle(tmp_path, [
        {"hyp_id": "P1", "fired": False},
        {"hyp_id": "F1", "fired": True, "panic_line":
            "src/percolator.rs:1: invalid RiskParams: Overflow"},
    ])
    out = triage_cycle(cycle_dir, poc_results=poc_results, hyp_meta=hyp_meta)
    # Only F1 should appear in results (P1 didn't fire)
    assert len(out["results"]) == 1
    assert out["results"][0]["hyp_id"] == "F1"


def test_triage_cycle_layer3_dispatch_set_contains_only_strong_representatives(
    tmp_path: Path,
) -> None:
    """Two STRONG fires in the same cluster + one FALSE fire → dispatch
    set has exactly 1 representative."""
    cycle_dir, poc_results, hyp_meta = _seed_cycle(tmp_path, [
        {"hyp_id": "A", "panic_line": "novel panic alpha",
         "claim": "vault balance equation drifts",
         "bug_class": "implicit_invariant", "engine_function": "absorb"},
        {"hyp_id": "B", "panic_line": "novel panic beta",
         "claim": "vault balance equation drifts further",
         "bug_class": "implicit_invariant", "engine_function": "absorb"},
        {"hyp_id": "F", "panic_line":
            "src/percolator.rs:1: invalid RiskParams: Overflow",
         "claim": "unrelated", "bug_class": "x", "engine_function": "y"},
    ])

    def all_strong(prompt, **kwargs):
        class R:
            text = '{"classification": "STRONG", "reason": "real bug"}'
        return R()

    out = triage_cycle(
        cycle_dir,
        poc_results=poc_results,
        hyp_meta=hyp_meta,
        complete_fn=all_strong,
    )
    assert out["counts"]["STRONG"] == 2
    assert out["counts"]["FALSE"] == 1
    assert len(out["layer3_dispatch_set"]) == 1  # one representative for the cluster


# ─────────────────── Hunt wire-up ───────────────────


def test_hunt_cmd_exposes_triage_fires_flag() -> None:
    from audit_pipeline.commands.hunt import hunt_cmd
    names = {p.name for p in hunt_cmd.params}
    assert "triage_fires" in names


def test_hunt_cmd_triage_fires_default_on() -> None:
    """Default must be ON. The whole point of building Layer 2.5 is to
    save $280 per cycle by default. Operators can opt out with
    --no-triage-fires for cheap CI."""
    from audit_pipeline.commands.hunt import hunt_cmd
    for p in hunt_cmd.params:
        if p.name == "triage_fires":
            assert p.default is True, (
                "regression: triage_fires default is no longer True — "
                "operators would lose the cost-saving filter"
            )


def test_hunt_source_threads_triage_fires_to_layer3_filter() -> None:
    """The wire-up: triage's layer3_dispatch_set must filter fired_for_kani."""
    import audit_pipeline.commands.hunt as hunt_mod
    src = Path(hunt_mod.__file__).read_text(encoding="utf-8")
    # Triage block exists
    assert "Layer 2.5" in src
    assert "from audit_pipeline.layer25_triage import triage_cycle" in src
    # Filter actually applied to fired_for_kani
    assert "layer3_dispatch_filter" in src
    # Summary includes triage block
    assert '"triage": triage_summary' in src


# ─────────────────── CLI registration ───────────────────


def test_triage_fires_cli_registered() -> None:
    from audit_pipeline.cli import main
    assert "triage-fires" in main.commands
