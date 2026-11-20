"""Un vide qui ne pose RIEN d'autre est refusé, en nommant la porte (#724).

⚠️ **Depuis oto#140 J2 (arbitré le 23/09/2026), `""` et `[]` sont des VALEURS** : ils
remplacent la valeur en place, et la valeur remplacée revient dans `valeurs_effacees`
— le risque des gabarits à demi remplis est accepté. #608 et #724 ne valent plus que
pour `{}`, qui n'est pas une valeur (oto#165). L'histoire ci-dessous est celle de `[]`
avant J2 ; les bancs éprouvent la règle d'aujourd'hui.

Mesuré en production le 2026-09-01, entre 04:16 et 04:20 : dix `data_write(id=…,
row={'contacts': []})` sur des fiches clientes, dix `200`, zéro retrait.

⚠️ **La porte existait déjà, et la réponse la nommait déjà.** `contacts: null` efface,
et le relevé `valeurs_ignorees` disait mot pour mot « Pour vider un champ pour de bon,
nomme-le avec `null` ». Elle n'a pas été empruntée : il n'y a eu ce jour-là **qu'une
seule** écriture `null` explicite, sur une table d'ESSAI jetable — jamais sur les fiches
ratées, dont l'une porte encore aujourd'hui le contact qu'on voulait retirer. C'est la
réfutation de « il suffit de le dire » : un témoin logé dans le corps d'une réponse
réussie n'oblige personne à le lire. **Le refus, lui, ne se rate pas.**

Deux options ont été écartées, et savoir pourquoi évite de les rejouer :

- **faire effacer la liste vide** (comme `null`) : détruit les 104 réémissions par mois
  mesurées ci-dessous — c'est la charge dominante, où `[]` veut dire « rien trouvé » ;
- **faire effacer la liste vide SEULE** : ferait dépendre un geste DESTRUCTEUR de ses
  voisines. « Selon le contexte ta donnée disparaît » est une perte silencieuse ; le
  refus, lui, dit « selon le contexte ton appel échoue » — un désagrément qui enseigne.

Mesure qui fonde la règle (journal de production, 30 j glissants au 2026-09-01) :
574 écritures portent une liste vide sur 43 444 ; **562 (98 %) réémettent la fiche
entière**, 12 portent le vide seul ; sur 324 couples (appel, colonne) résolubles,
**105 visaient une colonne encore peuplée — 104 réémissions, et le reste des vides
seuls**. Le détail et ses réserves sont dans `docs/datastore.md`.

Les deux faces sont éprouvées, sur la table de routes réelle et sur le tool monté :
un comportement vrai d'un seul côté est un comportement absent.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@vide.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


SUB = "usr_vide"
#: Les colonnes que les bancs écrivent, déclarées : une colonne non déclarée est refusée.
SCHEMA = {"fields": [{"key": k} for k in ("siren", "contacts", "statut", "qualification",
                                          "notes_verification")]}


def _h() -> dict:
    return {"Authorization": f"Bearer {SUB}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_vide_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        from oto_mcp import db
        db.upsert_user(SUB, email=f"{SUB}@vide.invalid", name=SUB)
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture
def fiche(live):
    """Une fiche dont l'unique interlocuteur est à retirer — le cas de #724."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "vivier-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    make_store(SUB).set_schema(ns, SCHEMA)
    row = make_store(SUB).append_row(
        ns, {"siren": "552032534", "contacts": ["a@exemple.invalid"], "statut": "nouveau"})
    return ns, ns_id, row["_id"]


async def _data_write(monkeypatch):
    """Le tool MCP tel qu'il est MONTÉ (`register` + `.fn`) — pas le store appelé à
    la main : ce qu'on éprouve est la face servie aux agents."""
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import datastore as tools_ds
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    mcp = FastMCP("test")
    tools_ds.register(mcp)
    return (await mcp.get_tool("data_write")).fn


def _blob(ns_id: int, row_id: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


# ── le cas SEUL : refusé, et le refus ENSEIGNE ────────────────────────────────

@pytest.mark.asyncio
async def test_face_MCP_un_objet_vide_SEUL_est_refuse_en_nommant_la_porte(
        fiche, client, monkeypatch):
    """Le geste des dix retraits perdus. Il ne pose rien d'autre : il ne peut pas
    aboutir par ce chemin, et il ne doit pas répondre comme un succès.

    ⚠️ Le refus doit NOMMER exactement quoi écrire à la place. C'est tout son
    intérêt sur un relevé : il arrive au moment où l'agent peut encore corriger."""
    from oto_mcp.mcp_errors import McpError

    data_write = await _data_write(monkeypatch)
    ns, ns_id, rid = fiche

    with pytest.raises(McpError) as e:
        data_write(datastore=ns, id=rid, row={"contacts": {}})

    message = str(e.value)
    assert "contacts" in message, message
    assert "null" in message, f"le refus doit dire PAR QUOI vider : {message}"
    assert '"contacts": null' in message, \
        f"nommer la porte ne suffit pas, il faut l'écrire telle quelle : {message}"
    assert _blob(ns_id, rid)["contacts"] == ["a@exemple.invalid"], \
        "un refus n'écrit rien — surtout pas l'effacement qu'il refuse"


def test_face_REST_un_objet_vide_SEUL_rend_400_et_pas_200(client, fiche):
    ns, ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts": {}})
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "")
    assert "contacts" in detail and '"contacts": null' in detail, detail
    assert _blob(ns_id, rid)["contacts"] == ["a@exemple.invalid"]


def test_la_chaine_vide_SEULE_remplace_l_objet_vide_SEUL_est_refuse(client, fiche):
    """oto#140 J2 : `""` est une valeur — il remplace, et le dit ; `{}` n'en est pas
    une — seul, il est refusé (#724)."""
    ns, ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"statut": {}})
    assert r.status_code == 400, r.text
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"statut": ""})
    assert r.status_code == 200, r.text
    assert _blob(ns_id, rid)["statut"] == ""
    assert [e["champ"] for e in r.json()["valeurs_effacees"]] == ["statut"]


def test_le_null_nomme_EST_la_porte_et_elle_est_ouverte(client, fiche):
    """Le refus ne vaut que si ce qu'il désigne fonctionne. C'est le besoin
    d'origine de l'issue : retirer le dernier contact d'une fiche."""
    ns, ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts": None})
    assert r.status_code == 200, r.text
    assert _blob(ns_id, rid)["contacts"] is None
    assert r.json().get("valeurs_effacees"), \
        "un effacement réel se dit — c'est lui qui a détruit quelque chose"


def test_deux_vides_ensemble_et_rien_dautre_sont_refuses_ensemble(client, fiche):
    """« Rien d'autre posé » se juge sur le GESTE entier, pas colonne par colonne."""
    ns, ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts": {}, "statut": {}})
    assert r.status_code == 400, r.text
    blob = _blob(ns_id, rid)
    assert blob["contacts"] == ["a@exemple.invalid"] and blob["statut"] == "nouveau"


# ── le cas ACCOMPAGNÉ ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_la_fiche_ENTIERE_reemise_avec_une_liste_vide_la_REMPLACE_et_le_dit(
        fiche, client, monkeypatch):
    """Payload relevé en production (01/09) : la fiche entière, où `contacts: []`
    voulait dire « l'enrichissement n'a rien trouvé ». Depuis oto#140 J2, `[]` est une
    valeur : il REMPLACE, risque accepté, et la valeur remplacée se dit dans
    `valeurs_effacees` — c'est ce qui permet de la rétablir.
    """
    data_write = await _data_write(monkeypatch)
    ns, ns_id, rid = fiche

    out = data_write(datastore=ns, id=rid, row={
        "qualification": {"valeur": "indetermine", "comment": "registre — rien trouvé"},
        "contacts": [],
        "notes_verification": "registre consulté ; aucune page au nom de la structure",
    })

    assert out["contacts"] == [], out
    assert [(e["champ"], e["valeur"]) for e in out["valeurs_effacees"]] == [
        ("contacts", ["a@exemple.invalid"])], out
    assert "valeurs_ignorees" not in out, out
    blob = _blob(ns_id, rid)
    assert blob["contacts"] == []
    assert blob["notes_verification"].startswith("registre consulté")


def test_face_REST_la_fiche_reemise_avec_un_objet_vide_NE_LEFFACE_PAS(client, fiche):
    ns, ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts": {}, "statut": "traite"})
    assert r.status_code == 200, r.text
    assert _blob(ns_id, rid)["contacts"] == ["a@exemple.invalid"], r.text
    assert r.json().get("valeurs_ignorees"), r.json()

# ── le voisinage qui ne doit pas bouger ───────────────────────────────────────

def test_reecrire_la_valeur_IDENTIQUE_reste_un_no_op_accepte(client, fiche):
    """Le round-trip qui réémet une valeur inchangée POSE une valeur : ce n'est pas
    un vide, et le refuser arrêterait la flotte (#623 → #625)."""
    ns, _ns_id, rid = fiche
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"statut": "nouveau"})
    assert r.status_code == 200, r.text


def test_un_vide_sur_une_colonne_DEJA_vide_passe_et_ne_dit_rien(client, live):
    """Rien à perdre, donc rien à refuser NI à annoncer : poser `[]` là où il n'y
    avait rien reste le chemin normal de création depuis un gabarit."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "neuve-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    make_store(SUB).set_schema(ns, SCHEMA)
    rid = make_store(SUB).append_row(ns, {"siren": "552032534"})["_id"]
    r = client.patch(f"/api/datastores/{ns}/rows/{rid}", headers=_h(),
                     json={"contacts": []})
    assert r.status_code == 200, r.text
    assert _blob(ns_id, rid)["contacts"] == []
    assert "valeurs_effacees" not in r.json(), r.json()
    assert "valeurs_ignorees" not in r.json(), r.json()


def test_un_LOT_qui_porte_la_cle_metier_ne_casse_pas_et_la_liste_vide_remplace(live):
    """Le lot dédouble par la clé : elle est TOUJOURS posée, donc une row de lot pose
    toujours quelque chose et ne tombe jamais sous #724. Depuis oto#140 J2, sa liste
    vide REMPLACE, comme sur les autres chemins."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "lot-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    store = make_store(SUB)
    store.set_schema(ns, {"key": "siren", "fields": [{"key": "siren", "type": "text"},
                                                     {"key": "contacts"}]})
    store.write_rows(ns, [{"siren": "552032534", "contacts": ["a@exemple.invalid"]}])
    out = store.write_rows(ns, [{"siren": "552032534", "contacts": []}], key="siren")
    assert out["updated"] == 1, out
    assert _blob(ns_id, out["ids"][0])["contacts"] == []
