"""Build a single Anchor program in isolation.

The L4 LiteSVM runner needs the program's compiled BPF (.so) to load
into the in-process Solana VM. Building it via the target repo's own
workspace risks (a) modifying the audited code (test-file pollution
hazard) and (b) blocking on upstream workspace defects (e.g. OSec
eval Solana repos ship a broken ``members = ["programs/*", "tests"]``
declaration without a ``tests/Cargo.toml``).

This module copies the program's source tree into the cycle directory,
synthesises a minimal single-member workspace ``Cargo.toml`` there, and
runs ``cargo build-sbf``. Nothing touches the audited repo at any point.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


def _solana_augmented_path() -> str:
    """Return a PATH that includes the Solana toolchain bin dirs.

    ``cargo build-sbf`` is shipped under the Solana installer's release
    dir and is rarely on the system PATH outside of an interactive
    shell that's sourced the installer's env. Without this augmentation
    the build dies with ``error: no such command: build-sbf`` even
    though the tool is installed.
    """
    base = os.environ.get("PATH", "")
    extras: list[str] = []
    home = os.environ.get("HOME") or "/root"
    candidates = [
        f"{home}/.local/share/solana/install/active_release/bin",
        f"{home}/.cargo/bin",
        "/usr/local/cargo/bin",
    ]
    for c in candidates:
        if c and os.path.isdir(c) and c not in base:
            extras.append(c)
    if extras:
        return ":".join(extras + ([base] if base else []))
    return base


@dataclass
class AnchorBuildResult:
    """Outcome of an isolated Anchor program build."""

    program_name: str
    so_path: Path | None
    build_dir: Path
    build_log_path: Path
    returncode: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.so_path is not None and self.so_path.is_file()


def _isolated_workspace_toml(program_name: str) -> str:
    """Workspace manifest for a single-program sandbox."""
    return f"""\
# Synthesised by audit_pipeline.anchor_builder — single-program sandbox.
# Lives entirely under <cycle>/build/. Target repo is read-only.
[workspace]
members = ["{program_name}"]
resolver = "2"

[profile.release]
overflow-checks = true
lto = "fat"
codegen-units = 1
"""


def _candidate_so_paths(build_dir: Path, program_name: str) -> Iterable[Path]:
    """Where ``cargo build-sbf`` can drop the .so for a member crate."""
    # Standard cargo build-sbf output
    yield build_dir / "target" / "deploy" / f"{program_name}.so"
    # Some toolchain versions stash it under sbf-solana-solana/release/
    yield build_dir / "target" / "sbf-solana-solana" / "release" / f"{program_name}.so"
    # And the older sbpf-solana variant
    yield build_dir / "target" / "sbpf-solana-solana" / "release" / f"{program_name}.so"


def build_anchor_program(
    *,
    cycle_dir: Path,
    target_repo: Path,
    program_name: str,
    timeout_s: int = 900,
) -> AnchorBuildResult:
    """Build ``programs/<program_name>`` from ``target_repo`` in isolation.

    Copies the program crate to ``<cycle>/build/<program_name>/``,
    writes a minimal workspace ``Cargo.toml`` alongside it, and runs
    ``cargo build-sbf``. The build log is persisted next to the build
    dir as ``<program_name>_build.log`` so a later cycle reader can see
    why a build failed without re-running it.
    """
    src_program = target_repo / "programs" / program_name
    if not src_program.is_dir():
        return AnchorBuildResult(
            program_name=program_name,
            so_path=None,
            build_dir=cycle_dir / "build" / program_name,
            build_log_path=cycle_dir / "build" / f"{program_name}_build.log",
            returncode=-1,
            error=f"program source not found at {src_program}",
        )

    # Resolve to absolute so downstream paths (so_path, log_path) are
    # absolute too — the LiteSVM tests run from inside a sidecar dir
    # and would fail on relative paths.
    cycle_dir = cycle_dir.resolve()
    target_repo = target_repo.resolve()
    build_root = cycle_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    dest_program = build_root / program_name
    if dest_program.exists():
        shutil.rmtree(dest_program)
    shutil.copytree(src_program, dest_program)

    workspace_toml = build_root / "Cargo.toml"
    workspace_toml.write_text(
        _isolated_workspace_toml(program_name), encoding="utf-8",
    )

    log_path = build_root / f"{program_name}_build.log"

    env = os.environ.copy()
    env["PATH"] = _solana_augmented_path()
    cargo_bin = shutil.which("cargo", path=env["PATH"]) or "cargo"
    proc = subprocess.run(
        [cargo_bin, "build-sbf", "--", "-p", program_name],
        cwd=str(build_root),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    combined = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    log_path.write_text(combined, encoding="utf-8")

    so_path: Path | None = None
    if proc.returncode == 0:
        for cand in _candidate_so_paths(build_root, program_name):
            if cand.is_file():
                so_path = cand
                break

    return AnchorBuildResult(
        program_name=program_name,
        so_path=so_path,
        build_dir=dest_program,
        build_log_path=log_path,
        returncode=proc.returncode,
        error=None if proc.returncode == 0 and so_path else (
            "build returned 0 but no .so found"
            if proc.returncode == 0 else
            f"cargo build-sbf rc={proc.returncode}"
        ),
    )


def list_anchor_programs(target_repo: Path) -> list[str]:
    """Return the list of program crate names under ``programs/`` in
    ``target_repo``. Each directory under ``programs/`` that contains a
    ``Cargo.toml`` counts as a program crate.
    """
    programs_dir = target_repo / "programs"
    if not programs_dir.is_dir():
        return []
    out: list[str] = []
    for child in sorted(programs_dir.iterdir()):
        if child.is_dir() and (child / "Cargo.toml").is_file():
            out.append(child.name)
    return out


__all__ = [
    "AnchorBuildResult",
    "build_anchor_program",
    "list_anchor_programs",
]
