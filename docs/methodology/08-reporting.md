# §08 · Reporting & notifications

The methodology produces three report cadences plus immediate alerts on Critical/High findings. Every report is signed (§07) so customers can verify it independently.

---

## Cadence

| Cadence    | Trigger                              | Audience           | Format                |
|------------|--------------------------------------|--------------------|-----------------------|
| Immediate  | finding moves to `confirmed` AND severity ∈ {Critical, High} | customer on-call   | Slack + email + PagerDuty (configurable) |
| 24h        | nightly cron                         | customer team alias | HTML email + PDF     |
| Weekly     | Monday 09:00 UTC                     | customer team alias | HTML email + PDF     |
| Monthly    | 1st of month, 09:00 UTC              | customer team alias + execs | HTML email + PDF |

All four are signed Ed25519. All four are archived under `<workspace>/reports/` and (for cycle reports) at `api.jelleo.com/cycles/<id>/`.

---

## Immediate channel

Trigger semantics:

```
finding.status_transition.from in {new, triaged}
  AND finding.status_transition.to == confirmed
  AND finding.severity in {Critical, High}
  ───────────────────────────────────────
  → emit immediate notification
```

There is no batching, no daily digest. The customer's primary on-call gets the alert; their team alias gets a CC. The same event fires the dashboard alert pulse and (if configured) the Slack webhook.

Notification body includes:

- Severity pill (Critical / High)
- Hypothesis ID + title
- Bug class
- Target file + line
- One-line attack-chain summary
- Link to the per-finding disclosure package (signed)
- Link to the public cycle report

---

## 24-hour cycle report

Every nightly cycle emits a report covering that cycle's verdicts:

```
─── Cycle 20260507-1340-percolator ─────────────────────────
Engine SHA           : abc1234567
Hypotheses dispatched: 31
Verdicts             : 18 TRUE / 9 FALSE / 4 NEEDS_LAYER_2
Confirmed findings   : 4 (1 Critical · 3 High)
Cycle cost           : $6.12 ($5.88 LLM, $0.24 RPC)
Receipt              : 3a:c1:8e:42:7f:11:b9:dd…
─────────────────────────────────────────────────────────────
```

For each confirmed finding, the report includes the full audit trail (verdict, debate outcome, PoC outcome, severity derivation), the bug class, and the propagation status (sibling-derivation done? cross-protocol hits?).

Mailed to customer + signed + archived.

---

## Weekly digest

Aggregates the past 7 cycles. Adds:

- Per-target severity rollup
- New disclosed/fixed/verified findings during the window
- Hypothesis-library growth (auto-derived siblings + manual additions)
- Cross-protocol propagation hits (+ which auto-fired into new hypotheses)
- Per-customer cumulative receipt count

Mailed Monday 09:00 UTC to customer team alias.

---

## Monthly digest

Aggregates 4-5 weekly windows. Adds:

- Severity-weighted finding density (per protocol per million LoC)
- Cumulative bug-class catalog growth
- Cross-protocol propagation impact (siblings → confirmed in other protocols)
- Engagement-tier metrics (hyps-per-day actual vs target)

Mailed 1st of month 09:00 UTC to customer team alias + executive summary.

---

## Public cycle archive

Every cycle's HTML + PDF + signature lands at:

```
https://api.jelleo.com/cycles/<cycle-id>/
  cycle.html         (signed)
  cycle.html.sig
  cycle.pdf          (signed)
  cycle.pdf.sig
  manifest.json      (per-customer; behind token gate)
```

The cycle archive is **public**. The per-customer manifest at `<base>/customer/<token>/manifest.json` is **token-gated** — only the customer behind the token sees their full finding details (including in-progress confirmed findings not yet disclosed).

The public snapshot at `api.jelleo.com/snapshot.json` includes only `disclosed | fixed | verified` findings (with title + hyp_id, since those are public anyway). In-progress findings stay private.

---

## Customer dashboard

Live at `jelleo.com/customer/<token>/`. Token-gated. Auto-refreshes from the per-customer manifest every 60 seconds. Shows:

- Counter row (Critical/High open + cycles signed + receipt verifiability)
- Findings table (lifecycle states, severity pills, links to disclosures)
- Recent signed cycle receipts (with verifiable fingerprints)
- Cross-protocol propagation panel
- Action bar (download PDFs, copy public key, email account manager)

The dashboard is generated server-side from the same data that drives the email reports — there is no separate "dashboard data path." Customers can verify any cycle they see in the dashboard against the public archive.

---

**Live reference:** [jelleo.com/methodology.html#reporting](https://jelleo.com/methodology.html#reporting)
**Demo customer dashboard:** [jelleo.com/customer/](https://jelleo.com/customer/) (token: `demo`)
