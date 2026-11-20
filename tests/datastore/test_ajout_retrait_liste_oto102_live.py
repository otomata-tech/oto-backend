"""Ajouter et retirer dans une colonne-liste SANS la relire, et compter ses éléments
(oto#102), sur une base réelle.

Ce que le banc tient : `tags[+]` ajoute un élément ou une liste d'éléments, `tags[-]`
retire des valeurs, le geste se résout sous le verrou de la ligne — deux ajouts partis
au même instant finissent TOUS LES DEUX dans la liste, sans précondition de révision ni
réservation. Les éléments ajoutés passent la validation, le journal porte l'avant et
l'après, et toutes les faces héritent du geste (store, REST, MCP ; patch par `id`,
fusion par clé, lot, création). `group_by: "tags[]"` compte par élément.

⚠️ Le banc ÉCRIT réellement et relit le STOCKAGE : une assertion sur la réponse prouverait
seulement que la réponse est d'accord avec elle-même.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore.errors import RowValidationError

SUB = "usr_liste_oto102"
SCHEMA = {"key": "siren", "fields": [
    {"key": "siren", "type": "text"},
    {"key": "tags", "type": "list", "of": {"type": "text"}},
    {"key": "notes", "type": "list", "of": {"type": "number"}},
    {"key": "journal", "type": "list", "of": {"type": "object", "fields": [
        {"key": "quand", "type": "date", "required": True},
        {"key": "qui", "type": "text"}]}},
]}
TOURS = 8


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@liste.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@liste.invalid", name=SUB)


@pytest.fixture(scope="module")
def client(base):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture(scope="module")
def outils(base):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("liste-oto102")
    tools_ds.register(mcp)
    return {n: asyncio.run(mcp.get_tool(n)).fn for n in ("data_write", "data_aggregate")}


@pytest.fixture
def acteur(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)


def _stockee(ns_id: int, row_id: str) -> dict:
    from oto_mcp import db
    return db.datastore_get_row(ns_id, row_id)["data"]


def _table(**data) -> tuple:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t102-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, SCHEMA)
    rid = st.append_row(ns, {"siren": "552032534", **data})["_id"]
    return st, ns, ns_id, rid


@pytest.fixture
def table(base):
    return _table(tags=["a", "b", "a"])


def _refus(geste) -> RowValidationError:
    with pytest.raises(RowValidationError) as exc:
        geste()
    return exc.value


def _ensemble(*gestes) -> list:
    """Lance les gestes au MÊME instant (barrière) ; rend les exceptions, jamais avalées."""
    barriere = threading.Barrier(len(gestes))
    erreurs: list = []

    def courir(geste):
        barriere.wait()
        try:
            geste()
        except BaseException as e:  # noqa: BLE001 — rendue au test qui l'examine
            erreurs.append(e)

    fils = [threading.Thread(target=courir, args=(g,)) for g in gestes]
    for f in fils:
        f.start()
    for f in fils:
        f.join(timeout=60)
    assert not any(f.is_alive() for f in fils), "un geste n'a pas rendu la main en 60 s"
    return erreurs


# ══ la concurrence : deux ajouts, aucune perte ═════════════════════════════════

def test_deux_ajouts_simultanes_ne_perdent_rien(base):
    st, ns, ns_id, rid = _table()
    for i in range(TOURS):
        erreurs = _ensemble(
            lambda: st.update_row(ns, rid, {"tags[+]": f"x{i}",
                                            "journal[+]": {"quand": "2026-10-04",
                                                           "qui": f"x{i}"}}),
            lambda: st.update_row(ns, rid, {"tags[+]": f"y{i}",
                                            "journal[+]": {"quand": "2026-10-04",
                                                           "qui": f"y{i}"}}))
        assert erreurs == []
    data = _stockee(ns_id, rid)
    attendus = {f"{p}{i}" for p in "xy" for i in range(TOURS)}
    assert sorted(data["tags"]) == sorted(attendus)
    assert sorted(e["qui"] for e in data["journal"]) == sorted(attendus)


def test_deux_ajouts_simultanes_par_la_cle_metier_ne_perdent_rien(base):
    st, ns, ns_id, rid = _table()
    for i in range(TOURS):
        erreurs = _ensemble(
            lambda: st.append_row(ns, {"siren": "552032534", "tags[+]": f"x{i}"},
                                  key="siren"),
            lambda: st.write_rows(ns, [{"siren": "552032534", "tags[+]": f"y{i}"}],
                                  key="siren"))
        assert erreurs == []
    assert sorted(_stockee(ns_id, rid)["tags"]) == sorted(
        f"{p}{i}" for p in "xy" for i in range(TOURS))


# ══ l'ajout ════════════════════════════════════════════════════════════════════

def test_une_liste_s_ajoute_dans_l_ordre_doublons_gardes(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"tags[+]": ["c", "a", "c"]})
    assert _stockee(ns_id, rid)["tags"] == ["a", "b", "a", "c", "a", "c"]


def test_une_colonne_vide_recoit_l_ajout(base):
    st, ns, ns_id, rid = _table()
    st.update_row(ns, rid, {"tags[+]": "seul"})
    assert _stockee(ns_id, rid)["tags"] == ["seul"]


@pytest.mark.parametrize("valeur, attendu", [
    (None, "`null`"), ([], "an empty list"), (["a", None], "a `null` in the list")])
def test_un_ajout_qui_ne_designe_rien_est_refuse(table, valeur, attendu):
    st, ns, ns_id, rid = table
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, {"tags[+]": valeur}))
    assert attendu in " ".join(e.errors)
    assert _stockee(ns_id, rid) == avant


# ══ le retrait par valeur ══════════════════════════════════════════════════════

def test_le_retrait_ote_toutes_les_occurrences(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"tags[-]": "a"})
    assert _stockee(ns_id, rid)["tags"] == ["b"]


def test_retirer_la_derniere_valeur_efface_la_colonne(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"tags[-]": ["a", "b"]})
    assert _stockee(ns_id, rid).get("tags") is None


def test_une_valeur_absente_est_refusee_et_rien_n_est_ecrit(table):
    st, ns, ns_id, rid = table
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, {"tags[-]": ["a", "zz"],
                                               "tags[+]": "neuf"}))
    texte = " ".join(e.errors)
    assert '`"zz"`' in texte and "nothing to remove" in texte
    assert _stockee(ns_id, rid) == avant


def test_le_retrait_passe_avant_l_ajout(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"tags[-]": "a", "tags[+]": "a"})
    assert _stockee(ns_id, rid)["tags"] == ["b", "a"]


def test_l_egalite_est_celle_du_json(base):
    st, ns, ns_id, rid = _table(notes=[1, 2.5, 1.0])
    st.update_row(ns, rid, {"notes[-]": 1})
    assert _stockee(ns_id, rid)["notes"] == [2.5]


def test_une_fiche_ne_se_retire_pas_par_valeur(base):
    st, ns, ns_id, rid = _table(journal=[{"quand": "2026-10-01"}])
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, {"journal[-]": {"quand": "2026-10-01"}}))
    assert "`\"journal[<index>]\": null`" in " ".join(e.errors)
    assert _stockee(ns_id, rid) == avant


def test_les_rangs_ecrits_restent_justes_apres_un_retrait(base):
    """La validation juge l'élément écrit à son rang dans la liste RÉSULTANTE : un
    retrait par valeur placé avant lui le décale."""
    st, ns, ns_id, rid = _table(tags=["a", "b", "c"])
    st.update_row(ns, rid, {"tags[-]": "a", "tags[+]": "d"})
    assert _stockee(ns_id, rid)["tags"] == ["b", "c", "d"]


# ══ la validation des éléments ajoutés ═════════════════════════════════════════

def test_un_element_ajoute_hors_type_est_refuse(base):
    st, ns, ns_id, rid = _table(notes=[1])
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, {"notes[+]": [2, "pas un nombre"]}))
    assert "notes[2]" in " ".join(e.errors)
    assert _stockee(ns_id, rid) == avant


def test_une_fiche_ajoutee_incomplete_est_refusee(base):
    st, ns, ns_id, rid = _table()
    e = _refus(lambda: st.update_row(ns, rid, {"journal[+]": [
        {"quand": "2026-10-04", "qui": "Ada"}, {"qui": "Bob"}]}))
    assert "journal[1].quand" in " ".join(e.errors)
    assert _stockee(ns_id, rid).get("journal") is None


def test_les_dates_ajoutees_et_retirees_se_normalisent(base):
    schema_dates = {"fields": [{"key": "jours", "type": "list", "of": {"type": "date"}}]}
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t102d-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, schema_dates)
    rid = st.append_row(ns, {"jours[+]": ["04/10/2026", "2026-10-05"]})["_id"]
    assert _stockee(ns_id, rid)["jours"] == ["2026-10-04", "2026-10-05"]
    st.update_row(ns, rid, {"jours[-]": "04/10/2026"})
    assert _stockee(ns_id, rid)["jours"] == ["2026-10-05"]


# ══ le journal des révisions ═══════════════════════════════════════════════════

def test_le_journal_porte_l_avant_et_l_apres(table):
    from oto_mcp.db import historique
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"tags[-]": "b", "tags[+]": ["c", "d"]})
    diff = historique.revisions_de_ligne(ns_id, rid, champ="tags")[0]["diff"]
    assert diff["tags"] == {"avant": ["a", "b", "a"], "apres": ["a", "a", "c", "d"]}


# ══ les faces ══════════════════════════════════════════════════════════════════

def test_la_face_REST_ajoute_et_retire(client, table):
    _st, ns, ns_id, rid = table
    h = {"Authorization": f"Bearer {SUB}"}
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=h,
                     json={"tags[+]": ["c"], "tags[-]": "b"})
    assert r.status_code == 200, r.text
    assert _stockee(ns_id, rid)["tags"] == ["a", "a", "c"]
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=h,
                     json={"tags[-]": "zz"})
    assert r.status_code == 400 and "nothing to remove" in r.text, r.text
    r = client.post(f"/api/datastores/{ns}/rows", headers=h,
                    json={"siren": "130025265", "tags[+]": ["n1", "n2"]})
    assert r.status_code in (200, 201), r.text
    assert _stockee(ns_id, r.json()["_id"])["tags"] == ["n1", "n2"]


def test_la_face_MCP_ajoute_retire_et_le_lot_aussi(outils, acteur, table):
    _st, ns, ns_id, rid = table
    outils["data_write"](datastore=ns, id=rid, row={"tags[-]": "a"})
    assert _stockee(ns_id, rid)["tags"] == ["b"]
    outils["data_write"](datastore=ns, key="siren",
                         rows=[{"siren": "552032534", "tags[+]": ["c", "d"]}])
    assert _stockee(ns_id, rid)["tags"] == ["b", "c", "d"]


# ══ compter par élément ════════════════════════════════════════════════════════

def test_l_agregat_compte_par_element(outils, acteur, base):
    st, ns, _ns_id, _rid = _table(tags=["chaud", "rappel", "chaud"])
    st.append_row(ns, {"siren": "130025265", "tags": ["rappel"]})
    st.append_row(ns, {"siren": "356000000"})
    st.append_row(ns, {"siren": "775665019", "tags": []})

    par_tag = st.aggregate(ns, group_by="tags[]",
                           metrics=[{"op": "count"}, {"op": "count_rows"}])
    assert {r["tags[]"]: (r["count"], r["count_rows"]) for r in par_tag} == {
        "chaud": (2, 1), "rappel": (2, 2)}

    filtre = st.aggregate(ns, filters=[{"field": "tags[]", "op": "eq",
                                        "value": "rappel"}])
    assert filtre == [{"count": 2}]

    total = st.aggregate(ns, metrics=[{"op": "count", "field": "tags[]"}])
    assert total[0]["count_tags[]"] == 4

    servi = outils["data_aggregate"](datastore=ns, group_by="tags[]",
                                     metrics=[{"op": "count_rows"}])
    assert {r["tags[]"]: r["count_rows"] for r in servi["results"]} == {
        "chaud": 1, "rappel": 2}


def test_un_element_de_rang_precis_se_regroupe(base):
    st, ns, _ns_id, _rid = _table(tags=["chaud", "rappel"])
    st.append_row(ns, {"siren": "130025265", "tags": ["froid"]})
    res = st.aggregate(ns, group_by="tags[0]")
    assert {r["tags[0]"]: r["count"] for r in res} == {"chaud": 1, "froid": 1}


def test_l_element_nu_ne_se_trie_pas(base):
    st, ns, _ns_id, _rid = _table(tags=["chaud"])
    with pytest.raises(ValueError, match="TOUS les items"):
        st.page_rows(ns, order_by="tags[]")


def test_un_nom_de_colonne_a_crochets_reste_un_nom():
    from oto_mcp.db.paths import split_list_path
    assert split_list_path("Note [1]") is None
    assert split_list_path("tags[]") == ("tags", None, None)
    assert json.dumps(split_list_path("tags[2]")) == '["tags", 2, null]'
