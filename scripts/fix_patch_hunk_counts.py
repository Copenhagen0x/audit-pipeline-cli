#!/usr/bin/env python3
"""Repair unified-diff hunk headers where the LLM lied about line counts.

Aptos-large 2026-05-15: bundle LLM produces patches with hunk headers
like `@@ -126,7 +126,7 @@` but the actual hunk body only has 4 old
and 4 new lines. `git apply` rejects these as "corrupt patch at line N".

This script counts the real `-`/` ` lines and `+`/` ` lines in each
hunk and rewrites the header to match.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


HUNK_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@")


def fix_patch(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = HUNK_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        old_start = int(m.group(1))
        new_start = int(m.group(3))
        header_end_idx = i
        # Scan forward to find the end of this hunk (next @@ or end)
        body_start = i + 1
        body_end = body_start
        while body_end < len(lines):
            if lines[body_end].startswith("@@") or lines[body_end].startswith("---"):
                break
            body_end += 1
        # Count old (` ` + `-`) and new (` ` + `+`) lines
        old_count = 0
        new_count = 0
        for line in lines[body_start:body_end]:
            if line.startswith("-") and not line.startswith("---"):
                old_count += 1
            elif line.startswith("+") and not line.startswith("+++"):
                new_count += 1
            elif line.startswith(" ") or line == "\n":
                old_count += 1
                new_count += 1
            elif line.startswith("\\"):
                # "\ No newline at end of file" — ignore
                pass
            # Other lines (commentary) ignored
        # Rewrite header
        new_header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"
        out.append(new_header)
        out.extend(lines[body_start:body_end])
        i = body_end
    return "".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fix_patch_hunk_counts.py <patch_file> [<patch_file>...]", file=sys.stderr)
        return 2
    for path_str in sys.argv[1:]:
        p = Path(path_str)
        if not p.is_file():
            print(f"skip: {p} not a file", file=sys.stderr)
            continue
        original = p.read_text(encoding="utf-8")
        fixed = fix_patch(original)
        if fixed != original:
            p.write_text(fixed, encoding="utf-8")
            print(f"fixed: {p}")
        else:
            print(f"unchanged: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
