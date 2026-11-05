"""« Partagés avec moi » (`GET /api/me/datastores/shared`) — sur une VRAIE base.

La garantie qui compte est une garantie de CONFIDENTIALITÉ : elle se prouve sur le SQL
réel, pas sur un double. Un partage à une personne n'est visible QUE de son
destinataire — jamais d'un tiers de la même org, jamais par un droit d'org ou d'équipe.
C'est l'incident du 30/06 (des ressources d'une autre org visibles dans une vue d'org)
qu'on s'interdit de rejouer (arbitrage d'Alexis, otomata-tech/oto#160, 10/09/2026).

La distribution :
- A possède `t-perso` (personnel) et le partage à B en LECTURE — et s'y est aussi posé
  un droit à lui-même (un tableau possédé n'est pas « partagé avec moi ») ;
- l'org X possède `t-org`, que A partage AUSSI nominativement à B, membre de X ;
- A partage `t-a-l-org` à l'org X et `t-a-l-equipe` à une équipe de X : des droits
  d'AUDIENCE, pas des partages à une personne ;
- C est membre de X et de cette équipe, sans aucun partage nominatif ;
- Y est une autre org de B, qui ne possède rien.

L'org active est posée par appel (`access.current_org`, le seam unique) ; les
appartenances, elles, sont de vraies lignes. Depuis le 29/09/2026 la route n'est servie
que dans l'org PERSO de l'appelant (409 `personal_view_outside_personal_org` ailleurs),
et ne dédoublonne plus avec la liste de l'org, qui y rend aussi ces tableaux.
"""
from __future__ import annotations

import pytest

from oto_mcp import access, db, group_store, org_store, ownership
from oto_mcp.capabilities import registry
from oto_mcp.capabilities._authz import SUB_ONLY
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.datastore import partages_recus as P
from oto_mcp.datastore.core import make_store

A, B, C = "u-partage-a", "u-partage-b", "u-partage-c"
DS = ownership.TYPE_RESSOURCE_DATASTORE


@pytest.fixture(scope="module")
def monde(live):
    for sub, nom in ((A, "Alice Proprio"), (B, "Bruno Dest"), (C, "Chloé Tiers")):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=nom)
    x = org_store.create_org("Org X", created_by=A)
    y = org_store.create_org("Org Y", created_by=B)
    org_store.add_org_member(x, B)
    org_store.add_org_member(x, C)
    org_store.add_org_member(y, B)
    equipe = group_store.create_group(x, "Équipe X", created_by=A)
    group_store.add_group_member(equipe, C)
    t = {nom: db.create_datastore(ot, oid, nom) for nom, ot, oid in (
        ("t-perso", "user", A), ("t-org", "org", str(x)),
        ("t-a-l-org", "user", A), ("t-a-l-equipe", "user", A))}
    ownership.grant(DS, str(t["t-perso"]), "user", B, "read", granted_by=A)
    ownership.grant(DS, str(t["t-perso"]), "user", A, "write", granted_by=A)
    ownership.grant(DS, str(t["t-org"]), "user", B, "write", granted_by=A)
    ownership.grant(DS, str(t["t-a-l-org"]), "org", str(x), "write", granted_by=A)
    ownership.grant(DS, str(t["t-a-l-equipe"]), "group", str(equipe), "write", granted_by=A)
    return {"x": x, "y": y, **t}


def _recus(monkeypatch, sub, org) -> dict:
    """La réponse de la capacité, pour `sub` naviguant dans `org` — son org PERSO, seule
    où la lentille est servie depuis le 29/09/2026 (ADR 0030 §9).

    ⚠️ La face REST sert le dict du handler TEL QUEL : `Output` décrit, il ne valide pas
    (`_types.Capability`). Mesuré sur la préproduction le 10/09/2026 : aucune clé `ns_id`
    sur le fil, alors que le modèle la dérive. On lit donc `out`, jamais un `model_dump` —
    ce serait asserter la DÉCLARATION, pas ce qui est servi. La déclaration, elle, doit au
    moins accepter ce qui est servi."""
    monkeypatch.setattr(access, "current_org", lambda s: org)
    out = P._shared_with_me(ResolvedCtx(sub=sub, org_id=org), P.SharedWithMeInput())
    P.SharedWithMe(**out)
    return {int(e["id"]): e for e in out["datastores"]}


def _perso(sub) -> int:
    return org_store.ensure_home_org(sub)


def _liste_de_l_org(monkeypatch, sub, org) -> set:
    monkeypatch.setattr(access, "current_org", lambda s: org)
    return {int(e["id"]) for e in make_store(sub).list_datastores()}


def test_la_route_reste_hors_MCP_sans_refus_declare():
    # 29/09/2026 : plus de 409 `personal_view_outside_personal_org` à déclarer — la
    # lentille est servie dans toute org.
    cap = registry.by_key("me.datastore.shared_with_me")
    assert cap.authz is SUB_ONLY
    assert cap.mcp is None
    assert [(b.verb, b.path) for b in cap.rest_bindings()] == [("GET", "/api/me/datastores/shared")]
    assert list(cap.errors) == []


@pytest.mark.parametrize("org", ["x", "y", None])
def test_hors_de_l_org_perso_la_lentille_rend_la_meme_chose(monde, monkeypatch, org):
    """Décision du 29/09/2026 : une org perso est une org comme une autre, et un partage à
    une personne n'appartient à aucune org — la lentille « moi » rend partout ce qu'elle
    rend dans l'org perso (elle était refusée en 409 ailleurs)."""
    assert _recus(monkeypatch, B, monde[org] if org else None) == \
        _recus(monkeypatch, B, _perso(B))


def test_le_destinataire_voit_le_partage_personnel_dans_son_org_perso(monde, monkeypatch):
    e = _recus(monkeypatch, B, _perso(B))[monde["t-perso"]]
    assert e["shared_by"] == "Alice Proprio", "qui a partagé — un nom, pas un identifiant"
    assert (e["permission"], e["can_write"], e["shared"]) == ("read", False, True)
    assert (e["owner_type"], e["is_personal"]) == ("user", False)
    assert e["id"] == monde["t-perso"]


def test_la_lentille_est_le_sous_ensemble_partage_a_moi_de_la_liste_de_l_org_perso(
        monde, monkeypatch):
    """Plus de dédoublonnage (29/09/2026) : dans l'org perso, la liste rend aussi ces
    tableaux ; la lentille en est le sous-ensemble, droit compris — `t-org`, possédé par
    X et partagé à B en personne, y est avec son droit d'écriture."""
    perso = _perso(B)
    recus = _recus(monkeypatch, B, perso)
    assert set(recus) == {monde["t-perso"], monde["t-org"]}
    assert set(recus) <= _liste_de_l_org(monkeypatch, B, perso)
    assert (recus[monde["t-org"]]["permission"], recus[monde["t-org"]]["can_write"]) == (
        "write", True)


def test_un_tiers_de_la_meme_org_ne_voit_rien(monde, monkeypatch):
    """C est dans X ET dans l'équipe : il voit les droits d'audience LÀ OÙ ILS VIVENT
    (la liste de l'org), jamais ici — ni les partages faits à B."""
    liste = _liste_de_l_org(monkeypatch, C, monde["x"])
    assert {monde["t-a-l-org"], monde["t-a-l-equipe"]} <= liste, "contrôle positif"
    assert _recus(monkeypatch, C, _perso(C)) == {}


def test_le_proprietaire_ne_se_voit_rien_partager(monde, monkeypatch):
    assert _recus(monkeypatch, A, _perso(A)) == {}


def test_la_requete_ne_connait_que_le_destinataire(monde):
    assert db.list_datastores_shared_to_user(C) == []
    assert db.list_datastores_shared_to_user(A) == [], "son propre tableau n'est pas reçu"
    assert {r["id"] for r in db.list_datastores_shared_to_user(B)} == {
        monde["t-perso"], monde["t-org"]}
