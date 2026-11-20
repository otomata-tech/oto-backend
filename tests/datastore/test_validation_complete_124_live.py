"""oto#124 — le format déclaré fait contrat sur TOUS les tableaux.

Décidé le 05/10/2026, en vigueur depuis le 21/10/2026 : toujours refuser, plus aucun
réglage. La validation complète (`options` de tête, forme de `text`/`url`/`object`/
`list`/`enum`, couches inconnues, sous-records fermés) s'applique à tout tableau qui
déclare au moins une colonne, sur toutes les faces.

**On juge ce que le geste ÉCRIT, jamais ce que la ligne porte déjà** : une ligne en
faute s'écrit sur ses autres colonnes ; sa faute se dit (`hors_type`), elle ne refuse
rien. Les données des tenants ne sont pas touchées.

On lit la BASE, pas seulement ce que l'appel a bien voulu rendre.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp.datastore import schema as S
from oto_mcp.datastore import validation_complete as vc

SUB = "usr_validation124"
SCHEMA = {"fields": [
    {"key": "nom", "type": "text"},
    {"key": "statut", "type": "enum", "options": ["a", "b"]},
    {"key": "etape", "type": "text", "options": ["x", "y"]},
    {"key": "site", "type": "url"},
    {"key": "fiche", "type": "object", "fields": [{"key": "ville", "type": "text"}]},
    {"key": "tags", "type": "list", "of": {"type": "text"}},
    {"key": "contacts", "type": "list",
     "of": {"type": "object", "fields": [{"key": "nom", "type": "text"}]}},
    {"key": "autre", "type": "text"},
]}

#: Une faute par famille : `(famille, colonne posée, valeur, ce que la faute dit)`.
FAUTES = [
    ("options d'un enum", "statut", "z", "value 'z' not among the options (a, b)"),
    ("options d'un texte", "etape", "w", "value 'w' not among the options (x, y)"),
    ("forme text", "nom", 42, "nom: expected text, got int"),
    ("forme url", "site", "www.x.fr", "site: expected an http(s) URL, got 'www.x.fr'"),
    ("forme object", "fiche", "Lyon", "fiche: expected object, got str"),
    ("forme list", "tags", "a,b", "tags: expected list, got str"),
    ("forme enum", "statut", ["a"], "statut: expected an enumeration value"),
    ("couche inconnue", "autre", {"valeur": "v", "commentaire": "c"},
     "autre: unknown sub-field(s) 'commentaire'"),
    ("sous-record fermé", "contacts", [{"nom": "A", "perso": "x"}],
     "contacts[0].perso: undeclared attribute"),
]
IDS = [f[0] for f in FAUTES]
#: La ligne qui porte TOUTES les fautes à la fois (une colonne par famille).
#: Sans la couche inconnue : une couche mal nommée est déjà refusée à l'écriture sur
#: TOUS les tableaux (`couches`, « n'est pas une couche ») — elle ne vit qu'en base.
EN_FAUTE = {"statut": "z", "etape": "w", "nom": 42, "site": "www.x.fr", "fiche": "Lyon",
            "tags": "a,b", "contacts": [{"nom": "A", "perso": "x"}]}


# ── la règle, sans base ──────────────────────────────────────────────────────

@pytest.mark.parametrize("famille,col,valeur,dit", FAUTES, ids=IDS)
def test_chaque_famille_est_refusee(famille, col, valeur, dit):
    errs = S.validate_row(SCHEMA, {col: valeur})
    assert any(dit in e for e in errs), (famille, errs)


def test_un_tableau_sans_colonne_n_a_pas_de_refus_de_format():
    assert S.validate_row({"fields": []}, {"x": 1}) == []
    assert not vc.complete(None) and not vc.complete({"fields": []})
    assert vc.complete(SCHEMA)


@pytest.mark.parametrize("famille,col,valeur,dit", FAUTES, ids=IDS)
def test_une_ligne_en_faute_s_ecrit_sur_une_AUTRE_colonne(famille, col, valeur, dit):
    """LA règle : on juge ce que le geste écrit. La faute en place part dans `gelees`
    (servie en `hors_type`) — ni refus, ni silence."""
    gelees: list = []
    ligne = {"autre" if col != "autre" else "nom": "ok", col: valeur}
    ecrite = "autre" if col != "autre" else "nom"
    errs = S.validate_row(SCHEMA, ligne, written={ecrite}, gelees=gelees,
                          en_place=dict(ligne))
    assert errs == [], (famille, errs)
    if famille != "sous-record fermé":   # la fermeture ne vaut que pour ce qu'on écrit
        assert any(g["champ"].startswith(col) for g in gelees), (famille, gelees)


def test_une_couche_inconnue_en_place_ne_bloque_pas():
    """Une couche inconnue en place ne refuse pas l'écriture d'une autre colonne."""
    gelees: list = []
    errs = S.validate_row(SCHEMA, {"autre": {"valeur": "v", "commentaire": "c"},
                                   "nom": "ok"}, written={"nom"}, gelees=gelees)
    assert errs == [] and [g["champ"] for g in gelees] == ["autre"]


def test_hors_options_ne_releve_que_ce_que_le_geste_pose():
    """L'autre défaut corrigé : une valeur hors options DÉJÀ en place entrait dans le
    relevé `hors` (ce que l'écriture peut ÉCARTER), et faussait l'écartement de la
    valeur que le geste pose, lui."""
    hors: list = []
    errs = S.validate_row(SCHEMA, {"statut": "z", "etape": "w"}, written={"etape"},
                          hors=hors)
    assert [h["champ"] for h in hors] == ["etape"] and len(errs) == 1


def test_le_texte_servi_dit_la_regle_au_present():
    texte = vc.description_ecriture()
    assert "CONTRACT on every table" in texte and "OTHER columns" in texte
    assert "unknown_columns` was REMOVED" in vc.description_schema()


def test_la_description_servie_dit_la_regle():
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.capabilities.datastore import columns
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp.tools import datastore as tools_ds
    mcp = FastMCP("t")
    tools_ds.register(mcp)
    for nom in ("data_set_schema", "data_write"):
        desc = asyncio.run(mcp.get_tool(nom)).description
        assert "the declared format is a contract on every table" in desc.lower(), nom
        assert '"unknown_columns"?' not in desc, nom
    patch = next(c for c in columns.CAPABILITIES if c.key == "me.datastore.patch_schema")
    assert "unknown_columns` was REMOVED" in patch.description
    for cle in ("me.datastore.append_row", "me.datastore.write_rows",
                "me.datastore.update_row"):
        cap = next(c for c in CAPABILITIES if c.key == cle)
        assert "declared format is a CONTRACT" in cap.description, cle


# ── la base ──────────────────────────────────────────────────────────────────

class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@v124.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


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
        db.upsert_user(SUB, email=f"{SUB}@v124.invalid", name=SUB)
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
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return asyncio.run(mcp.get_tool("data_write")).fn


def _table(schema=SCHEMA, lignes=()):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", SUB, "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    store = make_store(SUB)
    store.set_schema(ns, schema)
    ids = [store.append_row(ns, dict(ligne))["_id"] for ligne in lignes]
    return ns, ns_id, ids


def _base(ns_id: int) -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rows = conn.execute("SELECT row_id, data FROM datastore_rows WHERE ns_id = %s "
                            "ORDER BY created_at, row_id", (ns_id,)).fetchall()
    return [{"_id": r["row_id"], **r["data"]} for r in rows]


def _sans_id(lignes: list[dict]) -> list[dict]:
    return [{k: v for k, v in ligne.items() if k != "_id"} for ligne in lignes]


# ── refusé, rien n'est écrit ; la ligne en faute reste écrivable ────────────

def test_une_faute_est_refusee_sur_toutes_les_faces(client, data_write):
    from oto_mcp.mcp_errors import McpError
    ns, ns_id, _ = _table()
    with pytest.raises(McpError) as e:
        data_write(datastore=ns, row={"nom": "A", "site": "www.x.fr"})
    assert "expected an http(s) URL" in e.value.error.message
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(),
                    json={"nom": "A", "fiche": "Lyon"})
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text
    assert _base(ns_id) == []


def test_une_valeur_hors_options_seule_est_ecartee(client):
    """Le régime de tous les tableaux (#667)."""
    ns, ns_id, _ = _table()
    r = client.post(f"/api/datastores/{ns}/rows", headers=_h(),
                    json={"nom": "A", "statut": "z"})
    assert r.status_code == 201, r.text
    assert r.json()["valeurs_ecartees"][0]["champ"] == "statut"
    assert _sans_id(_base(ns_id)) == [{"nom": "A"}]


def test_la_ligne_en_faute_s_ecrit_sur_ses_autres_colonnes(client, data_write):
    """LA règle, sur la vraie base : la ligne porte une faute de CHAQUE famille, posée
    en base avant la règle ; écrire une autre colonne passe, la ligne n'est pas
    corrigée, et ses fautes se disent."""
    from oto_mcp import db
    ns, ns_id, _ = _table()
    ids = [uuid.uuid4().hex]
    db.datastore_insert_row(ns_id, ids[0], dict(EN_FAUTE))   # l'existant, hors format
    r = client.patch(f"/api/datastores/{ns}/rows/{ids[0]}", headers=_h(),
                     json={"nom": "Acme"})
    assert r.status_code == 200, r.text
    signale = set(r.json().get("hors_type") or {})
    for col in ("statut", "etape", "site", "fiche", "tags"):
        assert any(c.startswith(col) for c in signale), (col, signale)
    data_write(datastore=ns, id=ids[0], row={"nom": "Acme bis"})
    ligne = _sans_id(_base(ns_id))[0]
    assert ligne == {**EN_FAUTE, "nom": "Acme bis"}, "les données du tenant restent"
    # Réécrire la colonne fautive avec la MÊME faute, en revanche, est refusé.
    r = client.patch(f"/api/datastores/{ns}/rows/{ids[0]}", headers=_h(),
                     json={"site": "www.y.fr"})
    assert r.status_code == 400 and r.json()["error"] == "row_invalid", r.text


# ── `unknown_columns` : refusé à la pose et au patch, sur la face REST ───────

def test_unknown_columns_est_refuse_a_la_pose_et_au_patch(client):
    ns, ns_id, _ = _table()
    r = client.put(f"/api/datastores/{ns}/schema", headers=_h(),
                   json={"schema": {**SCHEMA, "unknown_columns": "report"}})
    assert r.status_code == 400 and r.json()["error"] == "invalid_schema", r.text
    assert "no setting any more" in r.json()["detail"]
    r = client.patch(f"/api/datastores/{ns}/schema", headers=_h(),
                     json={"unknown_columns": "reject"})
    assert r.status_code == 400 and r.json()["error"] == "unknown_fields", r.text
    assert "plus aucun réglage" in r.json()["detail"]
    from oto_mcp import db
    assert "unknown_columns" not in db.get_datastore_by_id(ns_id)["schema"]
