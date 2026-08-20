# MVP-04 guided authentication bridge

`agy-gen` owns one persistent tmux session inside the GenOS execution boundary. Authentication and normal Agent execution are separate windows in that same session:

- `agy-gen:auth` — interactive provider authentication;
- `agy-gen:runtime` — supervised Core Agent worker.

## Owner flow

1. GenOS provisions the pinned Gemini CLI and tmux toolchain.
2. `AUTH_START` launches Gemini CLI with `NO_BROWSER=true` in `agy-gen:auth`.
3. GenOS captures the pane internally and projects only typed state plus the Google authentication URL.
4. The Owner opens/copies that URL outside the VM/execution boundary.
5. When Gemini asks for an authorization code, the Owner sends the code through `AUTH_SUBMIT`.
6. GenOS streams the code to tmux via stdin-backed buffer handling. The code is not stored in Product DB/Agent JSON, echoed in API responses, logged by GenOS, or placed in process argv.
7. `AUTH_VERIFY` performs a direct real-model marker request against the requested target model.
8. Only after provider/model verification PASS may `agy-gen:runtime` become READY and accept work.

## Typed surfaces

CLI:

- `genos agent auth start [--restart]`
- `genos agent auth status`
- `genos agent auth submit` — reads one code from stdin
- `genos agent auth verify`

Product API, authenticated Owner session required:

- `GET /api/v1/agents/agy-gen/auth`
- `POST /api/v1/agents/agy-gen/auth/start`
- `POST /api/v1/agents/agy-gen/auth/code`
- `POST /api/v1/agents/agy-gen/auth/verify`

There is no arbitrary shell endpoint in MVP-04.

## Persistence and recovery

Closing or refreshing the future browser UI does not own or terminate the tmux session. Runtime restart replaces only the `runtime` window and preserves an in-progress `auth` window. Missing or failed external authentication remains `NEEDS_ACTION`; it is resumable and is never converted to fabricated READY state.

Production Mission Control UI for this flow remains governed by the MVP-08 mockup/visual-approval gate.
