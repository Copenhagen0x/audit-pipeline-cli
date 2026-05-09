"""Bundle directory layout helpers.

All bundle artifacts live under `<workspace>/recon/bundles/<finding_id>/`.
This module is the only place that knows the layout — every other bundle
component goes through these helpers.
"""

from __future__ import annotations

from pathlib import Path


def bundle_root(workspace: Path) -> Path:
    """Top-level bundles directory under the workspace."""
    return workspace / "recon" / "bundles"


def bundle_dir(workspace: Path, finding_id: int) -> Path:
    """Per-finding bundle directory."""
    return bundle_root(workspace) / str(finding_id)


def meta_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "meta.json"


def patch_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "patch.diff"


def writeup_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "writeup.md"


def balance_proof_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "balance_proof.md"


def verification_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "verification.json"


def authorization_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "authorization.json"


def signature_path(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "bundle.sig"


def poc_dir(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "poc"


def hooks_dir(workspace: Path, finding_id: int) -> Path:
    return bundle_dir(workspace, finding_id) / "hooks"


# Status values that may appear in meta.json.status.
# Stored as a literal tuple so external code can validate without importing
# enum machinery.
STATUSES = ("drafted", "verified", "authorized", "pr-opened", "merged", "fixed", "rejected")
