# Propagation operator runbook

> J37: operator-facing checklist. When P2 surfaces something, this is
> the playbook for what to do with it.

## Setup (one-time per workspace)

```bash
# 1. Initialize the corpus directory with starter protocols
audit-pipeline propagate init-corpus \
    --corpus /root/audit_runs/<workspace>/recon/propagate/corpus \
    --list-file /path/to/perp-corpus.json   # or use default 15

# 2. Verify clones succeeded + submodules initialized
ls /root/audit_runs/<workspace>/recon/propagate/corpus/

# 3. (Y1 work) wire the daily corpus refresh cron
sudo bash deploy/install_systemd.sh  # picks up jelleo-corpus-refresh.timer
```

## When a finding confirms (auto-fired hooks)

The lifecycle hook fires both sibling derivation AND cross-protocol
propagation in daemon threads. You don't run anything manually — but
you DO need to check what fired:

```bash
audit-pipeline propagate status <finding-id>
# Reports: hypothesis_id, bug_class, status, sibling files, propagation
# reports, idempotency marker, queued Layer-1 hunts.
```

If status reports the propagation marker as `not fired`, manually fire it:

```bash
audit-pipeline propagate auto-fire \
    --finding-id <id> \
    --corpus /root/audit_runs/<workspace>/recon/propagate/corpus
```

## When a propagation report has top hits

The report is at `<workspace>/recon/propagate/auto-fire/propagation_finding_<id>_<bug_class>.md`.

For each top hit:

1. **Read the snippet** — the report includes 5 lines of context around
   the matched signature. Verify the match looks structurally similar
   to the parent finding's pattern.

2. **Decide if it's worth a hunt** — high score (3+) on a perp DEX peer
   with clear visual similarity = yes. Score 1 in unrelated AMM = probably
   noise.

3. **Dispatch (operator-gated):**
   ```bash
   audit-pipeline propagate dispatch-pending --limit 3
   ```
   Reads the queue, marks items `dispatched`, in dry-run mode: prints
   what WOULD be dispatched.

4. **Track the dispatched hunt** via the regular `audit-pipeline hunt`
   cycle for the candidate protocol.

## Adding a new bug class

When a new bug class confirms (i.e. one not in `BUG_CLASS_SIGNATURES`):

1. Pick a name following the kebab-case convention from
   [`bug-class-catalog.md`](bug-class-catalog.md).
2. Update relevant YAML hyps to set `bug_class: <new-name>`.
3. Add an entry to `BUG_CLASS_SIGNATURES` in `propagate.py` with 2-4
   regex signatures.
4. Document in `bug-class-catalog.md`.
5. (Optional) Add a fixture-based signature unit test (item C11).
6. Re-run `audit-pipeline propagate auto-fire` for any
   already-confirmed findings of this class to populate reports.

## Adding a new corpus protocol

When a new customer signs up (or a new bug class implies coverage of a
new protocol):

```bash
audit-pipeline propagate add-target <name> <github-url> \
    --corpus /root/audit_runs/<workspace>/recon/propagate/corpus \
    [--ref <commit-sha>]
```

The clone runs with `--depth 1` and submodule init. Single-repo addition;
doesn't disturb existing corpus members.

## Cost management

- **Sibling derivation** is the main API spend per propagation event:
  ~$0.30 per derivation, hard-capped at $5/day per workspace
  (configurable via `derive_siblings_async(..., daily_budget_usd=...)`).
  Budget ledger at `<workspace>/derived/budget/<YYYYMMDD>.usd`.
- **Corpus sweep** is regex-only, $0 LLM spend.
- **Layer-1 dispatch** on top hits is the BIG cost — a full hunt
  cycle against a new candidate protocol with the parent's class
  filter is $3-8. Operator-gated by design (`dispatch-pending` is
  manual). Do not dispatch into protocols you don't intend to cover.

## Debugging a hook that didn't fire

Hook execution logs land at `<workspace>/hooks/<finding_id>-<hook>-<ts>.log`
(JSON-line per phase: started, completed_or_failed). If a hook didn't
appear to run:

```bash
ls /root/audit_runs/<workspace>/hooks/ | grep <finding-id>
cat /root/audit_runs/<workspace>/hooks/<finding-id>-*-*.log
```

Common failures:

| Log shows | Cause | Fix |
|---|---|---|
| `outcome: error · ANTHROPIC_API_KEY not set` | derive_siblings can't reach LLM | source `/root/.audit-env` before invoking |
| `phase: completed · siblings: 0` | LLM returned empty/malformed YAML | retry; check Claude API status |
| `propagate · reason: no_bug_class` | finding has no bug_class set | run `scripts/backfill_db_bug_class.py` |
| `propagate · reason: no_signatures_registered` | bug_class isn't in `BUG_CLASS_SIGNATURES` | add it (see "Adding a new bug class" above) |
| `propagate · reason: corpus_missing` | corpus directory doesn't exist | run `propagate init-corpus` first |
| (no log file at all) | hook didn't fire (status didn't transition to confirmed) | check `transitions` table for the finding |

## Resetting state

If you need to re-run propagation on a finding that's already fired:

```bash
# Reset propagation marker
rm /root/audit_runs/<workspace>/recon/propagate/markers/<finding-id>.fired

# Reset sibling-derivation marker
rm /root/audit_runs/<workspace>/derived/markers/<finding-id>.derived

# Then re-trigger
audit-pipeline propagate auto-fire --finding-id <id> --corpus <path>
audit-pipeline derive-siblings <id>
```

---

**Spec:** [`§04 — Cross-protocol propagation`](04-propagation.md)
**CLI help:** `audit-pipeline propagate --help`
