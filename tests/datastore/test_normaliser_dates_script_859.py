"""La reprise de l'existant (`scripts/normaliser_dates.py`, oto-backend#859).

Deux bancs : le juge, pur — ce qu'il réécrit, ce qu'il liste sans y toucher ; puis la
reprise sur une vraie base — à blanc n'écrit rien, `--appliquer` écrit sous le service
de reprise et laisse l'avant au journal, une collision de clé métier de type date est
sautée et listée, et une relance ne trouve plus rien.
"""
from __future__ import annotations

import pathlib
import sys
import uuid

import pytest

# La racine du dépôt n'est pas dans `sys.path` en CI : l'import ne passait que parce
# qu'un banc collecté avant (`test_migrer_colonnes_fantomes_957.py`) l'y avait mise.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import normaliser_dates as reprise  # noqa: E402

SCHEMA = {"fields": [
    {"key": "d", "type": "datetime"},
    {"key": "j", "type": "date"},
    {"key": "o", "type": "object", "fields": [{"key": "s", "type": "date"}]},
    {"key": "l", "type": "list", "of": {"fields": [{"key": "q", "type": "datetime"}]}},
    {"key": "t", "type": "text"},
]}


# ── 1. le juge ────────────────────────────────────────────────────────────────

def test_le_juge_reecrit_les_chaines_lisibles_et_liste_le_reste():
    data = {"d": "2026-09-04 10:00", "j": {"valeur": "2026-09-04T00:30:00+02:00",
                                         "origine": "2026-09-04T00:30:00+02:00"},
            "o": {"s": "04/09/2026"}, "l": [{"q": 1788516000}, {"q": "plus tard"}],
            "t": "2026-09-04 10:00"}
    neuf, constats = reprise.reprendre_ligne(SCHEMA, data)
    assert neuf == {"d": "2026-09-04T10:00:00Z",
                    "j": {"valeur": "2026-09-04", "origine": "2026-09-04T00:30:00+02:00"},
                    "o": {"s": "2026-09-04"},
                    "l": [{"q": 1788516000}, {"q": "plus tard"}],
                    "t": "2026-09-04 10:00"}
    assert sorted((c, p) for c, p, _ in constats) == sorted([
        ("reprise", "d"), ("dont_sans_fuseau", "d"), ("reprise", "j"),
        ("reprise", "o.s"), ("non_chaine", "l[].q"), ("illisible", "l[].q")])


def test_une_ligne_deja_dans_sa_forme_est_rendue_telle_quelle():
    data = {"d": "2026-09-04T10:00:00Z", "j": "2026-09", "o": {"s": "@empty"}}
    neuf, constats = reprise.reprendre_ligne(SCHEMA, data)
    assert neuf is data and constats == []


def test_la_cle_metier_de_type_date_est_reconnue():
    assert reprise.cle_de_type_date({**SCHEMA, "key": "j"}) == "j"
    assert reprise.cle_de_type_date({**SCHEMA, "key": "t"}) is None


# ── 2. la reprise sur une vraie base ──────────────────────────────────────────

SUB = "sub-normaliser-dates-859"


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def base(live):
    """Deux tableaux, leurs lignes posées À LA MAIN comme l'existant les porte —
    l'écriture d'aujourd'hui les normaliserait."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    from psycopg.types.json import Jsonb
    db.upsert_user(SUB, email=f"{SUB}@exemple.invalid", name=SUB)
    st = make_store(SUB)
    ns_noms = [f"nd859-{uuid.uuid4().hex[:6]}" for _ in range(2)]
    ns = [db.create_datastore("user", SUB, n) for n in ns_noms]
    st.set_schema(str(ns[0]), SCHEMA)
    # Le second tableau a sa CLÉ MÉTIER sur une date : deux formes du même instant.
    st.set_schema(str(ns[1]), {"key": "k", "fields": [{"key": "k", "type": "datetime"}]})
    lignes = {
        (ns[0], "a"): {"d": "2026-09-04T12:00:00+02:00", "t": "x"},
        (ns[0], "b"): {"d": "2026-09-04T10:00:00Z", "j": "2026-09-04T10:00:00Z"},
        (ns[0], "c"): {"d": 1788516000, "o": {"s": "4/9/2026"}},
        (ns[0], "d"): {"d": "bientôt"},
        (ns[1], "e"): {"k": "2026-09-04T10:00:00Z"},
        (ns[1], "f"): {"k": "2026-09-04T12:00:00+02:00"},
        (ns[1], "g"): {"k": "2026-09-05 08:00"},
    }
    for (n, rid), data in lignes.items():
        _sql("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
             n, rid, Jsonb(data))
    return ns


def _data(ns_id: int, row_id: str):
    return _sql("SELECT data, rev FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
                ns_id, row_id)[0]


def _silence(_):
    return None


def test_a_blanc_rien_n_est_ecrit_et_tout_est_compte(base):
    avant = {rid: _data(base[0], rid) for rid in "abcd"}
    bilan = reprise.executer(appliquer=False, taille=2, dire=_silence)
    assert {rid: _data(base[0], rid) for rid in "abcd"} == avant
    c = bilan["par_colonne"]
    assert c[(base[0], "d")] == {"reprise": 1, "non_chaine": 1, "illisible": 1}
    assert c[(base[0], "j")] == {"reprise": 1}
    assert c[(base[0], "o.s")] == {"reprise": 1}
    assert c[(base[1], "k")] == {"reprise": 2, "dont_sans_fuseau": 1}
    assert bilan["cles_date"] == {base[1]: "k"}
    assert bilan["lignes"] == 5     # a, b, c, f, g


def test_appliquer_ecrit_journalise_saute_la_collision_et_ne_trouve_plus_rien(base):
    rev_avant = _data(base[0], "a")["rev"]
    bilan = reprise.executer(appliquer=True, taille=2, dire=_silence)
    assert bilan["collisions"] == [(base[1], "f")]
    assert bilan["lignes"] == 4

    a = _data(base[0], "a")
    assert a["data"] == {"d": "2026-09-04T10:00:00Z", "t": "x"} and a["rev"] > rev_avant
    assert _data(base[0], "b")["data"] == {"d": "2026-09-04T10:00:00Z",
                                           "j": "2026-09-04"}
    # la non-chaîne et l'illisible ne bougent pas ; le sous-champ, si
    assert _data(base[0], "c")["data"] == {"d": 1788516000, "o": {"s": "2026-09-04"}}
    assert _data(base[0], "d")["data"] == {"d": "bientôt"}
    # la collision est sautée, la ligne suivante du même lot écrite
    assert _data(base[1], "f")["data"] == {"k": "2026-09-04T12:00:00+02:00"}
    assert _data(base[1], "g")["data"] == {"k": "2026-09-05T08:00:00Z"}

    revs = _sql("SELECT row_id, source, geste_id, diff FROM datastore_row_revisions "
                "WHERE ns_id = ANY(%s) AND acteur = %s ORDER BY id",
                base, f"service:{reprise.SERVICE}")
    assert sorted(r["row_id"] for r in revs) == ["a", "b", "c", "g"]
    assert {r["source"] for r in revs} == {"system"}
    assert len({r["geste_id"] for r in revs}) == 1, "UNE reprise, UN geste"
    diff_a = next(r["diff"] for r in revs if r["row_id"] == "a")
    assert diff_a == {"d": {"avant": "2026-09-04T12:00:00+02:00",
                            "apres": "2026-09-04T10:00:00Z"}}

    # idempotent : il ne reste que la collision, toujours sautée
    encore = reprise.executer(appliquer=True, taille=2, dire=_silence)
    assert encore["lignes"] == 0 and encore["collisions"] == [(base[1], "f")]
    assert reprise.main(["--taille-lot", "2"]) == 0


def test_les_parametres_invalides_sont_refuses_sans_rien_lire():
    assert reprise.main(["--depuis", "pas-une-cle"]) == 2
    assert reprise.main(["--taille-lot", "0"]) == 2
