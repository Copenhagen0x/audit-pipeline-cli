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
