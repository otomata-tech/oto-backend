# oto-mcp

The **central, deployable Oto product** (SaaS or on-premise): an MCP server, over
Streamable HTTP, that exposes the [oto-core](https://github.com/otomata-tech/connectors)
connectors (`oto.tools.*`) as tools to Claude — plus a REST API for the
[dashboard](https://github.com/otomata-tech/oto-dashboard). Imports oto-core directly;
no CLI dependency.

- **Public endpoint**: `https://mcp.oto.cx/mcp` (plug into claude.ai or Claude Code);
  `https://mcp.oto.ninja/mcp` is the pre-production deployment of the same service.
- **Auth**: OAuth via [Logto](https://logto.io) self-hosted (`auth.oto.ninja`), JWT
  verified against the Logto JWKS (ES384); the audience is the endpoint's own URL.
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
- **Orgs, groups & roles** — `member < admin < super_admin`, org/team hierarchy, and a
  single key-resolution walker (`user > cross-org > team > org > tenant > platform`).
- **Per-user tool visibility**, call monitoring, org guides and procedures.

MCP federation (mounting a third-party MCP server into our surface) was **removed on
2026-09-09** — three connectors were declared, none was ever used. A remote service is
now reached through the generic `api` connector (Custom API), or written as a native connector.

## Architecture

```
oto_mcp/
├── server.py          # FastMCP + uvicorn entrypoint, server instructions, route wiring
├── tools/             # one module per connector, each exposing register(mcp)
├── providers/         # the connector registry — one declaration per connector, single source of truth
├── connectors/        # platform-side connector governance: activation, selection, identities, link, verify
├── capabilities/      # capabilities shared across MCP + REST faces (ADR 0009)
├── api/               # REST /api/*: the route table (its order is a contract) + one handler per domain
├── access/            # roles, org context, key-resolution cascade, quotas — flat access.<fn> surface
├── org_store/         # the org tier: orgs, members, vault, settings, library
├── credentials_store.py / crypto.py  # the encrypted vault
├── db/                # PostgreSQL (psycopg pool) — flat db.<fn> surface, one module per domain
├── datastore/         # the typed-record spine behind data_*
├── middleware/        # the MCP middleware chain — registration order is a contract
├── auth/              # who is calling (Logto JWT → sub) and how a credential is acquired
├── fod/               # clients for French public-data services
└── config.py          # require_env, environment domains, dashboard URL

deploy/
├── oto-mcp.service    # systemd unit (port 9103)
├── *.timer/*.service  # maintenance and call-journal archiving units
└── *.sh               # deploy, blue/green, drain and data-refresh scripts
```

Adding a connector is two steps: a declaration in the `providers/` registry, and a
`tools/<service>.py` exposing `register(mcp)` — `register_all` derives loading from the
registry. The client itself lives in oto-core, never here. See [`CLAUDE.md`](CLAUDE.md)
for the full conventions.

## Local dev

The server only runs over Streamable HTTP and is always Logto-authenticated (the stdio
transport was removed — for a local CLI use [`oto-cli`](https://github.com/otomata-tech/oto-cli)).

```bash
python -m venv .venv
.venv/bin/pip install -e .
# set the LOGTO_* and DATABASE_URL env vars, then launch the HTTP server and
# call it with a bearer token. See docs/commands.md.
```

## Deploy

Single trunk, two rings. Pushing to `main` triggers `.github/workflows/deploy-canari.yml`
and deploys **pre-production**; pushing a `vX.Y.Z` tag triggers `.github/workflows/deploy.yml`
and deploys **production**. Both SSH the dedicated box, reset to the ref, reinstall
exactly what `uv.lock` pins (`uv sync --frozen`; a ref without a lock is refused), restart the
`oto-mcp` systemd service, run an HTTP smoke check and roll back on failure. Machine-level
details are kept in a private infrastructure repository, not here.

## Docs

In-depth docs live under [`docs/`](docs/): `conventions.md` and `commands.md` first,
then `architecture.md`, `connector-model.md`, `connector-vault.md`,
`roles-and-resolution.md`, `auth-logto.md`, `rest-api.md`, `datastore.md`,
`groups-and-roles.md`, `guides.md`, `monitoring.md`, `usage-loop.md`.
The security posture of the deployed service is in [`SECURITY.md`](SECURITY.md).

Licensed [MIT](LICENSE). Pull requests require a signed CLA (`.github/workflows/cla.yml`);
the CLA text lives in `otomata-tech/oto`.
