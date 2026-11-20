"""Le vide ASSUMÉ, relu en `empties=sentinel` et réécrit, sur une base réelle (oto#204, étape 2).

Le banc sans base tient la forme et la parité ; celui-ci tient ce que le STOCKAGE porte, sur
toutes les faces qui servent une ligne à réécrire : REST (page, fiche, réservation), MCP
(`data_rows`, `data_claim_next`) et le store.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


SUB = "usr_reemission_204"
MARQUEUR = "oto.vide_assume"
MARQUEE = {"valeur": "", MARQUEUR: True}
SCHEMA = {"fields": [
    {"key": "raison", "type": "text"},
    {"key": "fonction", "type": "text", "required": True},
    {"key": "contacts", "type": "list", "of": {"type": "object", "fields": [
        {"key": "nom", "type": "text", "required": True},
        {"key": "fonction", "type": "text", "required": True},
        {"key": "note", "type": "text"}]}},
]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@vide.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


def _aucune_fuite(obj) -> None:
    assert "vide_assume" not in json.dumps(obj, ensure_ascii=False, default=str), (
        "le marqueur interne FUIT dans ce qui est servi")


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@vide.invalid", name=SUB)


@pytest.fixture(scope="module")
def client(base):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture(scope="module")
def outils(base):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("reemission-204")
    tools_ds.register(mcp)
    return {nom: asyncio.run(mcp.get_tool(nom)).fn
            for nom in ("data_rows", "data_claim_next", "data_write")}


@pytest.fixture
def acteur(monkeypatch):
    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    # Un claim d'agent se fait dans un run (#727) — ce banc porte sur la forme servie.
    monkeypatch.setattr(tools_ds, "_current_run", lambda: "run-204")


def _poser_a_la_main(ns_id: int, row_id: str, data: dict) -> None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("UPDATE datastore_rows SET data = %s::jsonb "
                     "WHERE ns_id = %s AND row_id = %s", (json.dumps(data), ns_id, row_id))


def _stockee(ns_id: int, row_id: str) -> dict:
    from oto_mcp import db
    return db.datastore_get_row(ns_id, row_id)["data"]


def _revision(ns_id: int, row_id: str) -> str:
    from oto_mcp import db
    return str(db.datastore_get_row(ns_id, row_id)["rev"])


@pytest.fixture
def tableau(base):
    """Un tableau neuf à la ligne marquée POSÉE EN BASE : la lecture se juge sans dépendre
    du geste d'écriture, qui a ses propres bancs plus bas."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t204r-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, SCHEMA)
    rid = st.append_row(ns, {"raison": "ACME", "fonction": "DG",
                             "contacts": [{"nom": "Alice", "fonction": "CEO"}]})["_id"]
    _poser_a_la_main(ns_id, rid, {
        "raison": "ACME", "fonction": {**MARQUEE, "comment": "aucune source"},
        "contacts": [{"nom": "Alice", "fonction": MARQUEE},
                     {"nom": "Bruno", "fonction": "DAF"}]})
    return st, ns, ns_id, rid


# ── la lecture, sur chaque face ──────────────────────────────────────────────

@pytest.mark.parametrize("layers", ["flat", "nested"])
def test_REST_page_et_fiche_servent_le_mot_sur_demande_et_le_vide_au_defaut(
        client, tableau, layers):
    _, ns, _, rid = tableau
    attendu = "@empty" if layers == "flat" else {"valeur": "@empty"}
    fiche = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                       params={"layers": layers, "empties": "sentinel"})
    page = client.get(f"/api/datastores/{ns}/rows", headers=_h(),
                      params={"layers": layers, "empties": "sentinel"})
    assert fiche.status_code == 200 and page.status_code == 200, (fiche.text, page.text)
    assert fiche.json()["contacts"][0]["fonction"] == attendu
    assert page.json()["rows"][0]["contacts"][0]["fonction"] == attendu
    assert fiche.json()["contacts"][1]["fonction"] == "DAF"
    _aucune_fuite(fiche.json()), _aucune_fuite(page.json())
    defaut = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                        params={"layers": layers}).json()
    assert "@empty" not in json.dumps(defaut, ensure_ascii=False)


@pytest.mark.parametrize("param, code, admise", [
    ("empties", "invalid_empties", "sentinel"), ("layers", "invalid_layers", "nested")])
@pytest.mark.parametrize("route", ["rows", "fiche", "claim_next", "claim"])
def test_REST_les_refus_de_forme_declares_sont_rendus_sur_les_quatre_lectures(
        client, tableau, route, param, code, admise):
    """Le rejeu des refus DÉCLARÉS (`_forme._REFUS_DE_FORME`) sur les routes servies."""
    _, ns, _, rid = tableau
    if route == "rows":
        r = client.get(f"/api/datastores/{ns}/rows", headers=_h(), params={param: "vrai"})
    elif route == "fiche":
        r = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                       params={param: "vrai"})
    elif route == "claim_next":
        r = client.post(f"/api/datastores/{ns}/claim_next", headers=_h(),
                        json={"worker": "w-204", param: "vrai"})
    else:
        r = client.post(f"/api/datastores/{ns}/rows/{rid}/claim", headers=_h(),
                        json={"worker": "w-204", param: "vrai"})
    assert r.status_code == 400, r.text
    corps = r.json()
    assert corps.get("error") == code, corps
    assert admise in json.dumps(corps, ensure_ascii=False)


def test_REST_la_reservation_sert_le_mot_sur_demande(client, tableau):
    _, ns, _, rid = tableau
    r = client.post(f"/api/datastores/{ns}/claim_next", headers=_h(),
                    json={"worker": "w-204", "empties": "sentinel"})
    assert r.status_code == 200, r.text
    ligne = r.json()["row"]
    assert ligne["_id"] == rid and ligne["fonction"] == "@empty"
    assert ligne["contacts"][0]["fonction"] == "@empty"
    _aucune_fuite(r.json())


def test_MCP_data_rows_et_data_claim_next_servent_le_mot_sur_demande(
        outils, tableau, acteur):
    _, ns, _, rid = tableau
    fiche = outils["data_rows"](datastore=ns, id=rid, empties="sentinel")
    page = outils["data_rows"](datastore=ns, empties="sentinel")
    assert fiche["contacts"][0]["fonction"] == "@empty"
    assert page["rows"][0]["fonction"] == "@empty"
    assert outils["data_rows"](datastore=ns, id=rid)["contacts"][0]["fonction"] == ""
    reservee = outils["data_claim_next"](datastore=ns, worker="w-mcp", empties="sentinel")
    assert reservee["row"]["contacts"][0]["fonction"] == "@empty"
    _aucune_fuite(fiche), _aucune_fuite(page), _aucune_fuite(reservee)


@pytest.mark.parametrize("outil", ["data_rows", "data_claim_next"])
def test_MCP_refuse_une_forme_inconnue_en_la_nommant(outils, tableau, acteur, outil):
    _, ns, _, _ = tableau
    kw = {"worker": "w-mcp"} if outil == "data_claim_next" else {}
    with pytest.raises(Exception) as e:
        outils[outil](datastore=ns, empties="vrai", **kw)
    assert "empties" in str(e.value) and "sentinel" in str(e.value)


# ── l'écriture : `@empty` émet, la relecture referme ─────────────────────────
#
# Les lignes naissent ici par le GESTE (`POST` avec `@empty`), plus par une pose à la main :
# c'est l'émission que ce lot livre, et ce banc la tient sur chaque chemin.

SCHEMA_CLE = {"fields": [
    {"key": "raison", "type": "text"},
    {"key": "fonction", "type": "text", "required": True},
    {"key": "contacts", "type": "list", "of": {"type": "object", "key": "role", "fields": [
        {"key": "role", "type": "text"},
        {"key": "fonction", "type": "text", "required": True},
        {"key": "note", "type": "text"}]}},
]}
ALICE = {"raison": "ACME", "fonction": "@empty",
         "contacts": [{"nom": "Alice", "fonction": "@empty"},
                      {"nom": "Bruno", "fonction": "DAF", "note": "@empty"}]}


def _table(schema: dict = SCHEMA) -> tuple:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t204w-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    make_store(SUB).set_schema(ns, schema)
    return ns, ns_id


def _poster(client, ns: str, ligne: dict) -> dict:
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(), json=ligne)
    assert r.status_code == 201, r.text
    return r.json()


def _lire(client, ns: str, rid: str, **params) -> dict:
    r = client.get(f"/api/datastores/{ns}/rows/{rid}", headers=_h(), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _patcher(client, ns: str, rid: str, corps: dict, **params):
    return client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(), json=corps,
                        params=params)


def _corps(ligne: dict) -> dict:
    """La ligne lue, réémise telle quelle — sans les colonnes de la plateforme."""
    return json.loads(json.dumps({k: v for k, v in ligne.items() if not k.startswith("_")}))


def _intacte(ns_id: int, rid: str) -> tuple:
    return _stockee(ns_id, rid), _revision(ns_id, rid)


def test_creation_POST_sans_cle_pose_le_marqueur_jamais_le_litteral(client, base):
    ns, ns_id = _table()
    cree = _poster(client, ns, {
        "raison": "ACME", "fonction": "@empty",
        "contacts": [{"nom": "Alice", "fonction": "@empty"},
                     {"nom": "Bruno", "fonction": "DAF", "note": ""}]})
    stockee = _stockee(ns_id, cree["_id"])
    assert stockee["fonction"] == MARQUEE
    assert stockee["contacts"] == [{"nom": "Alice", "fonction": MARQUEE},
                                   {"nom": "Bruno", "fonction": "DAF", "note": ""}]
    brut = json.dumps(stockee)
    assert "@empty" not in brut, "un mot stocké comme texte"
    _aucune_fuite(cree)


def test_le_lot_resout_les_mots_a_la_creation(base):
    from oto_mcp.datastore.core import make_store
    ns, ns_id = _table()
    st = make_store(SUB)
    rid = st.write_rows(ns, [{"raison": "LOT", "fonction": "@empty",
                              "contacts": [{"nom": "A", "fonction": "@empty"}]}])["ids"][0]
    assert _stockee(ns_id, rid)["fonction"] == MARQUEE
    assert _stockee(ns_id, rid)["contacts"] == [{"nom": "A", "fonction": MARQUEE}]


def test_aller_retour_lecture_sentinel_puis_reecriture_base_identique(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    avant = _stockee(ns_id, rid)
    lue = _lire(client, ns, rid, empties="sentinel")
    assert lue["contacts"][0]["fonction"] == "@empty" and lue["fonction"] == "@empty"
    r = _patcher(client, ns, rid, _corps(lue))
    assert r.status_code == 200, r.text
    assert _stockee(ns_id, rid) == avant, "le marqueur est gardé, rien d'autre ne bouge"
    assert "couches_effacees" not in r.json()


def test_liste_reordonnee_en_sentinel_chaque_marqueur_suit_son_element(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    corps = _corps(_lire(client, ns, rid, empties="sentinel"))
    corps["contacts"].reverse()
    r = _patcher(client, ns, rid, corps)
    assert r.status_code == 200, r.text
    assert _stockee(ns_id, rid)["contacts"] == [
        {"nom": "Bruno", "fonction": "DAF", "note": MARQUEE},
        {"nom": "Alice", "fonction": MARQUEE}]
    assert "couches_effacees" not in r.json()


def test_elements_identiques_chacun_garde_son_marqueur(client, base):
    ns, ns_id = _table()
    x = {"nom": "X", "fonction": "@empty"}
    rid = _poster(client, ns, {"raison": "ACME", "fonction": "DG", "contacts": [x, x]})["_id"]
    lue = _lire(client, ns, rid, empties="sentinel")
    assert lue["contacts"] == [{"nom": "X", "fonction": "@empty"}] * 2
    assert _patcher(client, ns, rid, _corps(lue)).status_code == 200
    assert _stockee(ns_id, rid)["contacts"] == [{"nom": "X", "fonction": MARQUEE}] * 2


def test_liste_a_cle_element_nouveau_resolu_et_keep_refuse_sans_rien_ecrire(client, base):
    ns, ns_id = _table(SCHEMA_CLE)
    rid = _poster(client, ns, {"raison": "ACME", "fonction": "DG",
                               "contacts": [{"role": "rh", "fonction": "DRH"}]})["_id"]
    r = _patcher(client, ns, rid, {"contacts": [
        {"role": "rh", "fonction": "DRH", "note": ""},
        {"role": "paie", "fonction": "@empty"}]})
    assert r.status_code == 200, r.text
    assert _stockee(ns_id, rid)["contacts"] == [{"role": "rh", "fonction": "DRH", "note": ""},
                                                {"role": "paie", "fonction": MARQUEE}]
    avant = _intacte(ns_id, rid)
    refus = _patcher(client, ns, rid, {"contacts": [{"role": "achats", "fonction": "@keep"}]})
    assert refus.status_code == 400 and refus.json()["error"] == "row_invalid", refus.text
    assert "`@keep` (`contacts`)" in refus.text      # retiré : refusé à l'entrée
    assert _intacte(ns_id, rid) == avant


@pytest.mark.parametrize("corps, chemin", [
    ({"contacts": [{"role": "@empty", "fonction": "x"}]}, "contacts[0].role"),
    ({"contacts": [{"role": {"valeur": "@empty"}, "fonction": "x"}]}, "contacts[0].role"),
    ({"tags": ["a", "@empty"]}, "tags[1]"),
    ({"adresse": {"rue": "@empty"}}, "adresse.rue"),
], ids=["identite_empty", "identite_empty_en_couches", "liste_de_valeurs", "sous_champ_objet"])
def test_un_mot_hors_d_une_case_est_refuse_en_400_sans_rien_creer(client, base, corps, chemin):
    from oto_mcp import db
    ns, ns_id = _table(SCHEMA_CLE)
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(),
                    json={"raison": "ACME", "fonction": "DG", **corps})
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text
    assert chemin in r.text
    assert db.datastore_count_rows(ns_id) == 0


def test_un_non_requis_relu_au_defaut_puis_renvoye_se_releve(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    corps = _corps(_lire(client, ns, rid))                 # le défaut : `""` partout
    corps["contacts"][0]["fonction"] = "@empty"           # le requis rétabli, le non requis oublié
    r = _patcher(client, ns, rid, corps)
    assert r.status_code == 200, r.text
    releve = r.json().get("couches_effacees") or []
    assert [(e["champ"], e["couche"]) for e in releve] == [("contacts[1].note", "@empty")]
    assert "empties=sentinel" in r.json()["couches_effacees_hint"]
    assert _stockee(ns_id, rid)["contacts"][1]["note"] == ""
    _aucune_fuite(r.json())


def test_une_vraie_valeur_retire_le_marqueur(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    corps = _corps(_lire(client, ns, rid, empties="sentinel"))
    corps["contacts"][0]["fonction"] = "CEO"
    assert _patcher(client, ns, rid, corps).status_code == 200
    assert _stockee(ns_id, rid)["contacts"][0] == {"nom": "Alice", "fonction": "CEO"}


def test_un_vide_ordinaire_sur_un_requis_est_refuse_base_et_revision_intactes(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    avant = _intacte(ns_id, rid)
    r = _patcher(client, ns, rid, _corps(_lire(client, ns, rid)))
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text
    assert "contacts[0].fonction" in r.text and "empties=sentinel" in r.text
    assert _intacte(ns_id, rid) == avant


def test_une_relecture_perimee_est_refusee_par_expected_revision(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    lue = _lire(client, ns, rid, empties="sentinel")
    assert _patcher(client, ns, rid, {"raison": "ACME SAS"}).status_code == 200
    apres = _stockee(ns_id, rid)
    r = _patcher(client, ns, rid, _corps(lue), expected_revision=lue["_revision"])
    assert r.status_code == 409 and r.json()["error"] == "revision_conflict", r.text
    assert _stockee(ns_id, rid) == apres


def test_premier_niveau_et_element_en_couches_reviennent_tels_quels_en_nested(client, base):
    ns, ns_id = _table()
    rid = _poster(client, ns, {
        "raison": "ACME", "fonction": {"valeur": "@empty", "comment": "aucune source"},
        "contacts": [{"nom": "Alice",
                      "fonction": {"valeur": "@empty", "comment": "registre muet"}}]})["_id"]
    avant = _stockee(ns_id, rid)
    assert avant["fonction"] == {"valeur": "", "comment": "aucune source", MARQUEUR: True}
    lue = _lire(client, ns, rid, layers="nested", empties="sentinel")
    assert lue["fonction"] == {"valeur": "@empty", "comment": "aucune source"}
    assert lue["contacts"][0]["fonction"] == {"valeur": "@empty", "comment": "registre muet"}
    assert _patcher(client, ns, rid, _corps(lue)).status_code == 200
    assert _stockee(ns_id, rid) == avant
    plate = _lire(client, ns, rid, empties="sentinel")
    assert (plate["fonction"], plate["fonction.comment"]) == ("@empty", "aucune source")
    assert plate["contacts"][0]["fonction.comment"] == "registre muet"


def test_aucune_fuite_au_defaut_ni_sur_la_page_publique(client, base, monkeypatch):
    from oto_mcp import db, share_ui
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    for params in ({}, {"layers": "nested"}):
        fiche = _lire(client, ns, rid, **params)
        page = client.get(f"/api/datastores/{ns}/rows", headers=_h(), params=params).json()
        for servi in (fiche, page):
            texte = json.dumps(servi, ensure_ascii=False)
            assert "@empty" not in texte and "vide_assume" not in texte
    stockee = _stockee(ns_id, rid)
    projet = {"id": 5, "name": "Projet démo", "brief_md": "", "mcp_access": "secret",
              "mcp_expose_datastore": True, "mcp_expose_docs": True}
    monkeypatch.setattr(db, "list_project_links", lambda pid: [
        {"target_type": "tableau", "target_ref": "22", "label": "Vivier", "datastore": ns,
         "datastore_id": 22}])
    monkeypatch.setattr(db, "list_docs_for_project", lambda pid: [])
    monkeypatch.setattr(db, "get_datastore_by_id",
                        lambda _rid: {"datastore": ns, "schema": SCHEMA})
    monkeypatch.setattr(db, "datastore_count_rows", lambda _rid: 1)
    monkeypatch.setattr(db, "datastore_list_rows", lambda _rid, **kw: [{
        "row_id": "r1", "created_at": "2026-10-01 10:00:00",
        "updated_at": "2026-10-01 10:00:00", "rev": 1, "data": stockee}])
    # La page se lit sous le contexte de son endpoint (`HostDispatch`).
    from oto_mcp import subdomain_project as sp
    tok = sp._CTX.set(sp.AnonContext(5, None, frozenset(), datastore_exposed=True))
    try:
        html, status = share_ui.build_page(projet, "/data/22", connect_url="u")
    finally:
        sp._CTX.reset(tok)
    assert status == 200 and "ACME" in html
    assert "@empty" not in html and "vide_assume" not in html


# ── `@clear` et `@keep` : retirés, refusés à l'entrée (oto#140 J3) ──────────

@pytest.mark.parametrize("corps", [
    {"raison": "@clear"},
    {"fonction": {"comment": "@clear"}},
    {"raison": {"valeur": "ACME2", "comment": "@keep"}},
    {"contacts": [{"nom": "Alice", "fonction": "@keep"}]},
], ids=["valeur", "couche", "couche_keep", "element_de_liste"])
def test_clear_et_keep_sont_refuses_sans_rien_ecrire(client, base, corps):
    ns, ns_id = _table()
    rid = _poster(client, ns, ALICE)["_id"]
    avant = _intacte(ns_id, rid)
    r = _patcher(client, ns, rid, corps)
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text
    assert "are not accepted" in r.text
    assert _intacte(ns_id, rid) == avant
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(),
                    json={"fonction": "DG", "contacts": [], **corps})
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text


@pytest.mark.parametrize("mot", ["@empty"])
def test_un_effacement_sur_une_relique_est_refuse_pour_empty(client, base, mot):
    ns, ns_id = _table()
    rid = _poster(client, ns, {"raison": "ACME", "fonction": "DG", "contacts": []})["_id"]
    _poser_a_la_main(ns_id, rid, {**_stockee(ns_id, rid),
                                  "raison.link": "https://exemple.test/relique"})
    avant = _intacte(ns_id, rid)
    r = _patcher(client, ns, rid, {"raison": {"link": mot}})
    assert r.status_code == 400, r.text
    assert "raison.link" in r.text
    assert _intacte(ns_id, rid) == avant


def test_MCP_data_write_rejoue_l_exemple_servi(outils, base, acteur):
    from oto_mcp.datastore.core import make_store
    ns, ns_id = _table()
    rid = make_store(SUB).append_row(ns, {"raison": "ACME", "fonction": "DG",
                                          "contacts": [{"nom": "Alice", "fonction": "CEO"}]})["_id"]
    outils["data_write"](datastore=ns, id=rid,
                         row={"contacts": [{"nom": "Alice", "fonction": "@empty"}]})
    assert _stockee(ns_id, rid)["contacts"] == [{"nom": "Alice", "fonction": MARQUEE}]
