"""L1 — surface-coverage hypothesis generation (the new first layer).

Generates the hypothesis set the rest of the engine (now L2-L7) proves, from systematic
per-repo surface coverage instead of a blind, stale, hand-authored library. See
obsidian-vault/architecture/l1-surface-coverage-plan.md for the full design.

P0 (this package, in progress): the entrypoint AUTHORITY backbone — the independent,
ground-truth list of a program's declared entrypoints used as the coverage denominator
(so "coverage" can never silently read 100% over what we failed to enumerate).
"""
