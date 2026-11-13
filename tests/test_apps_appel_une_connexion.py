"""Une lecture d'app ouverte (`POST /api/me/apps/{id}/call`) tient UN emprunt au pool.

Une app ouverte lit ses tableaux par cette route, une requête par lecture, souvent huit
à la fois au premier écran. Chaque requête relisait, chacune sur son PROPRE emprunt au
pool (`BEGIN`/requête/`COMMIT`, ~3 allers-retours à 3,14 ms de la base managée) : l'app
et son propriétaire, l'org de l'app, l'org du tableau, l'accès de l'auteur, puis le
tableau lui-même (org active, équipes, résolution, schéma) — une vingtaine d'emprunts
pour quelques centaines de lignes. Les lectures de tableau (`read_rows`,
`read_aggregate`) tiennent désormais UN emprunt
(`db.reuse_connection`), et une lecture répétée dans l'appel n'est faite qu'une fois
(`lecture_d_appel`).

Trois choses à tenir, sur la base réelle :
1. un seul emprunt au pool par lecture d'app ;
2. un chemin qui ne fait que lire (la condition de `reuse_connection`, autocommit) ;
3. la même réponse qu'avant, refus compris.
"""
from __future__ import annotations

import re
import uuid

import psycopg
import pytest

from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

# Une écriture où qu'elle soit dans la requête, pas seulement en tête : une CTE
# (`WITH … INSERT`) écrit aussi.
_ECRITURE = re.compile(
    r"\b(INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\S+\s+SET|MERGE\s+INTO|CREATE|ALTER|DROP|"
    r"TRUNCATE)\b|\bFOR\s+(NO\s+KEY\s+)?UPDATE\b|\bFOR\s+SHARE\b|\bnextval\s*\(",
    re.IGNORECASE)


@pytest.fixture
def monde(live):
    """Une org, son auteur et un lecteur membres, un tableau de l'org à 30 lignes, une
    app de l'org qui le déclare (deux champs)."""
    from oto_mcp import db, org_store
    from oto_mcp.datastore.core import make_store
    u = uuid.uuid4().hex[:8]
    auteur, lecteur = f"auteur_{u}", f"lecteur_{u}"
    for sub in (auteur, lecteur):
        db.upsert_user(sub, email=f"{sub}@example.test")
    org = int(org_store.create_org(f"o_{u}"))
    for sub in (auteur, lecteur):
        org_store.add_org_member(org, sub)
        org_store.set_active_org(sub, org)
    ns = db.create_datastore("org", str(org), f"t_{u}")
    store = make_store(auteur)
    store.set_schema(str(ns), {"key": "nom", "fields": [
        {"key": "nom", "type": "text"}, {"key": "score", "type": "number"},
        {"key": "secret", "type": "text"}]})
    store.write_rows(str(ns), [{"nom": f"n{i}", "score": i, "secret": "x"}
                               for i in range(30)], key="nom")
    app = db.apps.create(("org", str(org)), org, title="Cockpit", by=auteur,
                         manifest={"tables": {"t": {"table": str(ns),
                                                    "fields": ["nom", "score"],
                                                    "set": {}, "add": []}},
                                   "agents": {}},
                         html="", thumbnail=None)
    return {"lecteur": lecteur, "org": org, "app": int(app["id"])}


def _lire(m, action="read_rows", **options):
    return C._call(ResolvedCtx(sub=m["lecteur"], org_id=m["org"]),
                   C.AppCallInput(app_id=m["app"], action=action, table="t",
                                  options=options))


def test_une_lecture_rend_les_lignes_et_les_seuls_champs_declares(monde):
    out = _lire(monde, limit=10, order_by="score", order_dir="asc")
    assert out["total"] == 30
    assert [r["score"] for r in out["rows"]] == list(range(10))
    assert all("secret" not in r for r in out["rows"])
    groupes = _lire(monde, "read_aggregate",
                    metrics=[{"op": "sum", "field": "score", "label": "s"}])["groups"]
    assert groupes and groupes[0]["s"] == sum(range(30))


@pytest.mark.parametrize("action", ["read_rows", "read_aggregate"])
def test_un_seul_emprunt_au_pool_par_lecture(monde, monkeypatch, action):
    from oto_mcp.db import _conn as dbconn
    _lire(monde, action)                          # caches de processus chauffés
    pool = dbconn._get_pool()
    emprunts = []
    reel = pool.connection
    monkeypatch.setattr(pool, "connection",
                        lambda *a, **k: emprunts.append(1) or reel(*a, **k))
    _lire(monde, action)
    assert len(emprunts) == 1, f"{len(emprunts)} emprunts au pool pour une lecture d'app"


@pytest.mark.parametrize("action", ["read_rows", "read_aggregate"])
def test_le_chemin_d_une_lecture_ne_fait_que_lire(monde, monkeypatch, action):
    """La garde de `reuse_connection` : autocommit, donc une écriture serait visible des
    autres connexions AVANT la fin de l'appel."""
    vues: list[str] = []
    for classe in (psycopg.Connection, psycopg.Cursor):
        reel = classe.execute

        def _releve(self, query, *a, _reel=reel, **k):
            vues.append(query if isinstance(query, str) else repr(query))
            return _reel(self, query, *a, **k)
        monkeypatch.setattr(classe, "execute", _releve)
    _lire(monde, action)
    assert vues, "le relevé ne voit rien : il ne regarde pas le bon chemin"
    ecritures = [q for q in vues if _ECRITURE.search(q)]
    assert not ecritures, f"écriture sur le chemin d'une lecture d'app : {ecritures[:3]}"


def test_un_refus_reste_un_refus(monde):
    with pytest.raises(AuthzDenied) as e:
        _lire(monde, filter={"secret": "x"})
    assert e.value.code == "undeclared_field"
    with pytest.raises(AuthzDenied) as e:
        C._call(ResolvedCtx(sub=monde["lecteur"], org_id=monde["org"]),
                C.AppCallInput(app_id=monde["app"], action="read_rows", table="absente"))
    assert e.value.code == "undeclared_table"


@pytest.mark.parametrize("action", ["read_rows", "read_aggregate"])
def test_une_lecture_ne_pose_pas_deux_fois_la_meme_question(monde, monkeypatch, action):
    """Dans la portée d'un emprunt, une lecture déjà faite pour les mêmes arguments ne
    repart pas à la base (`lecture_d_appel`) : l'org où vit le tableau et l'accès de
    l'auteur lisaient chacun le propriétaire du tableau, sa résolution le relisait."""
    _lire(monde, action)
    vues: list[tuple] = []
    reel = psycopg.Cursor.execute

    def _releve(self, query, params=None, *a, **k):
        vues.append((query if isinstance(query, str) else repr(query), repr(params)))
        return reel(self, query, params, *a, **k)
    monkeypatch.setattr(psycopg.Cursor, "execute", _releve)
    _lire(monde, action)
    assert vues, "le relevé ne voit rien : il ne regarde pas le bon chemin"
    doublons = [q for q in set(vues) if vues.count(q) > 1]
    assert not doublons, f"{len(doublons)} question(s) posée(s) deux fois : {doublons[:2]}"


def test_la_projection_descendue_rend_ce_que_rendait_la_projection_apres_coup(monde):
    """Les champs déclarés partent au store (`page_rows(fields=…)`) : même lignes, mêmes
    colonnes, et les métadonnées que l'hôte relit pour écrire (`_revision`)."""
    from oto_mcp.capabilities.datastore import rows as R
    ctx = ResolvedCtx(sub=monde["lecteur"], org_id=monde["org"])
    app = C._app_pour(ctx, monde["app"])
    ns, t, _ = C._table(app, "t")
    entier = R._list_rows(ctx, R.ListRowsInput(datastore=ns, limit=50, order_by="score",
                                               order_dir="asc"))
    avant = [C._projeter(t, r) for r in entier["rows"]]
    apres = _lire(monde, limit=50, order_by="score", order_dir="asc")["rows"]
    assert apres == avant
    assert all(r.get("_revision") and r.get("_id") for r in apres)
    assert set(apres[0]) == {"nom", "score", *C._META}
