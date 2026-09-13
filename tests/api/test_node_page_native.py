"""Le premier pas d'une page native (oto#198), rejoué sur la route servie.

Ce que ce banc prouve, sur `make_routes` et un PostgreSQL jetable — le seul niveau qui
dit un CODE DE RETOUR et un CORPS réellement servis :

- **R1/R2** : une racine privée se crée, se relit et se renomme sous la MÊME identité ;
  la fiche dit `edit_surface = node`, et un titre blanc est refusé sans rien écrire ;
- **R3** : un autre membre ordinaire de l'org ne lit ni n'écrit cette page, et ses refus
  sont, au caractère près, ceux d'un identifiant inconnu ;
- **R4** : le parcours ne touche pas l'ancien modèle — aucun projet, aucun document,
  aucun nœud marqué `legacy` — et la page est lisible sans attendre de projection ;
- **R5** : une racine héritée SANS `doc_id` ne bascule pas vers le chemin natif : elle
  dit `project` et refuse l'écriture native (`409 node_projete`) ; une page héritée
  dit `doc` avec son `doc_id` ;
- **R6** : un guide possédé dit `guide` et refuse l'écriture native (`409 node_guide`)
  en nommant sa destination, corps intact ; le readme injecté s'adresse sans slug ;
- **R7** : un tableau hérité au nom vide ne sert pas une fiche à poignée nulle
  (`500 noeud_incoherent`, journalisé) — et reste un 404 pour un tiers.

La dérivation elle-même se tient sans base dans `tests/test_node_edit_surface.py`, la
garde d'écriture au grain de la fonction dans `tests/test_node_edit_natif.py`.

Les nœuds hérités sont posés en SQL, comme la conversion les posait : plus aucun chemin
de produit n'en écrit depuis l'arrêt de la recopie. Le porteur est identifié par un
vérifieur factice dont le bearer EST le sub (recette de `test_node_edit_proprietaire.py`) :
ce qu'on teste est en aval de l'authentification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

_INCONNU = "nod_" + hashlib.md5(b"aucune page ici").hexdigest()[:24]


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@pages.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base JETABLE et le vrai `init_db()` — jamais dans la base partagée."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_node_native_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield init_db
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


@pytest.fixture(scope="module")
def org(live):
    """Une org et deux membres ORDINAIRES : la page est privée par son scope, pas parce
    que l'un des deux administrerait quelque chose."""
    from oto_mcp import db, org_store
    membre, autre = "usr_pn_membre", "usr_pn_autre"
    for sub in (membre, autre):
        db.upsert_user(sub, email=f"{sub}@pages.invalid", name=sub)
    oid = org_store.create_org("Maison des pages", created_by=membre)
    for sub in (membre, autre):
        org_store.add_org_member(oid, sub, "org_member")
        org_store.set_active_org(sub, oid)
    return {"id": oid, "membre": membre, "autre": autre}


def _edit(client, sub: str, **corps):
    return client.post("/api/me/nodes/edit", json=corps, headers=_h(sub))


def _get(client, sub: str, nid: str):
    return client.get(f"/api/me/nodes/{nid}", headers=_h(sub))


def _page(client, sub: str, titre: str) -> str:
    r = _edit(client, sub, op="create", kind="page", scope="user", title=titre)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _sql(sql: str, params=()) -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def _pose_heritee(owner: str, kind: str, props: dict) -> str:
    pid = "nod_" + uuid.uuid4().hex[:24]
    _sql("INSERT INTO nodes (public_id, kind, owner_type, owner_id, props) "
         "VALUES (%s, %s, 'user', %s, %s::jsonb)", (pid, kind, owner, json.dumps(props)))
    return pid


def _derive(owner: str, slug: str) -> str:
    """L'identifiant d'une couche de contexte, dérivé de sa clé (`db/guides._public_id_sql`)."""
    return "nod_" + hashlib.md5(f"ctx:user:{owner}:{slug}".encode()).hexdigest()[:24]


# ── R1, R2 : créer, relire, renommer — une seule identité ─────────────────────

def test_R1_une_racine_privee_se_cree_et_se_relit_sous_la_meme_identite(client, org):
    sub = org["membre"]
    r = _edit(client, sub, op="create", kind="page", scope="user", title="Page vide")
    assert r.status_code == 200, r.text
    nid = r.json()["id"]
    assert r.json() == {"ok": True, "id": nid, "op": "create"}

    lu = _get(client, sub, nid)
    assert lu.status_code == 200, lu.text
    fiche = lu.json()
    assert (fiche["id"], fiche["name"], fiche["edit_surface"]) == (nid, "Page vide", "node")
    assert fiche["doc_id"] is None and fiche["project_id"] is None


def test_R2_renommer_garde_l_identite_taille_le_titre_et_refuse_un_blanc(client, org):
    sub = org["membre"]
    nid = _page(client, sub, "Avant renommage")
    r = _edit(client, sub, op="update", node_id=nid, title="  Titre renommé  ")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": nid, "op": "update"}
    lu = _get(client, sub, nid).json()
    assert (lu["id"], lu["name"]) == (nid, "Titre renommé")

    r = _edit(client, sub, op="update", node_id=nid, title="   ")
    assert (r.status_code, r.json()["error"]) == (400, "missing_title"), r.text
    assert _get(client, sub, nid).json()["name"] == "Titre renommé", (
        "le titre blanc refusé a quand même été écrit")


# ── R3 : l'autre membre ne distingue pas la page d'un identifiant inconnu ─────

def test_R3_un_autre_membre_ne_lit_ni_n_ecrit_et_n_apprend_rien(client, org):
    nid = _page(client, org["membre"], "Page privée du membre")
    autre = org["autre"]
    lire, lire_inconnu = _get(client, autre, nid), _get(client, autre, _INCONNU)
    ecrire = _edit(client, autre, op="update", node_id=nid, title="PIRATÉ")
    ecrire_inconnu = _edit(client, autre, op="update", node_id=_INCONNU, title="PIRATÉ")

    assert lire.status_code == 404, lire.text
    assert (lire.status_code, lire.text) == (lire_inconnu.status_code, lire_inconnu.text)
    assert (ecrire.status_code, ecrire.text) == (ecrire_inconnu.status_code, ecrire_inconnu.text)
    assert (ecrire.status_code, ecrire.text) == (lire.status_code, lire.text), (
        "lecture et écriture refusent avec deux corps différents")
    assert _get(client, org["membre"], nid).json()["name"] == "Page privée du membre"


# ── R4 : rien de l'ancien modèle, aucune projection attendue ─────────────────

def _comptes() -> tuple:
    return tuple(_sql(q)[0]["n"] for q in (
        "SELECT count(*) AS n FROM projects",
        "SELECT count(*) AS n FROM docs",
        "SELECT count(*) AS n FROM nodes WHERE props ? 'legacy'"))


def test_R4_le_parcours_ne_cree_rien_dans_l_ancien_modele(client, org):
    sub = org["membre"]
    avant = _comptes()
    nid = _page(client, sub, "Page sans ancien monde")
    lu = _get(client, sub, nid)                   # tout de suite : aucune projection
    assert lu.status_code == 200, lu.text
    assert _edit(client, sub, op="update", node_id=nid, title="Renommée").status_code == 200
    assert _comptes() == avant, "créer ou renommer une page native a touché l'ancien modèle"


# ── R5 : un nœud hérité garde son chemin, `doc_id` absent ou non ─────────────

def test_R5_une_racine_heritee_sans_doc_id_ne_bascule_pas_vers_le_natif(client, org):
    sub = org["membre"]
    racine = _pose_heritee(sub, "page", {"legacy": "prj", "legacy_id": 7,
                                         "title": "Racine héritée", "pinned": True})
    lu = _get(client, sub, racine)
    assert lu.status_code == 200, lu.text
    out = lu.json()
    assert (out["edit_surface"], out["project_id"], out["doc_id"]) == ("project", 7, None)

    r = _edit(client, sub, op="update", node_id=racine, title="Renommée en natif")
    assert (r.status_code, r.json()["error"]) == (409, "node_projete"), r.text
    assert _get(client, sub, racine).json()["name"] == "Racine héritée"


def test_R5_une_page_heritee_dit_doc_avec_son_doc_id(client, org):
    sub = org["membre"]
    page = _pose_heritee(sub, "page", {"legacy": "doc", "legacy_id": 12, "project_id": 7,
                                       "title": "Page héritée"})
    lu = _get(client, sub, page)
    assert lu.status_code == 200, lu.text
    assert (lu.json()["edit_surface"], lu.json()["doc_id"]) == ("doc", 12)


# ── R6 : un guide possédé s'écrit par `oto_guide`, et le refus le dit ─────────

def test_R6_un_guide_possede_refuse_l_ecriture_native_corps_intact(client, org):
    from oto_mcp import guide_store
    sub = org["membre"]
    guide_store.set_guide("user", sub, "prospection", "Corps", title="Prospection")
    nid = _derive(sub, "prospection")
    lu = _get(client, sub, nid)
    assert lu.status_code == 200, lu.text
    assert lu.json()["edit_surface"] == "guide"

    r = _edit(client, sub, op="update", node_id=nid, body_md="Corps par la mauvaise porte")
    assert r.status_code == 409, r.text
    refus = r.json()
    assert refus["error"] == "node_guide"
    assert "`oto_guide`" in refus["detail"]
    assert refus["details"]["slug"] == "prospection"
    relu = guide_store.read_guide_scoped("prospection", scope="user", sub=sub)
    assert relu["body_md"] == "Corps", "le corps du guide a été réécrit malgré le refus"


def test_R6_le_readme_injecte_s_adresse_sans_slug(client, org):
    from oto_mcp import guide_store
    sub = org["membre"]
    guide_store.set_init_guide("user", sub, "Mon readme")
    r = _edit(client, sub, op="update", node_id=_derive(sub, "readme"), body_md="autre")
    assert r.status_code == 409, r.text
    assert "sans slug" in r.json()["detail"]
    assert guide_store.init_guide_body("user", sub) == "Mon readme"


# ── R7 : un stockage incohérent se refuse au propriétaire, se tait au tiers ───

def test_R7_un_tableau_herite_au_nom_vide_ne_sert_pas_de_poignee_nulle(client, org, caplog):
    sub = org["membre"]
    nid = _pose_heritee(sub, "tableau", {"legacy": "tbl", "legacy_id": 5, "title": ""})
    with caplog.at_level(logging.ERROR, logger="oto_mcp.capabilities.node_view"):
        r = _get(client, sub, nid)
    assert r.status_code == 500, r.text
    assert r.json()["error"] == "noeud_incoherent"
    assert nid in r.json()["detail"]
    assert any(nid in rec.getMessage() for rec in caplog.records), (
        "l'incohérence est servie sans être journalisée")

    tiers, inconnu = _get(client, org["autre"], nid), _get(client, org["autre"], _INCONNU)
    assert tiers.status_code == 404, tiers.text
    assert tiers.text == inconnu.text
