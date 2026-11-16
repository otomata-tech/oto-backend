"""Cas MM « un connecteur http par département » (feedback #183, ADR 0038).

La différenciation des droits tombe PAR CONSTRUCTION avec les secrets de GROUPE :
chaque département pose SON credential `api` (base_url scopé /finance vs /sales),
et la résolution ne le sert qu'aux lecteurs du groupe. On prouve la chaîne :

1. la cascade sert le credential du groupe de l'appel (`group=` co-pose org+groupe,
   déjà gardé can_read_group à la pose) — chaque département voit SON base_url ;
2. hors contexte de groupe, le credential départemental ne résout PAS (pas de
   fuite au niveau org) ;
3. `instance=` (ref group) refuse un non-lecteur — la garde partagée ;
4. la projection B4 montre à l'org_admin les instances de TOUS les départements
   (« vu au niveau org »), et au membre seulement les siennes.
"""
import pytest

from oto_mcp import access, credentials_store, group_store, instance_refs, session_org
from oto_mcp.credentials_store import pack_secret


FINANCE, SALES, ORG = 31, 32, 35


@pytest.fixture()
def dept(monkeypatch):
    """Deux départements, chacun son credential http (base_url scopé)."""
    monkeypatch.setattr(access, "current_org", lambda sub: ORG)
    from oto_mcp import org_store
    # `api` est multi-compte depuis #409 (credential multi-champs) : les paliers
    # partagés passent par la sélection de compte, donc les seams prennent le
    # segment `account` (ici la ligne mono '' de chaque département).
    monkeypatch.setattr(org_store, "get_org_secret",
                        lambda org, prov, account="": None)  # pas de secret ORG
    # Ni clé perso (`api` porte le palier membre depuis le 13/11/2026) : seule celle
    # du département peut servir.
    monkeypatch.setattr(access.db, "get_member_api_key",
                        lambda sub, org, prov, account="": None)
    vault = {
        (FINANCE, "api"): pack_secret("api", {
            "base_url": "https://acme-bridge.oto.zone/finance", "auth_mode": "bearer",
            "token": "TOK-FIN"}),
        (SALES, "api"): pack_secret("api", {
            "base_url": "https://acme-bridge.oto.zone/sales", "auth_mode": "bearer",
            "token": "TOK-SALES"}),
    }
    monkeypatch.setattr(group_store, "get_group_secret",
                        lambda gid, prov, account="": vault.get((gid, prov)))
    monkeypatch.setattr(credentials_store, "list_accounts",
                        lambda et, eid, con: ([{"account": ""}]
                                              if et == "group" and (int(eid), con) in vault
                                              else []))
    return vault


def _resolve_under_group(gid):
    """Résout http comme le ferait un appel `api_get(group=<gid>)` : le jeton
    co-pose l'org et le groupe (déjà gardé can_read_group à la pose par l'axe)."""
    t_org = session_org.set_call_org(ORG)
    t_grp = session_org.set_call_group(gid)
    try:
        return access._resolve_credential_impl("api", "byo", "u")
    finally:
        session_org.reset_call_group(t_grp)
        session_org.reset_call_org(t_org)


def test_each_department_resolves_its_own_base_url(dept):
    fin = _resolve_under_group(FINANCE)
    assert fin.mode == "group" and fin.entity_id == str(FINANCE)
    assert fin.fields["base_url"].endswith("/finance")
    assert fin.fields["token"] == "TOK-FIN"

    sales = _resolve_under_group(SALES)
    assert sales.fields["base_url"].endswith("/sales")
    assert sales.fields["token"] == "TOK-SALES"


def test_no_group_context_no_department_credential(dept, monkeypatch):
    # Hors contexte de groupe (pas de jeton, pas d'équipe maison) : le credential
    # départemental ne résout PAS — pas de fuite « au niveau org ».
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError, match="credential configured"):
        access._resolve_credential_impl("api", "byo", "u")


def test_instance_ref_group_guard_refuses_non_reader(monkeypatch):
    # `instance=group:31:http` posé par un non-lecteur du groupe → refus (garde
    # partagée pose + binding).
    from oto_mcp.mcp_errors import McpError
    from oto_mcp import roles
    monkeypatch.setattr(roles, "can_read_group", lambda sub, gid: False)
    ref = instance_refs.parse_ref(instance_refs.make_group_ref(FINANCE, "api"))
    with pytest.raises(McpError, match="group"):
        access.guard_instance_access("intrus", ref)


def test_projection_org_admin_sees_all_departments(monkeypatch):
    # « Vu au niveau org » : l'org_admin voit les instances de TOUS les départements
    # dans la projection ; un membre simple ne voit que les groupes dont il est membre.
    from types import SimpleNamespace
    from oto_mcp.capabilities.connectors import instances as ci
    from oto_mcp.capabilities._types import ResolvedCtx

    rows = {str(FINANCE): [{"connector": "api", "account": "", "secret_kind": "fields",
                            "set_by": "adm", "set_at": "x", "meta": {"label": "MM Finance"}}],
            str(SALES): [{"connector": "api", "account": "", "secret_kind": "fields",
                          "set_by": "adm", "set_at": "x", "meta": {"label": "MM Sales"}}]}
    monkeypatch.setattr(ci.credentials_store, "list_credentials",
                        lambda et, eid: rows.get(eid, []) if et == "group" else [])
    monkeypatch.setattr(ci.group_store, "list_groups",
                        lambda org: [{"id": FINANCE, "name": "Finance"},
                                     {"id": SALES, "name": "Sales"}])
    monkeypatch.setattr(ci.group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": FINANCE, "name": "Finance"}])
    import oto_mcp.roles as roles_mod
    monkeypatch.setattr(roles_mod, "is_org_admin", lambda sub, org: sub == "admin")
    monkeypatch.setattr(ci.db, "list_grants_for_user", lambda sub: [])
    monkeypatch.setattr(ci.db, "list_org_grants", lambda org: [])
    monkeypatch.setattr(ci.credentials_store, "list_platform_credentials", lambda provider=None: [])
    monkeypatch.setattr(ci.access, "is_super_admin", lambda sub: False)

    admin = ci._list_instances(ResolvedCtx(sub="admin", org_id=ORG),
                               ci.ListInstancesInput(level="group"))
    assert {i["name"] for i in admin["instances"]} == {"MM Finance", "MM Sales"}

    member = ci._list_instances(ResolvedCtx(sub="u-finance", org_id=ORG),
                                ci.ListInstancesInput(level="group"))
    assert {i["name"] for i in member["instances"]} == {"MM Finance"}
