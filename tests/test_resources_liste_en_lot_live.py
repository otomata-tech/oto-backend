"""`op=list` des tableaux (`resource_type=datastore_namespace`), sur la base réelle :
le budget de requêtes de l'enrichissement en lot.

Le banc sans base (`test_resources_liste_en_lot.py`) prouve que le chemin unitaire n'est
plus appelé ; celui-ci mesure ce que la base reçoit :
1. un nombre de requêtes qui ne suit ni le nombre de tableaux, ni le nombre de comptes
   propriétaires distincts (une org ou une équipe coûte une lecture par propriétaire
   distinct, gardé fixe ici) ;
2. un seul emprunt au pool pour l'enrichissement ;
3. un chemin qui ne fait que lire (la condition de `reuse_connection`) ;
4. la réponse identique à l'enrichissement tableau par tableau, nombres de lignes et
   libellés compris (compte sans e-mail, tableau vide).
"""
from __future__ import annotations

import json
import re
import uuid

import psycopg
import pytest

from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._types import ResolvedCtx

_ECRITURE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE)\b|\bFOR\s+UPDATE\b",
    re.IGNORECASE)


@pytest.fixture
def requetes(monkeypatch):
    compte = {"n": 0}
    execute = psycopg.Cursor.execute

    def compter(self, *a, **k):
        compte["n"] += 1
        return execute(self, *a, **k)
    monkeypatch.setattr(psycopg.Cursor, "execute", compter)
    return compte


@pytest.fixture
def monde(live, monkeypatch):
    """Un opérateur de plateforme (il liste TOUS les tableaux), une org et une équipe
    propriétaires, fixes ; les comptes propriétaires et les tableaux s'ajoutent."""
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:8]
    ops = f"ops_{u}"
    db.upsert_user(ops, email=f"{ops}@example.test")
    monkeypatch.setenv("OTO_MCP_ADMIN_SUB", ops)
    org = int(org_store.create_org(f"o_{u}"))
    equipe = int(group_store.create_group(org, f"g_{u}"))
    return {"u": u, "ops": ops, "org": org, "equipe": equipe, "n": 0}


def _ajouter(monde, comptes: int, par_org: int, lignes: int = 2) -> None:
    """`comptes` tableaux possédés chacun par un compte NEUF (un sur trois sans e-mail),
    `par_org` tableaux de l'org et autant de l'équipe ; `lignes` lignes dans chacun, sauf
    le premier de chaque lot (un tableau vide vaut 0)."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ids = []
    for _ in range(comptes):
        monde["n"] += 1
        sub = f"p{monde['n']}_{monde['u']}"
        db.upsert_user(sub, email=None if monde["n"] % 3 == 0 else f"{sub}@example.test")
        ids.append(db.create_datastore("user", sub, f"t_{sub}"))
    for i in range(par_org):
        monde["n"] += 1
        ids.append(db.create_datastore("org", str(monde["org"]), f"o{monde['n']}"))
        ids.append(db.create_datastore("group", str(monde["equipe"]), f"g{monde['n']}"))
    with _connect() as conn:
        for ns in ids[1:]:
            for k in range(lignes):
                conn.execute(
                    "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, "
                    "updated_at) VALUES (%s, %s, %s::jsonb, now(), now())",
                    (ns, f"r{k}", json.dumps({"k": k})))


def _lister(monde) -> list[dict]:
    return R._resources(ResolvedCtx(sub=monde["ops"], org_id=None),
                        R.ResourceInput(op="list", resource_type="datastore_namespace")
                        )["resources"]


def test_le_budget_ne_suit_ni_les_tableaux_ni_les_comptes(monde, requetes):
    mesures = []
    for comptes, par_org in ((3, 1), (40, 15)):
        _ajouter(monde, comptes, par_org)
        _lister(monde)                          # caches de processus chauffés
        requetes["n"] = 0
        out = _lister(monde)
        mesures.append((len(out), requetes["n"]))
    (petit, n_petit), (grand, n_grand) = mesures
    assert grand > petit + 60
    assert n_petit == n_grand, (
        f"{n_petit} requêtes pour {petit} tableaux, {n_grand} pour {grand}")


def test_un_seul_emprunt_pour_l_enrichissement(monde, monkeypatch):
    from oto_mcp import db
    from oto_mcp.db import _conn as dbconn
    _ajouter(monde, 5, 2)
    rows = db.list_all_datastores()
    R._enrich_datastores(rows)
    pool = dbconn._get_pool()
    emprunts = []
    reel = pool.connection
    monkeypatch.setattr(pool, "connection",
                        lambda *a, **k: emprunts.append(1) or reel(*a, **k))
    R._enrich_datastores(rows)
    assert len(emprunts) == 1, f"{len(emprunts)} emprunts au pool pour l'enrichissement"


def test_l_enrichissement_ne_fait_que_lire(monde, monkeypatch):
    from oto_mcp import db
    _ajouter(monde, 3, 1)
    rows = db.list_all_datastores()
    vues: list[str] = []
    for classe in (psycopg.Connection, psycopg.Cursor):
        reel = classe.execute

        def _releve(self, query, *a, _reel=reel, **k):
            vues.append(query if isinstance(query, str) else repr(query))
            return _reel(self, query, *a, **k)
        monkeypatch.setattr(classe, "execute", _releve)
    R._enrich_datastores(rows)
    assert vues, "le relevé ne voit rien : il ne regarde pas le bon chemin"
    ecritures = [q for q in vues if _ECRITURE.search(q)]
    assert not ecritures, f"écriture sur le chemin de la liste : {ecritures[:3]}"


def test_la_reponse_est_celle_de_l_enrichissement_unitaire(monde):
    _ajouter(monde, 6, 2)
    out = _lister(monde)
    from oto_mcp import db
    attendu = [R._enrich_datastore(r) for r in db.list_all_datastores()]
    assert out == attendu
    # Les cas jouent vraiment : un compte sans e-mail, un tableau vide, des lignes.
    assert any(e["owner_type"] == "user" and e["owner_label"] is None for e in out)
    assert any(e["row_count"] == 0 for e in out) and any(e["row_count"] == 2 for e in out)
    assert f"o_{monde['u']}" in {e["owner_label"] for e in out if e["owner_type"] == "org"}
