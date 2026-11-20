"""Les commentaires d'une ligne, par les VRAIES routes sur une VRAIE base.

1. écrire puis lire : le fil, du plus ancien au plus récent, `mine`/`can_delete` pour
   l'appelant ; la pagination par `before_id` ;
2. commenter n'écrit pas la ligne : sa `rev` et son journal ne bougent pas ;
3. l'accès : un LECTEUR lit et écrit ; retirer = l'auteur ou qui gouverne ; une ligne
   supprimée ne se lit que par qui gouverne et ne se commente plus ;
4. la face agent : les deux outils sont les mêmes capacités ;
5. CELLULES et COLONNES (20/11/2026) : un commentaire de cellule est un commentaire de
   sa ligne qui nomme une colonne ; une colonne a son propre fil ; les comptes rendent
   `cells` et `columns` ; une clé inconnue est refusée.
"""
from __future__ import annotations

import uuid

import pytest

A = "sub-commentaires-a"
B = "sub-commentaires-b"
C = "sub-commentaires-c"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@commentaires.invalid", "name": sub}


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
        db.upsert_user(sub, email=f"{sub}@commentaires.invalid", name=sub)
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str = A) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def _table_et_ligne(rest) -> tuple[str, int, str]:
    from oto_mcp import db
    ns = f"commentaires-{uuid.uuid4().hex[:6]}"
    ns_id = db.create_datastore("user", A, ns)
    db.set_datastore_schema(ns_id, {"fields": [{"key": "n"}]})
    r = rest.post(f"/api/datastores/{ns}/rows", headers=_h(), json={"n": 1})
    assert r.status_code == 201, r.text
    return ns, ns_id, r.json()["_id"]


def _partager(ns_id: int, sub: str) -> None:
    from oto_mcp import ownership
    ownership.grant(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id), "user", sub, "read",
                    granted_by=A)


def _url(ns, rid) -> str:
    return f"/api/datastores/{ns}/rows/{rid}/comments"


def _poster(rest, ns, rid, body, sub=A):
    return rest.post(_url(ns, rid), headers=_h(sub), json={"body": body})


# ── 1. le fil ──────────────────────────────────────────────────────────────────

def test_ecrire_puis_lire_le_fil(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    r = _poster(rest, ns, rid, "  premier  ")
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["body"], c["author"], c["mine"], c["can_delete"]) == ("premier", A, True, True)
    assert c["author_email"] == f"{A}@commentaires.invalid"
    assert c["at"]
    _poster(rest, ns, rid, "second")

    out = rest.get(_url(ns, rid), headers=_h()).json()
    assert [x["body"] for x in out["comments"]] == ["premier", "second"]
    assert (out["row_id"], out["ns_id"], out["next_before_id"]) == (rid, ns_id, None)


def test_corps_vide_ou_trop_long_refuse(rest):
    ns, _, rid = _table_et_ligne(rest)
    assert _poster(rest, ns, rid, "   ").status_code == 400
    assert _poster(rest, ns, rid, "x" * 5001).status_code == 400
    assert _poster(rest, ns, rid, "x" * 5000).status_code == 201


def test_la_pagination_couvre_tout_sans_doublon(rest):
    ns, _, rid = _table_et_ligne(rest)
    for i in range(5):
        _poster(rest, ns, rid, f"c{i}")
    page = rest.get(_url(ns, rid), headers=_h(), params={"limit": 2}).json()
    assert [x["body"] for x in page["comments"]] == ["c3", "c4"]
    vus = [x["body"] for x in page["comments"]]
    while page["next_before_id"]:
        page = rest.get(_url(ns, rid), headers=_h(),
                        params={"limit": 2, "before_id": page["next_before_id"]}).json()
        vus = [x["body"] for x in page["comments"]] + vus
    assert vus == [f"c{i}" for i in range(5)]


# ── 2. pas une écriture de la ligne ────────────────────────────────────────────

def test_commenter_ne_change_ni_la_ligne_ni_son_journal(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    avant = rest.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).json()["_revision"]
    histo = rest.get(f"/api/datastores/{ns}/rows/{rid}/history", headers=_h()).json()
    _poster(rest, ns, rid, "un mot")
    apres = rest.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).json()["_revision"]
    assert apres == avant
    assert rest.get(f"/api/datastores/{ns}/rows/{rid}/history",
                    headers=_h()).json()["revisions"] == histo["revisions"]


# ── 3. l'accès ─────────────────────────────────────────────────────────────────

def test_un_lecteur_lit_et_ecrit_et_ne_retire_que_le_sien(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    _partager(ns_id, B)
    _partager(ns_id, C)
    du_proprio = _poster(rest, ns, rid, "du propriétaire").json()
    r = _poster(rest, str(ns_id), rid, "du lecteur", sub=B)
    assert r.status_code == 201, r.text
    du_lecteur = r.json()

    vu_par_b = rest.get(_url(ns_id, rid), headers=_h(B)).json()["comments"]
    assert [(x["body"], x["mine"], x["can_delete"]) for x in vu_par_b] == [
        ("du propriétaire", False, False), ("du lecteur", True, True)]

    r = rest.delete(f"{_url(ns_id, rid)}/{du_proprio['id']}", headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (403, "comment_not_yours")
    r = rest.delete(f"{_url(ns_id, rid)}/{du_lecteur['id']}", headers=_h(C))
    assert (r.status_code, r.json()["error"]) == (403, "comment_not_yours")
    assert rest.delete(f"{_url(ns_id, rid)}/{du_lecteur['id']}",
                       headers=_h(B)).json() == {"deleted": True}
    r = rest.delete(f"{_url(ns_id, rid)}/{du_lecteur['id']}", headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")
    assert [x["body"] for x in rest.get(_url(ns, rid), headers=_h()).json()["comments"]] \
        == ["du propriétaire"]


def test_qui_gouverne_retire_le_commentaire_d_un_autre(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    _partager(ns_id, B)
    c = _poster(rest, str(ns_id), rid, "à retirer", sub=B).json()
    vu = rest.get(_url(ns, rid), headers=_h()).json()["comments"][0]
    assert (vu["mine"], vu["can_delete"]) == (False, True)
    assert rest.delete(f"{_url(ns, rid)}/{c['id']}", headers=_h()).status_code == 200
    assert rest.get(_url(ns, rid), headers=_h()).json()["comments"] == []


def test_ligne_supprimee_inconnue_et_tableau_hors_perimetre(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    _partager(ns_id, B)
    _poster(rest, ns, rid, "reste")
    assert rest.delete(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).status_code == 200

    r = rest.get(_url(ns_id, rid), headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (404, "row_not_found")
    assert [x["body"] for x in rest.get(_url(ns, rid), headers=_h()).json()["comments"]] \
        == ["reste"], "qui gouverne lit encore"
    r = _poster(rest, ns, rid, "trop tard")
    assert (r.status_code, r.json()["error"]) == (404, "row_not_found")

    r = rest.get(_url(ns, "pas-une-ligne"), headers=_h())
    assert (r.status_code, r.json()["error"]) == (404, "row_not_found")
    ns2, _, rid2 = _table_et_ligne(rest)
    r = rest.get(_url(ns2, rid2), headers=_h(C))
    assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found")



def test_hors_perimetre_les_quatre_routes_sont_un_tableau_inconnu(rest):
    """Le cloisonnement se juge sur CHAQUE route, pas sur le fil seul : un inconnu ne
    compte, n'écrit ni ne retire rien, et n'apprend même pas que le tableau existe."""
    ns, ns_id, rid = _table_et_ligne(rest)
    cid = _poster(rest, ns, rid, "à moi").json()["id"]
    for r in (rest.get(_url(ns_id, rid), headers=_h(C)),
              _comptes(rest, ns_id, [rid], sub=C),
              _poster(rest, ns_id, rid, "intrus", sub=C),
              rest.delete(f"{_url(ns_id, rid)}/{cid}", headers=_h(C))):
        assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found"), r.text
    assert [x["body"] for x in rest.get(_url(ns, rid), headers=_h()).json()["comments"]] \
        == ["à moi"]

# ── 3 bis. les comptes par ligne ───────────────────────────────────────────────

def _comptes(rest, ns, ids, sub=A):
    return rest.get(f"/api/datastores/{ns}/comments/counts", headers=_h(sub),
                    params={"row_ids": ",".join(ids)})


def test_les_comptes_ne_gardent_que_le_vivant_des_lignes_demandees(rest):
    from oto_mcp import db
    ns, ns_id, r1 = _table_et_ligne(rest)
    r2, r3, r4, r5 = (rest.post(f"/api/datastores/{ns}/rows", headers=_h(),
                                json={"n": n}).json()["_id"] for n in (2, 3, 4, 5))
    _poster(rest, ns, r1, "a")
    _poster(rest, ns, r1, "b" * 300)
    retire = _poster(rest, ns, r2, "retiré").json()
    rest.delete(f"{_url(ns, r2)}/{retire['id']}", headers=_h())
    _poster(rest, ns, r3, "ligne partie")
    assert rest.delete(f"/api/datastores/{ns}/rows/{r3}", headers=_h()).status_code == 200
    _poster(rest, ns, r5, "pas demandée")

    _partager(ns_id, B)
    out = _comptes(rest, str(ns_id), [r1, r2, r3, r4, "inconnue", r1], sub=B).json()
    assert (out["counts"], out["total"], out["ns_id"]) == ({r1: 2}, 2, ns_id)
    assert list(out["latest"]) == [r1]
    dernier = out["latest"][r1]
    assert (dernier["body"], dernier["body_truncated"]) == ("b" * 280, True)
    assert (dernier["author"], dernier["author_email"]) == (A, f"{A}@commentaires.invalid")
    assert dernier["id"] and dernier["at"]

    # La forme répétée vaut la forme à virgules.
    r = rest.get(f"/api/datastores/{ns}/comments/counts", headers=_h(),
                 params=[("row_ids", r1), ("row_ids", r5)])
    court = r.json()
    assert court["counts"] == {r1: 2, r5: 1}
    assert (court["latest"][r5]["body"], court["latest"][r5]["body_truncated"]) == (
        "pas demandée", False)

    r = _comptes(rest, str(ns_id), [r1], sub=C)
    assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found")
    assert [(l["row_id"], l["n"]) for l in
            db.datastore_commentaires.comptes(ns_id, [r1, r2])] == [(r1, 2)]


def test_les_comptes_exigent_des_lignes_et_en_bornent_le_nombre(rest):
    ns, _, rid = _table_et_ligne(rest)
    assert rest.get(f"/api/datastores/{ns}/comments/counts",
                    headers=_h()).status_code == 400
    assert _comptes(rest, ns, [" ", ""]).status_code == 400
    assert _comptes(rest, ns, [f"l{i}" for i in range(101)]).status_code == 400
    assert _comptes(rest, ns, [f"l{i}" for i in range(99)] + [rid]).status_code == 200


def test_le_plan_des_comptes_sonde_l_index_partiel_sans_parcours_complet(rest):
    """Un tableau aux milliers de commentaires, une page de 100 lignes demandée : le
    plan passe par l'index partiel, jamais par un parcours complet des commentaires ni
    par un tri. `enable_seqscan` coupé : sur une table de banc, le planificateur
    préfère parfois un parcours séquentiel PARCE QUE la table est petite — c'est
    l'index qu'on éprouve ici, pas l'estimation de coût."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns, ns_id, _ = _table_et_ligne(rest)
    ids = [f"r{i}" for i in range(2000)]
    with _connect() as conn:
        conn.execute("INSERT INTO datastore_rows (ns_id, row_id, data) "
                     "SELECT %s, x, '{}'::jsonb FROM unnest(%s::text[]) x", (ns_id, ids))
        conn.execute("INSERT INTO datastore_row_comments (ns_id, row_id, auteur_sub, body) "
                     "SELECT %s, ids[1 + (g %% 2000)], %s, 'c' "
                     "FROM generate_series(1, 20000) g, (SELECT %s::text[] AS ids) t",
                     (ns_id, A, ids))
        conn.execute("ANALYZE datastore_row_comments")
    with _connect() as conn:
        plan = "\n".join(r["QUERY PLAN"] for r in conn.execute(
            "EXPLAIN " + db.datastore_commentaires.SQL_COMPTES,
            db.datastore_commentaires.params_comptes(ns_id, ids[:100])).fetchall())
    assert "idx_datastore_row_comments_ligne" in plan, plan
    assert "Seq Scan on datastore_row_comments" not in plan, plan
    assert "Sort" not in plan, plan
    assert len(db.datastore_commentaires.comptes(ns_id, ids[:100])) == 100


# ── 4. la face agent ───────────────────────────────────────────────────────────

def test_les_outils_sont_les_memes_capacites():
    from oto_mcp.capabilities import registry
    attendu = {
        "me.datastore.row_comments": ("data_row_comments", [("GET", "/comments")]),
        "me.datastore.row_comment": ("data_row_comment", [("POST", "/comments")]),
        "me.datastore.delete_row_comment": (None, [("DELETE", "/comments/{comment_id}")]),
    }
    cap = registry.by_key("me.datastore.comment_counts")
    assert (cap.mcp, [(b.verb, b.path) for b in cap.rest_bindings()]) == (
        None, [("GET", "/api/datastores/{datastore}/comments/counts")])
    for cle, (mcp, chemins) in attendu.items():
        cap = registry.by_key(cle)
        assert cap.mcp == mcp
        assert [(b.verb, b.path.split("{row_id}")[1]) for b in cap.rest_bindings()] \
            == chemins


# ── 5. cellules et colonnes ────────────────────────────────────────────────────

def _col_url(ns, col) -> str:
    return f"/api/datastores/{ns}/columns/{col}/comments"


def test_un_commentaire_de_cellule_est_dans_le_fil_de_sa_ligne(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    _poster(rest, ns, rid, "sur la ligne")
    r = rest.post(_url(ns, rid), headers=_h(), json={"body": "sur la cellule", "column": "n"})
    assert r.status_code == 201, r.text
    assert (r.json()["column"], r.json()["body"]) == ("n", "sur la cellule")
    fil = rest.get(_url(ns, rid), headers=_h()).json()["comments"]
    assert [(x["body"], x["column"]) for x in fil] == [("sur la ligne", None),
                                                       ("sur la cellule", "n")]
    cellule = rest.get(_url(ns, rid), headers=_h(), params={"column": "n"}).json()["comments"]
    assert [x["body"] for x in cellule] == ["sur la cellule"]
    # Retirée par la route de la ligne, comme tout commentaire de la ligne.
    cid = r.json()["id"]
    assert rest.delete(f"{_url(ns, rid)}/{cid}", headers=_h()).status_code == 200
    assert rest.get(_url(ns, rid), headers=_h(), params={"column": "n"}).json()["comments"] == []


def test_une_cle_inconnue_est_refusee_une_cle_des_donnees_acceptee(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    r = rest.post(_url(ns, rid), headers=_h(), json={"body": "x", "column": "inconnue"})
    assert (r.status_code, r.json()["error"]) == (400, "unknown_column")
    r = rest.post(_col_url(ns, "inconnue"), headers=_h(), json={"body": "x"})
    assert (r.status_code, r.json()["error"]) == (400, "unknown_column")
    # Une clé restée dans les DONNÉES sans être déclarée (une colonne retirée du schéma
    # après coup) vaut encore une colonne : ses lignes la portent toujours.
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, 'vieille', "
                     "'{\"n\": 2, \"libre\": \"v\"}'::jsonb)", (ns_id,))
    assert rest.post(_url(ns, "vieille"), headers=_h(),
                     json={"body": "x", "column": "libre"}).status_code == 201
    assert rest.post(_col_url(ns, "libre"), headers=_h(), json={"body": "x"}).status_code == 201
    r = rest.post(_url(ns, rid), headers=_h(), json={"body": "x", "column": "libre"})
    assert (r.status_code, r.json()["error"]) == (400, "unknown_column"), \
        "une cellule se juge sur SA ligne : `libre` n'y est pas"


def test_une_colonne_a_son_propre_fil(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    _partager(ns_id, B)
    r = rest.post(_col_url(ns, "n"), headers=_h(), json={"body": "  la colonne  "})
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["column"], c["body"], c["mine"], c["can_delete"]) == ("n", "la colonne", True, True)
    rb = rest.post(_col_url(ns_id, "n"), headers=_h(B), json={"body": "du lecteur"})
    assert rb.status_code == 201, rb.text
    lu = rest.get(_col_url(ns, "n"), headers=_h()).json()
    assert ([x["body"] for x in lu["comments"]], lu["column"], lu["ns_id"]) == (
        ["la colonne", "du lecteur"], "n", ns_id)
    # Pas dans le fil d'une ligne.
    assert rest.get(_url(ns, rid), headers=_h()).json()["comments"] == []
    # Retrait : le sien, ou qui gouverne.
    r = rest.delete(f"{_col_url(ns_id, 'n')}/{c['id']}", headers=_h(B))
    assert (r.status_code, r.json()["error"]) == (403, "comment_not_yours")
    assert rest.delete(f"{_col_url(ns, 'n')}/{rb.json()['id']}", headers=_h()).status_code == 200
    r = rest.delete(f"{_col_url(ns, 'n')}/{rb.json()['id']}", headers=_h())
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")
    # Un commentaire de ligne ne se retire pas par la route d'une colonne.
    lc = _poster(rest, ns, rid, "de ligne").json()
    r = rest.delete(f"{_col_url(ns, 'n')}/{lc['id']}", headers=_h())
    assert (r.status_code, r.json()["error"]) == (404, "comment_not_found")


def test_colonne_hors_perimetre_est_un_tableau_inconnu(rest):
    ns, ns_id, _ = _table_et_ligne(rest)
    cid = rest.post(_col_url(ns, "n"), headers=_h(), json={"body": "à moi"}).json()["id"]
    for r in (rest.get(_col_url(ns_id, "n"), headers=_h(C)),
              rest.post(_col_url(ns_id, "n"), headers=_h(C), json={"body": "intrus"}),
              rest.delete(f"{_col_url(ns_id, 'n')}/{cid}", headers=_h(C))):
        assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found"), r.text


def test_les_comptes_rendent_cellules_et_colonnes(rest):
    ns, ns_id, r1 = _table_et_ligne(rest)
    r2 = rest.post(f"/api/datastores/{ns}/rows", headers=_h(), json={"n": 2}).json()["_id"]
    _poster(rest, ns, r1, "ligne")
    for _ in range(2):
        rest.post(_url(ns, r1), headers=_h(), json={"body": "cellule", "column": "n"})
    rest.post(_url(ns, r2), headers=_h(), json={"body": "cellule 2", "column": "n"})
    rest.post(_col_url(ns, "n"), headers=_h(), json={"body": "colonne"})
    out = _comptes(rest, ns, [r1]).json()
    assert out["counts"] == {r1: 3}, "le compte d'une ligne compte ses cellules"
    assert out["cells"] == {r1: {"n": 2}}
    assert out["columns"] == {"n": 1}
    vide = _comptes(rest, ns, ["pas-une-ligne"]).json()
    assert (vide["counts"], vide["cells"], vide["columns"]) == ({}, {}, {"n": 1})


def test_une_cellule_d_une_ligne_supprimee_ne_se_compte_plus(rest):
    ns, ns_id, rid = _table_et_ligne(rest)
    rest.post(_url(ns, rid), headers=_h(), json={"body": "cellule", "column": "n"})
    assert rest.delete(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).status_code == 200
    out = _comptes(rest, ns, [rid]).json()
    assert (out["counts"], out["cells"]) == ({}, {})


def test_la_base_refuse_un_commentaire_sans_cible(live):
    import psycopg
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns_id = db.create_datastore("user", A, f"sans-cible-{uuid.uuid4().hex[:6]}")
    with pytest.raises(psycopg.errors.CheckViolation):
        with _connect() as conn:
            conn.execute("INSERT INTO datastore_row_comments (ns_id, auteur_sub, body) "
                         "VALUES (%s, %s, 'x')", (ns_id, A))


def test_les_outils_agent_prennent_la_colonne_et_les_routes_de_colonne_sont_rest_seules():
    from oto_mcp.capabilities import registry
    from oto_mcp.capabilities.datastore import comments as dc
    assert "column" in dc.RowCommentInput.model_fields
    assert "column" in dc.RowCommentsInput.model_fields
    for cle, (verbe, fin) in {
            "me.datastore.column_comments": ("GET", "/comments"),
            "me.datastore.column_comment": ("POST", "/comments"),
            "me.datastore.delete_column_comment": ("DELETE", "/comments/{comment_id}")}.items():
        cap = registry.by_key(cle)
        assert cap.mcp is None
        assert [(b.verb, b.path.split("{column}")[1]) for b in cap.rest_bindings()] == [
            (verbe, fin)]
