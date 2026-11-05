"""Dans une org, on ne voit QUE l'org ; le personnel se liste dans la maison.

Plus d'org perso depuis le 05/11/2026 : la maison est l'org ACTIVE du compte
(`org_store.home_org`). Ce banc la pose sur l'org que l'inscription lui a donnée, et
« l'org perso » de ses noms se lit « la maison » ; le reste raconte l'histoire.

Décision d'Alexis du 28/09/2026 (ADR 0030 §9). Elle remplace la règle d'oto#160 — un
projet ou un tableau personnel listé dans l'org où il a été créé (`context_org_id`) :

- dans une org qui n'est pas l'org perso de l'appelant, les listes (projets, tableaux,
  recherche, pages reçues) rendent ce que possèdent l'org et ses équipes, plus ce qui
  est partagé à l'org ou à une de ses équipes — aucun objet `owner_type='user'`, quel
  que soit son `context_org_id`, et aucun partage fait à une personne ;
- dans l'org perso : tous les projets et tableaux personnels de l'appelant, quelle que
  soit l'org où ils ont été créés, et tout ce qui lui est partagé en personne ;
- créer (décision du 29/09/2026) : sans propriétaire nommé, un projet ou un tableau est
  à la PERSONNE, depuis n'importe quelle org — il se liste alors dans l'org perso, pas
  dans l'org où il a été créé ; l'org ou l'équipe se demandent explicitement ;
- les lentilles « moi » (`scope=me`, `GET /api/me/datastores/shared`, `shared_with_me`
  sans `scope` ou `me`) ne sont servies que dans l'org perso : ailleurs, 409
  `personal_view_outside_personal_org` (29/09/2026) ;
- l'accès par identifiant ne change pas ;
- amendement du 29/09/2026 (Alexis) : un projet PERSONNEL se liste aussi, pour son
  seul propriétaire, dans l'org où il l'a créé (`context_org_id`) — jamais pour un
  autre membre sans partage ;
- second amendement du 29/09/2026 (Alexis : « une org perso est une org comme une
  autre, fonctionnellement ce sont les mêmes ») : même règle pour les TABLEAUX
  personnels (`ownership.mes_tableaux_ici`), et les lentilles « moi » sont servies dans
  toute org, avec le même contenu — le 409 `personal_view_outside_personal_org` est
  retiré. Les tests qui encodaient l'ancienne règle sont réécrits en disant pourquoi ;
- troisième amendement du 07/10/2026 (Alexis : « l'affichage s'en tient à l'org
  consultée, on ne mélange pas ») : l'org perso ne liste plus les projets et tableaux
  perso créés dans une autre org, seulement ceux sans org de création et ce qui m'est
  partagé en personne (`tests/test_liste_s_en_tient_a_l_org.py`).

Base réelle, sur les faces servies : `POST /api/me/projects` (op=list, op=create),
`GET /api/datastores` et `POST /api/datastores` sous `X-Oto-Org`, l'outil
`data_list_datastores` sous `_org=`, la recherche (`accessible_project_ids`,
`search._accessible_namespaces`) tenue en parité avec les listes, et
`oto_doc op=shared_with_me scope=org`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

MOI, TIERS, VOISIN = "usr_listes_org_moi", "usr_listes_org_tiers", "usr_listes_org_voisin"


def _nom() -> str:
    return "lo-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, group_store, org_store, ownership
    from oto_mcp.capabilities.docs import common as _kind_doc  # noqa: F401 — enregistre `doc`
    for sub in (MOI, TIERS, VOISIN):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    perso = org_store.ensure_home_org(MOI)
    a = org_store.create_org("A listes", created_by=TIERS)
    b = org_store.create_org("B listes", created_by=TIERS)
    org_store.add_org_member(a, TIERS, "org_admin")
    org_store.add_org_member(a, MOI)
    org_store.add_org_member(a, VOISIN)      # membre de A, sans aucun partage
    org_store.add_org_member(b, TIERS)
    equipe = group_store.create_group(a, "E listes", created_by=TIERS)
    group_store.add_group_member(equipe, MOI)
    tds = ownership.TYPE_RESSOURCE_DATASTORE

    t = {
        "perso_a": db.create_datastore("user", MOI, _nom(), context_org_id=a),
        "perso_null": db.create_datastore("user", MOI, _nom(), context_org_id=None),
        "org_a": db.create_datastore("org", str(a), _nom()),
        "equipe": db.create_datastore("group", str(equipe), _nom()),
        "recu_moi": db.create_datastore("org", str(b), _nom()),
        "recu_a": db.create_datastore("org", str(b), _nom()),
        "recu_equipe": db.create_datastore("org", str(b), _nom()),
        "tiers_perso": db.create_datastore("user", TIERS, _nom(), context_org_id=a),
    }
    ownership.grant(tds, str(t["recu_moi"]), "user", MOI, "read", granted_by=TIERS)
    ownership.grant(tds, str(t["recu_a"]), "org", str(a), "read", granted_by=TIERS)
    ownership.grant(tds, str(t["recu_equipe"]), "group", str(equipe), "read",
                    granted_by=TIERS)

    def projet(owner_type, owner_id, ctx=None, partage_a=None):
        pid = int(db.create_project(owner_type, str(owner_id), _nom(), created_by=TIERS,
                                    context_org_id=ctx))
        if partage_a:
            ownership.grant("project", str(pid), *partage_a, role="viewer",
                            granted_by=TIERS)
        return pid

    p = {
        "perso_a": projet("user", MOI, ctx=a),
        "perso_p": projet("user", MOI, ctx=perso),
        "org_a": projet("org", a),
        "equipe": projet("group", equipe),
        "recu_moi": projet("org", b, partage_a=("user", MOI)),
        "recu_a": projet("org", b, partage_a=("org", str(a))),
        "tiers_perso": projet("user", TIERS, ctx=a),
    }
    page_recue = db.create_doc(projet("org", b), "page reçue", body_md="x",
                               created_by=TIERS)
    ownership.grant("doc", str(page_recue), "user", MOI, role="viewer", granted_by=TIERS)
    # Un tableau de B partagé à MOI en personne et LIÉ à un projet de A : la liste de A
    # ne le rend pas (partage nominatif) ; son numéro l'ouvre depuis A.
    t["lie_recu"] = db.create_datastore("org", str(b), _nom())
    ownership.grant(tds, str(t["lie_recu"]), "user", MOI, "read", granted_by=TIERS)
    db.add_project_link(p["org_a"], "tableau", str(t["lie_recu"]), label="vivier")
    # Plus d'org perso (05/11/2026) : la maison de MOI est son org ACTIVE. Rejoindre A
    # l'y avait fait passer (l'org d'inscription cède sa place) ; on l'y ramène, et
    # « l'org perso » de ce banc se lit désormais « la maison ».
    org_store.set_active_org(MOI, perso)
    return {"perso": perso, "a": a, "b": b, "equipe": equipe, "t": t, "p": p,
            "page_recue": page_recue}


@pytest.fixture(scope="module")
def client(monde):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes

    class _Claims:
        claims = {"sub": MOI, "email": f"{MOI}@t.invalid", "name": MOI}

    class _Verifier:
        async def verify_token(self, token):
            return _Claims()

    # `X-Oto-Org` est lu par `ViewAsMiddleware`, comme en production.
    return TestClient(api_routes.ViewAsMiddleware(
        Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)),
        verifier=_Verifier()))


def _entetes(org: int) -> dict:
    return {"Authorization": f"Bearer {MOI}", "X-Oto-Org": str(org)}


def _connus(monde, famille: str, ids) -> set[str]:
    """Les objets du monde, par leur nom de banc, parmi `ids`."""
    par_id = {v: k for k, v in monde[famille].items()}
    return {par_id[int(i)] for i in ids if int(i) in par_id}


def _projets_listes(client, org: int) -> list[dict]:
    r = client.post("/api/me/projects", json={"op": "list"}, headers=_entetes(org))
    assert r.status_code == 200, r.text
    return r.json()["projects"]


def _tableaux_listes(client, org: int) -> list[dict]:
    r = client.get("/api/datastores", headers=_entetes(org))
    assert r.status_code == 200, r.text
    return r.json()["datastores"]


# --- projets -------------------------------------------------------------------

def test_projets_dans_une_org_de_travail_l_org_et_mes_projets_crees_ici(monde, client):
    # Décision du 29/09/2026 : mon projet perso créé dans A (`perso_a`) se liste pour
    # MOI dans A ; celui d'un autre créé dans A (`tiers_perso`), jamais — il n'est à
    # personne d'autre que lui tant qu'il ne le partage pas.
    vus = _projets_listes(client, monde["a"])
    assert _connus(monde, "p", [x["id"] for x in vus]) == {
        "org_a", "equipe", "recu_a", "perso_a"}
    assert {x["owner_id"] for x in vus if x["owner_type"] == "user"} == {MOI}, (
        "le projet personnel d'un autre est listé dans une org")


def test_projets_dans_l_org_perso_mon_personnel_cree_ici_et_ce_qui_m_est_partage(monde, client):
    # 07/10/2026 : plus `perso_a` (créé dans A) — il ne se liste que dans A.
    vus = {x["id"]: x for x in _projets_listes(client, monde["perso"])}
    assert _connus(monde, "p", vus) == {"perso_p", "recu_moi"}
    assert vus[monde["p"]["perso_p"]]["shared"] is False
    assert vus[monde["p"]["recu_moi"]]["shared"] is True


# --- tableaux ------------------------------------------------------------------

def test_tableaux_rest_dans_une_org_de_travail_l_org_et_mes_tableaux_crees_ici(monde, client):
    # 29/09/2026 : mon tableau perso créé dans A (`perso_a`) s'y liste pour MOI — plus
    # « rien que l'org » ; celui d'un autre créé dans A (`tiers_perso`), jamais.
    vus = _tableaux_listes(client, monde["a"])
    assert _connus(monde, "t", [x["id"] for x in vus]) == {
        "org_a", "equipe", "recu_a", "recu_equipe", "perso_a"}
    assert {x["owner_id"] for x in vus if x["owner_type"] == "user"} == {MOI}, (
        "le tableau personnel d'un autre est listé dans une org")


def test_tableaux_rest_dans_l_org_perso(monde, client):
    # 07/10/2026 : plus `perso_a` (créé dans A) ; `perso_null` (sans org de création)
    # reste, faute de quoi il ne sortirait nulle part.
    vus = {x["id"]: x for x in _tableaux_listes(client, monde["perso"])}
    assert _connus(monde, "t", vus) == {"perso_null", "recu_moi", "lie_recu"}
    assert vus[monde["t"]["recu_moi"]]["shared"] is True
    assert vus[monde["t"]["perso_null"]]["is_personal"] is True


def test_tableaux_face_agent_sous_org(monde, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, session_org
    from oto_mcp.tools import datastore as surface
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: MOI)
    mcp = FastMCP("listes")
    surface.register(mcp)
    outil = asyncio.run(mcp.get_tool("data_list_datastores")).fn
    vus = {}
    for org in (monde["a"], monde["perso"]):
        jeton = session_org.set_call_org(org)
        try:
            vus[org] = _connus(monde, "t", [x["id"] for x in outil()["datastores"]])
        finally:
            session_org.reset_call_org(jeton)
    # 29/09/2026 : même règle que la face REST — mon tableau créé dans A y est listé.
    assert vus[monde["a"]] == {"org_a", "equipe", "recu_a", "recu_equipe", "perso_a"}
    assert vus[monde["perso"]] == {"perso_null", "recu_moi", "lie_recu"}


# --- recherche : « cherchable ⇔ lisible » ----------------------------------------

def test_la_recherche_reste_en_parite_avec_les_listes(monde, client):
    from oto_mcp import ownership, search
    for org in (monde["a"], monde["perso"]):
        tableaux = {int(x["id"]) for x in _tableaux_listes(client, org)}
        assert {int(r["id"]) for r in search._accessible_namespaces(MOI, org)} == tableaux
        projets = {int(x["id"]) for x in _projets_listes(client, org)}
        assert set(ownership.accessible_project_ids(MOI, org)) == projets


# --- pages reçues ----------------------------------------------------------------

def test_une_page_partagee_a_moi_se_liste_dans_l_org_perso(monde):
    from oto_mcp.capabilities.docs import partage
    assert monde["page_recue"] in {d["id"] for d in
                                   partage.recus(MOI, "org", monde["perso"])["docs"]}
    assert monde["page_recue"] not in {d["id"] for d in
                                       partage.recus(MOI, "org", monde["a"])["docs"]}
    # La lentille « moi » ne dépend pas de l'org consultée.
    assert monde["page_recue"] in {d["id"] for d in partage.recus(MOI, "me")["docs"]}


# --- création : à la personne, listée dans l'org perso (29/09/2026) ---------------

def test_un_projet_sans_proprietaire_cree_dans_a_est_perso_et_ne_se_liste_qu_ici(
        monde, client):
    nom = _nom()
    r = client.post("/api/me/projects", json={"op": "create", "name": nom},
                    headers=_entetes(monde["a"]))
    assert r.status_code == 200, r.text
    assert (r.json()["owner_type"], r.json()["context_org_id"]) == ("user", str(monde["a"]))
    # Il se liste pour moi dans A, où je l'ai créé (29/09/2026), et là seulement —
    # plus dans mon org perso (07/10/2026).
    assert nom in {x["name"] for x in _projets_listes(client, monde["a"])}
    assert nom not in {x["name"] for x in _projets_listes(client, monde["perso"])}
    # `owner_type=user` explicite depuis A : permis, même effet.
    r = client.post("/api/me/projects",
                    json={"op": "create", "name": _nom(), "owner_type": "user"},
                    headers=_entetes(monde["a"]))
    assert r.status_code == 200 and r.json()["owner_type"] == "user", r.text


def test_un_tableau_sans_proprietaire_cree_dans_a_est_perso_et_ne_se_liste_qu_ici(
        monde, client):
    # Comme un projet, il se liste pour moi dans A, où je l'ai créé, et là seulement
    # (29/09 et 07/10/2026) ; l'avertissement dit que les autres membres ne le voient pas.
    nom = _nom()
    r = client.post("/api/datastores", json={"datastore": nom}, headers=_entetes(monde["a"]))
    assert r.status_code == 201, r.text
    assert r.json()["owner_type"] == "user"
    assert "ne le voient pas" in r.json()["avertissement"], "dit qui le voit"
    assert nom in {x["datastore"] for x in _tableaux_listes(client, monde["a"])}
    assert nom not in {x["datastore"] for x in _tableaux_listes(client, monde["perso"])}
    # L'org se DEMANDE : ainsi il est à A et s'y liste.
    nom = _nom()
    r = client.post("/api/datastores",
                    json={"datastore": nom, "owner": {"type": "org", "id": monde["a"]}},
                    headers=_entetes(monde["a"]))
    assert r.status_code == 201 and r.json()["owner_type"] == "org", r.text
    assert nom in {x["datastore"] for x in _tableaux_listes(client, monde["a"])}


# --- lentilles « moi » : servies dans toute org (29/09/2026) -------------------------

@pytest.mark.parametrize("appel", [
    ("post", "/api/me/projects", {"op": "list", "scope": "me"}),
    ("get", "/api/me/datastores/shared", None),
    ("post", "/api/me/docs", {"op": "shared_with_me"}),
    ("post", "/api/me/docs", {"op": "shared_with_me", "scope": "me"}),
], ids=["projets-scope-me", "tableaux-shared", "pages-sans-scope", "pages-scope-me"])
def test_une_lentille_moi_est_servie_dans_toute_org_avec_le_meme_contenu(
        monde, client, appel):
    # 29/09/2026 : une org perso est une org comme une autre. Un partage à une personne
    # n'appartient à aucune org : la lentille « moi » rend la même chose partout (elle
    # rendait un 409 hors de l'org perso).
    verbe, chemin, corps = appel

    def _appel(org):
        f = getattr(client, verbe)
        r = f(chemin, headers=_entetes(org), **({"json": corps} if corps else {}))
        assert r.status_code == 200, r.text
        return r.json()

    assert _appel(monde["a"]) == _appel(monde["perso"])


def test_les_lentilles_moi_rendent_ce_qui_est_partage_a_moi(monde, client):
    r = client.post("/api/me/projects", json={"op": "list", "scope": "me"},
                    headers=_entetes(monde["perso"]))
    assert _connus(monde, "p", [x["id"] for x in r.json()["projects"]]) == {"recu_moi"}
    r = client.get("/api/me/datastores/shared", headers=_entetes(monde["perso"]))
    assert _connus(monde, "t", [x["id"] for x in r.json()["datastores"]]) == {
        "recu_moi", "lie_recu"}
    r = client.post("/api/me/docs", json={"op": "shared_with_me", "scope": "me"},
                    headers=_entetes(monde["perso"]))
    assert [d["id"] for d in r.json()["docs"]] == [monde["page_recue"]]


# --- `GET /api/datastores/{datastore}` : la lecture par identifiant ----------------

def test_un_tableau_partage_a_moi_et_lie_a_un_projet_de_a_se_lit_par_numero_depuis_a(
        monde, client):
    ns = monde["t"]["lie_recu"]
    assert ns not in {int(x["id"]) for x in _tableaux_listes(client, monde["a"])}, (
        "contrôle : la liste de A ne le rend pas")
    r = client.get(f"/api/datastores/{ns}", headers=_entetes(monde["a"]))
    assert r.status_code == 200, r.text
    e = r.json()
    assert (e["id"], e["ns_id"]) == (ns, ns)
    assert (e["shared"], e["permission"], e["can_write"]) == (True, "read", False)
    assert (e["owner_type"], e["owner_id"]) == ("org", str(monde["b"]))
    # La même forme qu'une entrée de la liste.
    une_entree = _tableaux_listes(client, monde["perso"])[0]
    assert set(e) == set(une_entree)
    # Mon tableau perso, lui, s'y lit comme possédé.
    r = client.get(f"/api/datastores/{monde['t']['perso_a']}", headers=_entetes(monde["a"]))
    assert r.status_code == 200 and r.json()["shared"] is False, r.text


def test_un_tiers_recoit_404_sur_la_lecture_par_numero(monde):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes

    class _Claims:
        claims = {"sub": VOISIN, "email": f"{VOISIN}@t.invalid", "name": VOISIN}

    class _Verifier:
        async def verify_token(self, token):
            return _Claims()

    voisin = TestClient(api_routes.ViewAsMiddleware(
        Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)),
        verifier=_Verifier()))
    h = {"Authorization": f"Bearer {VOISIN}", "X-Oto-Org": str(monde["a"])}
    for cle in ("lie_recu", "perso_a"):
        r = voisin.get(f"/api/datastores/{monde['t'][cle]}", headers=h)
        assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found"), r.text
    r = voisin.get("/api/datastores/987654321", headers=h)
    assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found")


# --- l'accès par identifiant ne change pas ----------------------------------------

def test_un_perso_cree_dans_a_s_ouvre_depuis_a_par_son_identifiant(monde, client):
    ns = monde["t"]["perso_a"]
    r = client.get(f"/api/datastores/{ns}/rows", headers=_entetes(monde["a"]))
    assert r.status_code == 200, r.text
    r = client.post("/api/me/projects", json={"op": "get", "project_id": monde["p"]["perso_a"]},
                    headers=_entetes(monde["a"]))
    assert r.status_code == 200, r.text
