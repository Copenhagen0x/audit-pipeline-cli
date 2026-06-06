"""Behavioral tests for the TypeScript / generic report renderer mode.

The cycle renderer (`_render_cycle_html`) frames every section by detected
language. Before this mode existed, a non-Solana TypeScript target (e.g. an
off-chain web3.js tool) fell through to the Solana default and emitted
"Solana BPF program" / Kani / LiteSVM boilerplate, forcing manual scrubbing
of every report. These tests pin:

  * `_detect_language` recognizes typescript + generic, normalizes the
    ecosystem aliases (ts/js/node/...), and still defaults unknowns to solana
  * A typescript render uses TS framing (fast-check, live end-to-end,
    package.json, the TS severity rubric) and emits ZERO Solana-isms
  * A generic render uses neutral framing
  * A solana render is UNCHANGED — it still takes the Solana branch (additive
    arms must not perturb existing languages)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit_pipeline.commands.report import _detect_language, _render_cycle_html


def _make_workspace(tmp_path: Path, language: str) -> tuple[Path, str]:
    ws = tmp_path / f"ws_{language}"
    ws.mkdir()
    (ws / "workspace.json").write_text(
        json.dumps({"language": language}), encoding="utf-8"
    )
    cycle_id = "20260605-pytest"
    narr = ws / "hunts" / cycle_id / "narratives"
    narr.mkdir(parents=True)
    (narr / "F1.md").write_text(
        "# [Medium] Compute-unit accounting is wrong on real transactions\n\n"
        "## Summary\nThe profiler double-counts CU across CPI frames.\n\n"
        "## Affected code\n`services/cuProfiler.ts:42`\n\n"
        "## Description\nThe profiler sums child and parent frames.\n\n"
        "## Impact\nReported compute units are inflated by up to 80%.\n\n"
        "## Reproduction\nRun the profiler on a real mainnet tx.\n\n"
        "## Recommended fix\nUse depth-aware accounting.\n",
        encoding="utf-8",
    )
    return ws, cycle_id


def _render(tmp_path: Path, language: str) -> str:
    ws, cycle_id = _make_workspace(tmp_path, language)
    target = {"id": 1, "name": "ExampleTarget"}
    cycle = {
        "cycle_id": cycle_id, "target_id": 1, "engine_sha": "1536ef5",
        "wrapper_sha": "", "started_at": "2026-06-05T00:00:00+00:00",
    }
    findings = [{
        "id": 1, "target_id": 1, "cycle_id": cycle_id, "hypothesis_id": "F1",
        "title": "Compute-unit accounting is wrong on real transactions",
        "severity": "Medium", "status": "confirmed", "verdict": "TRUE",
        "confidence": "high", "poc_fired": 1,
        "bug_class": "cu-accounting-double-count",
    }]
    return _render_cycle_html(
        target, cycle, findings, "PUBKEY",
        workspace=ws, public=False, draft=False,
    )


# --------------------------------------------------------------------------
# _detect_language
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("typescript", "typescript"),
    ("ts", "typescript"),
    ("tsx", "typescript"),
    ("javascript", "typescript"),
    ("js", "typescript"),
    ("node", "typescript"),
    ("nodejs", "typescript"),
    ("deno", "typescript"),
    ("bun", "typescript"),
    ("TypeScript", "typescript"),   # case-insensitive
    ("generic", "generic"),
    ("other", "generic"),
    ("solana", "solana"),
    ("c", "c"),
    ("aptos", "aptos"),
    ("solidity", "solidity"),
    ("", "solana"),                 # empty → safe default
    ("rust", "solana"),             # unknown → safe default (back-compat)
    ("python", "solana"),
])
def test_detect_language_normalizes(tmp_path: Path, raw: str, expected: str) -> None:
    ws = tmp_path / "w"
    ws.mkdir()
    (ws / "workspace.json").write_text(
        json.dumps({"language": raw}), encoding="utf-8"
    )
    assert _detect_language(ws, "x") == expected


def test_detect_language_via_tests_dir(tmp_path: Path) -> None:
    """Directory-based detection picks up tests/typescript/."""
    ws = tmp_path / "w"
    (ws / "tests" / "typescript").mkdir(parents=True)
    assert _detect_language(ws, "x") == "typescript"


# --------------------------------------------------------------------------
# typescript render
# --------------------------------------------------------------------------

SOLANA_ISMS = [
    "Solana BPF program",
    "cargo test",
    "On-chain BPF reproduction",
    "LiteSVM",
    "Layer 3 — Symbolic verification (Kani)",
    "The underwriting layer for TypeScript DeFi",
    "The underwriting layer for Application DeFi",
]


def test_typescript_render_has_no_solana_isms(tmp_path: Path) -> None:
    html = _render(tmp_path, "typescript")
    for ism in SOLANA_ISMS:
        assert ism not in html, f"TS render leaked Solana-ism: {ism!r}"


def test_typescript_render_uses_ts_framing(tmp_path: Path) -> None:
    html = _render(tmp_path, "typescript")
    for token in [
        "TypeScript",
        "fast-check",
        "Live end-to-end reproduction",
        "package.json",
        "Property-based testing (fast-check)",
        "TypeScript / Node application",
        "Autonomous security audits for TypeScript & web applications",
        "Remote code execution",  # TS severity rubric Critical wording
    ]:
        assert token in html, f"TS render missing expected framing: {token!r}"


# --------------------------------------------------------------------------
# generic render
# --------------------------------------------------------------------------

def test_generic_render_uses_neutral_framing(tmp_path: Path) -> None:
    html = _render(tmp_path, "generic")
    assert "Application source code" in html
    assert "Live end-to-end reproduction" in html
    assert "Solana BPF program" not in html
    assert "cargo test" not in html
    # generic must not assume an npm/Node toolchain in a signed report
    assert "package.json" not in html
    assert "Third-party dependencies beyond their declared interfaces" in html


# --------------------------------------------------------------------------
# solana regression — additive arms must not perturb the Solana branch
# --------------------------------------------------------------------------

def test_solana_render_unchanged_branch(tmp_path: Path) -> None:
    html = _render(tmp_path, "solana")
    for token in [
        "Solana BPF program",
        "Layer 3 — Symbolic verification (Kani)",
        "On-chain BPF reproduction",
        "The underwriting layer for Solana DeFi",
    ]:
        assert token in html, f"Solana render lost expected text: {token!r}"
    for ts_only in [
        "TypeScript / Node application",
        "package.json",
        "Live end-to-end reproduction",
        "Autonomous security audits for TypeScript",
    ]:
        assert ts_only not in html, f"Solana render leaked TS-only text: {ts_only!r}"
