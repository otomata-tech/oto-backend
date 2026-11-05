"""Un projet appartient à qui le crée et vit dans l'org où il l'a créé.

Décision d'Alexis du 29/09/2026, après un incident : une personne a invité un collègue
dans son org perso ; `add_org_member` retirait alors `personal_of`, elle n'avait plus
d'org perso, et depuis la règle du 28/09 (le personnel ne se liste que dans l'org
perso) ses projets personnels ne sortaient plus dans AUCUNE liste. Deux règles :

- `personal_of` est une ÉTIQUETTE (l'org créée à l'inscription), pas un état : un 2ᵉ
  membre ne la retire plus ;
- un projet personnel se liste pour son PROPRIÉTAIRE dans l'org où il l'a créé
  (`context_org_id`), perso ou partagée — et pour personne d'autre sans partage.

Base réelle, face servie `POST /api/me/projects` sous `X-Oto-Org`.
"""
from __future__ import annotations

import uuid

import pytest

MOI, INVITE, VOISIN, ADMIN = ("usr_ppv_moi", "usr_ppv_invite", "usr_ppv_voisin",
                              "usr_ppv_admin")


def _nom() -> str:
    return "ppv-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    for sub in (MOI, INVITE, VOISIN, ADMIN):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    perso = org_store.ensure_home_org(MOI)
    perso_invite = org_store.ensure_home_org(INVITE)
    a = org_store.create_org("A ppv", created_by=ADMIN)
    org_store.add_org_member(a, ADMIN, "org_admin")
    org_store.add_org_member(a, MOI)
    org_store.add_org_member(a, VOISIN)
    p = {
        "dans_perso": int(db.create_project("user", MOI, _nom(), created_by=MOI,
                                            context_org_id=perso)),
        "dans_a": int(db.create_project("user", MOI, _nom(), created_by=MOI,
                                        context_org_id=a)),
    }
    # L'incident : j'invite un collègue dans MON org perso.
    org_store.add_org_member(perso, INVITE, "org_member")
    return {"perso": perso, "perso_invite": perso_invite, "a": a, "p": p}


@pytest.fixture(scope="module")
def client(monde):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes

    class _Claims:
        def __init__(self, sub):
            self.claims = {"sub": sub, "email": f"{sub}@t.invalid", "name": sub}

    class _Verifier:
        async def verify_token(self, token):
            return _Claims(token)

    return TestClient(api_routes.ViewAsMiddleware(
        Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)),
        verifier=_Verifier()))


def _ids(client, sub: str, org: int) -> set[int]:
    r = client.post("/api/me/projects", json={"op": "list"},
                    headers={"Authorization": f"Bearer {sub}", "X-Oto-Org": str(org)})
    assert r.status_code == 200, r.text
    return {int(x["id"]) for x in r.json()["projects"]}


def test_mon_org_d_inscription_reste_mienne_quand_j_y_invite_quelqu_un(monde):
    """Plus d'org perso (05/11/2026) : aucune n'est étiquetée ; chacun garde son org
    d'inscription, et l'invité y entre sans y être déplacé."""
    from oto_mcp import org_store
    assert org_store.is_personal_org(monde["perso"]) is False
    mes_orgs = {int(o["org_id"]) for o in org_store.list_orgs_for_user(MOI)}
    assert monde["perso"] in mes_orgs
    assert monde["perso_invite"] in {int(o["org_id"]) for o in org_store.list_orgs_for_user(INVITE)}


def test_je_vois_toujours_mes_projets_dans_mon_org_perso_apres_l_invitation(monde, client):
    assert monde["p"]["dans_perso"] in _ids(client, MOI, monde["perso"])


def test_l_invite_ne_voit_pas_mes_projets_sans_partage(monde, client):
    vus = _ids(client, INVITE, monde["perso"])
    assert not vus & set(monde["p"].values())


def test_l_invite_voit_un_projet_une_fois_partage(monde, client):
    from oto_mcp import ownership
    pid = monde["p"]["dans_perso"]
    ownership.grant("project", str(pid), "org", str(monde["perso"]), role="viewer",
                    granted_by=MOI)
    try:
        assert pid in _ids(client, INVITE, monde["perso"])
    finally:
        ownership.revoke("project", str(pid), "org", str(monde["perso"]))


def test_mon_projet_cree_dans_une_org_d_equipe_s_y_liste_pour_moi_seul(monde, client):
    pid = monde["p"]["dans_a"]
    assert pid in _ids(client, MOI, monde["a"])
    assert pid not in _ids(client, VOISIN, monde["a"])
    assert pid not in _ids(client, ADMIN, monde["a"])
    # …et là seulement : plus dans mon org perso (07/10/2026, « on ne mélange pas »).
    assert pid not in _ids(client, MOI, monde["perso"])
