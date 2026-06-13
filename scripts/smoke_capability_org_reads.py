"""Smoke E2E des lectures orgs (ADR 0009 barreau 2d) sur PG jetable.

Vérifie ORG_MEMBER_OF (lecture par id de path) + formes superset.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5472/poc \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_org_reads
"""
from __future__ import annotations

from oto_mcp import db, org_store
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx


def run(key: str, actor: str, **fields):
    cap = next(c for c in registry.CAPABILITIES if c.key == key)
    inp = cap.Input(**fields)
    ctx = cap.authz(RawCtx(sub=actor), inp)
    return cap.handler(ctx, inp)


def denied(fn, status):
    try:
        fn(); raise SystemExit(f"  ✗ aurait dû lever {status}")
    except AuthzDenied as e:
        assert e.status == status, f"attendu {status}, reçu {e.status}"


def main() -> None:
    db.init_db()
    with db._connect() as c:
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        for s in ("u1", "u2", "padmin"):
            c.execute("INSERT INTO users(sub, email) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (s, f"{s}@x.io"))
        a = c.execute("INSERT INTO orgs(name) VALUES ('Alpha') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO org_members(org_id, sub, org_role, is_active) VALUES (%s,'u1','org_admin',true)", (a,))

    print("→ org.get : membre (u1) lit son org")
    d = run("org.get", "u1", org_id=a)
    assert d["org"]["id"] == a and d["org"]["my_role"] == "org_admin"
    assert d["members"][0]["email"] == "u1@x.io" and "secrets" in d and "entitlements" in d
    print("  ✓ détail + my_role + membres enrichis")

    print("→ org.get : non-membre (u2) → 403 (ORG_MEMBER_OF)")
    denied(lambda: run("org.get", "u2", org_id=a), 403)
    print("  ✓")

    print("→ org.get : platform admin (padmin, non-membre) → escalade OK")
    assert run("org.get", "padmin", org_id=a)["org"]["id"] == a
    print("  ✓")

    print("→ org.list (u1) : superset id+org_id + active_org")
    lst = run("org.list", "u1")
    o0 = lst["orgs"][0]
    assert o0["id"] == a and o0["org_id"] == a and o0["my_role"] == "org_admin" and o0["role"] == "org_admin"
    assert lst["active_org"] == a
    print("  ✓")

    print("→ org.admin.list : padmin OK, u1 (non platform-admin) → 403")
    assert any(o["id"] == a and "member_count" in o for o in run("org.admin.list", "padmin")["orgs"])
    denied(lambda: run("org.admin.list", "u1"), 403)
    print("  ✓")

    print("→ org.get : org inconnue, acteur autorisé (padmin) → 404")
    denied(lambda: run("org.get", "padmin", org_id=99999), 404)
    print("  ✓")

    print("\n✓ Lectures orgs validées (barreau 2d) — domaine orgs 100% en couche capacité.")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
