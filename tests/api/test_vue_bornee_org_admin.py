"""« Voir en tant que » d'un org_admin, BORNÉ à son org (oto#270) — contre PostgreSQL.

Un membre vit dans deux orgs, O et P, et a son palier personnel : des projets, des
tableaux, des partages reçus, des clés. L'org_admin de O le consulte : il voit ce que
le membre voit DANS O (dont la part perso qui descend dans O), rien de P, aucun secret.
Et le témoin : hors vue, les mêmes lectures et les fonctions du seam `ownership` rendent
ce qu'elles rendaient — les chemins normaux ne bougent pas.

Les requêtes passent par la vraie table de routes (`make_routes`), l'adaptateur des
capacités et le vrai `ViewAsMiddleware`. Le porteur est identifié par un vérifieur
factice dont le bearer EST le sub : ce qu'on teste est en aval de l'authentification.
"""
from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

ADMIN, MEMBRE, SIMPLE, DEHORS = "vb-admin", "vb-membre", "vb-simple", "vb-dehors"
# Opérateur plateforme (`role='admin'`), membre ORDINAIRE de O — sert deux volets
# oto#270 suite : `is_platform_operator` sur la liste des membres, et la vue
# d'OPÉRATEUR (non bornée, ≠ vue bornée de l'org_admin) sur `/api/me`.
OPERATEUR = "vb-operateur"
SECRET_O, SECRET_P = "hunter-SECRET-O-7f3a91", "hunter-SECRET-P-2c8d44"
MARQUE_P = "zzP"   # tout ce qui vit hors de O porte cette marque
_CLE_MAITRE = base64.b64encode(b"\x27" * 32).decode()


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@vue-bornee.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import credentials_store as cs
    from oto_mcp import db, group_store, org_store
    mp = pytest.MonkeyPatch()
    mp.setenv("OTO_MCP_MASTER_KEY", _CLE_MAITRE)
    for sub in (ADMIN, MEMBRE, SIMPLE, DEHORS, OPERATEUR):
        db.upsert_user(sub, email=f"{sub}@vue-bornee.invalid", name=sub)
    db.set_user_role(OPERATEUR, "admin")
    o = org_store.create_org("Org O", created_by=ADMIN)
    p = org_store.create_org(f"{MARQUE_P} Org P", created_by=DEHORS)
    org_store.add_org_member(o, ADMIN, "org_admin")
    org_store.add_org_member(o, MEMBRE, "org_member")
    org_store.add_org_member(o, SIMPLE, "org_member")
    org_store.add_org_member(o, OPERATEUR, "org_member")
    org_store.add_org_member(p, DEHORS, "org_admin")
    org_store.add_org_member(p, MEMBRE, "org_member")
    # La maison du membre est P : `/api/me` ne doit pas la nommer dans la vue.
    org_store.set_active_org(MEMBRE, p)
    g = group_store.create_group(o, "Équipe O", created_by=ADMIN)
    h = group_store.create_group(p, f"{MARQUE_P} Équipe P", created_by=DEHORS)
    group_store.add_group_member(g, MEMBRE)
    group_store.add_group_member(h, MEMBRE)

    pr = {
        "org_o": db.create_project("org", str(o), "Projet de O", created_by=ADMIN),
        "equipe_o": db.create_project("group", str(g), "Projet d'équipe O", created_by=ADMIN),
        "perso_o": db.create_project("user", MEMBRE, "Perso rangé dans O",
                                     created_by=MEMBRE, context_org_id=o),
        "org_p": db.create_project("org", str(p), f"{MARQUE_P} projet de P",
                                   created_by=DEHORS),
        "perso_p": db.create_project("user", MEMBRE, f"{MARQUE_P} perso rangé dans P",
                                     created_by=MEMBRE, context_org_id=p),
        "partage_p": db.create_project("org", str(p), f"{MARQUE_P} partagé en propre",
                                       created_by=DEHORS),
    }
    db.grant_resource("project", str(pr["partage_p"]), "user", MEMBRE, "read",
                      granted_by=DEHORS)
    ds = {
        "tab_o": db.create_datastore("org", str(o), "tab_o"),
        # Sans org de création (legacy) : sa maison est l'org perso du membre.
        "tab_perso": db.create_datastore("user", MEMBRE, "tab_perso"),
        "tab_perso_o": db.create_datastore("user", MEMBRE, "tab_perso_o",
                                           context_org_id=o),
        "tab_perso_p": db.create_datastore("user", MEMBRE, f"{MARQUE_P}_tab_perso_p",
                                           context_org_id=p),
        "tab_p": db.create_datastore("org", str(p), f"{MARQUE_P}_tab_p"),
        "tab_p_partage": db.create_datastore("org", str(p), f"{MARQUE_P}_tab_partage"),
    }
    db.grant_resource("datastore_namespace", str(ds["tab_p_partage"]), "user", MEMBRE,
                      "read", granted_by=DEHORS)
    cs.set_credential(cs.MEMBER, cs.member_id(o, MEMBRE), "hunter", SECRET_O, set_by=MEMBRE)
    cs.set_credential(cs.MEMBER, cs.member_id(p, MEMBRE), "hunter", SECRET_P, set_by=MEMBRE)
    jeton = db.create_api_token(MEMBRE, label=f"{MARQUE_P} jeton")
    yield {"o": o, "p": p, "g": g, "h": h, "pr": pr, "ds": ds, "jeton": jeton}
    mp.undo()


@pytest.fixture(scope="module")
def client(monde):
    from oto_mcp.api import routes as api_routes
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.ViewAsMiddleware(app, verifier=_Verifier()))


def _vue(monde, qui=ADMIN, cible=MEMBRE, org=None) -> dict:
    return {"Authorization": f"Bearer {qui}", "X-Oto-View-As": cible,
            "X-Oto-Org": str(org if org is not None else monde["o"])}


def _soi(sub, org) -> dict:
    return {"Authorization": f"Bearer {sub}", "X-Oto-Org": str(org)}


def _noms(r) -> set:
    return {p["name"] for p in r.json()["projects"]}


# ── Ce que la vue montre ─────────────────────────────────────────────────────

def test_la_vue_montre_ce_que_le_membre_voit_dans_o(client, monde):
    h = _vue(monde)
    me = client.get("/api/me", headers=h)
    assert me.status_code == 200, me.text
    assert me.json()["sub"] == MEMBRE
    assert me.json()["view_as_read_only"] is True
    assert me.json()["view_as_bound_org"] == monde["o"]
    # Dérivé de `_LECTURES_VUE_BORNEE` : un préfixe refusé connu y est, une lecture
    # ouverte de la vue (celle-là même qu'on vient d'appeler) n'y est jamais couverte.
    refus = me.json()["view_as_refused_prefixes"]
    assert "/api/admin/users" in refus
    assert not any("/api/me".startswith(p) for p in refus)
    assert me.json()["active_org"] == monde["o"]
    # La maison du membre est P : masquée, jamais nommée.
    assert me.json()["home_org"] is None and me.json()["home_org_name"] is None

    orgs = client.get("/api/me/orgs", headers=h).json()["orgs"]
    assert [x["id"] for x in orgs] == [monde["o"]]

    # O n'est pas l'org perso du membre : la liste rend O (28/09/2026) et son projet
    # perso CRÉÉ dans O (29/09/2026), qui descend dans O — vu en vue comme par lui.
    r = client.post("/api/me/projects", json={"op": "list"}, headers=h)
    assert r.status_code == 200, r.text
    assert _noms(r) == {"Projet de O", "Projet d'équipe O", "Perso rangé dans O"}
    # Lentille « moi » : servie dans toute org depuis le 29/09/2026 (une org perso est
    # une org comme une autre). En vue BORNÉE à O, elle ne rend rien de P : le partage
    # fait au membre sur un projet de P reste hors de la vue.
    r = client.post("/api/me/projects", json={"op": "list", "scope": "me"}, headers=h)
    assert r.status_code == 200, r.text
    assert _noms(r) == set()
    assert MARQUE_P not in r.text

    r = client.post("/api/me/projects",
                    json={"op": "get", "project_id": monde["pr"]["perso_o"]}, headers=h)
    assert r.status_code == 200, r.text

    tableaux = client.get("/api/datastores", headers=h)
    assert tableaux.status_code == 200, tableaux.text
    assert {t["datastore"] for t in tableaux.json()["datastores"]} == {"tab_o", "tab_perso_o"}

    cle = client.get("/api/settings/api-keys/hunter", headers=h)
    assert cle.status_code == 200, cle.text
    assert SECRET_O not in cle.text and SECRET_P not in cle.text


@pytest.mark.parametrize("cle", ["org_p", "perso_p", "partage_p"])
def test_un_projet_hors_de_o_est_introuvable_dans_la_vue(client, monde, cle):
    r = client.post("/api/me/projects",
                    json={"op": "get", "project_id": monde["pr"][cle]}, headers=_vue(monde))
    assert r.status_code == 404, r.text
    assert MARQUE_P not in r.text


@pytest.mark.parametrize("cle", ["tab_p", "tab_p_partage", "tab_perso_p", "tab_perso"])
@pytest.mark.parametrize("suffixe", ["rows", "rows/export.csv"])
def test_un_tableau_hors_de_o_est_introuvable_dans_la_vue(client, monde, cle, suffixe):
    """Un tableau PERSO du membre se range dans l'org où il l'a créé (ou, sans org de
    création, dans son org perso) : créé dans P, il ne se lit pas par son numéro dans
    une vue bornée à O — ni ses lignes, ni son export."""
    r = client.get(f"/api/datastores/{monde['ds'][cle]}/{suffixe}", headers=_vue(monde))
    assert r.status_code == 404, r.text
    assert MARQUE_P not in r.text


@pytest.mark.parametrize("suffixe", ["rows", "rows/export.csv"])
def test_un_tableau_perso_cree_dans_o_se_lit_dans_la_vue(client, monde, suffixe):
    r = client.get(f"/api/datastores/{monde['ds']['tab_perso_o']}/{suffixe}",
                   headers=_vue(monde))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("cle", ["tab_perso", "tab_perso_o", "tab_perso_p"])
def test_hors_vue_le_membre_lit_tous_ses_tableaux_perso(client, monde, cle):
    r = client.get(f"/api/datastores/{monde['ds'][cle]}/rows",
                   headers=_soi(MEMBRE, monde["o"]))
    assert r.status_code == 200, r.text


@pytest.fixture
def noeuds(client, monde):
    """Deux nœuds NÉS ICI, perso du membre, hors de tout projet : une page et un tableau
    à une ligne. Sans org de création, leur maison est l'org perso du membre."""
    h = _soi(MEMBRE, monde["o"])
    def creer(corps):
        r = client.post("/api/me/nodes/edit", json={"op": "create", **corps}, headers=h)
        assert r.status_code == 200, r.text
        return r.json()["id"]
    page = creer({"kind": "page", "title": f"{MARQUE_P} page perso", "body_md": "secret"})
    tableau = creer({"kind": "tableau", "title": f"{MARQUE_P} tableau perso"})
    creer({"kind": "ligne", "parent_id": tableau, "data": {"x": f"{MARQUE_P} cellule"}})
    return {"page": page, "tableau": tableau}


def test_un_noeud_perso_hors_projet_ne_sort_pas_dans_la_vue(client, monde, noeuds):
    for chemin in (f"/api/me/nodes/{noeuds['page']}", f"/api/me/nodes/{noeuds['tableau']}",
                   f"/api/me/nodes/{noeuds['tableau']}/rows"):
        r = client.get(chemin, headers=_vue(monde))
        assert r.status_code == 404, (chemin, r.text)
        assert MARQUE_P not in r.text


def test_hors_vue_le_membre_lit_ses_noeuds_perso(client, monde, noeuds):
    h = _soi(MEMBRE, monde["o"])
    for chemin in (f"/api/me/nodes/{noeuds['page']}",
                   f"/api/me/nodes/{noeuds['tableau']}/rows"):
        r = client.get(chemin, headers=h)
        assert r.status_code == 200, (chemin, r.text)


def test_les_lignes_d_un_tableau_ne_ici_ne_se_lisent_pas_sans_droit(client, monde, noeuds):
    """Un tableau né ici n'a pas de store pour garder ses lignes : leur lecture passe
    par la garde de sa fiche. Un autre membre de O ne les lit pas par leur identifiant."""
    r = client.get(f"/api/me/nodes/{noeuds['tableau']}/rows",
                   headers=_soi(SIMPLE, monde["o"]))
    assert r.status_code == 404, r.text
    assert MARQUE_P not in r.text


def test_rien_de_p_ni_aucun_secret_dans_les_lectures_ouvertes(client, monde):
    h = _vue(monde)
    corps = []
    for chemin in ("/api/me", "/api/me/orgs", "/api/me/shell", "/api/me/recent-changes",
                   "/api/me/guides", "/api/me/instructions", "/api/me/agent-context",
                   "/api/me/connectors", "/api/datastores", f"/api/orgs/{monde['o']}",
                   f"/api/groups/{monde['g']}", "/api/settings/api-keys/hunter"):
        r = client.get(chemin, headers=h)
        assert r.status_code == 200, (chemin, r.status_code, r.text[:300])
        corps.append(r.text)
    for op in ({"op": "list"}, {"op": "list_templates"}):
        r = client.post("/api/me/projects", json=op, headers=h)
        assert r.status_code == 200, r.text
        corps.append(r.text)
    r = client.post("/api/me/docs", json={"op": "shared_with_me", "scope": "org"}, headers=h)
    assert r.status_code == 200, r.text
    corps.append(r.text)
    # La lentille « moi » des pages : servie dans O (29/09/2026, org perso = org), sans
    # rien de P — c'est la vue bornée qui la tient.
    r = client.post("/api/me/docs", json={"op": "shared_with_me"}, headers=h)
    assert r.status_code == 200, r.text
    corps.append(r.text)
    tout = "\n".join(corps)
    assert MARQUE_P not in tout
    assert SECRET_O not in tout and SECRET_P not in tout and monde["jeton"] not in tout


# ── Ce que la vue refuse ─────────────────────────────────────────────────────

@pytest.mark.parametrize("chemin", [
    "/api/me/tokens", "/api/me/connector-accounts/grants", "/api/me/datastores/shared",
    "/api/me/model-subscriptions", "/api/me/connector-instances"])
def test_les_listes_compte_entier_sont_refusees(client, monde, chemin):
    r = client.get(chemin, headers=_vue(monde))
    assert (r.status_code, r.json()["error"]) == (403, "view_as_hors_org"), r.text


def test_l_org_et_l_equipe_de_p_sont_refusees(client, monde):
    for chemin in (f"/api/orgs/{monde['p']}", f"/api/groups/{monde['h']}"):
        r = client.get(chemin, headers=_vue(monde))
        assert (r.status_code, r.json()["error"]) == (403, "view_as_hors_org"), chemin


def test_une_org_resolue_hors_de_o_est_refusee(client, monde):
    """La règle d'autz résout l'org depuis un champ d'entrée : l'adaptateur la refuse."""
    r = client.post("/api/me/functions", json={"op": "list", "org": monde["p"]},
                    headers=_vue(monde))
    assert (r.status_code, r.json()["error"]) == (403, "view_as_hors_org"), r.text


def test_les_runs_ouverts_du_compte_sont_refuses(client, monde):
    r = client.post("/api/me/projects", json={"op": "runs"}, headers=_vue(monde))
    assert (r.status_code, r.json()["error"]) == (403, "view_as_hors_org"), r.text


def test_l_org_admin_ne_peut_rien_ecrire(client, monde):
    from oto_mcp import db
    avant = len(db.list_projects_for_owners([("user", MEMBRE)]))
    for entetes in (_vue(monde), {**_vue(monde), "X-Oto-View-As-Write": "1"}):
        r = client.post("/api/me/projects", json={"op": "create", "name": "intrus"},
                        headers=entetes)
        assert r.status_code == 403, r.text
    assert r.json()["error"] == "view_as_write_forbidden"
    assert len(db.list_projects_for_owners([("user", MEMBRE)])) == avant


def test_un_org_member_est_refuse(client, monde):
    r = client.get("/api/me", headers=_vue(monde, qui=SIMPLE))
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")


def test_une_cible_hors_de_o_est_refusee(client, monde):
    r = client.get("/api/me", headers=_vue(monde, cible=DEHORS))
    assert (r.status_code, r.json()["error"]) == (403, "view_as_hors_org")


def test_l_admin_de_o_n_ouvre_pas_de_vue_dans_p(client, monde):
    r = client.get("/api/me", headers=_vue(monde, org=monde["p"]))
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")


# ── Le témoin : hors vue, rien ne change ─────────────────────────────────────

def test_hors_vue_le_membre_lit_comme_avant(client, monde):
    h = _soi(MEMBRE, monde["o"])
    me = client.get("/api/me", headers=h).json()
    assert me["view_as_read_only"] is False and me["home_org"] == monde["p"]
    assert me["view_as_bound_org"] is None
    assert me["view_as_refused_prefixes"] is None
    # O, P et son espace personnel : toutes ses orgs.
    assert {monde["o"], monde["p"]} < {
        x["id"] for x in client.get("/api/me/orgs", headers=h).json()["orgs"]}
    # O n'est pas son org perso : O (28/09/2026) et son perso créé dans O (29/09/2026).
    r = client.post("/api/me/projects", json={"op": "list"}, headers=h)
    assert _noms(r) == {"Projet de O", "Projet d'équipe O", "Perso rangé dans O"}
    # La lentille « moi » : servie dans toute org (29/09/2026, org perso = org) — hors
    # vue, dans O comme dans son org perso, elle rend ce qui est partagé à lui.
    r = client.post("/api/me/projects", json={"op": "list", "scope": "me"}, headers=h)
    assert r.status_code == 200, r.text
    assert _noms(r) == {f"{MARQUE_P} partagé en propre"}
    from oto_mcp import org_store
    r = client.post("/api/me/projects", json={"op": "list", "scope": "me"},
                    headers=_soi(MEMBRE, org_store.ensure_personal_org(MEMBRE)))
    assert _noms(r) == {f"{MARQUE_P} partagé en propre"}
    # Règle d'avant : ma ressource perso et mon partage personnel me suivent partout.
    for cle in ("perso_p", "partage_p"):
        r = client.post("/api/me/projects",
                        json={"op": "get", "project_id": monde["pr"][cle]}, headers=h)
        assert r.status_code == 200, (cle, r.text)
    r = client.post("/api/me/projects",
                    json={"op": "get", "project_id": monde["pr"]["org_p"]}, headers=h)
    assert (r.status_code, r.json()["error"]) == (403, "wrong_org_context")
    tableaux = {t["datastore"] for t in
                client.get("/api/datastores", headers=h).json()["datastores"]}
    # Le partage reçu en propre et le tableau perso sans org de création ne se listent
    # que dans l'org perso ; le perso créé dans O se liste dans O.
    assert tableaux == {"tab_o", "tab_perso_o"}
    assert client.get("/api/me/tokens", headers=h).status_code == 200


def _dans_la_vue(monde, fn):
    from oto_mcp import session_org
    jeton = session_org.set_view_as_bound_org(monde["o"])
    try:
        return fn()
    finally:
        session_org.reset_view_as_bound_org(jeton)


def test_le_seam_est_identique_hors_vue_et_borne_en_vue(monde):
    from oto_mcp import org_store
    from oto_mcp import ownership as ow
    o, p, pr, ds = monde["o"], monde["p"], monde["pr"], monde["ds"]
    cas = {
        # appel : (hors vue — la règle d'avant, en vue bornée à O)
        "visible perso rangé dans P": (
            lambda: ow.visible_in_org(MEMBRE, o, "project", str(pr["perso_p"])), True, False),
        "visible partage personnel de P": (
            lambda: ow.visible_in_org(MEMBRE, o, "project", str(pr["partage_p"])), True, False),
        "visible perso rangé dans O": (
            lambda: ow.visible_in_org(MEMBRE, o, "project", str(pr["perso_o"])), True, True),
        # Un tableau perso se range comme un projet perso : dans l'org où il a été
        # créé, sinon dans l'org perso du membre (ce n'est pas O).
        "visible tableau perso sans org de création": (
            lambda: ow.visible_in_org(MEMBRE, o, ow.TYPE_RESSOURCE_DATASTORE,
                                      str(ds["tab_perso"])), True, False),
        "visible tableau perso créé dans O": (
            lambda: ow.visible_in_org(MEMBRE, o, ow.TYPE_RESSOURCE_DATASTORE,
                                      str(ds["tab_perso_o"])), True, True),
        "visible tableau perso créé dans P": (
            lambda: ow.visible_in_org(MEMBRE, o, ow.TYPE_RESSOURCE_DATASTORE,
                                      str(ds["tab_perso_p"])), True, False),
        "visible dans P": (
            lambda: ow.visible_in_org(MEMBRE, p, "project", str(pr["org_p"])), True, False),
        "contenu projet de P": (
            lambda: ow.can_access(MEMBRE, "project", str(pr["org_p"])), True, False),
        "contenu projet de O": (
            lambda: ow.can_access(MEMBRE, "project", str(pr["org_o"])), True, True),
        "contenu tableau partagé de P": (
            lambda: ow.can_access(MEMBRE, ow.TYPE_RESSOURCE_DATASTORE,
                                  str(ds["tab_p_partage"])), True, False),
        "portée de l'org P": (
            lambda: ow.owner_in_scope(MEMBRE, p, ("org", str(p))), True, False),
        # Hors vue : toutes ses orgs (O, P et son espace personnel) ; en vue : O seule.
        "orgs de l'acteur": (
            lambda: ow.accessor_scope(MEMBRE).org_ids,
            [int(x["org_id"]) for x in org_store.list_orgs_for_user(MEMBRE)], [o]),
        "équipes de l'acteur": (
            lambda: sorted(ow.accessor_scope(MEMBRE).group_ids),
            sorted([monde["g"], monde["h"]]), [monde["g"]]),
        # Dans O (pas son org perso) : O, et son perso créé dans O (29/09/2026) — en
        # vue comme hors vue, en parité avec la liste.
        "projets cherchables dans O": (
            lambda: sorted(ow.accessible_project_ids(MEMBRE, o)),
            sorted([pr["org_o"], pr["equipe_o"], pr["perso_o"]]),
            sorted([pr["org_o"], pr["equipe_o"], pr["perso_o"]])),
    }
    for nom, (appel, hors_vue, en_vue) in cas.items():
        assert appel() == hors_vue, f"hors vue — {nom}"
        assert _dans_la_vue(monde, appel) == en_vue, f"en vue — {nom}"
    # Les principals du contexte ne changent pas : le bornage vit dans les règles.
    assert _dans_la_vue(monde, lambda: ow.active_org_principals(MEMBRE, o)) == \
        ow.active_org_principals(MEMBRE, o)
    assert {o, p} < set(ow.accessor_scope(MEMBRE).org_ids)


# ── Vue d'OPÉRATEUR : non bornée, les trois champs le disent ─────────────────

def test_la_vue_d_operateur_n_est_pas_bornee(client, monde):
    """Un opérateur plateforme (`role=admin`) « voit en tant que » sans org de vue :
    `view_as_read_only` vaut vrai (même mécanique que la vue bornée), mais
    `view_as_bound_org`/`view_as_refused_prefixes` — propres à la vue BORNÉE de
    l'org_admin (oto#270 suite) — restent `null` : les deux mécanismes ne se
    confondent pas, même si tous deux passent par `ViewAsMiddleware`."""
    h = {"Authorization": f"Bearer {OPERATEUR}", "X-Oto-View-As": MEMBRE}
    me = client.get("/api/me", headers=h)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["view_as_read_only"] is True
    assert body["view_as_bound_org"] is None
    assert body["view_as_refused_prefixes"] is None


# ── `OrgMemberEntry.is_platform_operator` : org_admin seul, une requête ──────

def test_is_platform_operator_vrai_pour_l_operateur_faux_pour_un_membre(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}", headers=_soi(ADMIN, monde["o"]))
    assert r.status_code == 200, r.text
    par_sub = {m["sub"]: m["is_platform_operator"] for m in r.json()["members"]}
    assert par_sub[OPERATEUR] is True
    assert par_sub[MEMBRE] is False
    assert par_sub[SIMPLE] is False
    assert par_sub[ADMIN] is False


def test_is_platform_operator_masque_a_un_simple_membre(client, monde):
    r = client.get(f"/api/orgs/{monde['o']}", headers=_soi(MEMBRE, monde["o"]))
    assert r.status_code == 200, r.text
    assert all(m["is_platform_operator"] is None for m in r.json()["members"])


def test_is_platform_operator_une_seule_requete_pour_toute_la_liste(client, monde, monkeypatch):
    """`access.is_platform_operator` (1 fetch par sub) n'est JAMAIS appelée pour ce
    champ — le rôle est lu sur la ligne `users` déjà tirée par `_members` pour
    email/name : zéro requête ajoutée, jamais une par membre."""
    from oto_mcp import access

    def _interdit(sub):
        raise AssertionError(f"access.is_platform_operator appelée pour {sub} : "
                             "le champ doit réutiliser la ligne déjà lue, pas refetcher.")
    monkeypatch.setattr(access, "is_platform_operator", _interdit)
    r = client.get(f"/api/orgs/{monde['o']}", headers=_soi(ADMIN, monde["o"]))
    assert r.status_code == 200, r.text
    assert {m["sub"] for m in r.json()["members"]} == {ADMIN, MEMBRE, SIMPLE, OPERATEUR}


def test_la_fiche_lit_ses_membres_une_fois_sans_get_user_par_membre(client, monde,
                                                                    monkeypatch):
    """La liste des membres tient en UNE lecture jointe à `users` — plus de
    `db.get_user` par membre — et `member_count` la réutilise au lieu de la relire."""
    from oto_mcp import db, org_store

    vus, lectures = [], []
    vrai_get_user = db.get_user
    vraie_liste = org_store.list_org_members_with_users

    def _get_user(sub):
        vus.append(sub)
        return vrai_get_user(sub)

    def _liste(org_id):
        lectures.append(org_id)
        return vraie_liste(org_id)

    monkeypatch.setattr(db, "get_user", _get_user)
    monkeypatch.setattr(org_store, "list_org_members_with_users", _liste)
    r = client.get(f"/api/orgs/{monde['o']}", headers=_soi(ADMIN, monde["o"]))
    assert r.status_code == 200, r.text
    corps = r.json()
    assert lectures == [monde["o"]]
    assert corps["org"]["member_count"] == len(corps["members"]) == 4
    # Seul le porteur peut être relu (authentification, rôle) — jamais un autre membre.
    assert set(vus) <= {ADMIN}
    par_sub = {m["sub"]: m for m in corps["members"]}
    assert par_sub[MEMBRE]["email"] == f"{MEMBRE}@vue-bornee.invalid"
    assert par_sub[MEMBRE]["name"] == MEMBRE
