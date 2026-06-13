"""Smoke E2E des capacités org.member.* (ADR 0009 barreau 2) sur PG jetable.

Exerce autz ORG_ADMIN_OF (org_admin self-service + escalade platform-admin) +
anti-lockout dernier org_admin, via le même chemin que les adaptateurs
(validation Input → autz → handler).

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5469/poc \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_org_members
"""
from __future__ import annotations

from oto_mcp import db, org_store
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._types import AuthzDenied, RawCtx


def run(key: str, actor: str, **fields):
    cap = next(c for c in registry.CAPABILITIES if c.key == key)
    inp = cap.Input(**fields)
    ctx = cap.authz(RawCtx(sub=actor), inp)      # ORG_ADMIN_OF — lève si pas autorisé
    return cap.handler(ctx, inp)


def denied(fn, status):
    try:
        fn()
        raise SystemExit(f"  ✗ aurait dû lever AuthzDenied({status})")
    except AuthzDenied as e:
        assert e.status == status, f"attendu {status}, reçu {e.status}"
        return e


def main() -> None:
    db.init_db()
    with db._connect() as c:
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        for s in ("u1", "u2", "u3", "padmin"):
            c.execute("INSERT INTO users(sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
        a = c.execute("INSERT INTO orgs(name) VALUES ('Alpha') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO org_members(org_id, sub, org_role) VALUES (%s,'u1','org_admin')", (a,))

    print("→ u1 (org_admin) ajoute u2")
    assert run("org.member.add", "u1", org_id=a, target="u2")["sub"] == "u2"
    assert org_store.get_org_role(a, "u2") == "org_member"
    print("  ✓")

    print("→ u3 (non-membre) tente d'ajouter → 403")
    denied(lambda: run("org.member.add", "u3", org_id=a, target="u2"), 403)
    print("  ✓ ORG_ADMIN_OF refuse")

    print("→ u1 promeut u2 org_admin")
    run("org.member.set_role", "u1", org_id=a, sub="u2", role="org_admin")
    assert org_store.get_org_role(a, "u2") == "org_admin"
    print("  ✓")

    print("→ retrait u2 (reste u1 admin) OK")
    assert run("org.member.remove", "u1", org_id=a, target="u2")["removed"] is True
    print("  ✓")

    print("→ anti-lockout : retirer le dernier org_admin (u1) → 409")
    denied(lambda: run("org.member.remove", "u1", org_id=a, target="u1"), 409)
    assert org_store.get_org_role(a, "u1") == "org_admin", "u1 ne doit pas avoir été retiré"
    print("  ✓ last_org_admin protégé")

    print("→ escalade : padmin (platform admin, NON membre) peut ajouter")
    assert run("org.member.add", "padmin", org_id=a, target="u3")["sub"] == "u3"
    print("  ✓ platform-admin escalade ORG_ADMIN_OF")

    print("→ org inconnue → 404")
    denied(lambda: run("org.member.add", "padmin", org_id=99999, target="u2"), 404)
    print("  ✓")

    print("\n✓ Capacités org.member.* validées (barreau 2).")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
