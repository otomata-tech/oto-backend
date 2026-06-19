"""Smoke E2E de la bibliothèque publique de doctrines (capacités library.*) sur PG jetable.

publish (org_admin) → list/get (membre) → fork (autre org) → unpublish (auteur).
Couvre aussi les refus : non-org_admin ne publie pas, public_get ignore 'unlisted'.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5471/poc \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_doctrine_library
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
        c.execute("TRUNCATE doctrine_library RESTART IDENTITY")
        c.execute("TRUNCATE org_members, org_instructions, orgs RESTART IDENTITY CASCADE")
        for s in ("alice", "bob"):
            c.execute("INSERT INTO users(sub) VALUES (%s) ON CONFLICT DO NOTHING", (s,))

    # alice = org_admin d'Org A (org active) ; bob = org_admin d'Org B (org active)
    oa = org_store.create_org("Org A", created_by="alice")
    org_store.add_org_member(oa, "alice", "org_admin")
    org_store.set_active_org("alice", oa)
    ob = org_store.create_org("Org B", created_by="bob")
    org_store.add_org_member(ob, "bob", "org_admin")
    org_store.set_active_org("bob", ob)
    # alice possède un skill nommé dans son org
    org_store.set_instruction(oa, "outreach", "# Outreach\nLe playbook.",
                              title="Outreach", description="Comment prospecter", set_by="alice")

    print("→ bob (pas org_admin d'Org A, sans le skill) publie 'outreach' → 404 unknown_doctrine")
    denied(lambda: run("library.publish", "bob", slug="outreach"), 404)
    print("  ✓")

    print("→ alice publie 'outreach' dans la bibliothèque")
    res = run("library.publish", "alice", slug="outreach", category="Prospection",
              tags=["sales"], visibility="public")
    assert res["published"] and res["version"] == 1
    eid = res["id"]
    print(f"  ✓ entrée #{eid} (slug={res['slug']}, author=org)")

    print("→ list (membre) voit l'entrée publique")
    items = run("library.list", "bob", query="outreach")["doctrines"]
    assert any(i["slug"] == "outreach" and i["author_kind"] == "org" for i in items), items
    print("  ✓ author_kind=org, author_display porté")

    print("→ get (membre) renvoie le body complet")
    got = run("library.get", "bob", slug="outreach")
    assert got["body_md"].startswith("# Outreach"), got
    print("  ✓")

    print("→ bob forke dans Org B → skill versionné v1")
    fk = run("library.fork", "bob", slug="outreach")
    assert fk["forked"] and fk["org_id"] == ob and fk["version"] == 1
    forked = org_store.get_instruction(ob, fk["slug"])
    assert forked and forked["body_md"].startswith("# Outreach")
    print(f"  ✓ Org B a maintenant le skill '{fk['slug']}'")

    print("→ fork une 2e fois → slug dédupliqué (-2)")
    fk2 = run("library.fork", "bob", slug="outreach")
    assert fk2["slug"] != fk["slug"], fk2
    print(f"  ✓ {fk2['slug']}")

    print("→ bob (pas l'auteur) tente unpublish → 403")
    denied(lambda: run("library.unpublish", "bob", id=eid), 403)
    print("  ✓")

    print("→ alice (auteur) unpublish")
    assert run("library.unpublish", "alice", id=eid)["unpublished"] is True
    assert org_store.get_library_entry(entry_id=eid, include_unlisted=True) is None
    print("  ✓")

    print("→ publication 'unlisted' : invisible de la liste publique anonyme")
    org_store.publish_doctrine(slug="secret-skill", body_md="# Secret", author_kind="otomata",
                               author_display="Otomata", visibility="unlisted")
    assert not org_store.list_library(include_unlisted=False)
    assert org_store.get_library_entry(slug="secret-skill", include_unlisted=False) is None
    assert org_store.get_library_entry(slug="secret-skill", include_unlisted=True) is not None
    print("  ✓ deny-by-default sur la surface anonyme")

    print("\n✓ Bibliothèque publique de doctrines validée.")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
