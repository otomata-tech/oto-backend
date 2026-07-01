# oto-mcp

The **central, deployable Oto product** (SaaS or on-premise): an MCP server, over
Streamable HTTP, that exposes the [oto-core](https://github.com/otomata-tech/oto-core)
connectors (`oto.tools.*`) as tools to Claude — plus a REST API for the
[dashboard](https://github.com/otomata-tech/oto-dashboard). Imports oto-core directly;
no CLI dependency.

- **Public endpoint**: `https://mcp.oto.ninja/mcp` (plug into claude.ai or Claude Code).
- **Auth**: OAuth via [Logto](https://logto.io) self-hosted (`auth.oto.zone`), JWT
  verified against the Logto JWKS (ES384), audience `https://mcp.oto.ninja/mcp`.
- **Self-hostable**: image `Dockerfile`, configured entirely through environment variables.

## What it does

Each user connects once, then Claude can act on their accounts and data through a
catalogue of connectors: French company data (SIRENE/INPI/BODACC, `fr_*`), web search
(Serper), email finding (Hunter), CRM (Attio, Folk), outreach (Lemlist, Kaspr,
Fullenrich), LinkedIn (`unipile_*`), messaging (WhatsApp/Telegram/Instagram via Unipile),
Google Workspace, Slack, accounting (Pennylane), payroll (Silae), a native datastore
(`data_*`), and more. The full surface is driven by the connector registry, not a
hand-maintained list.

Around the connectors, oto-mcp provides the platform plumbing:

- **Credential vault** — encrypted (AES-256-GCM), single `connector_credentials` table;
  per-user keys, per-org/group shared secrets, and platform keys with quotas.
- **Orgs, groups & roles** — `member < admin < super_admin`, org/department hierarchy,
  cascading key resolution (`user_key > group > org > platform_grant`).
- **Per-user tool visibility**, presets, call monitoring, org doctrines/skills, and
  MCP federation (mount / remote bridge).

## Architecture

```
oto_mcp/
├── server.py          # FastMCP + uvicorn entrypoint, server instructions, route wiring
├── tools/             # one module per connector, each exposing register(mcp)
├── connectors.py      # the connector registry — single source of truth
├── capabilities/      # capabilities shared across MCP + REST faces (ADR 0009)
├── api_routes*.py     # REST /api/* (account, settings, orgs, admin, datastore…)
├── access.py          # roles, resolve_api_key, quotas, key-resolution cascade
├── credentials_store.py / crypto.py  # the encrypted vault
├── db.py              # PostgreSQL (psycopg pool) — per-user state
├── auth_hooks.py      # Logto JWT → current user sub for the tool context
└── config.py          # require_env

deploy/
├── oto-mcp.service    # systemd unit (port 9103)
├── Caddyfile.snippet  # mcp.oto.ninja → :9103
└── DEPLOY.md          # DNS + Caddy + systemd procedure
```

Adding a connector is two steps: a `tools/<service>.py` exposing `register(mcp)`, and an
entry in the `connectors.py` registry — `register_all` derives loading from the registry.
See [`CLAUDE.md`](CLAUDE.md) for the full conventions.

## Local dev

The server only runs over Streamable HTTP and is always Logto-authenticated (the stdio
transport was removed — for a local CLI use [`oto-cli`](https://github.com/otomata-tech/oto-cli)).

```bash
python -m venv .venv
.venv/bin/pip install -e .
# set the LOGTO_* and DATABASE_URL env vars, then launch the HTTP server and
# call it with a bearer token. See deploy/DEPLOY.md.
```

## Deploy

Pushing to `main` triggers `.github/workflows/deploy.yml`, which SSHes the dedicated box,
resets to `origin/main`, reinstalls (`pip install -e .`), and restarts the `oto-mcp`
systemd service. Full procedure in [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

## Docs

In-depth docs live under [`docs/`](docs/): `connector-vault.md` (the central
architecture), `roles-and-resolution.md`, `auth-logto.md`, `rest-api.md`,
`datastore.md`, `groups-and-roles.md`, `federation.md`, `doctrines.md`, `monitoring.md`,
`usage-loop.md`.

Open source.
