"""AJOUTER n'est pas DÉSIGNER : la fusion sur la clé métier se demande (`upsert`,
oto#141).

**Le fait.** Sur un tableau à clé métier, une écriture SANS `id` dont la valeur de clé
existait déjà mettait la ligne à jour en silence — ligne seule, lot, REST, upload. Et
dans un lot, deux lignes à la même clé fusionnaient entre elles : `ids: [r1, r1, r2, r3]`
pour trois lignes écrites, sans un mot.

**Arbitré le 30/09/2026, précisé le 01/10.** Désigner (`id=`, `key=` nommé, tableau
fermé) modifie la ligne de la clé, sans `upsert`. AJOUTER (ni `id` ni `key=`) sur une
clé présente est REFUSÉ (`business_key_exists`) sauf `upsert=true`. Deux lignes d'un
même appel à la même clé suivent cette règle dans les deux cas. Un lot rend chaque
fusion dans `fusions`, `ids` aligné rang pour rang.

Chaque face est jouée contre un vrai PostgreSQL : on lit la BASE, pas seulement ce que
l'appel a bien voulu rendre.
"""
from __future__ import annotations

import json
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore import upsert_implicite as upi

SUB = "usr_upsert141"
SCHEMA = {"key": "siren", "fields": [{"key": "siren", "type": "text"},
                                     {"key": "nom", "type": "text"}]}
FERME = {**SCHEMA, "new_rows": "reject"}
LIBRE = {"fields": [{"key": "siren", "type": "text"}, {"key": "nom", "type": "text"}]}


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@upsert.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


# ── le texte servi (sans base) ───────────────────────────────────────────────

def test_le_texte_servi_dit_la_regle_au_present():
    ecriture = upi.DESCRIPTION_ECRITURE
    assert "ADDING a row whose key value already exists is REFUSED" in ecriture
    assert "DESIGNATE" in ecriture and "`key=`" in ecriture
    assert "`upsert=true`" in ecriture and "`fusions`" in ecriture
    for texte in (ecriture, upi.description_parametre(), upi.description_cle_schema()):
        assert "2026-10-21" not in texte and "until" not in texte.lower(), texte


def test_la_description_servie_ne_promet_plus_l_upsert_de_toute_ecriture():
    """La phrase d'origine (« EVERY write carrying that key value then UPSERTs on it »)
    est remplacée par la règle, sur `data_set_schema`, `data_write` et
    `data_patch_schema` — le texte servi pilote l'agent."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.capabilities.datastore import columns
    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("t")
    tools_ds.register(mcp)
    for nom in ("data_set_schema", "data_write"):
        desc = asyncio.run(mcp.get_tool(nom)).description
        assert "UPSERTs on it" not in desc and "<<" not in desc, nom
        assert "`upsert=true`" in desc and "REFUSED" in desc, nom
        assert "DESIGNATE" in desc and "ADD" in desc, nom
    assert "existing_violations" in asyncio.run(mcp.get_tool("data_set_schema")).description
    patch = next(c for c in columns.CAPABILITIES if c.key == "me.datastore.patch_schema")
    assert "`upsert=true`" in patch.description and "REFUSED" in patch.description


def test_juger_le_lot_nomme_les_doublons_et_les_lignes_en_place():
    en_base = {"222": "r7"}
    rows = [{"siren": "111"}, {"siren": {"valeur": "111", "comment": "x"}},
            {"siren": "222"}, {"nom": "sans clé"}, {"siren": 333}, {"siren": "333"}]
    with pytest.raises(upi.BusinessKeyExists) as e:
        upi.juger_le_lot(rows, key="siren", datastore="t", designation=False,
                         chercher=lambda kv: en_base.get(str(kv)), decalage=10,
                         textes=lambda vs: [str(v) for v in vs])
    msg = str(e.value)
    assert "nothing was written" in msg
    assert "Rows 11 and 12: same siren '111'." in msg
    assert "Rows 15 and 16: same siren '333'." in msg
    assert "Row 13: siren '222' already designates row `r7`." in msg
    assert "`upsert=true`" in msg and "`key='siren'`" in msg
    assert "a duplicate is removed from the batch" in msg.lower()
    assert e.value.details == {
        "key": "siren",
        "doublons": [{"rangs": [11, 12], "valeur": "111"},
                     {"rangs": [15, 16], "valeur": 333}],
        "existantes": [{"rang": 13, "valeur": "222", "id": "r7"}]}


def test_un_lot_qui_designe_n_est_juge_que_sur_ses_doublons():
    """`key=` nommé : une clé en base est la ligne VISÉE — le lookup n'est même pas
    appelé ; seul un doublon interne refuse (on ne désigne pas deux fois une ligne)."""
    def _jamais(kv):
        raise AssertionError("lookup appelé pour un lot qui désigne")

    texte = lambda vs: [str(v) for v in vs]  # noqa: E731
    upi.juger_le_lot([{"siren": "1"}, {"siren": "2"}], key="siren", datastore="t",
                     designation=True, chercher=_jamais, textes=texte)
    with pytest.raises(upi.BusinessKeyExists) as e:
        upi.juger_le_lot([{"siren": "1"}, {"siren": "1"}], key="siren", datastore="t",
                         designation=True, chercher=_jamais, textes=texte)
    assert "the same row is not designated twice" in str(e.value)
    assert e.value.details["existantes"] == []


# ── la base ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live(pg_module_dsn):
    import os

    from oto_mcp.db import _conn as dbconn
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_module_dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@upsert.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture
def data_write(live, monkeypatch):
    """Le tool MCP `data_write` monté, tel qu'un agent l'appelle."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_write")).fn


#: Le nom affiché de chaque tableau du banc, par son adresse (le numéro).
_NOMS: dict = {}


def _table(schema=SCHEMA, lignes=()):
    """Un tableau neuf, et ses lignes posées AVANT le geste jugé — rend (adresse,
    ns_id, {siren: _id}) ; l'adresse est le numéro, le nom se lit dans `_NOMS`."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    nom = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, nom)
    ns = str(ns_id)
    _NOMS[ns] = nom
    store = make_store(SUB)
    if schema is not None:
        store.set_schema(ns, {**schema, "new_rows": "create"})
    ids = {}
    for ligne in lignes:
        ids[ligne["siren"]] = store.append_row(ns, ligne)["_id"]
    if schema is not None and schema.get("new_rows") == "reject":
        store.set_schema(ns, schema)
    return ns, ns_id, ids


def _base(ns_id: int) -> dict:
    """Ce que porte LA BASE, par siren."""
    from oto_mcp.datastore import schema as dsv2
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT row_id, data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchall()
    return {str(dsv2.unwrap(r["data"].get("siren"))): {"_id": r["row_id"], **r["data"]}
            for r in rows}


def _refus_mcp(data_write, **kw) -> str:
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as exc:
        data_write(**kw)
    return exc.value.error.message


def _ligne(client, ns, corps, query=""):
    return client.post(f"/api/datastores/{ns}/rows{query}", headers=_h(), json=corps)


def _lot(client, ns, corps):
    return client.post(f"/api/datastores/{ns}/rows/batch", headers=_h(), json=corps)


EN_PLACE = [{"siren": "222", "nom": "B"}]
#: rang 1 neuf, rang 2 doublon du 1, rang 3 déjà en base, rang 4 neuf.
LOT = [{"siren": "111", "nom": "A"}, {"siren": "111", "nom": "A bis"},
       {"siren": "222", "nom": "B bis"}, {"siren": "333", "nom": "C"}]


def _verifier_lot_fusionne(rep: dict, en_place: dict, base: dict) -> None:
    a, b, c = base["111"]["_id"], en_place["222"], base["333"]["_id"]
    assert (rep["inserted"], rep["updated"], rep["count"]) == (2, 2, 4)
    assert rep["ids"] == [a, a, b, c]
    assert rep["fusions"] == [
        {"rang": 2, "dans_rang": 1, "id": a, "cle": {"siren": "111"}},
        {"rang": 3, "dans_rang": None, "id": b, "cle": {"siren": "222"}}]
    assert sorted(base) == ["111", "222", "333"]
    assert base["111"]["nom"] == "A bis" and base["222"]["nom"] == "B bis"


def test_les_doublons_du_lot_se_jugent_sur_le_texte_que_rend_la_base(live):
    """Deux lignes « à la même clé » : le texte que la BASE rend de la valeur (la
    conversion du lookup, oto#223), pas une conversion Python — `333` et `"333"` sont
    la même clé, `true` n'est pas `"True"`."""
    from oto_mcp import db
    assert db.datastore_textes_de_cle([333, "333", True, "True", "é"]) == [
        "333", "333", "true", "True", "é"]
    assert db.datastore_textes_de_cle([]) == []


# ── désigner, fusionner ──────────────────────────────────────────────────────

def test_la_ligne_seule_qui_designe_par_key_modifie_sans_upsert(client, data_write):
    """`key=` = la clé déclarée : la ligne est DÉSIGNÉE — une valeur en place la
    modifie, sans `upsert` et sans un mot ; une valeur neuve la crée. Une autre colonne
    reste refusée, REST compris."""
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    rep = data_write(datastore=ns, row={"siren": "222", "nom": "M"}, key="siren")
    assert rep["_id"] == ids["222"]
    assert not any("refus" in n for n in rep.get("notices") or [])
    r = _ligne(client, ns, {"siren": "222", "nom": "R"}, "?key=siren")
    assert r.status_code == 201, r.text
    assert r.json()["_id"] == ids["222"]
    assert not any("refus" in n for n in r.json().get("notices") or [])
    neuve = _ligne(client, ns, {"siren": "777"}, "?key=siren")
    assert neuve.status_code == 201 and neuve.json()["_id"] != ids["222"]
    autre = _ligne(client, ns, {"siren": "888", "nom": "x"}, "?key=nom")
    assert autre.status_code == 400 and "has no effect" in autre.json()["detail"]
    base = _base(ns_id)
    assert sorted(base) == ["222", "777"] and base["222"]["nom"] == "R"


def test_l_upload_signe_rend_fusions_sans_identifiant(live):
    """L'accusé d'un upload est lu par un porteur de lien ANONYME (oto#86) : `fusions`
    y dit les rangs, jamais l'identifiant interne d'une ligne."""
    from oto_mcp import upload_tokens
    ns, ns_id, _ = _table(lignes=EN_PLACE)
    corps = "\n".join(json.dumps(r) for r in LOT).encode()
    cible = {"kind": "datastore", "ns_id": ns_id, "namespace": _NOMS[ns], "format": "ndjson",
             "key": "siren", "upsert": True}
    rep = upload_tokens.materialize(SUB, cible, corps, "application/x-ndjson")
    assert (rep["inserted"], rep["updated"]) == (2, 2)
    assert rep["fusions"] == [{"rang": 2, "dans_rang": 1, "cle": {"siren": "111"}},
                              {"rang": 3, "dans_rang": None, "cle": {"siren": "222"}}]
    assert sorted(_base(ns_id)) == ["111", "222", "333"]


def test_l_import_en_tranches_garde_les_rangs_du_fichier(live, monkeypatch):
    """Découpé en tranches de 2, le fichier garde SES rangs, et `dans_rang` traverse
    les tranches : la ligne 3 fusionne dans la ligne 1, posée par la tranche d'avant."""
    import time

    from oto_mcp import upload_tokens
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 2)
    ns, ns_id, en_place = _table(lignes=EN_PLACE)
    rows = [{"siren": "111"}, {"siren": "444"}, {"siren": "111"}, {"siren": "222"}]
    cible = {"kind": "datastore", "ns_id": ns_id, "namespace": _NOMS[ns], "key": "siren",
             "upsert": True}
    rep = upload_tokens.import_rows(SUB, cible, rows, deadline=time.monotonic() + 60)
    a = _base(ns_id)["111"]["_id"]
    assert rep["done"] and (rep["inserted"], rep["updated"]) == (2, 2)
    assert rep["fusions"] == [
        {"rang": 3, "dans_rang": 1, "id": a, "cle": {"siren": "111"}},
        {"rang": 4, "dans_rang": None, "id": en_place["222"], "cle": {"siren": "222"}}]


# ── les refus ────────────────────────────────────────────────────────────────

def test_la_ligne_seule_est_refusee_et_nomme_la_ligne_en_place(
        client, data_write):
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    ligne = {"siren": "222", "nom": "B bis"}
    msg = _refus_mcp(data_write, datastore=ns, row=ligne)
    r = _ligne(client, ns, ligne)
    assert r.status_code == 409, r.text
    corps = r.json()
    assert corps["error"] == "business_key_exists"
    assert corps["details"] == {"key": "siren", "valeur": "222", "id": ids["222"]}
    for texte in (msg, corps["detail"]):
        assert f"already designates row `{ids['222']}`" in texte
        assert f"`id={ids['222']}`" in texte and "`upsert=true`" in texte
        assert "nothing was written" in texte
    assert _base(ns_id)["222"]["nom"] == "B"


def test_upsert_true_fusionne_et_une_cle_neuve_se_cree(client, data_write):
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    assert data_write(datastore=ns, row={"siren": "222", "nom": "M"},
                      upsert=True)["_id"] == ids["222"]
    r = _ligne(client, ns, {"siren": "222", "nom": "R"}, "?upsert=true")
    assert r.status_code == 201 and r.json()["_id"] == ids["222"]
    # Une clé NEUVE n'a rien à fusionner : elle se crée, avec ou sans `upsert`.
    assert data_write(datastore=ns, row={"siren": "555"})["_id"] != ids["222"]
    base = _base(ns_id)
    assert sorted(base) == ["222", "555"] and base["222"]["nom"] == "R"


@pytest.mark.parametrize("lot, key, attendu", [
    ([{"siren": "111"}, {"siren": "111"}, {"siren": "333"}], None,
     "Rows 1 and 2: same siren '111'."),
    ([{"siren": "111"}, {"siren": "111"}, {"siren": "333"}], "siren",
     "the same row is not designated twice"),
    ([{"siren": "333"}, {"siren": "222"}], None,
     "Row 2: siren '222' already designates row"),
], ids=["doublon interne, ajout", "doublon interne, designation", "clé en base, ajout"])
def test_le_lot_est_refuse_entier_avant_sa_premiere_ligne(
        client, data_write, lot, key, attendu):
    ns, ns_id, _ = _table(lignes=EN_PLACE)
    msg = _refus_mcp(data_write, datastore=ns, rows=lot, key=key)
    r = _lot(client, ns, {"rows": lot, **({"key": key} if key else {})})
    assert r.status_code == 409, r.text
    corps = r.json()
    assert corps["error"] == "business_key_exists"
    for texte in (msg, corps["detail"]):
        assert texte.startswith("batch refused ENTIRELY, nothing was written") and attendu in texte
    assert set(corps["details"]) == {"key", "doublons", "existantes"}
    # Ni la ligne neuve `333`, ni aucune autre : le lot n'a pas commencé.
    assert sorted(_base(ns_id)) == ["222"]


def test_le_lot_qui_designe_modifie_les_lignes_en_place(client, data_write):
    """`key=` nommé : une clé en base est la ligne VISÉE — modifiée sans `upsert`,
    sans avertissement ; une clé neuve est créée."""
    for face in ("mcp", "rest"):
        ns, ns_id, en_place = _table(lignes=EN_PLACE)
        lot = [{"siren": "333", "nom": "C"}, {"siren": "222", "nom": "B ter"}]
        if face == "mcp":
            rep = data_write(datastore=ns, rows=lot, key="siren")
        else:
            r = _lot(client, ns, {"rows": lot, "key": "siren"})
            assert r.status_code == 200, r.text
            rep = r.json()
        assert (rep["inserted"], rep["updated"]) == (1, 1), face
        assert rep["fusions"] == [{"rang": 2, "dans_rang": None, "id": en_place["222"],
                                   "cle": {"siren": "222"}}]
        assert not any("refus" in n for n in rep.get("notices") or []), face
        base = _base(ns_id)
        assert sorted(base) == ["222", "333"] and base["222"]["nom"] == "B ter"


@pytest.mark.parametrize("key", [None, "siren"], ids=["ajout", "designation"])
def test_le_lot_avec_upsert_fusionne_et_rend_fusions(client, data_write, key):
    for face in ("mcp", "rest"):
        ns, ns_id, en_place = _table(lignes=EN_PLACE)
        if face == "mcp":
            rep = data_write(datastore=ns, rows=LOT, upsert=True, key=key)
        else:
            r = _lot(client, ns, {"rows": LOT, "upsert": True, **({"key": key} if key else {})})
            assert r.status_code == 200, r.text
            rep = r.json()
        _verifier_lot_fusionne(rep, en_place, _base(ns_id))
        assert not any("refus" in n for n in rep.get("notices") or []), face


def test_l_upload_signe_est_refuse_sans_nommer_de_ligne(live):
    from oto_mcp import upload_tokens
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    corps = "\n".join(json.dumps(r) for r in LOT).encode()
    cible = {"kind": "datastore", "ns_id": ns_id, "namespace": _NOMS[ns], "format": "ndjson",
             "key": "siren"}
    with pytest.raises(upload_tokens.UploadError) as e:
        upload_tokens.materialize(SUB, cible, corps, "application/x-ndjson")
    assert (e.value.status, e.value.code) == (409, "business_key_exists")
    assert "Rows 1 and 2: same siren '111'." in e.value.message
    assert "Row 3: siren '222' already designates a row of the table." in e.value.message
    assert ids["222"] not in e.value.message
    assert e.value.details["existantes"] == [{"rang": 3, "valeur": "222"}]
    assert sorted(_base(ns_id)) == ["222"]
    # Jeton frappé avec `key` (désigne) : le doublon interne reste refusé…
    with pytest.raises(upload_tokens.UploadError) as e:
        upload_tokens.materialize(SUB, {**cible, "cle_passee": True}, corps,
                                  "application/x-ndjson")
    assert e.value.details["doublons"] and e.value.details["existantes"] == []
    # …la ligne en place, elle, est désignée et modifiée.
    sans_doublon = "\n".join(json.dumps(r) for r in LOT[1:]).encode()
    rep = upload_tokens.materialize(SUB, {**cible, "cle_passee": True}, sans_doublon,
                                    "application/x-ndjson")
    assert (rep["inserted"], rep["updated"]) == (2, 1)
    assert not any("refus" in n for n in rep.get("notices") or [])
    # Le fichier entier, jeton frappé AVEC `upsert` : il fusionne.
    rep = upload_tokens.materialize(SUB, {**cible, "upsert": True}, corps,
                                    "application/x-ndjson")
    assert (rep["inserted"], rep["updated"]) == (0, 4) and len(rep["fusions"]) == 4


def test_l_import_est_refuse_avant_sa_premiere_tranche(live, monkeypatch):
    import time

    from oto_mcp import upload_tokens
    monkeypatch.setattr(upload_tokens, "IMPORT_SLICE", 1)
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    rows = [{"siren": "444"}, {"siren": "555"}, {"siren": "222"}]
    cible = {"kind": "datastore", "ns_id": ns_id, "namespace": _NOMS[ns], "key": "siren"}
    with pytest.raises(upload_tokens.UploadError) as e:
        upload_tokens.import_rows(SUB, cible, rows, deadline=time.monotonic() + 60)
    assert (e.value.status, e.value.code) == (409, "business_key_exists")
    assert e.value.details["written"] == 0
    assert e.value.details["existantes"] == [{"rang": 3, "valeur": "222", "id": ids["222"]}]
    assert sorted(_base(ns_id)) == ["222"]


def test_le_put_signe_rend_409_et_le_mint_scelle_upsert(live, monkeypatch):
    """De bout en bout : la frappe (`POST /api/me/upload-url`) scelle `upsert` et la clé
    NOMMÉE dans le jeton, la réception (`PUT /api/upload/{token}`) les lit — un ajout
    sans eux rend 409, une désignation (`key`) ou un `upsert` modifie la ligne."""
    from oto_mcp.api import routes as api_routes
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "secret-de-banc-assez-long-pour-141")
    http = TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))
    ns, ns_id, _ = _table(lignes=EN_PLACE)
    corps = b'{"siren": "222", "nom": "PUT"}\n'
    for frappe, statut in (({}, 409), ({"key": "siren"}, 200), ({"upsert": True}, 200)):
        m = http.post("/api/me/upload-url", headers=_h(),
                      json={"target": "datastore", "datastore": ns, **frappe})
        assert m.status_code == 200, m.text
        cible = m.json()["target"]
        assert cible["upsert"] is bool(frappe.get("upsert"))
        assert cible["cle_passee"] is ("key" in frappe) and cible["key"] == "siren"
        jeton = m.json()["url"].rsplit("/", 1)[1]
        r = http.put(f"/api/upload/{jeton}", content=corps,
                     headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code == statut, r.text
    assert r.json()["updated"] == 1 and "id" not in r.json()["fusions"][0]
    assert _base(ns_id)["222"]["nom"] == "PUT"


def test_un_tableau_ferme_vise_par_sa_cle_sans_upsert(client, data_write):
    """`key_required` ne change pas : sur un tableau fermé, la valeur de clé EST la
    désignation (son refus de création le conseille) — ni refus, ni avertissement."""
    ns, ns_id, ids = _table(FERME, lignes=EN_PLACE)
    rep = data_write(datastore=ns, row={"siren": "222", "nom": "F"})
    assert rep["_id"] == ids["222"]
    assert not any("refus" in n for n in rep.get("notices") or [])
    r = _lot(client, ns, {"rows": [{"siren": "222", "nom": "G"}]})
    assert r.status_code == 200 and r.json()["fusions"][0]["dans_rang"] is None
    assert _base(ns_id)["222"]["nom"] == "G"


def test_une_course_perdue_sans_upsert_est_refusee(data_write, monkeypatch):
    """La clé arrive ENTRE le lookup et l'insert : l'index la refuse, et la course
    perdue suit la même règle que le lookup — refus nommé, rien d'écrit."""
    from oto_mcp import db
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    vrai = db.datastore_find_row_id_by_key
    appels = []

    def _aveugle_une_fois(*a, **k):
        appels.append(a)
        return None if len(appels) == 1 else vrai(*a, **k)

    monkeypatch.setattr(db, "datastore_find_row_id_by_key", _aveugle_une_fois)
    msg = _refus_mcp(data_write, datastore=ns, row={"siren": "222", "nom": "course"})
    assert f"already designates row `{ids['222']}`" in msg
    assert _base(ns_id)["222"]["nom"] == "B"


# ── `upsert` là où il ne ferait rien : refusé, jamais ignoré ─────────────────

def test_upsert_sans_cle_est_refuse_sur_chaque_face(client, data_write):
    ns, ns_id, _ = _table(LIBRE)
    for kw in ({"row": {"siren": "1"}}, {"rows": [{"siren": "1"}]}):
        assert "has nothing to merge on" in _refus_mcp(
            data_write, datastore=ns, upsert=True, **kw)
    assert _ligne(client, ns, {"siren": "1"}, "?upsert=true").status_code == 400
    assert _lot(client, ns, {"rows": [{"siren": "1"}], "upsert": True}).status_code == 400
    m = client.post("/api/me/upload-url", headers=_h(),
                    json={"target": "datastore", "datastore": ns, "upsert": True})
    assert (m.status_code, m.json()["error"]) == (400, "upsert_without_key")
    assert _base(ns_id) == {}
    # Un `key=` de lot donne une clé : `upsert` y a de quoi fusionner.
    rep = data_write(datastore=ns, rows=[{"siren": "1"}, {"siren": "1"}], key="siren",
                     upsert=True)
    assert rep["ids"][0] == rep["ids"][1] and rep["fusions"][0]["dans_rang"] == 1


def test_upsert_avec_id_est_refuse(data_write):
    ns, ns_id, ids = _table(lignes=EN_PLACE)
    msg = _refus_mcp(data_write, datastore=ns, id=ids["222"], row={"nom": "X"},
                     upsert=True)
    assert "only applies WITHOUT `id=`" in msg
    assert _base(ns_id)["222"]["nom"] == "B"
