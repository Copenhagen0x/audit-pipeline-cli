# §06 · Finding lifecycle state machine

Every finding has a status. Transitions are restricted: you cannot jump from `new` straight to `fixed`. The chain must be walked.

This keeps the audit trail honest — a finding that ended up `verified` has a complete chain of transitions, each with a timestamp, actor, and reason. Bookkeeping mistakes are forced into visibility.

---

## States

```
   ┌──────┐    ┌──────────┐    ┌────────────┐    ┌─────────────┐    ┌────────┐    ┌──────────┐
   │ new  │───▶│ triaged  │───▶│ confirmed  │───▶│ disclosed   │───▶│ fixed  │───▶│ verified │
   └──────┘    └──────────┘    └────────────┘    └─────────────┘    └────────┘    └──────────┘
      │             │                 │                  │                │
      ▼             ▼                 ▼                  ▼                ▼
                              [rejected at any state]
```

| State        | Definition                                                   | Typical actor          |
|--------------|--------------------------------------------------------------|------------------------|
| `new`        | Fresh from a hunt cycle, not yet reviewed                    | system (initial insert) |
| `triaged`    | Human (or automation) confirmed it's a real candidate        | human reviewer         |
| `confirmed`  | Empirical proof exists (PoC fired)                           | system (PoC fire) or human |
| `disclosed`  | Reported to the maintainer (issue filed / email sent)        | system or human        |
| `fixed`      | Maintainer shipped a patch                                   | system (re-run cycle)  |
| `verified`   | Patch confirmed effective via a re-run cycle                 | system                 |
| `rejected`   | Refuted (debate flipped it, PoC didn't fire, or human-rejected) | any                    |

Terminal states: `verified` and `rejected`. No transitions out of either.

---

## Valid transitions

Encoded as `VALID_TRANSITIONS` in [`audit_pipeline/lifecycle.py`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/lifecycle.py):

```
new        → {triaged, confirmed, rejected}
triaged    → {confirmed, rejected}
confirmed  → {disclosed, rejected}
disclosed  → {fixed, rejected}
fixed      → {verified, rejected}
verified   → ∅
rejected   → ∅
```

Any other transition raises `InvalidTransition`. The DB enforces this at the row-update layer — `transition_finding` validates before writing.

---

## Audit trail

Every transition appends to a `transitions` table:

```
id  | finding_id | from_status | to_status | reason                            | actor          | ts
────┼────────────┼─────────────┼───────────┼───────────────────────────────────┼────────────────┼─────
1   | 378        | new         | triaged   | human-review during triage UI     | triage-ui      | 2026-04-22T14:08:11Z
2   | 378        | triaged     | confirmed | PoC fired (cargo test failed)     | system         | 2026-04-22T14:32:47Z
3   | 378        | confirmed   | disclosed | PR #39 filed                      | system         | 2026-04-30T19:14:02Z
```

The table is **append-only**. There is no UPDATE or DELETE on transitions. A finding that's been wrongly transitioned must be transitioned to `rejected` (with a reason) and a new finding row created — the original audit trail is preserved.

---

## Auto-advance

Most findings auto-advance through the early states without human intervention:

- `new` → `confirmed` happens automatically when Layer-2 PoC fires (cargo test failed → invariant violated)
- `new` → `rejected` happens automatically when debate flips a TRUE verdict to FALSE
- `confirmed` → `disclosed` happens automatically when `audit-pipeline issue auto-file-confirmed` files a GitHub issue or sends an email

Manual transitions (typically `triaged`, `fixed`, `verified`) come from human action or from re-run cycles detecting a maintainer's patch.

---

## Hooks on transitions

Two hooks fire automatically on the `confirmed` transition:

1. **Sibling derivation** — auto-emits N hypotheses targeting the same root cause across adjacent code paths (§04).
2. **Cross-protocol propagation** — sweeps the indexed corpus for the same bug class (§04).

Both run as fire-and-forget background threads. The lifecycle transition itself never blocks on hook completion.

The hook surface is intentionally narrow — only `confirmed` fires hooks today. Future hook points (`disclosed` → notify customer, `fixed` → trigger verification cycle) follow the same pattern: they execute after the DB commit, in a daemon thread, with silenced exceptions.

---

## Why this state machine

Three properties:

1. **Restricted transitions.** A finding can't be marked `fixed` without first being `disclosed`. A finding can't be `verified` without first being `fixed`. The state machine enforces the audit story.
2. **Append-only audit log.** Every transition is permanently recorded. Disputes about "when was this disclosed?" or "did it ever reach `verified`?" have a primary-source answer.
3. **Hooks on transitions** — the only place auto-derivation and propagation fire. Centralizing the trigger means there's exactly one point where the catalog compounds: the moment a finding crosses into `confirmed`.

---

**Live reference:** [jelleo.com/methodology.html#lifecycle](https://jelleo.com/methodology.html#lifecycle)
**Implementation:** [`audit_pipeline.lifecycle`](https://github.com/Copenhagen0x/audit-pipeline-cli/blob/main/src/audit_pipeline/lifecycle.py)
