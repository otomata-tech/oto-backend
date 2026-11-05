"""`GET /api/me/recent-changes` sur la route SERVIE, contre un vrai PostgreSQL (oto#191).

Ce que ce fichier prouve, et rien d'autre :
1. le PÉRIMÈTRE est celui de la lecture — un compte ne voit pas les pages d'un projet
   qu'il ne lit pas (test négatif), ni les procédures d'un palier qu'il n'a pas ; un
   partage de projet ouvre exactement ce qu'il ouvre ;
2. la FUSION pages + procédures est triée par date de modification, la plus récente
   d'abord, et `limit` coupe après le tri ;
3. l'AUTEUR est rendu quand la donnée existe, `null` sinon — jamais déduit ;
4. une liste vide est une 200 vide, y compris SANS org active (l'accueil charge
   cet îlot d'office).

Depuis la décision du 28/09/2026 (ADR 0030 §9), ce qui est à MOI ou partagé à MOI en
personne ne se liste que dans mon org PERSO : ces cas-là sont lus sous `X-Oto-Org`
= l'org perso du compte (`_items_perso`).

Le porteur est identifié par un vérifieur factice dont le bearer EST le sub : ce qu'on
teste est en aval de l'authentification. Une base JETABLE par module (`pg_module_dsn`).
"""
from __future__ import annotations

import os

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@recent.invalid", "name": sub.upper()}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


ADMIN, MEMBRE, AUTRE, TIERS, VIDE = ("usr_rc_admin", "usr_rc_membre", "usr_rc_autre",
                                     "usr_rc_tiers", "usr_rc_vide")


@pytest.fixture(scope="module")
def monde(pg_module_dsn):
    """Deux orgs, une équipe, trois projets, six pages, quatre procédures.

    Org A : ADMIN (org_admin), MEMBRE et AUTRE (org_member) ; MEMBRE seul est dans
    l'équipe G. Org B : TIERS, seul. VIDE a son org à lui, sans contenu.
    """
    pytest.importorskip("psycopg")
    url_avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp import db, group_store, org_store, ownership
        from oto_mcp.db import init_db
        init_db()
        for sub in (ADMIN, MEMBRE, AUTRE, TIERS, VIDE):
            db.upsert_user(sub, email=f"{sub}@recent.invalid", name=sub.upper())
        a = org_store.create_org("Org A", created_by=ADMIN)
        org_store.add_org_member(a, ADMIN, "org_admin")
        org_store.add_org_member(a, MEMBRE, "org_member")
        org_store.add_org_member(a, AUTRE, "org_member")
        b = org_store.create_org("Org B", created_by=TIERS)
        org_store.add_org_member(b, TIERS, "org_admin")
        v = org_store.create_org("Org vide", created_by=VIDE)
        org_store.add_org_member(v, VIDE, "org_admin")
        for sub, oid in ((ADMIN, a), (MEMBRE, a), (AUTRE, a), (TIERS, b), (VIDE, v)):
            org_store.set_active_org(sub, oid)
        g = group_store.create_group(a, "Équipe G", created_by=ADMIN)
        group_store.add_group_member(g, MEMBRE)

        # Ordre d'écriture = ordre chronologique attendu (chaque appel est sa
        # propre transaction, donc son propre NOW()).
        p1 = db.create_project("org", str(a), "Projet A1", created_by=ADMIN)
        alpha = db.create_doc(p1, "Alpha", body_md="a", created_by=ADMIN)
        beta = db.create_doc(p1, "Beta", body_md="b", created_by=ADMIN)
        gamma = db.create_doc(p1, "Gamma", body_md="g", created_by=ADMIN)
        org_store.set_instruction("org", a, "proc-a", "corps A", title="Procédure A",
                                  set_by=ADMIN)
        org_store.set_instruction("user", MEMBRE, "perso-m", "corps M",
                                  title="Ma procédure", set_by=MEMBRE)
        org_store.set_instruction("group", g, "proc-g", "corps G", title="Procédure G",
                                  set_by=MEMBRE)
        p2 = db.create_project("org", str(b), "Projet B1", created_by=TIERS)
        delta = db.create_doc(p2, "Delta", body_md="d", created_by=TIERS)
        org_store.set_instruction("org", b, "proc-b", "corps B", title="Procédure B",
                                  set_by=TIERS)
        # Un projet d'org A PARTAGÉ à TIERS (lecteur) : ce que le partage ouvre, et
        # rien de plus.
        p3 = db.create_project("org", str(a), "Projet A3", created_by=ADMIN)
        epsilon = db.create_doc(p3, "Epsilon", body_md="e", created_by=ADMIN)
        ownership.grant("project", str(p3), "user", TIERS, role="viewer", granted_by=ADMIN)
        # Les deux modifications : Beta par MEMBRE (auteur connu), Gamma par un
        # écrivain qui ne s'est pas nommé (auteur inconnu → null, pas « ADMIN »).
        db.update_doc(beta, body_md="b2", edited_by=MEMBRE)
        db.update_doc(gamma, body_md="g2", edited_by=None)
        yield {"a": a, "b": b, "g": g, "p1": p1, "p2": p2, "p3": p3,
               "alpha": alpha, "beta": beta, "gamma": gamma, "delta": delta,
               "epsilon": epsilon}
    finally:
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant


@pytest.fixture(scope="module")
def client(monde):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


def _items(client, sub: str, **params) -> list[dict]:
    r = client.get("/api/me/recent-changes", headers=_h(sub), params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"items", "limit"}
    return body["items"]


def _items_perso(sub: str, **params) -> list[dict]:
    """La même route, consultée dans la MAISON de `sub` — son org active (`X-Oto-Org`, lu par
    `ViewAsMiddleware` comme en production)."""
    from oto_mcp import org_store
    from oto_mcp.api import routes as api_routes
    vue = TestClient(api_routes.ViewAsMiddleware(
        Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)),
        verifier=_Verifier()))
    r = vue.get("/api/me/recent-changes", params=params,
                headers={**_h(sub), "X-Oto-Org": str(org_store.ensure_home_org(sub))})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _cles(items) -> set[tuple[str, str]]:
    return {(i["type"], i["title"]) for i in items}


# ── 1. Périmètre ─────────────────────────────────────────────────────────────

def test_l_admin_voit_les_pages_de_son_org_et_les_procedures_de_tous_les_paliers_de_l_org(
        client, monde):
    items = _items(client, ADMIN)
    assert _cles(items) == {("doc", "Alpha"), ("doc", "Beta"), ("doc", "Gamma"),
                            ("doc", "Epsilon"),
                            ("procedure", "Procédure A"), ("procedure", "Procédure G")}
    # Rien de l'org B, et pas la procédure PERSONNELLE d'un autre membre.
    assert ("doc", "Delta") not in _cles(items)
    assert ("procedure", "Procédure B") not in _cles(items)
    assert ("procedure", "Ma procédure") not in _cles(items)


def test_un_membre_hors_de_l_equipe_ne_voit_ni_sa_procedure_ni_celle_d_un_autre(
        client, monde):
    """AUTRE est membre de l'org A mais pas de l'équipe G : les pages de l'org, la
    procédure d'org, et c'est tout — même règle que `roles.can_read_group`."""
    cles = _cles(_items(client, AUTRE))
    assert {("doc", "Alpha"), ("doc", "Beta"), ("doc", "Gamma"), ("doc", "Epsilon"),
            ("procedure", "Procédure A")} <= cles
    assert ("procedure", "Procédure G") not in cles
    assert ("procedure", "Ma procédure") not in cles


def test_le_membre_de_l_equipe_voit_la_sienne_et_celle_de_l_equipe(client, monde):
    cles = _cles(_items(client, MEMBRE))
    assert ("procedure", "Procédure G") in cles
    assert ("procedure", "Procédure B") not in cles
    # SA procédure personnelle : dans sa maison, qui est son org ACTIVE depuis qu'il
    # n'y a plus d'org perso (05/11/2026) — ici A.
    assert ("procedure", "Ma procédure") in cles
    assert ("procedure", "Ma procédure") in _cles(_items_perso(MEMBRE))


def test_NEGATIF_un_compte_ne_voit_pas_les_pages_d_un_projet_qu_il_ne_lit_pas(client, monde):
    """TIERS n'est pas dans l'org A : aucune page de Projet A1, aucune procédure de A.
    Le seul contenu d'A qui lui parvient est le projet qu'on lui a PARTAGÉ — et
    seulement lui."""
    cles = _cles(_items(client, TIERS))
    assert ("doc", "Alpha") not in cles
    assert ("doc", "Beta") not in cles
    assert ("doc", "Gamma") not in cles
    assert ("procedure", "Procédure A") not in cles
    assert ("procedure", "Procédure G") not in cles
    # Le projet partagé à TIERS en personne se lit dans sa maison (son org active, B,
    # depuis le 05/11/2026), et lui seul de A.
    assert cles == {("doc", "Delta"), ("procedure", "Procédure B"), ("doc", "Epsilon")}
    assert _cles(_items_perso(TIERS)) == cles


def test_le_perimetre_est_celui_de_la_lecture_d_une_page(client, monde):
    """Chaque page listée s'OUVRE par le même compte ; chaque page refusée à l'ouverture
    est absente de la liste — la liste n'est ni plus large ni plus étroite que la
    lecture (`docs.common.can`, le seam de `oto_doc op=get`), une fois réunies l'org
    consultée et l'org perso, où se range ce qui est partagé en personne (28/09/2026)."""
    from oto_mcp.capabilities.docs import common as docs_common
    for sub in (ADMIN, AUTRE, TIERS):
        listees = {i["project"]["id"] for i in _items(client, sub) + _items_perso(sub)
                   if i["type"] == "doc"}
        for pid in (monde["p1"], monde["p2"], monde["p3"]):
            assert (pid in listees) == docs_common.can(sub, pid, "read"), (sub, pid)


def test_chaque_procedure_listee_s_ouvre_par_son_id(client, monde):
    """Le versant procédures de « jamais plus large » : chaque procédure listée s'ouvre
    par le même compte sur la route SERVIE de lecture par id, et une procédure d'équipe
    absente de la liste d'un membre hors équipe lui est refusée à l'ouverture."""
    for sub in (ADMIN, MEMBRE, AUTRE, TIERS):
        for i in _items(client, sub):
            if i["type"] != "procedure":
                continue
            r = client.get(f"/api/me/guides/{i['id']}", headers=_h(sub))
            assert r.status_code == 200, (sub, i["title"], r.status_code, r.text)
    proc_g = next(i for i in _items(client, ADMIN) if i["title"] == "Procédure G")
    r = client.get(f"/api/me/guides/{proc_g['id']}", headers=_h(AUTRE))
    assert r.status_code != 200, r.text


# ── 2. Fusion et tri ─────────────────────────────────────────────────────────

def test_la_fusion_est_triee_par_date_de_modification_la_plus_recente_d_abord(client, monde):
    items = _items(client, ADMIN)
    dates = [i["updated_at"] for i in items]
    assert dates == sorted(dates, reverse=True)
    # Les deux dernières écritures du monde sont les modifications de Beta puis Gamma.
    assert [i["title"] for i in items[:2]] == ["Gamma", "Beta"]
    # Une page et une procédure se mêlent dans la MÊME liste, triées ensemble.
    types = [i["type"] for i in items]
    assert "doc" in types and "procedure" in types
    assert types.index("procedure") < len(types) - 1  # pas reléguées en queue


def test_limit_coupe_apres_le_tri(client, monde):
    deux = _items(client, ADMIN, limit=2)
    assert [i["title"] for i in deux] == ["Gamma", "Beta"]
    assert client.get("/api/me/recent-changes", headers=_h(ADMIN),
                      params={"limit": 2}).json()["limit"] == 2


@pytest.mark.parametrize("limit", ["0", "51", "abc", "-1"])
def test_un_limit_hors_bornes_est_refuse_pas_plafonne(client, monde, limit):
    r = client.get("/api/me/recent-changes", headers=_h(ADMIN), params={"limit": limit})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_input"


def test_un_parametre_inconnu_est_refuse(client, monde):
    r = client.get("/api/me/recent-changes", headers=_h(ADMIN), params={"since": "7"})
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_fields"


# ── 3. Auteur ────────────────────────────────────────────────────────────────

def test_l_auteur_est_celui_de_la_revision_appariee_ou_null(client, monde):
    par_titre = {i["title"]: i for i in _items(client, ADMIN)}
    # Jamais modifiée : la modification EST la création.
    assert par_titre["Alpha"]["author"] == {"sub": ADMIN, "name": ADMIN.upper()}
    # Modifiée par MEMBRE : la révision porte son nom.
    assert par_titre["Beta"]["author"] == {"sub": MEMBRE, "name": MEMBRE.upper()}
    # Modifiée par un écrivain anonyme : `null`, pas le créateur.
    assert par_titre["Gamma"]["author"] is None
    # Une procédure : `set_by` de la version courante, appariée.
    assert par_titre["Procédure A"]["author"] == {"sub": ADMIN, "name": ADMIN.upper()}


def test_une_page_deplacee_n_a_pas_d_auteur_deduit(client, monde):
    """`move_doc` touche `updated_at` sans révision : la date bouge, l'auteur devient
    `null` — l'ancien auteur n'est PAS reconduit sur une modification qu'il n'a pas faite."""
    from oto_mcp import db
    db.move_doc(monde["alpha"], monde["beta"])
    par_titre = {i["title"]: i for i in _items(client, ADMIN)}
    assert par_titre["Alpha"]["author"] is None
    assert [i["title"] for i in _items(client, ADMIN, limit=1)] == ["Alpha"]


def test_la_forme_d_un_element(client, monde):
    par_titre = {i["title"]: i for i in _items(client, MEMBRE) + _items_perso(MEMBRE)}
    page = par_titre["Beta"]
    assert page["type"] == "doc" and page["id"] == monde["beta"]
    assert page["project"] == {"id": monde["p1"], "name": "Projet A1"}
    assert page["slug"] is None and page["scope"] is None
    proc = par_titre["Ma procédure"]
    assert proc["type"] == "procedure" and isinstance(proc["id"], int)
    assert proc["project"] is None
    assert proc["slug"] == "perso-m" and proc["scope"] == "user"
    assert {i["scope"] for i in par_titre.values() if i["type"] == "procedure"} == {
        "user", "org", "group"}


# ── 4. Vide ──────────────────────────────────────────────────────────────────

def test_une_org_sans_contenu_rend_une_liste_vide_en_200(client, monde):
    r = client.get("/api/me/recent-changes", headers=_h(VIDE))
    assert r.status_code == 200
    assert r.json() == {"items": [], "limit": 20}


def test_sans_org_active_la_liste_est_vide_en_200_jamais_un_400(client, monde, monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    r = client.get("/api/me/recent-changes", headers=_h(ADMIN), params={"limit": 5})
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "limit": 5}
