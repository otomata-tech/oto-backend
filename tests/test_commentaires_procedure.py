"""Les commentaires d'une procédure, par les VRAIES routes sur une VRAIE base
(`docs/commentaires-de-procedure.md`).

1. écrire puis lire : le fil, du plus ancien au plus récent, la version commentée,
   `mine`/`can_delete`, la pagination par `before_id` ;
2. OPTIONNELS : commenter ne crée aucune version ; écrire la procédure ensuite marche
   comme sans eux ; `op=get` (les deux faces) ne les sert pas ;
3. l'accès : qui LIT la procédure lit et écrit ; retirer = l'auteur ou qui gouverne ; un
   inconnu ne voit rien (404 sur les trois routes) ;
4. un agent hébergé qui travaille ne les lit ni ne les écrit ;
5. la face agent : les deux outils sont les mêmes capacités, et le copilote lit sans
   demander.
"""
from __future__ import annotations

import uuid

import pytest

A = "sub-comproc-a"
B = "sub-comproc-b"
C = "sub-comproc-c"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@comproc.invalid", "name": sub}


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
        db.upsert_user(sub, email=f"{sub}@comproc.invalid", name=sub)
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str = A) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def _procedure() -> tuple[str, int]:
    """Une procédure PERSONNELLE de A : `(slug, guide_id)`."""
    from oto_mcp import org_store
    slug = f"comproc-{uuid.uuid4().hex[:6]}"
    org_store.set_instruction("user", A, slug, "# Étapes\n1. faire", title="Essai",
                              set_by=A)
    return slug, int(org_store.get_instruction("user", A, slug)["id"])


def _partager(gid: int, sub: str) -> None:
    from oto_mcp import ownership
    ownership.grant(ownership.TYPE_RESSOURCE_PROCEDURE, str(gid), "user", sub, "read",
                    granted_by=A)


def _url(gid) -> str:
    return f"/api/me/processes/{gid}/comments"


def _poster(rest, gid, body, sub=A):
    return rest.post(_url(gid), headers=_h(sub), json={"body": body})


# ── 1. le fil ──────────────────────────────────────────────────────────────────

def test_ecrire_puis_lire_le_fil(rest):
    _, gid = _procedure()
    r = _poster(rest, gid, "  premier  ")
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["body"], c["author"], c["mine"], c["can_delete"], c["version"]) == (
        "premier", A, True, True, 1)
    assert c["author_email"] == f"{A}@comproc.invalid"
    _poster(rest, gid, "second")
    lu = rest.get(_url(gid), headers=_h()).json()
    assert [x["body"] for x in lu["comments"]] == ["premier", "second"]
    assert (lu["guide_id"], lu["version"], lu["total"], lu["next_before_id"]) == (
        gid, 1, 2, None)


def test_pagination_par_before_id(rest):
    _, gid = _procedure()
    for i in range(5):
        _poster(rest, gid, f"c{i}")
    p1 = rest.get(_url(gid), headers=_h(), params={"limit": 2}).json()
    assert [x["body"] for x in p1["comments"]] == ["c3", "c4"]
    assert p1["total"] == 5
    p2 = rest.get(_url(gid), headers=_h(),
                  params={"limit": 2, "before_id": p1["next_before_id"]}).json()
    assert [x["body"] for x in p2["comments"]] == ["c1", "c2"]


def test_corps_vide_ou_trop_long_refuse(rest):
    _, gid = _procedure()
    assert _poster(rest, gid, "   ").status_code == 400
    assert _poster(rest, gid, "x" * 5001).status_code == 400


# ── 2. optionnels ──────────────────────────────────────────────────────────────

def test_commenter_ne_cree_aucune_version_et_l_ecriture_suit_sans_eux(rest):
    from oto_mcp import org_store
    slug, gid = _procedure()
    _poster(rest, gid, "sur la v1")
    assert org_store.get_instruction("user", A, slug)["version"] == 1
    # La procédure s'écrit comme si de rien n'était, et le commentaire garde sa version.
    assert org_store.set_instruction("user", A, slug, "# Étapes\n1. refaire",
                                     set_by=A, expected_version=1) == 2
    lu = rest.get(_url(gid), headers=_h()).json()
    assert (lu["version"], lu["comments"][0]["version"]) == (2, 1)


def test_op_get_ne_sert_pas_les_commentaires(rest):
    _, gid = _procedure()
    _poster(rest, gid, "MARQUEUR-DE-COMMENTAIRE")
    r = rest.get(f"/api/me/guides/{gid}", headers=_h())
    assert r.status_code == 200, r.text
    assert "MARQUEUR-DE-COMMENTAIRE" not in r.text
    assert "comment" not in r.text.lower()


# ── 3. l'accès ─────────────────────────────────────────────────────────────────

def test_un_lecteur_lit_et_commente_mais_ne_retire_que_le_sien(rest):
    _, gid = _procedure()
    a = _poster(rest, gid, "de A").json()
    _partager(gid, B)
    b = _poster(rest, gid, "de B", sub=B)
    assert b.status_code == 201, b.text
    vus = {x["body"]: x for x in rest.get(_url(gid), headers=_h(B)).json()["comments"]}
    assert (vus["de A"]["mine"], vus["de A"]["can_delete"]) == (False, False)
    assert (vus["de B"]["mine"], vus["de B"]["can_delete"]) == (True, True)
    r = rest.delete(f"{_url(gid)}/{a['id']}", headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (403, "comment_not_yours")
    assert rest.delete(f"{_url(gid)}/{b.json()['id']}", headers=_h(B)).status_code == 200


def test_qui_gouverne_retire_le_commentaire_d_un_autre(rest):
    _, gid = _procedure()
    _partager(gid, B)
    c = _poster(rest, gid, "à retirer", sub=B).json()
    assert rest.delete(f"{_url(gid)}/{c['id']}", headers=_h()).status_code == 200
    assert rest.get(_url(gid), headers=_h()).json()["comments"] == []
    r = rest.delete(f"{_url(gid)}/{c['id']}", headers=_h())
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")


def test_hors_perimetre_les_trois_routes_sont_une_procedure_inconnue(rest):
    _, gid = _procedure()
    cid = _poster(rest, gid, "à moi").json()["id"]
    for r in (rest.get(_url(gid), headers=_h(C)),
              _poster(rest, gid, "intrus", sub=C),
              rest.delete(f"{_url(gid)}/{cid}", headers=_h(C)),
              rest.get(_url(10 ** 12), headers=_h())):
        assert (r.status_code, r.json()["error"]) == (404, "procedure_not_found"), r.text
    assert [x["body"] for x in rest.get(_url(gid), headers=_h()).json()["comments"]] \
        == ["à moi"]


# ── 4. jamais pendant un travail ───────────────────────────────────────────────

def test_un_agent_heberge_ne_les_lit_ni_ne_les_ecrit(rest, monkeypatch):
    from oto_mcp.capabilities import commentaires_procedure as cp
    _, gid = _procedure()
    _poster(rest, gid, "avant le travail")
    monkeypatch.setattr(cp, "procedure_du_travail", lambda: (True, "un-slug"))
    for r in (rest.get(_url(gid), headers=_h()), _poster(rest, gid, "pendant")):
        assert (r.status_code, r.json()["error"]) == (403, "comments_outside_job"), r.text


# ── 5. la face agent ───────────────────────────────────────────────────────────

def test_face_agent_par_slug(live):
    from oto_mcp.capabilities import commentaires_procedure as cp
    from oto_mcp.capabilities._types import ResolvedCtx
    slug, gid = _procedure()
    ctx = ResolvedCtx(sub=A, channel="mcp")
    c = cp._procedure_comment(ctx, cp.ProcedureCommentInput(slug=slug, body="par MCP"))
    lu = cp._procedure_comments(ctx, cp.ProcedureCommentsInput(guide_id=gid))
    assert [x["id"] for x in lu["comments"]] == [c["id"]]
    with pytest.raises(ValueError):
        cp.ProcedureCommentsInput()


def test_outils_declares_et_classes_pour_le_copilote():
    from oto_mcp.capabilities import commentaires_procedure  # noqa: F401
    from oto_mcp.capabilities.copilot import classement
    from oto_mcp.capabilities.registry import CAPABILITIES
    mcp = {c.mcp: c.key for c in CAPABILITIES if c.mcp}
    assert mcp["oto_procedure_comments"] == "me.procedure.comments"
    assert mcp["oto_procedure_comment"] == "me.procedure.comment"
    assert "oto_procedure_comments" in classement.OUTILS_EN_LECTURE
    assert "oto_procedure_comment" not in classement.OUTILS_EN_LECTURE


def test_l_horodatage_suit_la_forme_des_commentaires_de_ligne(rest):
    """`at` est servi comme celui d'une ligne (`YYYY-MM-DD HH:MM:SS`, sans offset) : le
    front le lit avec le même parseur."""
    import re
    _, gid = _procedure()
    at = _poster(rest, gid, "heure").json()["at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", at), at


# ── 6. fils et passages (à la Notion) ──────────────────────────────────────────

def test_un_fil_porte_son_passage_et_ses_reponses(rest):
    _, gid = _procedure()
    r = rest.post(_url(gid), headers=_h(), json={
        "body": "à préciser", "quote": "  1.   faire ", "quote_prefix": "# Étapes "})
    assert r.status_code == 201, r.text
    racine = r.json()
    assert (racine["quote"], racine["quote_prefix"], racine["reply_to"]) == (
        "1. faire", "# Étapes", None)
    rep = rest.post(_url(gid), headers=_h(), json={"body": "d'accord",
                                                   "reply_to": racine["id"]})
    assert rep.status_code == 201, rep.text
    assert (rep.json()["reply_to"], rep.json()["quote"]) == (racine["id"], None)
    lu = rest.get(_url(gid), headers=_h()).json()["comments"]
    assert [(c["body"], c["reply_to"]) for c in lu] == [("à préciser", None),
                                                        ("d'accord", racine["id"])]


def test_une_reponse_ne_cite_rien_et_ne_repond_qu_a_une_racine(rest):
    _, gid = _procedure()
    racine = _poster(rest, gid, "racine").json()
    r = rest.post(_url(gid), headers=_h(), json={"body": "x", "reply_to": racine["id"],
                                                 "quote": "faire"})
    assert r.status_code == 400
    rep = rest.post(_url(gid), headers=_h(), json={"body": "y", "reply_to": racine["id"]})
    r = rest.post(_url(gid), headers=_h(), json={"body": "z",
                                                 "reply_to": rep.json()["id"]})
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")
    _, autre = _procedure()
    r = rest.post(_url(autre), headers=_h(), json={"body": "z", "reply_to": racine["id"]})
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")


def test_retirer_la_racine_garde_le_fil_tant_qu_il_a_des_reponses(rest):
    _, gid = _procedure()
    racine = rest.post(_url(gid), headers=_h(), json={"body": "racine",
                                                      "quote": "faire"}).json()
    rep = rest.post(_url(gid), headers=_h(), json={"body": "rep",
                                                   "reply_to": racine["id"]}).json()
    _poster(rest, gid, "autre fil")
    assert rest.delete(f"{_url(gid)}/{racine['id']}", headers=_h()).status_code == 200
    lu = rest.get(_url(gid), headers=_h()).json()
    vus = {c["id"]: c for c in lu["comments"]}
    assert (vus[racine["id"]]["deleted"], vus[racine["id"]]["body"],
            vus[racine["id"]]["quote"], vus[racine["id"]]["author"]) == (True, "", "faire", None)
    assert rep["id"] in vus and lu["total"] == 2
    # On répond encore dans le fil ; il disparaît avec sa dernière réponse.
    r = rest.post(_url(gid), headers=_h(), json={"body": "encore", "reply_to": racine["id"]})
    assert r.status_code == 201, r.text
    for cid in (rep["id"], r.json()["id"]):
        assert rest.delete(f"{_url(gid)}/{cid}", headers=_h()).status_code == 200
    assert [c["body"] for c in rest.get(_url(gid), headers=_h()).json()["comments"]] == ["autre fil"]


def test_retirer_une_reponse_ne_retire_qu_elle(rest):
    _, gid = _procedure()
    racine = _poster(rest, gid, "racine").json()
    rep = rest.post(_url(gid), headers=_h(), json={"body": "rep",
                                                   "reply_to": racine["id"]}).json()
    assert rest.delete(f"{_url(gid)}/{rep['id']}", headers=_h()).status_code == 200
    assert [c["body"] for c in rest.get(_url(gid), headers=_h()).json()["comments"]] == ["racine"]
