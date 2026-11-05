"""La liste des orgs dit laquelle est l'org PERSO (#1216, décision du 14/10/2026).

Un compte avait une org créée à la main et nommée « Perso », en plus de son org perso
posée d'office à l'inscription : un agent les confondait, `oto_list_orgs` ne disant pas
laquelle est l'étiquetée. Chaque entrée porte désormais `personal`, lu à la même source
que la fiche d'org (`orgs.personal_of`) et dans la même requête que la liste — aucune
lecture par org.

Base réelle : c'est la requête de `list_orgs_for_user` qui porte l'étiquette.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.capabilities._types import ResolvedCtx


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db, org_store
    sub = "usr_perso1216_" + uuid.uuid4().hex[:6]
    db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    perso = org_store.ensure_home_org(sub)
    nommee = org_store.create_org("Perso", created_by=sub)
    org_store.add_org_member(nommee, sub, "org_admin")
    return {"sub": sub, "perso": perso, "nommee": nommee}


def test_l_org_nommee_perso_n_est_pas_l_org_perso(compte, monkeypatch):
    from oto_mcp.capabilities.orgs import reads as R

    def _par_org(org_id):
        raise AssertionError("la liste ne lit pas l'étiquette org par org")

    monkeypatch.setattr(R.org_store, "is_personal_org", _par_org)
    out = R._list_my_orgs(ResolvedCtx(sub=compte["sub"]), R.NoInput())
    par_id = {o["id"]: o for o in out["orgs"]}
    assert par_id[compte["nommee"]]["name"] == "Perso"
    assert par_id[compte["nommee"]]["personal"] is False
    # Plus d'org perso (05/11/2026) : le champ reste servi, toujours False.
    assert par_id[compte["perso"]]["personal"] is False
    # Le contrat déclaré (OpenAPI dérivé) porte le champ, requis.
    assert R.MyOrgs(**out).orgs[0].personal in (True, False)
    assert R.MyOrgEntry.model_fields["personal"].is_required()


def test_la_liste_et_la_fiche_disent_la_meme_chose(compte):
    from oto_mcp import org_store
    for o in org_store.list_orgs_for_user(compte["sub"]):
        assert o["personal"] is org_store.is_personal_org(o["org_id"]), o
