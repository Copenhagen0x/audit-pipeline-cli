"""Schema + loader tests for the bundled hypothesis class libraries.

Tier 2 #7 deliverable. Every yaml file in templates/hypotheses/ must:
  - parse as YAML
  - have a top-level `hypotheses:` list
  - pass scoping.load_hypotheses() validation (id regex, required fields)
  - have unique IDs within the file
  - have unique IDs across files in the same protocol-class cluster
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from audit_pipeline.scoping import (
    PROTOCOL_CLASSES,
    HypothesisValidationError,
    hypotheses_dir,
    list_classes,
    load_class_library,
    load_hypotheses,
)

# Every yaml file in the bundled templates dir is a parametrized test case.
ALL_LIBRARY_FILES = sorted(hypotheses_dir().glob("*.yaml"))


@pytest.mark.parametrize("path", ALL_LIBRARY_FILES, ids=lambda p: p.name)
def test_library_file_parses(path: Path) -> None:
    """Every shipped yaml file must be valid YAML with a `hypotheses:` list."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path.name}: top-level must be a mapping"
    hyps = raw.get("hypotheses")
    assert isinstance(hyps, list), f"{path.name}: 'hypotheses:' must be a list"
    assert len(hyps) > 0, f"{path.name}: empty library"


@pytest.mark.parametrize("path", ALL_LIBRARY_FILES, ids=lambda p: p.name)
def test_library_file_passes_loader_validation(path: Path) -> None:
    """Every shipped yaml file must pass load_hypotheses validation."""
    hyps = load_hypotheses(path)
    assert len(hyps) > 0


@pytest.mark.parametrize("path", ALL_LIBRARY_FILES, ids=lambda p: p.name)
def test_library_file_has_unique_ids(path: Path) -> None:
    """No duplicate IDs within a single file."""
    hyps = load_hypotheses(path)
    ids = [h["id"] for h in hyps]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"{path.name}: duplicate ids {duplicates}"


def test_protocol_classes_catalog_complete() -> None:
    """KNOWN_CLASSES catalog matches the actual files on disk."""
    expected_glob_classes = {"perp_dex", "amm_cp", "clmm", "lending", "lst"}
    assert set(PROTOCOL_CLASSES.keys()) == expected_glob_classes


@pytest.mark.parametrize(
    "class_id", sorted(PROTOCOL_CLASSES.keys()), ids=lambda c: c
)
def test_class_library_loads_with_no_collisions(class_id: str) -> None:
    """Every protocol class loads without raising, with at least one file."""
    hyps, files = load_class_library(class_id)
    assert len(hyps) > 0, f"class {class_id!r} loaded 0 hyps"
    assert len(files) > 0, f"class {class_id!r} matched 0 files"


def test_class_library_total_at_least_500() -> None:
    """The bundled library should hold at least 500 distinct hyps."""
    seen: set[str] = set()
    for path in ALL_LIBRARY_FILES:
        for h in load_hypotheses(path):
            seen.add(h["id"])
    assert len(seen) >= 500, f"library has {len(seen)} distinct hyps (target: 500+)"


def test_list_classes_returns_dicts_with_known_keys() -> None:
    """list_classes() output shape stays stable for the website + methodology
    page consumers."""
    out = list_classes()
    assert len(out) == len(PROTOCOL_CLASSES)
    for entry in out:
        assert {"id", "label", "protocols"} <= set(entry.keys())
        assert isinstance(entry["protocols"], list)


def test_unknown_protocol_class_raises() -> None:
    """load_class_library on an unknown class raises with a useful message."""
    with pytest.raises(HypothesisValidationError) as exc:
        load_class_library("nonexistent_protocol_class_xyz")
    assert "unknown protocol class" in str(exc.value).lower()


def test_extra_dirs_picked_up_by_loader(tmp_path: Path) -> None:
    """Workspace-local class libraries are merged into the bundled set."""
    extra = tmp_path / "hypotheses"
    extra.mkdir()
    extra_file = extra / "amm_cp_workspace_extra.yaml"
    extra_file.write_text(
        "hypotheses:\n"
        "  - id: WSEXTRA1-test-only\n"
        "    class: invariant_property\n"
        "    severity: Low\n"
        "    claim: workspace-extra hypothesis used only by this test suite\n"
        "    applies_to: ['*']\n",
        encoding="utf-8",
    )
    bundled, _ = load_class_library("amm_cp")
    merged, _ = load_class_library("amm_cp", extra_dirs=[extra])
    assert len(merged) == len(bundled) + 1
    assert any(h["id"] == "WSEXTRA1-test-only" for h in merged)
