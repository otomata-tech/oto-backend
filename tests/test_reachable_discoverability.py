"""Découvrabilité des instances à portée (barreau 1).

Une clé peut exister dans une équipe dont on est membre sans que la cascade la
lise (équipe non active) : le connecteur paraît alors « vide ». L'info existait
déjà, mais SEULEMENT dans l'erreur « aucun credential » — donc jamais atteinte
quand le connecteur n'est pas installé, puisque l'appel meurt avant, au dispatch
(`tool_not_mounted`). Vécu sur une org cliente : clé `zoho` sur l'équipe sales.

Deux surfaces couvertes ici :
1. la carte batchée `access.reachable_instances_map` (annote le catalogue sans
   payer N×M requêtes) ;
2. `ErrorInfo.connector`, qui permet à l'enveloppe d'erreur d'enrichir le
   premier mur.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, credentials_store, error_taxonomy


@pytest.fixture
def wired(monkeypatch):
    """Un sub membre de l'équipe 2 (sales, secret zoho) dans l'org 35, plus une
    autre org 167 qui porte un secret zoho d'org."""
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", {"zoho", "serper"})
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 2, "name": "sales"}])
    # Les clés d'équipe et d'org se lisent par LOTS (`list_credentials_many`), les prêts
    # d'équipe un par équipe.
    cles = {("group", "2"): [{"connector": "zoho"}], ("org", "167"): [{"connector": "zoho"}]}
    monkeypatch.setattr(credentials_store, "list_credentials_many",
                        lambda et, ids: {i: cles[(et, i)] for i in ids if (et, i) in cles})
    monkeypatch.setattr(access.group_store, "lent_instances", lambda gid: [])
    monkeypatch.setattr(access.org_store, "list_orgs_for_user",
                        lambda sub: [{"org_id": 35, "name": "acme"},
                                     {"org_id": 167, "name": "MM Test"}])
    # Mêmes faits, exposés par l'API per-provider qu'utilise `reachable_instances`.
    monkeypatch.setattr(access.group_store, "has_group_secret",
                        lambda gid, p: gid == 2 and p == "zoho")
    monkeypatch.setattr(access.group_store, "groups_holding",
                        lambda gids, org, p: {g for g in gids if g == 2 and p == "zoho"})
    monkeypatch.setattr(access.org_store, "has_org_secret",
                        lambda oid, p: oid == 167 and p == "zoho")
    monkeypatch.setattr(access.db, "has_member_api_key",
                        lambda sub, oid, p: False)
    from oto_mcp import roles
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: False)


def test_map_finds_team_key_not_active(wired):
    """Le cas Acme : clé sur l'équipe sales, équipe non active."""
    m = access.reachable_instances_map("u1", 35)
    assert {"kind": "group", "id": 2, "name": "sales"} in m["zoho"]


def test_map_finds_other_org(wired):
    m = access.reachable_instances_map("u1", 35)
    assert {"kind": "org", "id": 167, "name": "MM Test"} in m["zoho"]


def test_map_omits_providers_without_key(wired):
    """Pas de clé `serper` nulle part → absent de la carte (pas une liste vide)."""
    assert "serper" not in access.reachable_instances_map("u1", 35)


def test_map_ignores_non_shareable_providers(wired, monkeypatch):
    """Un provider hors ORG_SHAREABLE_PROVIDERS n'a pas de palier équipe/org."""
    monkeypatch.setattr(access, "ORG_SHAREABLE_PROVIDERS", set())
    assert access.reachable_instances_map("u1", 35) == {}


def test_map_is_batched_one_query_per_entity(wired, monkeypatch):
    """Garde-fou PERF : le coût est borné par le nombre d'ÉQUIPES (un relevé de prêts
    chacune), JAMAIS par le nombre de providers (serveur mono-loop — une boucle par
    provider ferait N×M allers-retours) ni par le nombre d'orgs du membre : les clés de
    toutes ses autres orgs se lisent en UNE requête."""
    calls = []
    monkeypatch.setattr(access.org_store, "list_orgs_for_user",
                        lambda sub: [{"org_id": 35, "name": "acme"}]
                        + [{"org_id": 100 + i, "name": f"o{i}"} for i in range(40)])
    monkeypatch.setattr(credentials_store, "list_credentials_many",
                        lambda et, ids: calls.append((et, tuple(ids))) or {})
    monkeypatch.setattr(access.group_store, "lent_instances",
                        lambda gid: calls.append(("prets", gid)) or [])
    access.reachable_instances_map("u1", 35)
    assert calls == [("group", ("2",)), ("prets", 2),
                     ("org", tuple(str(100 + i) for i in range(40)))]


def test_map_never_raises(monkeypatch):
    """Best-effort : un hoquet DB ne doit pas casser le catalogue."""
    monkeypatch.setattr(access.group_store, "list_groups_for_user",
                        lambda sub, org: (_ for _ in ()).throw(RuntimeError("db")))
    assert access.reachable_instances_map("u1", 35) == {}


def test_map_without_org_still_scans_other_orgs(wired):
    """Hors org active, les autres orgs restent atteignables."""
    m = access.reachable_instances_map("u1", None)
    assert any(i["kind"] == "org" for i in m.get("zoho", []))


# --- le premier mur porte le connecteur -------------------------------------

def test_tool_not_mounted_carries_the_connector(monkeypatch):
    """`ErrorInfo.connector` est ce qui permet à l'enveloppe d'ajouter le hint
    « instances à portée » sur le PREMIER mur (le classifieur reste pur)."""
    monkeypatch.setattr(error_taxonomy, "_unknown_tool_name", lambda e: "zoho_record")
    monkeypatch.setattr(error_taxonomy, "_connector_of_tool", lambda n: "zoho")
    info = error_taxonomy.classify(RuntimeError("Unknown tool: 'zoho_record'"))
    assert info.code == "tool_not_mounted"
    assert info.connector == "zoho"


def test_unknown_tool_has_no_connector(monkeypatch):
    monkeypatch.setattr(error_taxonomy, "_unknown_tool_name", lambda e: "wat_wat")
    monkeypatch.setattr(error_taxonomy, "_connector_of_tool", lambda n: None)
    info = error_taxonomy.classify(RuntimeError("Unknown tool: 'wat_wat'"))
    assert info.code == "unknown_tool"
    assert info.connector is None


def test_hint_text_names_the_pin_gesture(wired, monkeypatch):
    """Le hint doit donner le GESTE, pas seulement constater l'existence."""
    txt = access._reachable_hint("u1", 35, "zoho")
    assert "`_group=2`" in txt and "`_instance=group:2:zoho`" in txt
    assert "`_org=167`" in txt


def test_map_and_per_provider_agree(wired):
    """GARDE-FOU DE NON-DIVERGENCE. `reachable_instances` (per-provider, chemin
    d'erreur) et `reachable_instances_map` (batché, catalogue) énumèrent la même
    chose par deux requêtes différentes. Deux énumérations parallèles qui dérivent,
    c'est exactement ce qui a fait qu'un correctif Zoho n'a couvert qu'un tiers des
    clients : ce test casse dès que l'une évolue sans l'autre.

    Périmètre volontaire : la clé MEMBRE dans une autre org n'est couverte que par
    la version per-provider (pas de listing groupé) — neutralisée dans la fixture."""
    per_provider = access.reachable_instances("u1", 35, "zoho")
    batched = access.reachable_instances_map("u1", 35).get("zoho", [])
    key = lambda items: sorted((i["kind"], i["id"]) for i in items)  # noqa: E731
    assert key(per_provider) == key(batched)
