#!/usr/bin/env python3
"""One-shot DB migration: backfill `findings.bug_class` from YAML library.

Joins `findings.hypothesis_id` against the bundled hypothesis YAMLs to
copy each finding's bug_class into the findings table. Required because:

  1. The findings table had bug_class added retroactively (see _MIGRATIONS
     in db.py); the column exists but old rows are NULL.
  2. The hunt path historically didn't propagate bug_class from YAML into
     the findings row at insert time. It does now (TBD in this commit
     wave) but legacy rows still need the backfill.
  3. P2 propagation can only fire when bug_class is set; without backfill,
     no historical confirmed finding (including F7) can trigger a sweep.

Run on the VPS:

    python3 scripts/backfill_db_bug_class.py [--workspace /root/audit_runs/percolator-live]

Idempotent. Skips rows where bug_class is already set. Prints a summary.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml


def build_yaml_map(yaml_dir: Path) -> dict[str, str]:
    """Walk every YAML and return {hypothesis_id: bug_class}."""
    out: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    for path in sorted(yaml_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"warn: could not parse {path.name}: {e}", file=sys.stderr)
            continue
        for h in (raw or {}).get("hypotheses", []):
            hid = h.get("id")
            bc = h.get("bug_class")
            if not hid or not bc:
                continue
            if hid in out and out[hid] != bc:
                collisions.append((hid, out[hid], bc))
            out[hid] = bc
    if collisions:
        print(f"warn: {len(collisions)} hypothesis IDs have conflicting "
              f"bug_class across files; using last-seen value")
        for hid, old, new in collisions[:10]:
            print(f"  {hid}: {old!r} -> {new!r}")
    return out


def backfill(db_path: Path, mapping: dict[str, str], dry_run: bool = False) -> dict[str, int]:
    """UPDATE findings SET bug_class = ? WHERE hypothesis_id = ? AND bug_class IS NULL."""
    if not db_path.is_file():
        print(f"error: db not found at {db_path}", file=sys.stderr)
        return {"updated": 0, "skipped": 0, "unmapped": 0, "total": 0}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, hypothesis_id, bug_class FROM findings"
        ).fetchall()

        updated = 0
        skipped = 0
        unmapped_ids: set[str] = set()
        for r in rows:
            hid = r["hypothesis_id"]
            existing = r["bug_class"]
            if existing:
                skipped += 1
                continue
            bc = mapping.get(hid)
            if not bc:
                unmapped_ids.add(hid)
                continue
            if not dry_run:
                conn.execute(
                    "UPDATE findings SET bug_class = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (bc, r["id"]),
                )
            updated += 1

        if not dry_run:
            conn.commit()

        return {
            "total":    len(rows),
            "updated":  updated,
            "skipped":  skipped,
            "unmapped": len(unmapped_ids),
            "unmapped_ids_sample": sorted(unmapped_ids)[:20],
        }
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=Path("/root/audit_runs/percolator-live"))
    p.add_argument("--dry-run", action="store_true", help="report only; don't UPDATE")
    args = p.parse_args()

    repo = Path(__file__).resolve().parents[1]
    yaml_dir = repo / "src" / "audit_pipeline" / "templates" / "hypotheses"
    db_path = args.workspace / "findings.db"

    print(f"workspace: {args.workspace}")
    print(f"db:        {db_path}")
    print(f"yamls:     {yaml_dir}")
    print(f"dry_run:   {args.dry_run}")

    mapping = build_yaml_map(yaml_dir)
    print(f"\nLoaded {len(mapping)} (hyp_id -> bug_class) entries from YAMLs.")

    result = backfill(db_path, mapping, dry_run=args.dry_run)
    print("\n--- Result ---")
    print(f"  total findings: {result['total']}")
    print(f"  updated:        {result['updated']}")
    print(f"  skipped (already had bug_class): {result['skipped']}")
    print(f"  unmapped (hyp_id not in YAML map): {result['unmapped']}")
    if result.get("unmapped_ids_sample"):
        print("  sample unmapped hyp_ids:")
        for hid in result["unmapped_ids_sample"]:
            print(f"    {hid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
