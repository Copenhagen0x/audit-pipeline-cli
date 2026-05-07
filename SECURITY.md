# Security Policy

Jelleo is a Solana-program audit platform. This file describes how to report security issues against (1) the platform itself and (2) findings Jelleo produces against its audit targets.

## Reporting a vulnerability

If you have found a security issue in the Jelleo platform — the audit pipeline, the hypothesis library, the CLI, the dashboard, the website, or the signing/attestation key handling — please report it to:

**security@jelleo.com** (preferred)
or **kirill@jelleo.com** (direct to founder for time-sensitive cases)

A PGP key is published at `keys/jelleo.gpg.pub` in this repo for sensitive reports. We will acknowledge receipt within 48 hours and provide a status update within 5 business days.

For findings Jelleo has produced against a third-party Solana protocol, the protocol maintainer is the disclosure target — Jelleo's role is to coordinate that disclosure. Direct disclosures are routed via the same email above.

## Scope

### In scope

- The audit-pipeline CLI (this repository)
- The Jelleo website source (`website/deploy/`)
- The hypothesis library (`OUTREACH/*.yaml`, `src/audit_pipeline/templates/hypotheses/*.yaml`)
- The findings database schema (`src/audit_pipeline/db.py`)
- The Ed25519 signing implementation (`src/audit_pipeline/commands/sign.py`)
- The shadow-audit logic (`src/audit_pipeline/commands/shadow.py`)
- The dashboard (`src/audit_pipeline/commands/dashboard.py`)
- Any signed Jelleo cycle receipts that are claimed but cryptographically invalid

### Out of scope

- The Solana programs Jelleo audits (report those upstream — Jelleo will help coordinate)
- The Anthropic API, the Solana RPC providers, or other third-party services Jelleo depends on
- Self-XSS, missing security headers absent demonstrable impact, or other low-impact web findings without an exploit chain
- Issues requiring physical access to the maintainer's workstation
- Social engineering attacks against Jelleo personnel

## Disclosure policy — for findings Jelleo produces

Jelleo follows a coordinated-disclosure model with the following defaults. These are the floors; specific engagements may extend any of them by mutual agreement.

### Timeline

| State                | Action                                                                                              | Default duration |
|----------------------|-----------------------------------------------------------------------------------------------------|------------------|
| `confirmed`          | PoC reproduces. Disclosure email sent to the maintainer with a private GitHub issue or alternative. | Day 0            |
| `disclosed`          | Maintainer acknowledged. Embargo begins.                                                            | 30 days          |
| `fixed` (in window)  | Maintainer ships patch within the 30-day window. Public disclosure can proceed coordinated.         | within 30 days   |
| `fixed` (extension)  | Maintainer requests extension — granted by default for active fix work, up to 90 days total.        | 30–90 days       |
| Public release       | Writeup, PoC, fix bundle, attestation made public.                                                   | day 30 or after fix |
| No-acknowledgement   | If no acknowledgement after 14 days of repeated outreach, public disclosure proceeds.               | day 30           |

### Embargo conditions

- Critical findings affecting deployed mainnet protocols receive maximum embargo flexibility — Jelleo will not publish until a fix is shipped or the protocol's funds are no longer at risk.
- High findings against active protocols default to 30 days, extensible to 90.
- Medium / Low / Info findings default to public disclosure on the next regular cadence (weekly or monthly rollup).
- A finding's embargo duration is recorded in the signed cycle receipt (§07 of methodology).

## Responsible-disclosure principles

Jelleo follows these principles, derived from the [CERT Coordination Center](https://www.kb.cert.org/vuls/guidance/) guidelines:

1. **Maintainer first.** Findings are sent to maintainers before any third party.
2. **Embargo is genuine.** Customers, partners, and Jelleo employees do not receive in-embargo finding details until the embargo lifts.
3. **No silent disclosure.** Every finding eventually becomes public — either with a fix in place or with the maintainer's explicit acknowledgement that no fix will ship.
4. **No selling.** Jelleo does not sell findings. It does not sell early access to findings. Customers receive findings against their own protocol; nothing more.
5. **Crediting researchers.** External researchers who report platform vulnerabilities are credited in the disclosure unless they request anonymity.

## Cryptographic attestation

Every Jelleo cycle produces an Ed25519-signed receipt. The current platform public key is published at:

- `keys/jelleo.ed25519.pub` in this repo
- `https://jelleo.com/methodology.html#attestation`
- `https://jelleo.com/keys/jelleo.ed25519.pub` (when deployed)

To verify a Jelleo signature:

```
audit-pipeline sign verify <file> <file>.sig --pubkey jelleo.ed25519.pub
```

If a signature does not verify, **do not trust the file**. A failed verification means either the file has been altered since signing, or the file was not signed by the published Jelleo platform key. Either case warrants a direct report to security@jelleo.com.

## Out-of-scope reports we still want to hear about

Even if a finding falls outside the scope above, please do still send it. We may pay a courtesy bounty at our discretion, and the report will help us improve the platform. Examples:

- Security-relevant typos or omissions in published methodology / dashboards / reports
- Third-party-service misconfigurations that affect Jelleo customers (e.g. a Solana RPC quota exposing customer cycle data via timing)
- Supply-chain concerns about Jelleo dependencies that we are not yet tracking

## Bug bounty

Jelleo does not currently operate a formal bug bounty program. We do extend courtesy compensation for impactful findings on a case-by-case basis. The intent is to formalize a bounty within the Year-1 funded build.

## Acknowledgements

We thank every researcher who has improved Jelleo's security. The honor roll is published at [jelleo.com/security.html#acknowledgements](https://jelleo.com/security.html#acknowledgements).

---

*This policy is published under the same Apache-2.0 license as the rest of the repository. Last updated: 2026-05-07. Maintained by Jelleo (Kirill Sakharuk · kirill@jelleo.com).*
