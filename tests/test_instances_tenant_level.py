"""L-clés, PR 2 — la ligne `tenant` dans `oto_instance op=list`.

Un compte d'un tenant tiers voit la clé de son tenant comme une instance de niveau
`tenant`, entre l'org et la plateforme, avec un `ref` épinglable (`tenant:{slug}:…`).
Un compte nu ne la voit pas : il ne pourrait pas la résoudre, et « qui peut la
résoudre la voit » (R9).
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, db, group_store, tenancy
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.connectors import instances as ci

PILOTE = "pilote"
SUB_T = f"{PILOTE}:u1"


@pytest.fixture
def socle(monkeypatch):
    monkeypatch.setattr(tenancy, "_INSTALLED", tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": PILOTE, "issuer": "https://auth.pilote.test/oidc"}])),
        raising=False)
    def _list(entity_type, entity_id):
        if entity_type == credentials_store.TENANT and entity_id == PILOTE:
            return [{"connector": "serper", "account": "", "secret_kind": "api_key",
                     "set_by": "operateur", "set_at": None, "meta": {}}]
        return []
    monkeypatch.setattr(credentials_store, "list_credentials", _list)
    monkeypatch.setattr(credentials_store, "list_platform_credentials", lambda: [])
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda s, o=None: [])
    monkeypatch.setattr(db, "list_grants_for_user", lambda s: [])
    monkeypatch.setattr(db, "list_org_grants", lambda o: [])
    monkeypatch.setattr(ci, "_stamp_instance_identity", lambda out: [i.pop("_vault_key", None) for i in out])
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda s, o: False)


def test_un_compte_du_tenant_voit_la_cle_de_son_tenant(socle):
    out = ci._list_instances(ResolvedCtx(sub=SUB_T, org_id=5), ci.ListInstancesInput())
    assert [(i["level"], i["ref"], i["owner"]["type"], i["owner"]["id"], i["via"])
            for i in out["instances"]] == [
        ("tenant", f"tenant:{PILOTE}:serper", "tenant", PILOTE, "tenant_key")]


def test_un_compte_nu_ne_la_voit_pas(socle):
    out = ci._list_instances(ResolvedCtx(sub="u1", org_id=5), ci.ListInstancesInput())
    assert out["instances"] == []


def test_le_filtre_level_accepte_tenant(socle):
    out = ci._list_instances(ResolvedCtx(sub=SUB_T, org_id=5),
                             ci.ListInstancesInput(level="tenant"))
    assert out["count"] == 1
    out = ci._list_instances(ResolvedCtx(sub=SUB_T, org_id=5),
                             ci.ListInstancesInput(level="org"))
    assert out["count"] == 0


def test_le_tenant_se_range_entre_l_org_et_la_plateforme():
    assert ci._LEVEL_RANK["org"] < ci._LEVEL_RANK["tenant"] < ci._LEVEL_RANK["platform"]
