"""Regression tests for the L1.5 → L2 cold-verify pre-gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from audit_pipeline.commands.cold_verify import (
    _claim_canon,
    _extract_code_sites,
    _is_amplification,
    _is_phantom_body,
    cluster_candidates,
    cold_verify_cycle,
)


def test_phantom_phrase_detects_apt29_body() -> None:
    """APT29's exact phrasing — the parser had recorded TRUE but the
    body clearly says the hyp doesn't apply."""
    text = (
        "The hypothesis simply does not apply to this codebase. "
        "There is no auction module..."
    )
    is_phantom, phrase = _is_phantom_body(text)
    assert is_phantom
    assert phrase is not None


def test_phantom_phrase_detects_presupposes_construct() -> None:
    text = (
        "The hypothesis presupposes the existence of auction settlement "
        "logic. None of these constructs exist."
    )
    is_phantom, phrase = _is_phantom_body(text)
    assert is_phantom


def test_phantom_phrase_does_not_misfire_on_real_bug() -> None:
    text = (
        "The hypothesis is TRUE. Permissionless emergency_withdraw is "
        "directly reachable at treasury.move:61. The vulnerability is real."
    )
    is_phantom, _ = _is_phantom_body(text)
    assert not is_phantom


def test_amplify_detects_cannot_find_a_hole() -> None:
    """APT10/APT12-style: challenger writes DISAGREE but says the bug
    is real and the proposer understated it."""
    text = (
        "## Verdict\nDISAGREE — confidence HIGH\n\n"
        "After a hard adversarial pass, I cannot find a meaningful hole "
        "in the proposer's reasoning. The vulnerability is real and the "
        "reasoning is sound. However, the proposer understated the scope."
    )
    is_amp, phrase = _is_amplification(text)
    assert is_amp
    assert phrase is not None


def test_amplify_detects_proposer_missed() -> None:
    text = "DISAGREE. The proposer missed the staking.move:69 overflow."
    is_amp, _ = _is_amplification(text)
    assert is_amp


def test_amplify_negative_on_pure_refutation() -> None:
    """If the challenger genuinely refutes (says bug is false) without
    amplification cues, we should NOT treat as amplification."""
    text = (
        "DISAGREE — the precondition does not hold. The proposer's "
        "evidence is based on a misreading of the code. Looking at "
        "vault.move:42, the guard the proposer claims is missing is "
        "actually present three lines above."
    )
    is_amp, _ = _is_amplification(text)
    assert not is_amp


def test_claim_canon_collapses_whitespace_and_punctuation() -> None:
    a = "Every `borrow_global_mut<T>(addr)` operation is gated by auth."
    b = "every borrow_global_mut t addr operation is gated by auth"
    assert _claim_canon(a) == _claim_canon(b)


def test_cluster_dedup_prefers_aptm_over_apt() -> None:
    """When APT38 and APTM2 share (bug_class, target_file, claim), the
    APTM* representative should win — it has the exact function name."""
    candidates = [
        {
            "id": "APT38-treasury-drain",
            "bug_class": "treasury-drain",
            "target_file": "sources/treasury.move",
            "claim": "Every treasury withdrawal function checks admin auth.",
        },
        {
            "id": "APTM2-treasury-emergency-withdraw-no-auth",
            "bug_class": "treasury-drain",
            "target_file": "sources/treasury.move",
            "claim": "Every treasury withdrawal function checks admin auth.",
        },
    ]
    clusters = cluster_candidates(candidates)
    assert len(clusters) == 1
    rep = next(iter(clusters.values()))[0]
    assert rep == "APTM2-treasury-emergency-withdraw-no-auth"


def test_cluster_dedup_does_not_collapse_distinct_bugs() -> None:
    candidates = [
        {
            "id": "APT16-oracle-staleness",
            "bug_class": "oracle-staleness",
            "target_file": "sources/oracle.move",
            "claim": "Oracle reads check updated_ts freshness.",
        },
        {
            "id": "APT17-oracle-zero-price",
            "bug_class": "oracle-zero-price",
            "target_file": "sources/oracle.move",
            "claim": "Oracle reads reject zero or negative prices.",
        },
    ]
    clusters = cluster_candidates(candidates)
    assert len(clusters) == 0  # different bug_class — no cluster


def test_cold_verify_cycle_drops_phantom_hyp(tmp_path: Path) -> None:
    """End-to-end: build a minimal cycle dir with a phantom hyp (recon
    says TRUE but body says 'does not apply'). Cold-verify must drop it."""
    cycle_dir = tmp_path / "hunts" / "20260514-fake"
    (cycle_dir / "recon").mkdir(parents=True)
    (cycle_dir / "debate").mkdir(parents=True)

    # Fake hyp library
    hyp_lib = tmp_path / "hyps.yaml"
    hyp_lib.write_text(yaml.safe_dump({"hypotheses": [
        {
            "id": "FAKE-phantom",
            "class": "invariant_property",
            "severity": "Low",
            "bug_class": "phantom",
            "engine_function": "settle",
            "target_file": "sources/auction.move",
            "claim": "Auctions settle correctly to seller on no-bid.",
        },
        {
            "id": "FAKE-real",
            "class": "authorization",
            "severity": "Critical",
            "bug_class": "missing-auth",
            "engine_function": "drain",
            "target_file": "sources/treasury.move",
            "claim": "Drain is gated by admin auth.",
        },
    ]}))

    recon_summary = {
        "cycle_id": "20260514-fake",
        "verdicts": [
            {"hypothesis_id": "FAKE-phantom", "verdict": "TRUE", "confidence": "HIGH"},
            {"hypothesis_id": "FAKE-real", "verdict": "TRUE", "confidence": "HIGH"},
        ],
    }
    (cycle_dir / "recon" / "recon_summary.json").write_text(json.dumps(recon_summary))

    (cycle_dir / "recon" / "FAKE-phantom_response.md").write_text(
        "## Verdict\n\n**FALSE** — Confidence: **HIGH**\n\n"
        "The hypothesis simply does not apply to this codebase. "
        "There is no auction module anywhere in sources/."
    )
    (cycle_dir / "recon" / "FAKE-real_response.md").write_text(
        "## Verdict\n\n**TRUE** — HIGH\n\n"
        "Drain at treasury.move:42 is permissionless. The vulnerability is real."
    )

    payload = cold_verify_cycle(cycle_dir, hyp_lib)

    keep = set(payload["keep"])
    drop = {hid for hid, _ in payload["drop"]}
    assert "FAKE-phantom" in drop, "phantom hyp should have been dropped"
    assert "FAKE-real" in keep, "real bug should be kept"

    # Drop reason should mention 'phantom'
    phantom_decision = next(
        d for d in payload["decisions"] if d["hyp_id"] == "FAKE-phantom"
    )
    assert "phantom" in (phantom_decision["drop_reason"] or "").lower()


def test_extract_code_sites_basic() -> None:
    """Should pull (file, line) tuples from common citation formats."""
    text = (
        "The bug is at sources/treasury.move:61. The fix would be at "
        "treasury.move:65 or vault.move line 42. Also sources/vault.move:61-66."
    )
    sites = _extract_code_sites(text)
    files = {s[0] for s in sites}
    assert "treasury.move" in files
    assert "vault.move" in files


def test_extract_code_sites_buckets_nearby_lines() -> None:
    """treasury.move:61 and treasury.move:64 should snap to the same
    5-line bucket — same code site."""
    text = "Path 1 at treasury.move:61. Path 2 at treasury.move:64."
    sites = _extract_code_sites(text)
    # Both should map to bucket starting at 60
    buckets = {s[1] for s in sites if s[0] == "treasury.move"}
    assert len(buckets) == 1
    assert 60 in buckets


def test_cluster_dedup_collapses_when_responses_cite_same_site() -> None:
    """APT38 and APTM2 have different (bug_class, target_file) but
    cite the same code site at treasury.move:60-65. The code-site
    pass-2 clusterer must collapse them.
    """
    candidates = [
        {
            "id": "APT38-treasury-drain",
            "bug_class": "treasury-drain",
            "target_file": "sources/*.move",          # glob — differs
            "claim": "Generic treasury drain via emergency.",
        },
        {
            "id": "APTM2-treasury-emergency-withdraw-no-auth",
            "bug_class": "treasury-drain",
            "target_file": "sources/treasury.move",   # specific — differs
            "claim": "treasury::emergency_withdraw is permissionless.",
        },
    ]
    responses = {
        "APT38-treasury-drain": (
            "## Verdict\n**TRUE**\n\nThe drain is at sources/treasury.move:61. "
            "emergency_drain bypasses ACL checks at treasury.move:64."
        ),
        "APTM2-treasury-emergency-withdraw-no-auth": (
            "## Verdict\n**TRUE**\n\nThe critical issue is at treasury.move:61-66 "
            "where emergency_withdraw never calls acl::assert_admin."
        ),
    }
    clusters = cluster_candidates(candidates, response_texts=responses)
    assert len(clusters) == 1
    rep = next(iter(clusters.values()))[0]
    assert rep == "APTM2-treasury-emergency-withdraw-no-auth"


def test_cluster_dedup_keeps_distinct_sites_separate() -> None:
    """Two hyps that cite DIFFERENT code sites should NOT cluster
    even with similar bug_class."""
    candidates = [
        {
            "id": "APT-treasury",
            "bug_class": "missing-auth",
            "target_file": "sources/*.move",
            "claim": "Missing auth somewhere.",
        },
        {
            "id": "APTM-oracle",
            "bug_class": "missing-auth",
            "target_file": "sources/*.move",
            "claim": "Missing auth somewhere else.",
        },
    ]
    responses = {
        "APT-treasury": "## Verdict\nTRUE\n\nbug at treasury.move:61",
        "APTM-oracle":  "## Verdict\nTRUE\n\nbug at oracle.move:51",
    }
    clusters = cluster_candidates(candidates, response_texts=responses)
    assert len(clusters) == 0  # different sites, no cluster


def test_cold_verify_cycle_collapses_duplicate(tmp_path: Path) -> None:
    """Two hyps with identical (bug_class, target_file, claim) should
    collapse into one — APTM* preferred."""
    cycle_dir = tmp_path / "hunts" / "20260514-dup"
    (cycle_dir / "recon").mkdir(parents=True)
    (cycle_dir / "debate").mkdir(parents=True)

    hyp_lib = tmp_path / "hyps.yaml"
    hyp_lib.write_text(yaml.safe_dump({"hypotheses": [
        {
            "id": "APT38-treasury-drain",
            "class": "authorization",
            "severity": "Critical",
            "bug_class": "treasury-drain",
            "engine_function": "emergency_drain",
            "target_file": "sources/treasury.move",
            "claim": "Every treasury withdrawal function checks admin auth.",
        },
        {
            "id": "APTM2-treasury-emergency-withdraw-no-auth",
            "class": "authorization",
            "severity": "Critical",
            "bug_class": "treasury-drain",
            "engine_function": "emergency_withdraw",
            "target_file": "sources/treasury.move",
            "claim": "Every treasury withdrawal function checks admin auth.",
        },
    ]}))

    recon_summary = {
        "cycle_id": "20260514-dup",
        "verdicts": [
            {"hypothesis_id": "APT38-treasury-drain", "verdict": "TRUE", "confidence": "HIGH"},
            {"hypothesis_id": "APTM2-treasury-emergency-withdraw-no-auth", "verdict": "TRUE", "confidence": "HIGH"},
        ],
    }
    (cycle_dir / "recon" / "recon_summary.json").write_text(json.dumps(recon_summary))
    for hid in ("APT38-treasury-drain", "APTM2-treasury-emergency-withdraw-no-auth"):
        (cycle_dir / "recon" / f"{hid}_response.md").write_text(
            "## Verdict\n\n**TRUE** — HIGH\n\nThe drain is permissionless."
        )

    payload = cold_verify_cycle(cycle_dir, hyp_lib)
    assert payload["n_clusters_collapsed"] == 1
    assert "APTM2-treasury-emergency-withdraw-no-auth" in payload["keep"]
    drop_ids = {hid for hid, _ in payload["drop"]}
    assert "APT38-treasury-drain" in drop_ids
