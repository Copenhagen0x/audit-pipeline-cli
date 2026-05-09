"""Pillar 3 — closed-loop fix bundle package.

A bundle is the end-to-end disclosure artifact for a single confirmed
finding. It contains everything needed for a maintainer to apply, verify,
and merge the fix:

  meta.json          — finding_id, engine_sha, bug_class, status, patch_sha
  patch.diff         — unified diff of the proposed fix
  poc/               — PoC test files (copied from the confirm cycle)
  writeup.md         — root-cause writeup (markdown)
  balance_proof.md   — worked numerical proof (when applicable)
  verification.json  — last machine-verification result (per-gate PASS/FAIL)
  authorization.json — operator authorization marker (only after review)
  bundle.sig         — Ed25519 signature of the bundle digest
  hooks/             — per-bundle hook execution log

Status lifecycle:

  drafted → verified → authorized → pr-opened → merged → fixed
                                  ↑
                           (RULE: only Kirill authorizes;
                            engine NEVER auto-fires open-pr)

See `jelleo_p3_pr_authorization_policy.md` in operator memory for the
five-gate enforcement chain.
"""

from audit_pipeline.bundle.paths import (
    bundle_dir,
    bundle_root,
    meta_path,
)

__all__ = ["bundle_dir", "bundle_root", "meta_path"]
