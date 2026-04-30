# Sample Jelleo agent outputs

These are unedited responses from autonomous `hunt-deep` agent runs against
Anatoly Yakovenko's Percolator engine (commit `a946e5508f`).

Each agent had access to three tools — `read_file`, `grep`, `find_function` —
and was given one hypothesis to investigate. The agent iteratively explored
the codebase and produced a line-cited verdict with `HIGH` / `MED` / `LOW`
confidence.

| File | Verdict | What it shows |
|---|---|---|
| [V4-vault-cap-respect_response.md](V4-vault-cap-respect_response.md) | **TRUE / HIGH** | Safety attestation. Agent inventoried all 6 vault-mutation sites (lines 5041, 5080, 7041, 7117, 7143, 7385, 6492, 6928), identified the guard mechanism for each, quoted exact code at deposit / top-up / fee-credit paths, and confirmed the `MAX_VAULT_TVL` cap is enforced everywhere with a backstop at line 4075. |
| [V1-vault-residual-conservation_response.md](V1-vault-residual-conservation_response.md) | **FALSE / HIGH** | Demonstrates the platform correctly identifying *intentional design* vs *bug*. Agent traced 4 sites where `insurance_fund.balance` decreases (lines 3082, 7078, 7116, 7142), built a delta table for each, identified the spec-documented design intent at lines 3087–3101 (the F7 family), and concluded the hypothesis as stated is FALSE — but FALSE *by design*, not a vulnerability. The actual enforced invariant is `V >= C_tot + I` (cited to lines 9, 4000, 4075). |

These are typical outputs — Jelleo produced ~5 of these per cycle in
the last 101-hypothesis run. Cost per response: ~$1–2 in API spend at
production depth.
