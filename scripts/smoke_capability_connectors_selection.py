"""Smoke E2E des capacités de sélection de connecteurs (ADR 0019) sur PG jetable.

me (catalogue+état) → select → pause → unselect ; plafond d'exposition (404 sur
connecteur non-exposé) ; recommend (org_admin) → orgs.default_connectors.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5471/poc \
          OTO_MCP_ADMIN_SUB=padmin OTO_CONFIG_DISABLE_SOPS=1 \
          .venv/bin/python -m scripts.smoke_capability_connectors_selection
"""
from __future__ import annotations

from oto_mcp import connector_activation, connector_selection, db, org_store
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


def state_of(connectors: list[dict], name: str) -> str | None:
    for c in connectors:
        if c["name"] == name:
            return c["state"]
    return None  # absent du catalogue exposé


def main() -> None:
    db.init_db()  # crée + seed connector_activation (tout ON global) + user_selected_connectors
    with db._connect() as c:
        c.execute("TRUNCATE user_selected_connectors RESTART IDENTITY")
        c.execute("TRUNCATE org_members, orgs RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO users(sub) VALUES ('alice') ON CONFLICT DO NOTHING")

    oa = org_store.create_org("Org A", created_by="alice")
    org_store.add_org_member(oa, "alice", "org_admin")
    org_store.set_active_org("alice", oa)

    # serper est au registre → activé par seed_initial → exposé.
    print("→ me : serper exposé, état not_selected")
    me = run("connectors.me", "alice")["connectors"]
    assert state_of(me, "serper") == "not_selected", state_of(me, "serper")
    print("  ✓")

    print("→ select serper → active")
    assert run("connectors.select", "alice", name="serper")["state"] == "active"
    assert state_of(run("connectors.me", "alice")["connectors"], "serper") == "active"
    print("  ✓")

    print("→ pause serper → paused")
    assert run("connectors.pause", "alice", name="serper")["state"] == "paused"
    assert state_of(run("connectors.me", "alice")["connectors"], "serper") == "paused"
    print("  ✓")

    print("→ plafond : select d'un connecteur non-exposé → 404")
    denied(lambda: run("connectors.select", "alice", name="nope-connector"), 404)
    print("  ✓")

    print("→ unselect serper → not_selected")
    assert run("connectors.unselect", "alice", name="serper")["removed"] is True
    assert state_of(run("connectors.me", "alice")["connectors"], "serper") == "not_selected"
    print("  ✓")

    print("→ recommend (org_admin) : baseline org → default_connectors + recommended=true")
    run("connectors.recommend", "alice", org_id=oa, connectors=["serper", "hunter"])
    assert org_store.get_org_default_connectors(oa) == ["serper", "hunter"]
    me = run("connectors.me", "alice")["connectors"]
    assert next(c for c in me if c["name"] == "serper")["recommended"] is True
    print("  ✓")

    print("\n✓ Sélection de connecteurs (ADR 0019) validée.")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
