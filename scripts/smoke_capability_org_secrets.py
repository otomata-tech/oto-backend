"""Smoke E2E des capacités org.secret.* (ADR 0009 barreau 2b) sur PG jetable.

autz ORG_ADMIN_OF + validation provider via connectors.org_secret_meta + coffre
chiffré (OTO_MCP_MASTER_KEY factice). Même chemin que les adaptateurs.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5470/poc \
          OTO_MCP_MASTER_KEY=$(python -c "print('0'*64)") \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_org_secrets
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
        fn()
        raise SystemExit(f"  ✗ aurait dû lever AuthzDenied({status})")
    except AuthzDenied as e:
        assert e.status == status, f"attendu {status}, reçu {e.status} ({e.code})"


def main() -> None:
    db.init_db()
    with db._connect() as c:
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        for s in ("u1", "u3"):
            c.execute("INSERT INTO users(sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))
        a = c.execute("INSERT INTO orgs(name) VALUES ('Alpha') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO org_members(org_id, sub, org_role) VALUES (%s,'u1','org_admin')", (a,))

    print("→ u1 (org_admin) pose un secret serper")
    assert run("org.secret.set", "u1", org_id=a, provider="serper", api_key="sk-test")["provider"] == "serper"
    providers = [s["provider"] for s in org_store.list_org_secrets(a)]
    assert "serper" in providers, providers
    print("  ✓ secret posé (chiffré)")

    print("→ provider non-partageable (slack) → 400")
    denied(lambda: run("org.secret.set", "u1", org_id=a, provider="slack", api_key="x"), 400)
    print("  ✓ org_secret_meta refuse")

    print("→ u3 (non-membre) → 403")
    denied(lambda: run("org.secret.set", "u3", org_id=a, provider="serper", api_key="x"), 403)
    print("  ✓ ORG_ADMIN_OF refuse")

    print("→ suppression du secret serper")
    assert run("org.secret.delete", "u1", org_id=a, provider="serper")["deleted"] is True
    assert "serper" not in [s["provider"] for s in org_store.list_org_secrets(a)]
    print("  ✓")

    print("→ org inconnue (acteur autorisé = platform admin) → 404")
    denied(lambda: run("org.secret.set", "padmin", org_id=99999, provider="serper", api_key="x"), 404)
    print("  ✓ (autz 403 prime sur 404 pour un non-admin — pas de fuite d'existence)")

    print("\n✓ Capacités org.secret.* validées (barreau 2b).")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
