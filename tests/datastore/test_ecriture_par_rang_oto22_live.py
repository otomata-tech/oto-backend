"""L'écriture PAR RANG dans une colonne-liste de fiches (oto#22, point c), sur une base réelle.

Ce que le banc tient : on écrit à l'adresse qu'on lit — `contacts[1].email`, une couche
`contacts[0].email.comment`, l'ajout `contacts[+]`, la suppression `contacts[0]: null` —
par la même fusion que les colonnes (l'origine survit, `comment`/`link` tombent avec une
valeur qui change), sur les trois faces (store, REST, MCP) et les trois chemins (patch par
`id`, fusion par clé, lot). Les refus nomment la forme qui aboutit. La validation ne juge
que l'élément écrit. Le journal des révisions porte l'avant et l'après de la colonne.

⚠️ Le banc ÉCRIT réellement et relit le STOCKAGE : une assertion sur la réponse prouverait
seulement que la réponse est d'accord avec elle-même.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore.errors import RowValidationError


SUB = "usr_rang_oto22"
SCHEMA = {"key": "siren", "fields": [
    {"key": "siren", "type": "text"},
    {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": "nom", "type": "text", "required": True},
        {"key": "email", "type": "email"},
        {"key": "debut", "type": "date"}]}},
]}
SCHEMA_CLE = {"key": "siren", "fields": [
    {"key": "siren", "type": "text"},
    {"key": "contacts", "type": "list", "of": {"type": "object", "key": "role", "fields": [
        {"key": "role", "type": "text"},
        {"key": "nom", "type": "text"}]}},
]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@rang.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@rang.invalid", name=SUB)


@pytest.fixture(scope="module")
def client(base):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture(scope="module")
def outils(base):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("rang-oto22")
    tools_ds.register(mcp)
    return {"data_write": asyncio.run(mcp.get_tool("data_write")).fn}


@pytest.fixture
def acteur(monkeypatch):
    from oto_mcp import access
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)


def _poser_a_la_main(ns_id: int, row_id: str, data: dict) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET data = %s::jsonb "
                     "WHERE ns_id = %s AND row_id = %s", (json.dumps(data), ns_id, row_id))


def _stockee(ns_id: int, row_id: str) -> dict:
    from oto_mcp import db
    return db.datastore_get_row(ns_id, row_id)["data"]


def _table(schema: dict, contacts: list) -> tuple:
    """Un tableau neuf, une ligne dont les contacts sont POSÉS EN BASE — l'état de départ
    ne dépend d'aucun des gestes que le banc éprouve."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t22-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, schema)
    rid = st.append_row(ns, {"siren": "552032534"})["_id"]
    _poser_a_la_main(ns_id, rid, {"siren": "552032534", "contacts": contacts})
    return st, ns, ns_id, rid


ADA = {"nom": "Ada", "email": {"valeur": "ada@exemple.fr", "origine": "ada@import.fr",
                               "comment": "fichier client"}}
BOB = {"nom": "Bob", "email": "bob@exemple.fr"}


@pytest.fixture
def table(base):
    return _table(SCHEMA, [ADA, BOB])


def _refus(geste) -> RowValidationError:
    with pytest.raises(RowValidationError) as exc:
        geste()
    return exc.value


# ══ écrire à l'adresse qu'on lit ═══════════════════════════════════════════════

def test_un_attribut_s_ecrit_a_son_rang_et_rien_d_autre_ne_bouge(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[1].email": "bob@neuf.fr"})
    assert _stockee(ns_id, rid)["contacts"] == [
        ADA, {"nom": "Bob", "email": "bob@neuf.fr"}]


def test_l_attribut_suit_la_regle_des_colonnes_l_origine_survit_le_comment_tombe(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[0].email": "ada@neuf.fr"})
    assert _stockee(ns_id, rid)["contacts"][0]["email"] == {
        "valeur": "ada@neuf.fr", "origine": "ada@import.fr"}
    st.update_row(ns, rid, {"contacts[0].email": {"valeur": "ada@neuf.fr",
                                                  "comment": "site officiel"}})
    assert _stockee(ns_id, rid)["contacts"][0]["email"] == {
        "valeur": "ada@neuf.fr", "origine": "ada@import.fr", "comment": "site officiel"}


def test_une_couche_seule_annote_l_attribut_en_place_sans_le_reecrire(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[1].email.comment": "vu sur le site"})
    assert _stockee(ns_id, rid)["contacts"][1]["email"] == {
        "valeur": "bob@exemple.fr", "comment": "vu sur le site"}


def test_null_efface_l_attribut_seul(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[1].email": None})
    assert _stockee(ns_id, rid)["contacts"] == [ADA, {"nom": "Bob"}]


def test_l_ajout_pose_une_fiche_en_fin_de_liste_par_les_memes_gardes(table):
    """Les couches pointées de la fiche se rangent, ses dates se normalisent : les gardes
    du payload valent pour l'élément ajouté."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[+]": {"nom": "Cy", "email": "cy@exemple.fr",
                                            "email.comment": "annuaire",
                                            "debut": "04/09/2026"}})
    assert _stockee(ns_id, rid)["contacts"] == [ADA, BOB, {
        "nom": "Cy", "email": {"valeur": "cy@exemple.fr", "comment": "annuaire"},
        "debut": "2026-09-04"}]


def test_les_rangs_d_un_geste_designent_la_liste_en_place(table):
    """`contacts[0]: null` et `contacts[1].email` dans le même geste : le rang 1 est
    BOB tel qu'il est en place, pas l'élément qui deviendrait le premier."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[0]": None, "contacts[1].email": "bob@neuf.fr"})
    assert _stockee(ns_id, rid)["contacts"] == [{"nom": "Bob", "email": "bob@neuf.fr"}]


def test_supprimer_le_dernier_element_efface_la_colonne_et_le_dit(base):
    st, ns, ns_id, rid = _table(SCHEMA, [BOB])
    st.update_row(ns, rid, {"contacts[0]": None})
    assert "contacts" not in _stockee(ns_id, rid) or _stockee(ns_id, rid)["contacts"] is None
    assert "valeurs_effacees" in st.off_schema_report()


def test_le_journal_porte_l_avant_et_l_apres_de_la_colonne(table):
    from oto_mcp.db import historique
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"contacts[1].email": "bob@neuf.fr"})
    diff = historique.revisions_de_ligne(ns_id, rid, champ="contacts")[0]["diff"]
    assert diff["contacts"]["avant"] == [ADA, BOB]
    assert diff["contacts"]["apres"] == [ADA, {"nom": "Bob", "email": "bob@neuf.fr"}]


# ══ les refus nomment la forme qui aboutit ═════════════════════════════════════

def test_un_rang_hors_bornes_est_refuse_en_nommant_la_taille_et_l_ajout(table):
    st, ns, ns_id, rid = table
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, {"contacts[5].email": "x@y.fr"}))
    texte = " ".join(e.errors)
    assert "`contacts` has 2 items" in texte and "index 5 does not exist" in texte
    assert "`contacts[+]`" in texte
    assert _stockee(ns_id, rid) == avant


@pytest.mark.parametrize("geste, attendu", [
    ({"contacts[0]": {"email": "x@y.fr"}}, "`contacts[0].<attribute>`"),
    ({"contacts[].email": "x@y.fr"}, "ALL the items"),
    ({"contacts[+].email": "x@y.fr"}, "appended WHOLE"),
    ({"contacts[role=RH].email": "x@y.fr"}, "its INDEX"),
    ({"contacts[0].email": "x@y.fr", "contacts": [BOB]}, "WHOLE and by index"),
    ({"siren[0].x": "1"}, "only a list column"),
])
def test_les_formes_non_servies_sont_refusees_et_orientees(table, geste, attendu):
    st, ns, ns_id, rid = table
    avant = _stockee(ns_id, rid)
    e = _refus(lambda: st.update_row(ns, rid, geste))
    assert attendu in " ".join(e.errors)
    assert _stockee(ns_id, rid) == avant


def test_sous_of_key_le_rang_ne_fabrique_pas_une_identite_en_double(base):
    st, ns, ns_id, rid = _table(SCHEMA_CLE, [{"role": "RH", "nom": "Ada"},
                                            {"role": "DAF", "nom": "Bob"}])
    e = _refus(lambda: st.update_row(ns, rid, {"contacts[1].role": "RH"}))
    assert "duplicated" in " ".join(e.errors)
    e = _refus(lambda: st.update_row(ns, rid, {"contacts[+]": {"role": "DAF"}}))
    assert "duplicated" in " ".join(e.errors)
    st.update_row(ns, rid, {"contacts[1].nom": "Bruno"})
    assert _stockee(ns_id, rid)["contacts"][1] == {"role": "DAF", "nom": "Bruno"}


# ══ la validation juge l'élément écrit, et lui seul ════════════════════════════

def test_un_voisin_incomplet_ne_bloque_pas_l_element_ecrit(base):
    """L'élément de rang 0 n'a pas son `nom` requis (posé à la main) : écrire le rang 1
    passe, et le défaut du voisin ne bloque rien (J4, oto#140)."""
    st, ns, ns_id, rid = _table(SCHEMA, [{"email": "sans-nom@exemple.fr"}, BOB])
    st.update_row(ns, rid, {"contacts[1].email": "bob@neuf.fr"})
    assert _stockee(ns_id, rid)["contacts"][1]["email"] == "bob@neuf.fr"


def test_l_element_fautif_est_refuse_et_la_charge_ne_porte_que_lui(table):
    st, ns, ns_id, rid = table
    e = _refus(lambda: st.update_row(ns, rid, {"contacts[+]": {"email": "x@y.fr"}}))
    assert "contacts[2].nom" in " ".join(e.errors)
    assert e.details.get("a_renvoyer_elements") == ["contacts[2]"]
    assert list(e.details["a_renvoyer"]) == ["contacts"]
    assert len(e.details["a_renvoyer"]["contacts"]) == 1


def test_une_couche_exigee_ne_juge_que_l_element_ecrit(base):
    schema = {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
            {"key": "nom", "type": "text"},
            {"key": "email", "type": "text", "required_layers": ["comment"]}]}}]}
    st, ns, ns_id, rid = _table(schema, [{"nom": "Ada", "email": "ada@exemple.fr"},
                                         {"nom": "Bob"}])
    st.update_row(ns, rid, {"contacts[1].email": {"valeur": "bob@exemple.fr",
                                                  "comment": "site"}})
    e = _refus(lambda: st.update_row(ns, rid, {"contacts[1].email": "bob@autre.fr"}))
    assert "contacts[1].email" in " ".join(e.errors)
    assert "contacts[0]" not in " ".join(e.errors)


# ══ les chemins : fusion par clé, lot, création ════════════════════════════════

def test_la_fusion_par_cle_et_le_lot_ecrivent_par_rang(table):
    st, ns, ns_id, rid = table
    st.append_row(ns, {"siren": "552032534", "contacts[1].email": "bob@cle.fr"},
                  key="siren")
    assert _stockee(ns_id, rid)["contacts"][1]["email"] == "bob@cle.fr"
    recap = st.write_rows(ns, [{"siren": "552032534", "contacts[0].nom": "Ada L."}],
                          key="siren")
    assert recap["updated"] == 1
    assert _stockee(ns_id, rid)["contacts"][0]["nom"] == "Ada L."


def test_une_ligne_creee_accepte_l_ajout_et_refuse_un_rang(table):
    from oto_mcp import db
    st, ns, ns_id, _rid = table
    cree = st.append_row(ns, {"siren": "130025265", "contacts[+]": {"nom": "Cy"}})
    assert _stockee(ns_id, cree["_id"])["contacts"] == [{"nom": "Cy"}]
    e = _refus(lambda: st.write_rows(ns, [{"siren": "356000000", "contacts[0].nom": "x"}],
                                     key="siren"))
    assert "CREATES the row" in " ".join(e.errors)
    assert db.datastore_find_row_id_by_key(ns_id, "siren", "356000000") is None


# ══ les faces : REST et MCP ════════════════════════════════════════════════════

def test_la_face_REST_ecrit_par_rang_et_refuse_hors_bornes(client, table):
    _st, ns, ns_id, rid = table
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts[1].email": "bob@rest.fr"})
    assert r.status_code == 200, r.text
    assert _stockee(ns_id, rid)["contacts"][1]["email"] == "bob@rest.fr"
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts[9].email": "x@y.fr"})
    assert r.status_code == 400 and "index 9 does not exist" in r.text, r.text


def test_la_face_MCP_ecrit_par_rang(outils, acteur, table):
    _st, ns, ns_id, rid = table
    outils["data_write"](datastore=ns, id=rid, row={"contacts[+]": {"nom": "Cy"}})
    assert _stockee(ns_id, rid)["contacts"][-1] == {"nom": "Cy"}


# ══ ce qui n'est pas une adresse de rang reste un nom de colonne ═══════════════

def test_un_crochet_hors_grammaire_reste_un_nom_de_colonne():
    from oto_mcp.datastore.rangs import sortir_les_rangs
    reste, rangs = sortir_les_rangs(None, {"Prix [EUR]": 3, "note[a]": 1})
    assert rangs is None and reste == {"Prix [EUR]": 3, "note[a]": 1}
