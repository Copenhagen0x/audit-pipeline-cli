#!/usr/bin/env python3
"""Triage every PoC-fired hyp in the latest cycle.

Dumps a structured report: hyp claim + severity + bug_class +
the fires() function body + the cargo-log panic line. Output:
/tmp/triage_full.md
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CYCLE = Path("/root/audit_runs/percolator-live/hunts/20260511-183154")
HYP_DIR = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")


def slug(h: str) -> str:
    return h.lower().replace("-", "_")


def load_claims() -> dict[str, tuple[str, str, str, str]]:
    try:
        import yaml
    except ImportError:
        return {}
    claims: dict[str, tuple[str, str, str, str]] = {}
    for f in HYP_DIR.glob("*.yaml"):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for _k, v in (d or {}).items():
            if not isinstance(v, list):
                continue
            for h in v:
                if isinstance(h, dict) and h.get("id"):
                    claims[h["id"]] = (
                        (h.get("claim") or "").strip(),
                        h.get("severity", "?"),
                        h.get("bug_class", "?"),
                        h.get("engine_function", "?"),
                    )
    return claims


def fired_hyps(after_ts: str = "2026-05-11T23:33") -> list[str]:
    out: list[str] = []
    for line in (CYCLE / "hunt.log.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (
            d.get("event") == "poc_test_run"
            and d.get("fired") is True
            and d.get("ts", "") >= after_ts
        ):
            out.append(d["hypothesis_id"])
    return sorted(set(out))


PANIC_RE = re.compile(r"panicked at[^\n]+(?:\n[^=].*)?")
FIRE_FN_RE = re.compile(
    r"(?:/\*\*?[^*]*\*+/|//[^\n]*\n)*\s*"
    r"#\[test\]\s*(?:#\[should_panic[^\]]*\])?\s*"
    r"fn\s+\w*_fires\b.*?(?=\n#\[test\]|\n}\n\n|\Z)",
    re.DOTALL,
)


def cargo_panic(log_path: Path) -> str:
    if not log_path.is_file():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = PANIC_RE.search(text)
    if m:
        return m.group(0)[:300].strip()
    for L in reversed(text.splitlines()):
        low = L.lower()
        if "failed" in low or "thread" in low:
            return L.strip()[:200]
    return ""


def fire_body(test_text: str) -> str:
    m = FIRE_FN_RE.search(test_text)
    return (m.group(0) if m else test_text)[:2200]


def main() -> int:
    claims = load_claims()
    fired = fired_hyps()
    out = [f"# Triage of {len(fired)} fires\n"]
    for i, hyp_id in enumerate(fired, 1):
        claim, sev, bclass, efn = claims.get(hyp_id, ("?", "?", "?", "?"))
        test_path = CYCLE / "poc" / f"test_{slug(hyp_id)}.rs"
        log_path = CYCLE / "poc" / f"cargo_{slug(hyp_id)}.log"
        test_text = (
            test_path.read_text(encoding="utf-8", errors="replace")
            if test_path.is_file()
            else "(MISSING)"
        )
        panic = cargo_panic(log_path)
        body = fire_body(test_text)
        out.append(f"## [{i}/{len(fired)}] {hyp_id}")
        out.append(f"**severity:** {sev} | **bug_class:** {bclass} | **engine_function:** {efn}")
        out.append(f"**claim:** {claim[:400]}")
        out.append(f"**panic:** {panic if panic else '(no panic captured)'}")
        out.append("")
        out.append("**fires() body:**")
        out.append("RUST_FENCE_OPEN")
        out.append(body)
        out.append("RUST_FENCE_CLOSE")
        out.append("")
    Path("/tmp/triage_full.md").write_text(
        "\n".join(out)
        .replace("RUST_FENCE_OPEN", "```rust")
        .replace("RUST_FENCE_CLOSE", "```"),
        encoding="utf-8",
    )
    print(f"wrote /tmp/triage_full.md with {len(fired)} fires")
    return 0


if __name__ == "__main__":
    sys.exit(main())
