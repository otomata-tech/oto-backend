"""Smoke E2E de la capacité org.use_org (ADR 0009 barreau 1) sur PG jetable.

Exerce le chemin réel autz (SUB_ONLY) → handler (resolve_org_for_user +
set_active_org) contre une vraie DB. Vérifie : bascule org membre OK, par nom
OK, org non-membre → AuthzDenied(404). + parité des surfaces (tool MCP plat,
route REST montée).

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5468/poc \
          OTO_CONFIG_DISABLE_SOPS=1 .venv/bin/python -m scripts.smoke_capability_use_org
"""
from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from oto_mcp import db, org_store
from oto_mcp.capabilities import _mcp_adapter, _rest_adapter, registry
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import AuthzDenied, RawCtx
from oto_mcp.capabilities.orgs import UseOrgInput, _use_org


def main() -> None:
    db.init_db()
    with db._connect() as c:
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO users(sub) VALUES ('u1') ON CONFLICT DO NOTHING")
        a = c.execute("INSERT INTO orgs(name) VALUES ('Alpha') RETURNING id").fetchone()["id"]
        b = c.execute("INSERT INTO orgs(name) VALUES ('Beta') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO org_members(org_id, sub, org_role) VALUES (%s,'u1','org_member')", (a,))
        # u1 n'est PAS membre de Beta (b)

    ctx = SUB_ONLY(RawCtx(sub="u1"))

    print("→ bascule par id vers org membre (Alpha)")
    res = _use_org(ctx, UseOrgInput(org=str(a)))
    assert res["active_org"] == a and res["name"] == "Alpha", res
    assert org_store.get_active_org("u1") == a
    print(f"  ✓ active_org={a} ({res['name']})")

    print("→ bascule par nom (Alpha)")
    assert _use_org(ctx, UseOrgInput(org="Alpha"))["active_org"] == a
    print("  ✓")

    print("→ org NON-membre (Beta) → refus")
    try:
        _use_org(ctx, UseOrgInput(org=str(b)))
        raise SystemExit("  ✗ aurait dû lever AuthzDenied")
    except AuthzDenied as e:
        assert e.status == 404, e.status
        print(f"  ✓ AuthzDenied({e.status}, {e.code!r})")
    assert org_store.get_active_org("u1") == a, "l'org active ne doit pas avoir changé"

    print("→ parité des surfaces")
    m = FastMCP("t")
    _mcp_adapter.register(m, registry.CAPABILITIES)

    async def _check():
        tool = await m.get_tool("oto_use_org")
        assert tool is not None
        params = getattr(tool, "parameters", {})
        assert set(params.get("properties", {})) == {"org"}, params
    asyncio.run(_check())
    routes = _rest_adapter.make_routes(None, None, None, None, None, registry.CAPABILITIES)
    assert any(r.path == "/api/me/active-org" and "PUT" in r.methods for r in routes)
    print("  ✓ tool MCP oto_use_org (schéma plat {org}) + route PUT /api/me/active-org")

    print("\n✓ Capacité org.use_org validée (barreau 1).")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
