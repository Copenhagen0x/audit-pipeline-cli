"""audit-pipeline — CLI orchestrator for the Jelleo Solana audit loop.

For methodology details see ``docs/methodology/`` in this repo.
For a worked example see https://github.com/Copenhagen0x/percolator-audit-2026-04.
"""

__version__ = "0.1.0"

# Auto-install the Anthropic SDK spend tracker on every CLI invocation.
# Captures real billable token usage from every Messages API response
# and appends one line per call to /root/.audit-spend-log.jsonl (0600).
# NO prompt or response content is logged — only token counts + cost.
try:
    from audit_pipeline.spend_tracker import install as _install_spend_tracker
    _install_spend_tracker()
except Exception:
    pass
