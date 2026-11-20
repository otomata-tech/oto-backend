"""L'historique d'une ligne et le parcours qui lit les valeurs — jalon M3 (oto#273).

Sur une VRAIE base et par les VRAIES routes : ce qui se juge ici, c'est ce que le journal
écrit par PostgreSQL rend à la lecture, et ce que la route en laisse voir à qui.

1. `GET …/rows/{row_id}/history` : les révisions, plus récente d'abord, valeurs, acteur,
   source et geste ; le filtre `champ` ; la pagination par `before_id` ;
2. la couverture est DITE : date de mise en service, insertion journalisée ou non — une
   ligne antérieure au journal ne se présente pas comme « jamais modifiée » ;
3. l'accès : un lecteur lit l'historique d'une ligne vivante ; une ligne supprimée ne se
   lit que par qui gouverne le tableau, sa suppression en tête (`deletion`) ; une ligne
   inconnue est un 404 ;
4. la face agent : `data_row_history` est la même capacité, les colonnes masquées aux
   agents sortent des diffs ;
5. le parcours (`…/activity`) : l'appel REST porte ses révisions, valeurs comprises ;
   une écriture qu'aucun appel ne porte devient une entrée `kind='revision'`.
"""
from __future__ import annotations

import json
import uuid

import pytest

A = "sub-historique-273-a"
B = "sub-historique-273-b"


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@historique.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def rest(live):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oto_mcp import db
    from oto_mcp.api import routes as api_routes
    for sub in (A, B):
        db.upsert_user(sub, email=f"{sub}@historique.invalid", name=sub)
    app = Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None))
    return TestClient(api_routes.RestCallLogger(app))


def _h(sub: str = A) -> dict:
    return {"Authorization": f"Bearer {sub}"}


#: Les colonnes que les bancs écrivent, déclarées sans type (oto#124).
LIBRE = {"fields": [{"key": k} for k in ("n", "nom", "a", "b")]}


def _table(schema: dict | None = None) -> tuple[str, int]:
    from oto_mcp import db
    ns_id = db.create_datastore("user", A, f"historique-{uuid.uuid4().hex[:6]}")
    ns = str(ns_id)
    db.set_datastore_schema(ns_id, schema or LIBRE)
    return ns, ns_id


def _ligne(rest, ns: str, data: dict) -> str:
    r = rest.post(f"/api/datastores/{ns}/rows", headers=_h(), json=data)
    assert r.status_code == 201, r.text
    return r.json()["_id"]


def _patch(rest, ns: str, row_id: str, patch: dict) -> None:
    r = rest.patch(f"/api/datastores/{ns}/rows/{row_id}", headers=_h(), json=patch)
    assert r.status_code == 200, r.text


def _historique(rest, ns: str, row_id: str, sub: str = A, **params):
    return rest.get(f"/api/datastores/{ns}/rows/{row_id}/history",
                    headers=_h(sub), params=params)


# ── 1. l'historique ────────────────────────────────────────────────────────────

def test_l_historique_rend_les_valeurs_l_acteur_la_source_et_le_geste(rest):
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1, "nom": "A"})
    _patch(rest, ns, rid, {"n": 2})

    r = _historique(rest, ns, rid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["ns_id"], out["row_id"], out["row_deleted"]) == (ns_id, rid, False)
    revs = out["revisions"]
    assert [(e["rev"], e["diff"]) for e in revs] == [
        (1, {"n": {"avant": 1, "apres": 2}}),
        (0, {"n": {"apres": 1}, "nom": {"apres": "A"}}),
    ], "plus récente d'abord, valeurs avant et après"
    assert {(e["source"], e["acteur"]) for e in revs} == {("console", A)}
    assert revs[0]["geste_id"] and revs[0]["geste_id"] != revs[1]["geste_id"]
    assert revs[0]["id"] > revs[1]["id"] and revs[0]["at"]
    assert out["next_before_id"] is None


def test_champ_ne_garde_que_les_revisions_de_cette_colonne_et_sa_part_du_diff(rest):
    ns, _ = _table()
    rid = _ligne(rest, ns, {"a": 1, "b": 1})
    _patch(rest, ns, rid, {"a": 2})
    _patch(rest, ns, rid, {"b": 2})

    out = _historique(rest, ns, rid, champ="a").json()
    assert out["champ"] == "a"
    assert [e["diff"] for e in out["revisions"]] == [
        {"a": {"avant": 1, "apres": 2}}, {"a": {"apres": 1}}]
    # La couverture parle de la LIGNE, pas du filtre.
    assert out["coverage"]["total_revisions"] == 3
    assert _historique(rest, ns, rid, champ="").status_code == 400


def test_la_pagination_par_before_id_couvre_tout_sans_doublon(rest):
    ns, _ = _table()
    rid = _ligne(rest, ns, {"n": 0})
    for i in range(1, 5):
        _patch(rest, ns, rid, {"n": i})

    p1 = _historique(rest, ns, rid, limit=2).json()
    assert [e["rev"] for e in p1["revisions"]] == [4, 3]
    assert p1["next_before_id"] == p1["revisions"][-1]["id"]
    p2 = _historique(rest, ns, rid, limit=2, before_id=p1["next_before_id"]).json()
    p3 = _historique(rest, ns, rid, limit=2, before_id=p2["next_before_id"]).json()
    assert [e["rev"] for e in p2["revisions"] + p3["revisions"]] == [2, 1, 0]
    assert p3["next_before_id"] is None


# ── 2. la couverture ───────────────────────────────────────────────────────────

def test_la_couverture_est_dite_et_une_ligne_anterieure_ne_se_dit_pas_vierge(rest):
    from oto_mcp import db
    from oto_mcp.db import historique
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1})
    # Une ligne « d'avant le journal » : son insertion n'y est pas.
    _sql("DELETE FROM datastore_row_revisions WHERE ns_id = %s", ns_id)

    out = _historique(rest, ns, rid).json()
    assert out["revisions"] == []
    cov = out["coverage"]
    assert cov["journal_since"] == historique.MISE_EN_SERVICE == "2026-09-24"
    assert (cov["insert_recorded"], cov["total_revisions"]) == (False, 0)
    assert "n'est PAS une ligne jamais modifiée" in cov["note"]
    assert db.datastore_get_row(ns_id, rid), "la ligne vit : lue, pas refusée"

    _patch(rest, ns, rid, {"n": 2})
    cov = _historique(rest, ns, rid).json()["coverage"]
    assert (cov["insert_recorded"], cov["total_revisions"]) == (False, 1)
    assert cov["first_revision_at"]


# ── 3. l'accès ─────────────────────────────────────────────────────────────────

def test_un_lecteur_lit_la_ligne_vivante_pas_la_ligne_supprimee(rest):
    from oto_mcp import ownership
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1})
    ownership.grant(ownership.TYPE_RESSOURCE_DATASTORE, str(ns_id), "user", B, "read",
                    granted_by=A)

    assert _historique(rest, str(ns_id), rid, sub=B).status_code == 200
    assert rest.delete(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).status_code == 200

    r = _historique(rest, str(ns_id), rid, sub=B)
    assert (r.status_code, r.json()["error"]) == (404, "row_not_found")
    out = _historique(rest, ns, rid).json()
    assert out["row_deleted"] is True, "le propriétaire gouverne : il lit"
    assert [(e["rev"], e["suppression"]) for e in out["revisions"]] == [
        (0, True), (0, False)], "la suppression est la révision la plus récente"
    suppression = out["revisions"][0]
    assert suppression["diff"] == {"n": {"avant": 1}}
    assert (suppression["source"], suppression["acteur"]) == ("console", A)
    assert out["deletion"] == {k: suppression[k] for k in
                               ("id", "at", "acteur", "run_id", "source", "geste_id")}
    assert out["coverage"]["insert_recorded"] is True, \
        "la `rev` 0 de la suppression ne compte pas pour une insertion"


def test_ligne_supprimee_avant_le_journal_des_suppressions(rest):
    """Supprimée sans révision de suppression (avant qu'elles ne soient journalisées,
    ou journal coupé) : `row_deleted` vient du tableau, `deletion` est `null`."""
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1})
    assert rest.delete(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).status_code == 200
    _sql("DELETE FROM datastore_row_revisions WHERE ns_id = %s AND suppression", ns_id)
    out = _historique(rest, ns, rid).json()
    assert (out["row_deleted"], out["deletion"]) == (True, None)
    assert [e["suppression"] for e in out["revisions"]] == [False]


def test_ligne_recreee_n_est_pas_supprimee(rest):
    """Une suppression suivie d'une recréation sous le même `_id` : la ligne vit, et
    `deletion` ne s'affiche pas — la suppression reste lisible dans la liste."""
    from oto_mcp import db
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1})
    assert rest.delete(f"/api/datastores/{ns}/rows/{rid}", headers=_h()).status_code == 200
    db.datastore_insert_row(ns_id, rid, {"n": 2})
    out = _historique(rest, ns, rid).json()
    assert (out["row_deleted"], out["deletion"]) == (False, None)
    assert [(e["rev"], e["suppression"]) for e in out["revisions"]] == [
        (0, False), (0, True), (0, False)]


def test_ligne_inconnue_et_tableau_hors_perimetre(rest):
    ns, _ = _table()
    r = _historique(rest, ns, "pas-une-ligne")
    assert (r.status_code, r.json()["error"]) == (404, "row_not_found")
    rid = _ligne(rest, ns, {"n": 1})
    r = _historique(rest, ns, rid, sub=B)
    assert (r.status_code, r.json()["error"]) == (404, "datastore_not_found")


# ── 4. la face agent ───────────────────────────────────────────────────────────

def test_data_row_history_est_la_meme_capacite_et_dit_sa_couverture():
    from oto_mcp.capabilities import registry
    from oto_mcp.db import historique
    cap = registry.by_key("me.datastore.row_history")
    assert cap.mcp == "data_row_history"
    assert [(b.verb, b.path) for b in cap.rest_bindings()] == [
        ("GET", "/api/datastores/{datastore}/rows/{row_id}/history")]
    assert historique.MISE_EN_SERVICE in cap.description
    assert "NOT a row that was never modified" in cap.description


def test_face_agent_les_colonnes_masquees_sortent_des_diffs(rest):
    from oto_mcp import session_org
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.datastore import history
    ns, _ = _table({"fields": [{"key": "vu", "type": "number"},
                               {"key": "cache", "type": "number",
                                "agent_access": "none"}]})
    rid = _ligne(rest, ns, {"vu": 1, "cache": 1})
    _patch(rest, ns, rid, {"cache": 2})
    _patch(rest, ns, rid, {"vu": 2})

    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        out = history._row_history(ResolvedCtx(sub=A), history.RowHistoryInput(
            datastore=ns, row_id=rid))
        par_champ = history._row_history(ResolvedCtx(sub=A), history.RowHistoryInput(
            datastore=ns, row_id=rid, champ="cache"))
    finally:
        session_org.reset_call_face(jeton)
    assert [e["diff"] for e in out["revisions"]] == [
        {"vu": {"avant": 1, "apres": 2}}, {"vu": {"apres": 1}}], \
        "la révision qui ne touchait que `cache` disparaît, l'insertion en perd la clé"
    assert par_champ["revisions"] == []
    # Hors face agent (l'écran du propriétaire), tout est servi.
    assert len(_historique(rest, ns, rid).json()["revisions"]) == 3


# ── 5. le parcours d'une ligne lit les valeurs ─────────────────────────────────

def test_le_parcours_rattache_les_revisions_a_l_appel_et_montre_les_autres(rest):
    ns, ns_id = _table()
    rid = _ligne(rest, ns, {"n": 1})
    _patch(rest, ns, rid, {"n": 2})
    # Une écriture qu'aucun appel ne porte : hors serveur, sans estampille.
    _sql("UPDATE datastore_rows SET data = %s::jsonb WHERE ns_id = %s AND row_id = %s",
         json.dumps({"n": 3}), ns_id, rid)

    r = rest.get(f"/api/datastores/{ns}/rows/{rid}/activity", headers=_h())
    assert r.status_code == 200, r.text
    activity = r.json()["activity"]
    appels = [e for e in activity if e["kind"] == "rest"]
    ecriture = next(e for e in appels if e["revisions"]
                    and e["revisions"][0]["rev"] == 1)
    assert ecriture["revisions"][0]["diff"] == {"n": {"avant": 1, "apres": 2}}
    assert (ecriture["source"], ecriture["acteur"]) == ("console", A)
    assert ecriture["geste_id"] == ecriture["revisions"][0]["geste_id"]

    (hors,) = [e for e in activity if e["kind"] == "revision"]
    assert hors["revisions"][0]["diff"] == {"n": {"avant": 2, "apres": 3}}
    assert (hors["tool"], hors["source"], hors["row_id"], hors["fields"]) == \
        (None, None, rid, ["n"])
    assert activity == sorted(activity, key=lambda e: e["created_at"], reverse=True)
    # Même jeu de clés sur toutes les entrées : la forme ne dépend pas de l'origine.
    assert len({frozenset(e) for e in activity}) == 1
