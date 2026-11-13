"""Les favoris d'une org (`capabilities/orgs/favoris.py`), sur une vraie base — par les
handlers ET par la face REST servie (`make_routes`, corps du POST, chemin et query string
du DELETE).

Portée `org` : un membre épingle une page ou un tableau de l'org, tous les membres le
voient, dans l'ordre de pose, et épingler deux fois ne fait qu'un favori ; un favori
n'accorde rien — l'objet perso d'un membre, partagé à un seul collègue, reste invisible
aux autres, qui ne peuvent pas l'épingler non plus ; un objet lisible par son seul auteur
est refusé (409), accepté en `me`, et accepté en `org` une fois partagé ; un objet d'une
autre org ne s'épingle pas (sauf partagé à l'org), même réponse que pour un id absent ;
seuls l'auteur et un admin de l'org désépinglent (403 pour un autre membre qui le voit,
404 pour celui qui ne lit pas la cible).

Portée `me` : le favori personnel n'est rendu qu'à son propriétaire, même sur un objet
d'une autre org qu'il lit ; il coexiste avec le favori partagé du même objet, et retirer
l'un laisse l'autre ; personne d'autre ne le retire ; quitter l'org l'emporte ; une
fusion de comptes qui l'a des deux côtés en garde un, à la date la plus ancienne.

Et : un objet supprimé emporte ses lignes, toutes portées, dans la transaction de la
suppression, et une ligne morte restée est retirée à la lecture (filet) ; une page
archivée sort de la liste ; `kind` ou `scope` inconnus sont refusés ; un non-membre
n'entre pas.
"""
from __future__ import annotations

import uuid

import pytest

ADMIN, MOI, VOISIN, DEHORS, PARTANT = (
    "usr_fav_admin", "usr_fav_moi", "usr_fav_voisin", "usr_fav_dehors", "usr_fav_partant")


def _nom() -> str:
    return "fav-" + uuid.uuid4().hex[:6]


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store, ownership
    from oto_mcp.capabilities.docs import common as _kind_doc  # noqa: F401 — enregistre `doc`
    for sub in (ADMIN, MOI, VOISIN, DEHORS, PARTANT):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    a = org_store.create_org("A favoris", created_by=ADMIN)
    b = org_store.create_org("B favoris", created_by=ADMIN)
    org_store.add_org_member(a, ADMIN, "org_admin")
    org_store.add_org_member(a, MOI)
    org_store.add_org_member(a, VOISIN)
    org_store.add_org_member(b, ADMIN, "org_admin")
    org_store.add_org_member(b, MOI)
    org_store.add_org_member(b, DEHORS)
    tds = ownership.TYPE_RESSOURCE_DATASTORE

    p_org = int(db.create_project("org", str(a), _nom(), created_by=ADMIN))
    p_perso = int(db.create_project("user", MOI, _nom(), created_by=MOI, context_org_id=a))
    p_b = int(db.create_project("org", str(b), _nom(), created_by=ADMIN))
    # Un projet perso de MOI, rangé dans A, partagé à ADMIN SEUL.
    p_perso_admin = int(db.create_project("user", MOI, _nom(), created_by=MOI,
                                          context_org_id=a))
    ownership.grant("project", str(p_perso_admin), "user", ADMIN, "read", granted_by=MOI)
    d = {
        "org": db.create_doc(p_org, "Page de l'org", body_md="x", created_by=ADMIN),
        "archivee": db.create_doc(p_org, "Page archivée", body_md="x", created_by=ADMIN),
        "supprimee": db.create_doc(p_org, "Page supprimée", body_md="x", created_by=ADMIN),
        "perso": db.create_doc(p_perso, "Ma page", body_md="x", created_by=MOI),
        "perso_admin": db.create_doc(p_perso_admin, "Page pour l'admin", body_md="x",
                                     created_by=MOI),
        "b": db.create_doc(p_b, "Page de B", body_md="x", created_by=ADMIN),
    }
    t = {
        "org": db.create_datastore("org", str(a), _nom()),
        "supprime": db.create_datastore("org", str(a), _nom()),
        "perso": db.create_datastore("user", MOI, _nom(), context_org_id=a),
        "perso_admin": db.create_datastore("user", MOI, _nom(), context_org_id=a),
        "perso_partage_a": db.create_datastore("user", MOI, _nom(), context_org_id=a),
        "b": db.create_datastore("org", str(b), _nom()),
        "b_partage_a": db.create_datastore("org", str(b), _nom()),
    }
    ownership.grant(tds, str(t["b_partage_a"]), "org", str(a), "read", granted_by=ADMIN)
    ownership.grant(tds, str(t["perso_admin"]), "user", ADMIN, "read", granted_by=MOI)
    ownership.grant(tds, str(t["perso_partage_a"]), "org", str(a), "read", granted_by=MOI)
    return {"a": a, "b": b, "p_org": p_org, "d": d, "t": t}


@pytest.fixture(autouse=True)
def vide(monde):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("DELETE FROM org_favorites WHERE org_id = ANY(%s)",
                     ([monde["a"], monde["b"]],))


# ── la face servie : `make_routes`, un vérificateur qui prend le jeton pour le sub ─────

class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@t.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def rest(monde):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp.api import routes as api_routes
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def _get(rest, sub, org):
    r = rest.get(f"/api/orgs/{org}/favorites", headers=_h(sub))
    assert r.status_code == 200, r.text
    return r.json()["favorites"]


def _post(rest, sub, org, corps):
    return rest.post(f"/api/orgs/{org}/favorites", headers=_h(sub), json=corps)


def _delete(rest, sub, org, kind, ref, scope=None):
    q = f"?scope={scope}" if scope is not None else ""
    return rest.delete(f"/api/orgs/{org}/favorites/{kind}/{ref}{q}", headers=_h(sub))


# ── les handlers, pour les cas sans enjeu de transport ─────────────────────────────

def _ctx(sub, org):
    from oto_mcp.capabilities._types import ResolvedCtx
    return ResolvedCtx(sub=sub, org_id=org)


def _poser(sub, org, kind, ref, scope="org"):
    from oto_mcp.capabilities.orgs import favoris as F
    return F._ajouter(_ctx(sub, org),
                      F.FavoriteAddInput(org_id=org, kind=kind, id=ref, scope=scope))


def _retirer(sub, org, kind, ref, scope="org"):
    from oto_mcp.capabilities.orgs import favoris as F
    return F._retirer(_ctx(sub, org),
                      F.FavoriteRemoveInput(org_id=org, kind=kind, ref_id=ref, scope=scope))


def _lire(sub, org):
    from oto_mcp.capabilities.orgs import favoris as F
    return F._lister(_ctx(sub, org), F.FavoritesInput(org_id=org))["favorites"]


def _cles(favoris):
    return [(f["kind"], f["id"], f["scope"]) for f in favoris]


# ── portée org ────────────────────────────────────────────────────────────────────

def test_un_membre_epingle_et_tous_les_membres_voient_dans_l_ordre(monde, rest):
    a, d, t = monde["a"], monde["d"], monde["t"]
    r = _post(rest, MOI, a, {"kind": "table", "id": t["org"]})   # scope omis = org
    assert r.status_code == 200, r.text
    vue = _post(rest, MOI, a, {"kind": "doc", "id": d["org"], "scope": "org"}).json()[
        "favorites"]
    assert _cles(vue) == [("table", t["org"], "org"), ("doc", d["org"], "org")]
    page = vue[1]
    assert page["title"] == "Page de l'org"
    assert page["project_id"] == monde["p_org"]
    assert page["added_by"] == MOI and page["added_at"].endswith("+00:00")
    assert vue[0]["project_id"] is None and vue[0]["title"]
    assert _cles(_get(rest, VOISIN, a)) == _cles(vue)
    assert _cles(_get(rest, ADMIN, a)) == _cles(vue)


def test_epingler_deux_fois_ne_fait_qu_un_favori_et_garde_son_auteur(monde):
    a, t = monde["a"], monde["t"]
    _poser(MOI, a, "table", t["org"])
    vue = _poser(VOISIN, a, "table", t["org"])["favorites"]
    assert _cles(vue) == [("table", t["org"], "org")]
    assert vue[0]["added_by"] == MOI


def test_un_favori_n_accorde_rien(monde, rest):
    """Les objets perso de MOI partagés à ADMIN seul, épinglés par MOI pour l'org : MOI
    et ADMIN les voient, VOISIN non — et VOISIN ne peut pas les épingler lui-même (même
    404 qu'un id absent)."""
    a, d, t = monde["a"], monde["d"], monde["t"]
    _poser(MOI, a, "doc", d["perso_admin"])
    _poser(MOI, a, "table", t["perso_admin"])
    attendu = [("doc", d["perso_admin"], "org"), ("table", t["perso_admin"], "org")]
    assert _cles(_get(rest, MOI, a)) == attendu
    assert _cles(_get(rest, ADMIN, a)) == attendu
    assert _get(rest, VOISIN, a) == []
    for kind, ref in (("doc", d["perso_admin"]), ("table", t["perso_admin"]),
                      ("doc", d["perso"]), ("table", t["perso"]), ("table", 10 ** 12)):
        for scope in ("org", "me"):
            r = _post(rest, VOISIN, a, {"kind": kind, "id": ref, "scope": scope})
            assert r.status_code == 404, (kind, scope, r.text)
            assert r.json()["error"] == "favorite_target_unknown"


def test_un_objet_d_une_autre_org_ne_s_epingle_pas_pour_l_org_sauf_partage(monde):
    """MOI lit les objets de B (il en est membre) : ils ne s'épinglent pas pour A. Le
    tableau de B partagé à A, lui, s'y épingle et s'y voit."""
    from oto_mcp.capabilities._types import AuthzDenied
    a, d, t = monde["a"], monde["d"], monde["t"]
    for kind, ref in (("doc", d["b"]), ("table", t["b"])):
        with pytest.raises(AuthzDenied) as e:
            _poser(MOI, a, kind, ref)
        assert (e.value.status, e.value.code) == (404, "favorite_target_unknown")
    _poser(MOI, a, "table", t["b_partage_a"])
    assert _cles(_lire(VOISIN, a)) == [("table", t["b_partage_a"], "org")]
    assert _lire(MOI, monde["b"]) == []


def test_un_objet_lisible_par_son_seul_auteur_ne_s_epingle_que_pour_soi(monde, rest):
    """La page et le tableau perso de MOI, rangés dans A, que rien ne partage : refusés
    en `org` (409), acceptés en `me`. Le tableau perso partagé à l'org s'épingle en `org`
    et tous les membres le voient."""
    a, d, t = monde["a"], monde["d"], monde["t"]
    for kind, ref, phrase in (("doc", d["perso"], "partage-la d'abord"),
                              ("table", t["perso"], "partage-le d'abord")):
        r = _post(rest, MOI, a, {"kind": kind, "id": ref, "scope": "org"})
        assert (r.status_code, r.json()["error"]) == (409, "favorite_target_private"), r.text
        assert "visible par toi seul" in r.json()["detail"] and phrase in r.json()["detail"]
        r = _post(rest, MOI, a, {"kind": kind, "id": ref, "scope": "me"})
        assert r.status_code == 200, r.text
    assert _cles(_get(rest, MOI, a)) == [("doc", d["perso"], "me"), ("table", t["perso"], "me")]
    r = _post(rest, MOI, a, {"kind": "table", "id": t["perso_partage_a"], "scope": "org"})
    assert r.status_code == 200, r.text
    assert _cles(_get(rest, VOISIN, a)) == [("table", t["perso_partage_a"], "org")]


def test_l_auteur_desepingle_deux_fois_sans_erreur(monde, rest):
    a, d, t = monde["a"], monde["d"], monde["t"]
    _poser(MOI, a, "doc", d["org"])
    _poser(MOI, a, "table", t["org"])
    for _ in range(2):
        r = _delete(rest, MOI, a, "doc", d["org"])
        assert r.status_code == 200, r.text
        assert _cles(r.json()["favorites"]) == [("table", t["org"], "org")]


def test_un_admin_de_l_org_desepingle_le_favori_d_un_autre(monde, rest):
    """Même sur une cible qu'il ne lit pas, et 200 si le favori est déjà absent."""
    a, d, t = monde["a"], monde["d"], monde["t"]
    _poser(MOI, a, "table", t["org"])
    _poser(MOI, a, "doc", d["perso_admin"])
    _poser(MOI, a, "table", t["perso_partage_a"])
    for kind, ref in (("table", t["org"]), ("table", t["org"]), ("doc", d["perso_admin"]),
                      ("table", 10 ** 12)):
        r = _delete(rest, ADMIN, a, kind, ref)
        assert r.status_code == 200, (kind, ref, r.text)
    assert _cles(_get(rest, MOI, a)) == [("table", t["perso_partage_a"], "org")]


def test_un_autre_membre_qui_voit_le_favori_recoit_403_et_rien_ne_part(monde, rest):
    a, t = monde["a"], monde["t"]
    _poser(MOI, a, "table", t["org"])
    r = _delete(rest, VOISIN, a, "table", t["org"])
    assert (r.status_code, r.json()["error"]) == (403, "favorite_not_yours"), r.text
    assert _cles(_get(rest, VOISIN, a)) == [("table", t["org"], "org")]
    # Le favori absent sur une cible qu'il lit : rien à retirer, 200.
    r = _delete(rest, VOISIN, a, "doc", monde["d"]["org"])
    assert r.status_code == 200, r.text


def test_un_membre_qui_ne_lit_pas_la_cible_recoit_404(monde, rest):
    """Le favori existe (posé par MOI sur sa page partagée à ADMIN seul) : VOISIN, qui ne
    la lit pas, reçoit le même 404 que sur un id absent — il n'apprend rien."""
    a, d, t = monde["a"], monde["d"], monde["t"]
    _poser(MOI, a, "doc", d["perso_admin"])
    for kind, ref in (("doc", d["perso_admin"]), ("table", t["perso"]), ("table", 10 ** 12)):
        r = _delete(rest, VOISIN, a, kind, ref)
        assert (r.status_code, r.json()["error"]) == (404, "favorite_target_unknown"), r.text
    assert _cles(_lire(MOI, a)) == [("doc", d["perso_admin"], "org")]


# ── portée me ─────────────────────────────────────────────────────────────────────

def test_un_favori_personnel_se_pose_se_lit_et_se_retire_par_la_face_rest(monde, rest):
    a, d, t = monde["a"], monde["d"], monde["t"]
    r = _post(rest, MOI, a, {"kind": "table", "id": t["perso"], "scope": "me"})
    assert r.status_code == 200, r.text
    _post(rest, MOI, a, {"kind": "doc", "id": d["perso"], "scope": "me"})
    vue = _get(rest, MOI, a)
    assert _cles(vue) == [("table", t["perso"], "me"), ("doc", d["perso"], "me")]
    assert vue[0]["added_by"] == MOI
    r = _delete(rest, MOI, a, "table", t["perso"], scope="me")
    assert r.status_code == 200, r.text
    assert _cles(r.json()["favorites"]) == [("doc", d["perso"], "me")]


def test_un_autre_membre_ne_voit_ni_ne_retire_mes_favoris_personnels(monde, rest):
    a, t = monde["a"], monde["t"]
    _post(rest, MOI, a, {"kind": "table", "id": t["org"], "scope": "me"})
    assert _get(rest, VOISIN, a) == []
    assert _get(rest, ADMIN, a) == []
    # Ni par sa propre portée « me » (il n'a pas de ligne), ni par la portée « org ».
    assert _delete(rest, VOISIN, a, "table", t["org"], scope="me").status_code == 200
    assert _delete(rest, VOISIN, a, "table", t["org"]).status_code == 200
    assert _cles(_get(rest, MOI, a)) == [("table", t["org"], "me")]


def test_un_favori_personnel_hors_de_l_org_mais_lisible_s_epingle(monde, rest):
    """MOI lit la page et le tableau de B : pour lui seul, il les épingle dans A."""
    a, d, t = monde["a"], monde["d"], monde["t"]
    for kind, ref in (("doc", d["b"]), ("table", t["b"])):
        r = _post(rest, MOI, a, {"kind": kind, "id": ref, "scope": "me"})
        assert r.status_code == 200, r.text
    assert _cles(_get(rest, MOI, a)) == [("doc", d["b"], "me"), ("table", t["b"], "me")]
    assert _get(rest, VOISIN, a) == []


def test_le_meme_objet_partage_et_personnel_coexiste_et_se_retire_separement(monde, rest):
    a, t = monde["a"], monde["t"]
    _post(rest, MOI, a, {"kind": "table", "id": t["org"]})
    _post(rest, MOI, a, {"kind": "table", "id": t["org"], "scope": "me"})
    assert sorted(_cles(_get(rest, MOI, a))) == [("table", t["org"], "me"),
                                                ("table", t["org"], "org")]
    assert _cles(_get(rest, VOISIN, a)) == [("table", t["org"], "org")]
    r = _delete(rest, MOI, a, "table", t["org"], scope="me")
    assert _cles(r.json()["favorites"]) == [("table", t["org"], "org")]
    _post(rest, MOI, a, {"kind": "table", "id": t["org"], "scope": "me"})
    r = _delete(rest, MOI, a, "table", t["org"])          # scope omis = org
    assert _cles(r.json()["favorites"]) == [("table", t["org"], "me")]


def test_quitter_l_org_emporte_ses_favoris_personnels_pas_les_partages(monde):
    from oto_mcp import org_store
    from oto_mcp.db._conn import _connect
    a, t = monde["a"], monde["t"]
    org_store.add_org_member(a, PARTANT)
    _poser(PARTANT, a, "table", t["org"], scope="me")
    _poser(PARTANT, a, "table", t["org"], scope="org")
    assert org_store.remove_org_member(a, PARTANT, actor=ADMIN)
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT owner_sub, added_by FROM org_favorites WHERE org_id = %s",
            (a,)).fetchall()
    assert [(l["owner_sub"], l["added_by"]) for l in lignes] == [(None, PARTANT)]


def test_une_fusion_de_comptes_qui_a_le_meme_favori_des_deux_cotes_en_garde_un(monde):
    """Les deux comptes de la personne ont épinglé le même tableau pour eux dans A :
    la fusion ne lève pas, une ligne reste, à la date la plus ancienne ; le favori que
    seul l'ancien avait le suit."""
    from oto_mcp import db, org_store
    from oto_mcp.db._conn import _connect
    from oto_mcp.db.users import migrate_sub
    a, d, t = monde["a"], monde["d"], monde["t"]
    vieux, neuf = f"usr_fav_vieux_{uuid.uuid4().hex[:4]}", f"usr_fav_neuf_{uuid.uuid4().hex[:4]}"
    for sub in (vieux, neuf):
        db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
        org_store.add_org_member(a, sub)
    _poser(vieux, a, "table", t["org"], scope="me")
    _poser(vieux, a, "doc", d["org"], scope="me")
    _poser(neuf, a, "table", t["org"], scope="me")
    with _connect() as conn:
        conn.execute("UPDATE org_favorites SET added_at = '2026-01-01T00:00:00Z' "
                     "WHERE owner_sub = %s AND kind = 'table'", (vieux,))
    migrate_sub(vieux, neuf, operator_source="test")
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT owner_sub, kind, ref_id, added_at FROM org_favorites "
            "WHERE owner_sub IN (%s, %s) ORDER BY kind", (vieux, neuf)).fetchall()
    assert [(l["owner_sub"], l["kind"], l["ref_id"]) for l in lignes] == [
        (neuf, "doc", d["org"]), (neuf, "table", t["org"])]
    assert str(lignes[1]["added_at"]).startswith("2026-01-01")


# ── ce qui sort, ce qui est refusé ─────────────────────────────────────────────────

def _restes(org):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [(r["kind"], r["ref_id"]) for r in conn.execute(
            "SELECT kind, ref_id FROM org_favorites WHERE org_id = %s ORDER BY added_at",
            (org,)).fetchall()]


def test_un_objet_supprime_emporte_ses_lignes_toutes_portees_sans_lecture(monde):
    """La purge est faite PAR la suppression (page et sous-pages, tableau), avant toute
    lecture de la liste ; le favori personnel de VOISIN part aussi."""
    from oto_mcp import db
    a, d, t = monde["a"], monde["d"], monde["t"]
    sous_page = db.create_doc(monde["p_org"], "Sous-page", body_md="x", created_by=ADMIN,
                              parent_id=d["supprimee"])
    _poser(MOI, a, "doc", d["supprimee"])
    _poser(VOISIN, a, "doc", d["supprimee"], scope="me")
    _poser(MOI, a, "doc", sous_page)
    _poser(MOI, a, "table", t["supprime"], scope="me")
    _poser(MOI, a, "doc", d["org"])
    db.delete_doc(d["supprimee"])
    db.delete_datastore_by_id(t["supprime"])
    assert _restes(a) == [("doc", d["org"])]
    assert _cles(_lire(MOI, a)) == [("doc", d["org"], "org")]


def test_une_ligne_morte_restee_est_retiree_a_la_lecture(monde):
    """Le filet : une ligne dont la cible n'existe pas (aucun chemin de suppression ne
    l'a vue) sort de la liste et de la table à la première lecture."""
    from oto_mcp.db._conn import _connect
    a = monde["a"]
    with _connect() as conn:
        conn.execute("INSERT INTO org_favorites (org_id, owner_sub, kind, ref_id, added_by) "
                     "VALUES (%s, NULL, 'table', %s, %s)", (a, 10 ** 12, MOI))
    assert _lire(VOISIN, a) == []
    assert _restes(a) == []


def test_une_page_archivee_sort_de_la_liste_sans_perdre_sa_ligne(monde):
    from oto_mcp.db import doc_archivage
    from oto_mcp.db._conn import _connect
    a, d = monde["a"], monde["d"]
    _poser(MOI, a, "doc", d["archivee"])
    doc_archivage.archive_doc(d["archivee"])
    assert _lire(MOI, a) == []
    with _connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM org_favorites WHERE org_id = %s",
                            (a,)).fetchone()["n"] == 1


def test_un_kind_ou_un_scope_inconnu_est_refuse(monde, rest):
    a, t = monde["a"], monde["t"]
    r = _post(rest, MOI, a, {"kind": "app", "id": 1})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_favorite_kind")
    r = _delete(rest, MOI, a, "app", 1)
    assert (r.status_code, r.json()["error"]) == (400, "invalid_favorite_kind")
    r = _post(rest, MOI, a, {"kind": "table", "id": t["org"], "scope": "team"})
    assert (r.status_code, r.json()["error"]) == (400, "invalid_favorite_scope")
    r = _delete(rest, MOI, a, "table", t["org"], scope="team")
    assert (r.status_code, r.json()["error"]) == (400, "invalid_favorite_scope")


def test_un_non_membre_n_entre_pas(monde, rest):
    a, t = monde["a"], monde["t"]
    assert _get(rest, VOISIN, a) == []
    assert rest.get(f"/api/orgs/{a}/favorites", headers=_h(DEHORS)).status_code == 403
    assert _post(rest, DEHORS, a, {"kind": "table", "id": t["org"]}).status_code == 403
    assert _delete(rest, DEHORS, a, "table", t["org"]).status_code == 403
