"""Ce que le paramètre ne ferme PAS — les portes latérales de l'origine (oto#70 lot 2).

Le barreau 2 tient l'ÉCRITURE de la couche. Restent les gestes qui l'atteignent sans
l'écrire : lever le format puis le remettre, supprimer la colonne, supprimer la ligne.
Ce fichier les MESURE plutôt que de les déduire — c'est la seule façon de savoir
lesquelles sont déjà fermées, et lesquelles restent ouvertes en connaissance de cause.

⚠️ Il grave aussi le contraire du refus : **déclarer un format doit continuer de
marcher**. La plateforme écrit alors une origine sur toutes les lignes existantes ; si
la garde la prenait pour un appelant, le 1er octobre aurait cassé la fonctionnalité qui
POSE l'origine légitime — la garde aurait mangé ce qu'elle protège.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from oto_mcp.datastore import schema as dsv2


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_org_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


SCHEMA = {"fields": [{"key": "ref", "type": "text"},
                     {"key": "prio", "type": "text", "origine": "system"}]}


def _table(schema=SCHEMA):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-portes", ns)
    st = make_store("sub-portes")
    st.set_schema(ns, schema)
    return st, ns, ns_id


def _arme(monkeypatch, arme: bool = True):
    quand = date(2000, 1, 1) if arme else date(2099, 1, 1)
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, quand.isoformat())


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


# ── ce que la garde ne doit PAS manger ───────────────────────────────────────

def test_declarer_le_format_marche_TOUJOURS_apres_la_date(live, monkeypatch):
    """⚠️ Le faux positif qui aurait coûté le plus cher. Déclarer `origine: "system"`
    fait écrire une origine sur toutes les lignes existantes — par la PLATEFORME. Si la
    garde y voyait un appelant, le 1er octobre aurait cassé le mécanisme même qui pose
    l'origine légitime : la garde aurait mangé ce qu'elle protège."""
    _arme(monkeypatch)
    st, ns, ns_id = _table({"fields": [{"key": "ref"}, {"key": "prio"}]})
    row = st.append_row(ns, {"ref": "a", "prio": "B"})
    st.patch_schema(ns, fields=[{"key": "prio", "origine": "system"}])
    # ⚠️ `(origine inconnue)` et non `"B"` : c'est le lot 1 (`4f6f52ab`). La valeur
    # courante N'EST PAS la valeur d'import — le format déclaré après coup ne peut pas
    # savoir ce qui a été remis au départ, et le dire est plus honnête que de graver la
    # dernière valeur d'agent sous un nom qui invite à la rétablir.
    assert _blob(ns_id, row["_id"])["prio"]["origine"] == dsv2.ORIGINE_INCONNUE


def test_la_plateforme_pose_l_origine_au_PREMIER_ecrasement_apres_la_date(live, monkeypatch):
    """L'autre moitié du mécanisme : la capture paresseuse. Un agent écrit la VALEUR,
    la plateforme conserve l'ancienne dans `origine`. Personne n'a déclaré quoi que ce
    soit — et c'est juste, l'appelant n'a pas écrit la couche."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "b", "prio": "B"})
    st.update_row(ns, row["_id"], {"prio": "C"})
    cellule = _blob(ns_id, row["_id"])["prio"]
    assert (cellule["valeur"], cellule["origine"]) == ("C", "B")


# ── la manœuvre « lever, écrire, remettre » ──────────────────────────────────

def test_lever_le_format_ne_rouvre_PAS_l_ecriture_d_origine(live, monkeypatch):
    """⚠️ La porte que #658/#668 ont rendue célèbre : le schéma comme sortie de secours.
    Sur `readonly`, elle était réelle — lever le cran, écrire, le remettre.

    Ici elle est fermée, et par construction : la garde de l'origine regarde ce que
    l'APPELANT écrit, pas ce que la colonne déclare. Retirer le format ne la désarme
    donc pas. Mesuré, parce qu'une fermeture déduite d'une lecture de code n'est pas
    une fermeture."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "c", "prio": "B"})
    # 1. lever le format
    st.patch_schema(ns, fields=[{"key": "prio", "origine": None}])
    # 2. écrire l'origine — refusé quand même
    with pytest.raises(ValueError) as e:
        st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "forgée"}})
    assert dsv2.PARAMETRE_ORIGINE in str(e.value)


def test_lever_le_format_ne_change_rien_a_ce_qui_est_DECLARE(live, monkeypatch):
    """Le pendant du précédent : sans format, l'écriture déclarée passe — comme avec.
    La déclaration est le seul cran, et il ne dépend pas du schéma."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "d", "prio": "B"})
    st.patch_schema(ns, fields=[{"key": "prio", "origine": None}])
    st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "socle"}},
                  origine_override=True)
    assert _blob(ns_id, row["_id"])["prio"]["origine"] == "socle"


# ── ce qui reste OUVERT, et il faut le savoir ────────────────────────────────

def test_supprimer_la_colonne_emporte_l_origine_SANS_rien_demander(live, monkeypatch):
    """⚠️ **Porte OUVERTE, mesurée, laissée ouverte au barreau 2.** `data_drop_column`
    détruit la colonne et l'origine avec elle, sans déclaration ni trace propre.

    Elle ne relève pas du paramètre : rien n'écrit une origine ici, on en supprime une.
    Ce que la définition d'Alexis protège — « un agent ne doit pas pouvoir y toucher » —
    est donc encore atteignable par ce chemin. C'est au verrou humain (barreau 3) de le
    fermer, pas à une déclaration : déclarer une destruction ne la rendrait pas
    réversible.

    Ce banc n'approuve pas : il DATE l'état, pour qu'un jour on ne croie pas la porte
    fermée parce que le paramètre existe.

    ⚠️ Mesuré aussi : la purge exige DEUX gestes. Une colonne encore déclarée au schéma
    est refusée (« retire d'abord le champ, puis purge »), ce qui écarte la faute de
    frappe mais pas le geste voulu."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "e", "prio": "B"})
    st.update_row(ns, row["_id"], {"prio": "C"})           # la plateforme pose l'origine
    assert _blob(ns_id, row["_id"])["prio"]["origine"] == "B"

    with pytest.raises(ValueError) as e:
        st.drop_column(ns, "prio", confirm=True)
    assert "DÉCLARÉE" in str(e.value), "la friction du schéma a disparu"

    st.patch_schema(ns, remove=["prio"])
    st.drop_column(ns, "prio", confirm=True)
    assert "prio" not in _blob(ns_id, row["_id"]), "l'origine a survécu — tant mieux"


def test_supprimer_la_ligne_emporte_l_origine_SANS_rien_demander(live, monkeypatch):
    """⚠️ **Porte OUVERTE, même raison.** Une ligne se supprime, son origine part avec.
    Fermer ce geste-là demande de décider ce qu'on fait d'une ligne dont l'origine est
    verrouillée — refuser, ou emporter avec une trace au journal (#19) : un arbitrage,
    pas une garde. Daté ici, ouvert jusqu'au barreau 3."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "f", "prio": "B"})
    st.update_row(ns, row["_id"], {"prio": "C"})
    st.delete_row(ns, row["_id"])
    assert _blob(ns_id, row["_id"]) == {}
