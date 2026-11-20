"""Ajouter du TEXTE à une cellule sans la relire (`journal[+]`), sur une base réelle.

Ce que le banc tient : sur une colonne texte, `[+]` ajoute en fin de cellule, chaque
morceau sur sa ligne, sous le verrou de la ligne — deux ajouts partis au même instant
finissent TOUS LES DEUX dans le texte, sans réémettre la cellule. Le texte résultant
passe la validation de la colonne (`max_length`). Une colonne ni liste ni texte refuse
`[+]` en le nommant. Les faces REST et MCP héritent du geste.

⚠️ Le banc ÉCRIT réellement et relit le STOCKAGE.
"""
from __future__ import annotations

import asyncio
import threading
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore.errors import RowValidationError

SUB = "usr_texte_ajout"
SCHEMA = {"key": "siren", "fields": [
    {"key": "siren", "type": "text"},
    {"key": "journal", "type": "text"},
    {"key": "court", "type": "text", "max_length": 10},
    {"key": "long", "type": "text", "max_length": 100_000},
    {"key": "score", "type": "number"},
    {"key": "tags", "type": "list", "of": {"type": "text"}},
]}
TOURS = 8


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@texte.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@texte.invalid", name=SUB)


@pytest.fixture(scope="module")
def client(base):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture(scope="module")
def outils(base):
    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("texte-ajout")
    tools_ds.register(mcp)
    return {"data_write": asyncio.run(mcp.get_tool("data_write")).fn}


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
    ns = "ttx-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    st = make_store(SUB)
    st.set_schema(ns, SCHEMA)
    rid = st.append_row(ns, {"siren": "552032534", **data})["_id"]
    return st, ns, ns_id, rid


@pytest.fixture
def table(base):
    return _table(journal="ligne 1")


def _refus(geste) -> str:
    with pytest.raises(RowValidationError) as exc:
        geste()
    return str(exc.value)


def _valeur(cellule):
    return cellule.get("valeur") if isinstance(cellule, dict) else cellule


# ══ l'ajout ════════════════════════════════════════════════════════════════════

def test_l_ajout_va_en_fin_de_cellule_sur_sa_ligne(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"journal[+]": "ligne 2"})
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1\nligne 2"


def test_une_liste_ajoute_chaque_morceau_sur_sa_ligne(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"journal[+]": ["ligne 2", "ligne 3"]})
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1\nligne 2\nligne 3"


def test_une_cellule_vide_prend_l_ajout_tel_quel(base):
    st, ns, ns_id, rid = _table()
    st.update_row(ns, rid, {"journal[+]": "premiere"})
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "premiere"


def test_la_creation_d_une_ligne_accepte_l_ajout(base):
    st, ns, ns_id, _rid = _table()
    rid = st.append_row(ns, {"siren": "130025265", "journal[+]": ["a", "b"]})["_id"]
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "a\nb"


def test_deux_ajouts_simultanes_ne_perdent_rien(base):
    st, ns, ns_id, rid = _table()
    for i in range(TOURS):
        barriere = threading.Barrier(2)
        erreurs: list = []

        def courir(texte):
            barriere.wait()
            try:
                st.update_row(ns, rid, {"journal[+]": texte})
            except BaseException as e:  # noqa: BLE001 — rendue au test
                erreurs.append(e)

        fils = [threading.Thread(target=courir, args=(f"{p}{i}",)) for p in "xy"]
        for f in fils:
            f.start()
        for f in fils:
            f.join(timeout=60)
        assert not any(f.is_alive() for f in fils)
        assert erreurs == []
    lignes = _valeur(_stockee(ns_id, rid)["journal"]).split("\n")
    assert sorted(lignes) == sorted(f"{p}{i}" for p in "xy" for i in range(TOURS))


# ══ la validation et les refus ═════════════════════════════════════════════════

def test_le_texte_resultant_passe_max_length(base):
    st, ns, ns_id, rid = _table(court="12345")
    st.update_row(ns, rid, {"court[+]": "678"})
    assert _valeur(_stockee(ns_id, rid)["court"]) == "12345\n678"
    _refus(lambda: st.update_row(ns, rid, {"court[+]": "trop long"}))
    assert _valeur(_stockee(ns_id, rid)["court"]) == "12345\n678"


def test_une_colonne_ni_liste_ni_texte_refuse_l_ajout(table):
    st, ns, ns_id, rid = table
    msg = _refus(lambda: st.update_row(ns, rid, {"score[+]": 3}))
    assert "`score[+]`" in msg and "list" in msg and "text" in msg


@pytest.mark.parametrize("valeur, attendu", [
    (12, "string"),
    ({"a": 1}, "string"),
    ("", "empty string"),
    (None, "null"),
    ([], "empty list"),
])
def test_un_ajout_qui_n_est_pas_du_texte_est_refuse(table, valeur, attendu):
    st, ns, ns_id, rid = table
    msg = _refus(lambda: st.update_row(ns, rid, {"journal[+]": valeur}))
    assert attendu in msg
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1"


@pytest.mark.parametrize("cle", ["journal[0]", "journal[-]"])
def test_un_texte_n_a_ni_rang_ni_retrait(table, cle):
    st, ns, _ns_id, rid = table
    _refus(lambda: st.update_row(ns, rid, {cle: "x"}))


def test_un_mot_reserve_ne_s_ajoute_pas(table):
    st, ns, ns_id, rid = table
    _refus(lambda: st.update_row(ns, rid, {"journal[+]": "@empty"}))
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1"


def test_la_liste_garde_son_ajout_d_element(base):
    st, ns, ns_id, rid = _table(tags=["a"])
    st.update_row(ns, rid, {"tags[+]": "b"})
    assert _valeur(_stockee(ns_id, rid)["tags"]) == ["a", "b"]


# ══ les faces ══════════════════════════════════════════════════════════════════

def test_la_face_REST_ajoute(client, table):
    _st, ns, ns_id, rid = table
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}",
                     headers={"Authorization": f"Bearer {SUB}"},
                     json={"journal[+]": "ligne 2"})
    assert r.status_code == 200, r.text
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1\nligne 2"


def test_la_face_MCP_ajoute_par_id_et_par_lot(outils, acteur, table):
    _st, ns, ns_id, rid = table
    outils["data_write"](datastore=ns, id=rid, row={"journal[+]": "ligne 2"})
    outils["data_write"](datastore=ns, key="siren",
                         rows=[{"siren": "552032534", "journal[+]": "ligne 3"}])
    assert _valeur(_stockee(ns_id, rid)["journal"]) == "ligne 1\nligne 2\nligne 3"


# ══ la borne de l'ajout ════════════════════════════════════════════════════════

def test_un_ajout_qui_deborde_est_refuse_et_oriente_vers_une_table(base):
    from oto_mcp.datastore.rangs import TAILLE_MAX_APRES_AJOUT
    plein = "x" * (TAILLE_MAX_APRES_AJOUT - 3)
    st, ns, ns_id, rid = _table(journal=plein)
    st.update_row(ns, rid, {"journal[+]": "a"})          # \n + a : 64 000 pile
    msg = _refus(lambda: st.update_row(ns, rid, {"journal[+]": "b"}))
    assert "`journal[+]`" in msg and "TABLE" in msg and "Nothing was written" in msg
    assert _valeur(_stockee(ns_id, rid)["journal"]) == plein + "\na"


def test_une_ecriture_entiere_n_est_pas_jugee_par_la_borne(table):
    from oto_mcp.datastore.rangs import TAILLE_MAX_APRES_AJOUT
    st, ns, ns_id, rid = table
    gros = "y" * (TAILLE_MAX_APRES_AJOUT + 10)
    st.update_row(ns, rid, {"journal": gros})
    assert _valeur(_stockee(ns_id, rid)["journal"]) == gros


def test_un_max_length_declare_est_seul_juge(base):
    from oto_mcp.datastore.rangs import TAILLE_MAX_APRES_AJOUT
    st, ns, ns_id, rid = _table(long="z" * TAILLE_MAX_APRES_AJOUT)
    st.update_row(ns, rid, {"long[+]": "encore"})
    assert _valeur(_stockee(ns_id, rid)["long"]).endswith("\nencore")


def test_une_liste_qui_deborde_refuse_l_ajout(base, monkeypatch):
    from oto_mcp.datastore import rangs
    monkeypatch.setattr(rangs, "TAILLE_MAX_APRES_AJOUT", 20)
    st, ns, ns_id, rid = _table(tags=["aaaa", "bbbb"])
    msg = _refus(lambda: st.update_row(ns, rid, {"tags[+]": "cccccccc"}))
    assert "`tags[+]`" in msg and "TABLE" in msg
    assert _valeur(_stockee(ns_id, rid)["tags"]) == ["aaaa", "bbbb"]
