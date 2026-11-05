"""Une liste s'en tient à l'org consultée : on ne mélange pas (décision d'Alexis du
07/10/2026).

Un objet personnel (projet ou tableau, `owner_type='user'`) se liste, pour son seul
propriétaire, dans l'org où il a été créé (`context_org_id`) — et nulle part ailleurs,
pas même dans son org perso, qui les listait tous depuis le 28/09 : on croyait alors
qu'ils y vivaient. L'org perso garde seulement ceux qui n'ont d'org de création nulle
part (legacy), faute de quoi ils ne sortiraient dans aucune liste.

Base réelle ; projets par la face servie `POST /api/me/projects`, tableaux par
`list_datastores` et par la recherche (parité « cherchable ⇔ lisible »).
"""
from __future__ import annotations

import uuid

import pytest

MOI = "usr_lso_moi"


def _nom() -> str:
    return "lso-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    db.upsert_user(MOI, email=f"{MOI}@t.invalid", name=MOI)
    perso = org_store.ensure_home_org(MOI)
    a = org_store.create_org("A lso", created_by=MOI)
    org_store.add_org_member(a, MOI, "org_admin")
    p = {
        "dans_a": int(db.create_project("user", MOI, _nom(), created_by=MOI,
                                        context_org_id=a)),
        "dans_perso": int(db.create_project("user", MOI, _nom(), created_by=MOI,
                                            context_org_id=perso)),
        "sans_org": int(db.create_project("user", MOI, _nom(), created_by=MOI)),
    }
    t = {
        "dans_a": int(db.create_datastore("user", MOI, _nom(), context_org_id=a)),
        "dans_perso": int(db.create_datastore("user", MOI, _nom(), context_org_id=perso)),
        "sans_org": int(db.create_datastore("user", MOI, _nom())),
    }
    # Plus d'org perso (05/11/2026) : la maison est l'org ACTIVE ; on la pose sur l'org
    # d'inscription, que « perso » désigne dans ce banc.
    org_store.set_active_org(MOI, perso)
    return {"perso": perso, "a": a, "p": p, "t": t}


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


def _projets(client, org: int) -> set[int]:
    r = client.post("/api/me/projects", json={"op": "list"},
                    headers={"Authorization": f"Bearer {MOI}", "X-Oto-Org": str(org)})
    assert r.status_code == 200, r.text
    return {int(x["id"]) for x in r.json()["projects"]}


def _tableaux(org: int, monkeypatch) -> set[int]:
    from oto_mcp import access
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(access, "current_org", lambda s: org)
    return {int(e["id"]) for e in make_store(MOI).list_datastores()}


def _tableaux_cherchables(org: int) -> set[int]:
    from oto_mcp import search
    return {int(r["id"]) for r in search._accessible_namespaces(MOI, org)}


def test_un_projet_perso_ne_se_liste_que_dans_son_org_de_creation(monde, client):
    p = monde["p"]
    dans_a, dans_perso = _projets(client, monde["a"]), _projets(client, monde["perso"])
    assert p["dans_a"] in dans_a and p["dans_a"] not in dans_perso
    assert p["dans_perso"] in dans_perso and p["dans_perso"] not in dans_a


def test_un_projet_perso_sans_org_de_creation_reste_dans_l_org_perso(monde, client):
    p = monde["p"]
    assert p["sans_org"] in _projets(client, monde["perso"])
    assert p["sans_org"] not in _projets(client, monde["a"])


@pytest.mark.parametrize("liste", ["liste", "recherche"])
def test_un_tableau_perso_ne_se_liste_que_dans_son_org_de_creation(monde, monkeypatch,
                                                                   liste):
    t = monde["t"]

    def vus(org):
        return (_tableaux(org, monkeypatch) if liste == "liste"
                else _tableaux_cherchables(org))

    dans_a, dans_perso = vus(monde["a"]), vus(monde["perso"])
    assert t["dans_a"] in dans_a and t["dans_a"] not in dans_perso
    assert t["dans_perso"] in dans_perso and t["dans_perso"] not in dans_a
    assert t["sans_org"] in dans_perso and t["sans_org"] not in dans_a
