"""Les commentaires d'une page de mémoire (doc), par les VRAIES routes sur une VRAIE base
(`docs/commentaires-de-procedure.md` §Pages de mémoire).

Le pendant des commentaires de procédure (`test_commentaires_procedure.py`) :

1. écrire puis lire : le fil, du plus ancien au plus récent, `version` toujours nulle,
   `mine`/`can_delete`, la pagination par `before_id` ;
2. commenter n'écrit pas la page : ni révision, ni `updated_at` ;
3. l'accès : qui LIT la page (par son projet, ou par un partage de la page) lit et
   écrit ; retirer = l'auteur ou qui gouverne ; un inconnu ne voit rien (404) ;
4. un agent hébergé qui travaille ne les lit ni ne les écrit ;
5. fils et passages, retrait doux de la racine ;
6. REST seule : aucun outil agent.
"""
from __future__ import annotations

import uuid

import pytest

A = "sub-comdoc-a"
B = "sub-comdoc-b"
C = "sub-comdoc-c"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@comdoc.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def rest(live):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp import db
    from oto_mcp.api import routes as api_routes
    for sub in (A, B, C):
        db.upsert_user(sub, email=f"{sub}@comdoc.invalid", name=sub)
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str = A) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def _page() -> tuple[int, int]:
    """Une page dans un projet PERSONNEL de A : `(project_id, doc_id)`."""
    from oto_mcp import db
    pid = db.create_project("user", A, f"comdoc-{uuid.uuid4().hex[:6]}", created_by=A)
    did = db.create_doc(pid, "Essai", body_md="# Essai\nUn passage à commenter.",
                        created_by=A)
    return pid, did


def _partager(rtype: str, rid: int, sub: str) -> None:
    from oto_mcp import ownership
    ownership.grant(rtype, str(rid), "user", sub, "read", granted_by=A)


def _url(did) -> str:
    return f"/api/me/docs/{did}/comments"


def _poster(rest, did, body, sub=A, **extra):
    return rest.post(_url(did), headers=_h(sub), json={"body": body, **extra})


# ── 1. le fil ──────────────────────────────────────────────────────────────────

def test_ecrire_puis_lire_le_fil(rest):
    _, did = _page()
    r = _poster(rest, did, "  premier  ")
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["body"], c["author"], c["mine"], c["can_delete"], c["version"],
            c["deleted"]) == ("premier", A, True, True, None, False)
    assert c["author_email"] == f"{A}@comdoc.invalid"
    _poster(rest, did, "second")
    lu = rest.get(_url(did), headers=_h()).json()
    assert [x["body"] for x in lu["comments"]] == ["premier", "second"]
    assert (lu["doc_id"], lu["total"], lu["next_before_id"]) == (did, 2, None)


def test_pagination_par_before_id(rest):
    _, did = _page()
    for i in range(5):
        _poster(rest, did, f"c{i}")
    p1 = rest.get(_url(did), headers=_h(), params={"limit": 2}).json()
    assert ([x["body"] for x in p1["comments"]], p1["total"]) == (["c3", "c4"], 5)
    p2 = rest.get(_url(did), headers=_h(),
                  params={"limit": 2, "before_id": p1["next_before_id"]}).json()
    assert [x["body"] for x in p2["comments"]] == ["c1", "c2"]


def test_corps_vide_ou_trop_long_refuse(rest):
    _, did = _page()
    assert _poster(rest, did, "   ").status_code == 400
    assert _poster(rest, did, "x" * 5001).status_code == 400
    assert _poster(rest, did, "x" * 5000).status_code == 201


# ── 2. pas une écriture de la page ─────────────────────────────────────────────

def test_commenter_ne_cree_aucune_revision(rest):
    from oto_mcp import db
    _, did = _page()
    avant = db.get_doc_by_id(did)
    revisions = db.list_doc_revisions(did)
    _poster(rest, did, "un mot")
    apres = db.get_doc_by_id(did)
    assert (apres["updated_at"], apres["body_md"]) == (avant["updated_at"], avant["body_md"])
    assert db.list_doc_revisions(did) == revisions


# ── 3. l'accès ─────────────────────────────────────────────────────────────────

def test_un_lecteur_du_projet_lit_et_commente_mais_ne_retire_que_le_sien(rest):
    pid, did = _page()
    a = _poster(rest, did, "de A").json()
    _partager("project", pid, B)
    b = _poster(rest, did, "de B", sub=B)
    assert b.status_code == 201, b.text
    vus = {x["body"]: x for x in rest.get(_url(did), headers=_h(B)).json()["comments"]}
    assert (vus["de A"]["mine"], vus["de A"]["can_delete"]) == (False, False)
    assert (vus["de B"]["mine"], vus["de B"]["can_delete"]) == (True, True)
    r = rest.delete(f"{_url(did)}/{a['id']}", headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (403, "comment_not_yours")
    assert rest.delete(f"{_url(did)}/{b.json()['id']}", headers=_h(B)).json() == {
        "deleted": True}


def test_un_partage_de_la_page_seule_suffit(rest):
    _, did = _page()
    _partager("doc", did, B)
    r = _poster(rest, did, "par le partage de page", sub=B)
    assert r.status_code == 201, r.text
    assert [x["body"] for x in rest.get(_url(did), headers=_h(B)).json()["comments"]] \
        == ["par le partage de page"]


def test_qui_gouverne_retire_le_commentaire_d_un_autre(rest):
    pid, did = _page()
    _partager("project", pid, B)
    c = _poster(rest, did, "à retirer", sub=B).json()
    vu = rest.get(_url(did), headers=_h()).json()["comments"][0]
    assert (vu["mine"], vu["can_delete"]) == (False, True)
    assert rest.delete(f"{_url(did)}/{c['id']}", headers=_h()).status_code == 200
    assert rest.get(_url(did), headers=_h()).json()["comments"] == []
    r = rest.delete(f"{_url(did)}/{c['id']}", headers=_h())
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")


def test_hors_perimetre_les_trois_routes_sont_une_page_inconnue(rest):
    _, did = _page()
    cid = _poster(rest, did, "à moi").json()["id"]
    for r in (rest.get(_url(did), headers=_h(C)),
              _poster(rest, did, "intrus", sub=C),
              rest.delete(f"{_url(did)}/{cid}", headers=_h(C)),
              rest.get(_url(10 ** 12), headers=_h())):
        assert (r.status_code, r.json()["error"]) == (404, "doc_not_found"), r.text
    assert [x["body"] for x in rest.get(_url(did), headers=_h()).json()["comments"]] \
        == ["à moi"]


def test_une_page_supprimee_emporte_ses_commentaires(rest):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    _, did = _page()
    _poster(rest, did, "partira avec la page")
    with _connect() as conn:
        conn.execute("DELETE FROM docs WHERE id = %s", (did,))
        n = conn.execute("SELECT count(*) AS n FROM doc_comments WHERE doc_id = %s",
                         (did,)).fetchone()["n"]
    assert n == 0
    assert db.get_doc_by_id(did) is None


# ── 4. jamais pendant un travail ───────────────────────────────────────────────

def test_un_agent_heberge_ne_les_lit_ni_ne_les_ecrit(rest, monkeypatch):
    from oto_mcp.capabilities import commentaires_doc as cd
    _, did = _page()
    _poster(rest, did, "avant le travail")
    monkeypatch.setattr(cd, "procedure_du_travail", lambda: (True, "un-slug"))
    for r in (rest.get(_url(did), headers=_h()), _poster(rest, did, "pendant")):
        assert (r.status_code, r.json()["error"]) == (403, "comments_outside_job"), r.text


# ── 5. fils et passages ────────────────────────────────────────────────────────

def test_un_fil_porte_son_passage_et_ses_reponses(rest):
    _, did = _page()
    r = _poster(rest, did, "à préciser", quote="  Un   passage ", quote_prefix="# Essai ")
    assert r.status_code == 201, r.text
    racine = r.json()
    assert (racine["quote"], racine["quote_prefix"], racine["reply_to"]) == (
        "Un passage", "# Essai", None)
    rep = _poster(rest, did, "d'accord", reply_to=racine["id"])
    assert rep.status_code == 201, rep.text
    lu = rest.get(_url(did), headers=_h()).json()["comments"]
    assert [(c["body"], c["reply_to"]) for c in lu] == [("à préciser", None),
                                                        ("d'accord", racine["id"])]


def test_une_reponse_ne_cite_rien_et_ne_repond_qu_a_une_racine_de_sa_page(rest):
    _, did = _page()
    racine = _poster(rest, did, "racine").json()
    assert _poster(rest, did, "x", reply_to=racine["id"], quote="Un").status_code == 400
    rep = _poster(rest, did, "y", reply_to=racine["id"]).json()
    r = _poster(rest, did, "z", reply_to=rep["id"])
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")
    _, autre = _page()
    r = _poster(rest, autre, "z", reply_to=racine["id"])
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")


def test_retirer_la_racine_garde_le_fil_tant_qu_il_a_des_reponses(rest):
    _, did = _page()
    racine = _poster(rest, did, "racine", quote="passage").json()
    rep = _poster(rest, did, "rep", reply_to=racine["id"]).json()
    _poster(rest, did, "autre fil")
    assert rest.delete(f"{_url(did)}/{racine['id']}", headers=_h()).status_code == 200
    lu = rest.get(_url(did), headers=_h()).json()
    vus = {c["id"]: c for c in lu["comments"]}
    assert (vus[racine["id"]]["deleted"], vus[racine["id"]]["body"],
            vus[racine["id"]]["quote"], vus[racine["id"]]["author"]) == (
        True, "", "passage", None)
    assert rep["id"] in vus and lu["total"] == 2
    assert rest.delete(f"{_url(did)}/{rep['id']}", headers=_h()).status_code == 200
    assert [c["body"] for c in rest.get(_url(did), headers=_h()).json()["comments"]] \
        == ["autre fil"]


# ── 6. REST seule ──────────────────────────────────────────────────────────────

def test_rest_seule_aucun_outil_agent():
    from oto_mcp.capabilities import registry
    attendu = {"me.doc.comments": ("GET", "/api/me/docs/{doc_id}/comments"),
               "me.doc.comment": ("POST", "/api/me/docs/{doc_id}/comments"),
               "me.doc.delete_comment": ("DELETE",
                                         "/api/me/docs/{doc_id}/comments/{comment_id}")}
    for cle, verbe_chemin in attendu.items():
        cap = registry.by_key(cle)
        assert cap.mcp is None
        assert [(b.verb, b.path) for b in cap.rest_bindings()] == [verbe_chemin]
