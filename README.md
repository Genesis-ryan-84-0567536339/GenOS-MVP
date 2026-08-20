# GenOS MVP

GenOS MVP is a public self-hosted vertical slice focused on one installable system boundary, truthful infrastructure visibility, one persistent Core Agent, and controlled collaboration through Google Drive.

## MVP goal

`one-command install → infrastructure truth → agy-gen → Google Drive Reports/Kanban bridge → doctor/repair/update/backup/uninstall/purge`

## Scope

The MVP is intentionally small. It will provide:

- lifecycle CLI: install, status, doctor/recon, repair, update, reconfigure, backup, restore, support-bundle, uninstall, purge;
- adaptive install boundary: native install for dedicated server/VPS, isolated VM for shared workstation when appropriate;
- infrastructure dashboard backed by typed collectors for CPU, RAM, GPU, storage, network, gateway, containers, services, daemons/processes, cron/timers, runtime, MCP and database state;
- one persistent Core Agent `agy-gen` with durable identity, supervised tmux runtime, model/effort controls, memory, skills and evidence-gated usage telemetry;
- credentials/connections UI using secret references rather than exposing raw secrets;
- Google Drive guided setup, system reports, history, progress-visible jobs and controlled two-way Kanban collaboration;
- local PostgreSQL + artifact authority. Google Drive is a collaboration replica, not the operational source of truth.

## Truth and safety rules

- No demo/fabricated current state. Unverified values remain `UNKNOWN`.
- Raw secrets must not be stored in Git, Google Drive reports, logs, cards or support bundles.
- External requests never gain arbitrary shell/root authority through Drive sync.
- Agent identity is durable; tmux/CLI processes are replaceable runtime bindings.
- UI implementation is mockup-first and requires visual approval before material screen changes.

## Status

Repository bootstrap is active. Production MVP runtime is not released yet.
