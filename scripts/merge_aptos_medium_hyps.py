#!/usr/bin/env python3
"""Merge aptos class + medium-specific hyp libraries into a single combined yaml.

Auto-rewrites the 7 class-library hyps whose target_file references
aptos-small-only filenames so they resolve against any aptos engine.
"""
import sys
from pathlib import Path

import yaml

TEMPLATES = Path("/root/audit-pipeline-cli/src/audit_pipeline/templates/hypotheses")
CLASS_YAML = TEMPLATES / "osec_aptos_class.yaml"
MED_YAML = TEMPLATES / "osec_aptos_medium_specific.yaml"
OUT = TEMPLATES / "osec_aptos_medium.yaml"

SMALL_ONLY_FILES = {
    "sources/access_control.move",
    "sources/staking_pool.move",
    "sources/token_vault.move",
}

class_lib = yaml.safe_load(CLASS_YAML.read_text())
med_lib = yaml.safe_load(MED_YAML.read_text())

rewritten = 0
class_hyps_fixed = []
for h in class_lib["hypotheses"]:
    h2 = dict(h)
    if h2.get("target_file") in SMALL_ONLY_FILES:
        h2["target_file"] = "sources/*.move"
        rewritten += 1
    class_hyps_fixed.append(h2)
print(f"rewrote {rewritten} class-library target_files to glob")

merged = class_hyps_fixed + med_lib["hypotheses"]

header = (
    "# OtterSec evaluation - Aptos MEDIUM target (combined library).\n"
    "# Auto-generated 2026-05-14 from osec_aptos_class.yaml (40) + osec_aptos_medium_specific.yaml (21).\n"
    "# 7 class hyps with aptos-small-only filenames (access_control.move,\n"
    "# staking_pool.move, token_vault.move) auto-rewritten to sources/*.move\n"
    "# so they resolve correctly against the medium engine's actual files\n"
    "# (acl.move, staking.move, vault.move). DO NOT EDIT BY HAND.\n"
    "# To regenerate: python3 merge_aptos_medium.py (script in scripts/).\n"
)

with OUT.open("w") as f:
    f.write(header + "\n")
    yaml.safe_dump({"hypotheses": merged}, f, sort_keys=False, allow_unicode=True, width=120)
print(f"wrote {OUT}: {len(merged)} hyps")

# Validate via loader
sys.path.insert(0, "/root/audit-pipeline-cli/src")
from audit_pipeline.scoping import load_hypotheses

hl = load_hypotheses(OUT)
print(f"loader sees: {len(hl)} hyps (all valid)")

# Cross-check resolvable target_files
src_dir = Path("/root/ottersec-eval/repos/aptos-medium/sources")
files_on_disk = {p.name for p in src_dir.glob("*.move")}
missing = set()
for h in hl:
    tf = h.get("target_file", "")
    if "*" in tf or "?" in tf:
        continue
    name = tf.replace("sources/", "")
    if name not in files_on_disk:
        missing.add(name)
if missing:
    print(f"UNRESOLVABLE target_files: {sorted(missing)}")
else:
    print("all non-glob target_files resolve against medium engine - clean")
