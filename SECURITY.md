# Security Policy

GenOS MVP is a self-hosted system that can observe and operate infrastructure. Security issues involving authentication, secrets, privilege boundaries, command execution, Google Drive synchronization, report redaction, or destructive lifecycle actions are treated as high priority.

## Reporting a vulnerability

Do not publish credentials, tokens, private keys, recovery material, infrastructure secrets, or exploit details in a public issue.

Use GitHub's private vulnerability reporting/security-advisory channel for this repository when available. If that channel is unavailable, contact the repository owner through a private channel before disclosing technical details publicly.

## Security invariants

- Raw secrets are one-way ingress and must not be returned by normal APIs or rendered back in the UI.
- Git, Google Drive reports/cards, logs and support bundles must not contain raw secrets.
- Google Drive is a controlled collaboration replica, not the operational authority for GenOS state.
- External Drive requests do not grant arbitrary shell/root authority.
- `genos doctor/recon` is read-only. Mutating recovery actions belong to typed `genos repair` operations.
- Destructive purge is explicit, separately confirmed and evidence-producing.
- Unverified runtime facts remain `UNKNOWN`; the product must not invent healthy/live state.
- Core Agent identity is durable and separate from replaceable tmux/CLI runtime bindings.

## Current status

The project is pre-release. No public production-ready version has been declared yet.
