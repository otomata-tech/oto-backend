"""Smoke E2E des capacités orgs platform-admin (ADR 0009 barreau 2c) sur PG jetable.

create + entitlement grant/revoke sous PLATFORM_ADMIN.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5471/poc \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_org_admin
"""
from __future__ import annotations

from oto_mcp import db, org_store
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx
from oto_mcp.tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES


def run(key: str, actor: str, **fields):
    cap = next(c for c in registry.CAPABILITIES if c.key == key)
    inp = cap.Input(**fields)
    ctx = cap.authz(RawCtx(sub=actor), inp)
    return cap.handler(ctx, inp)


def denied(fn, status):
    try:
        fn()
        raise SystemExit(f"  ✗ aurait dû lever AuthzDenied({status})")
    except AuthzDenied as e:
        assert e.status == status, f"attendu {status}, reçu {e.status} ({e.code})"


def main() -> None:
    db.init_db()
    with db._connect() as c:
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        for s in ("padmin", "u3"):
            c.execute("INSERT INTO users(sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))

    print("→ padmin (platform admin) crée une org")
    res = run("org.admin.create", "padmin", name="Demo Corp")
    oid = res["id"]
    assert res["org_id"] == oid and res["name"] == "Demo Corp"
    print(f"  ✓ org #{oid} créée (réponse superset id+org_id+name)")

    print("→ u3 (non platform-admin) crée → 403")
    denied(lambda: run("org.admin.create", "u3", name="Nope"), 403)
    print("  ✓")

    ns = sorted(ADMIN_GRANT_ONLY_NAMESPACES)[0]
    print(f"→ grant entitlement gouverné '{ns}'")
    assert run("org.entitlement.grant", "padmin", org_id=oid, namespace=ns)["granted"] is True
    assert ns in [e["namespace"] for e in org_store.list_org_entitlements(oid)]
    print("  ✓")

    print("→ grant namespace NON gouverné (fr) → 400")
    denied(lambda: run("org.entitlement.grant", "padmin", org_id=oid, namespace="fr"), 400)
    print("  ✓ namespace_not_controlled")

    print("→ grant sur org inconnue → 404")
    denied(lambda: run("org.entitlement.grant", "padmin", org_id=99999, namespace=ns), 404)
    print("  ✓")

    print(f"→ revoke entitlement '{ns}'")
    assert run("org.entitlement.revoke", "padmin", org_id=oid, namespace=ns)["revoked"] is True
    assert ns not in [e["namespace"] for e in org_store.list_org_entitlements(oid)]
    print("  ✓")

    print("\n✓ Capacités orgs platform-admin validées (barreau 2c).")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
