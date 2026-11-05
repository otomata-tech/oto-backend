"""Une org perso est une org comme une autre (décision d'Alexis du 29/09/2026).

« Perso » n'est qu'une étiquette (`orgs.personal_of`), du même genre que la maison. Deux
exceptions seulement : elle est créée à l'inscription, et son propriétaire ne peut pas
la quitter. Ce banc tient ce qui la traitait encore à part :

- un TABLEAU personnel se liste, pour son seul propriétaire, dans l'org où il l'a créé
  (même règle que les projets, `ownership.mes_tableaux_ici`) — jamais pour un autre
  membre sans partage ;
- la lentille « moi » est servie dans toute org (plus de 409) ;
- sans étiquette, le rattrapage de boot réclame l'org unique que le compte a créée,
  même si d'autres y sont entrés, au lieu d'en recréer une à côté (le cas de l'org
  perso recréée au boot le 29/09) ;
- archiver l'org perso d'un autre suit la règle commune, et qui en sort sans aucune
  org retrouve un espace tout de suite.

Base réelle.
"""
from __future__ import annotations

import uuid

import pytest

MOI, COLLEGUE = "usr_opo_moi", "usr_opo_collegue"


def _nom() -> str:
    return "opo-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    for sub in (MOI, COLLEGUE):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    perso = org_store.ensure_home_org(MOI)
    chez_lui = org_store.ensure_home_org(COLLEGUE)
    # Mon org perso accueille un collègue : elle garde son étiquette (ffbe1c64). Son
    # org d'inscription, où il était seul, lui cède la place d'org active
    # (`seule_d_inscription`) ; il y revient, pour que sa maison soit ailleurs.
    org_store.add_org_member(perso, COLLEGUE, "org_member")
    org_store.set_active_org(COLLEGUE, chez_lui)
    t = {
        "mien_ici": db.create_datastore("user", MOI, _nom(), context_org_id=perso),
        "sien_ici": db.create_datastore("user", COLLEGUE, _nom(), context_org_id=perso),
    }
    return {"perso": perso, "t": t}


def _tableaux(sub: str, org: int, monkeypatch) -> set[int]:
    from oto_mcp import access
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(access, "current_org", lambda s: org)
    return {int(e["id"]) for e in make_store(sub).list_datastores()}


def test_mon_tableau_perso_se_liste_pour_moi_la_ou_je_l_ai_cree(monde, monkeypatch):
    from oto_mcp import org_store
    t = monde["t"]
    # Pour le collègue, l'org où il a créé son tableau n'est pas son org perso : il l'y
    # voit quand même (la règle ne regarde pas l'étiquette), et jamais le mien.
    assert org_store.home_org(COLLEGUE) != monde["perso"]
    vus = _tableaux(COLLEGUE, monde["perso"], monkeypatch)
    assert t["sien_ici"] in vus and t["mien_ici"] not in vus
    vus = _tableaux(MOI, monde["perso"], monkeypatch)
    assert t["mien_ici"] in vus and t["sien_ici"] not in vus


def test_le_rattrapage_ne_pose_pas_d_org_a_cote_de_l_unique(live):
    """Un compte membre d'UNE seule org (même partagée) la garde au rattrapage — il ne
    reçoit pas une org neuve à côté (le cas du 29/09). Plus d'étiquette perso
    depuis le 05/11/2026 : il n'y a plus rien à « réclamer »."""
    from oto_mcp import db, org_store
    solo = "usr_opo_solo_" + uuid.uuid4().hex[:6]
    autre = "usr_opo_autre_" + uuid.uuid4().hex[:6]
    for sub in (solo, autre):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    oid = org_store.ensure_home_org(solo)
    org_store.add_org_member(oid, autre, "org_member")
    assert org_store.ensure_home_org(solo) == oid
    assert [int(o["org_id"]) for o in org_store.list_orgs_for_user(solo)] == [oid]
    assert org_store.home_org(solo) == oid


def test_le_proprietaire_ne_peut_pas_quitter_son_org_perso(monde):
    """Plus d'org perso (05/11/2026) : on ne quitte pas sa DERNIÈRE org (409 `last_org`)."""
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    from oto_mcp.capabilities.orgs import members as M
    with pytest.raises(AuthzDenied):
        M._leave_org(ResolvedCtx(sub=MOI, org_id=monde["perso"]),
                     M.LeaveOrgInput(org_id=monde["perso"]))
