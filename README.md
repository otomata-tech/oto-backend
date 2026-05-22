# oto-mcp

MCP server exposing selected `oto-cli` connectors to Claude over Streamable HTTP.

First batch of tools wraps the data.gouv.fr "API Recherche Entreprises"
(no upstream API key needed):

| Tool | What it returns |
| --- | --- |
| `recherche_entreprises_search` | Filtered list of French companies (full-text + NAF / dept / postal / commune / employees / CA + **IDCC convention collective** filters) |
| `recherche_entreprises_get` | Single enriched company by SIREN |
| `recherche_entreprises_directors` | `dirigeants` for a SIREN |
| `recherche_entreprises_finances` | `finances` block for a SIREN |

## Architecture

```
oto_mcp/
├── server.py        # FastMCP entrypoint (stdio + streamable_http)
├── tools.py         # @mcp.tool wrappers around oto.tools.* clients
└── config.py        # require_env helper
```

Each new oto connector exposed = one extra block in `tools.py` that imports
the relevant `oto.tools.<service>` client.

## Local dev (stdio)

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/oto-mcp     # MCP_TRANSPORT defaults to stdio
```

Hook it into Claude Code via `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "oto": {
      "command": "/data/oto/mcp/.venv/bin/oto-mcp"
    }
  }
}
```

## Remote (HTTP, OAuth) — see `deploy/DEPLOY.md`

Auth via Logto (`auth.oto.zone`) : le backend valide les access tokens en JWT
contre les JWKS Logto, audience = `https://mcp.oto.ninja/mcp`. Tout user avec
un compte Logto sur le tenant peut se connecter ; révocation par compte côté
admin Logto.

Public URL : `https://mcp.oto.ninja/mcp`

## Adding a new tool

1. Make sure the underlying oto connector exists in `oto.tools.<service>`.
2. In `oto_mcp/tools.py`, add a new `@mcp.tool()` async function with a
   precise docstring (the LLM picks tools from docstrings).
3. Restart the server.

Example skeleton:

```python
from oto.tools.<service> import <Client>
client = <Client>()

@mcp.tool()
async def <verb>_<noun>(arg: str) -> dict:
    """One-sentence purpose. List relevant args."""
    return client.<method>(arg)
```
