# Security policy

Thanks for taking the time to help keep Jelleo safe.

## Reporting a vulnerability

**Please do NOT open a public GitHub Issue for security reports.**

Email **security@jelleo.com** with:

- A short description of the issue (one or two sentences is fine)
- The affected file / function / endpoint
- Steps to reproduce (or a proof-of-concept if you have one)
- Optional: your suggested fix, and whether you'd like attribution

You should hear back within **72 hours**. If the issue is confirmed, we will:

1. Acknowledge the report and assign a tracking ID.
2. Coordinate a disclosure timeline with you (typically 30–90 days depending on severity and complexity of the fix).
3. Credit you in the release notes when the fix ships — unless you prefer to stay anonymous.

## Scope

In scope for this project:

- `audit-pipeline-cli` engine code (Layer 1-6 logic, bundle authoring, attestation, signing)
- Customer-facing notifier / email surface
- Deploy scripts and configuration
- Cryptographic operations (Ed25519 signing, Merkle roots, snapshot verification)
- Public website code at `jelleo.com`

Out of scope:

- Theoretical issues without a reproducer
- Findings in third-party dependencies (please report those upstream)
- Issues that require physical access to the operator's machine
- Social engineering of the operator

## What you can expect

- A real human reading the report (no autoresponder).
- No legal threats for good-faith research.
- A clear answer on whether the issue is in scope and, if so, what the fix timeline looks like.
- A credit in the changelog when the fix lands, with a link to your handle if you want one.

## What we ask

- Do not exploit the vulnerability beyond what's necessary to demonstrate it.
- Do not access, modify, or exfiltrate customer data.
- Do not run automated scanners against the production infrastructure — read the public code first, and only test against your own deployment if you need to confirm a finding.
- Give us reasonable time to fix the issue before public disclosure.

Thank you.
