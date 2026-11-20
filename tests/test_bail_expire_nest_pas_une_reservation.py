"""Un bail expiré n'est pas une réservation — la lecture doit le savoir comme la garde.

**Le fait, mesuré le 01/09/2026 sur un fichier de production.** 495 lignes sur 8 910
portaient `_claimed_by` ; **les 495 étaient expirées**, la plus ancienne depuis dix-huit
jours, au nom de travailleurs d'une campagne close.

⚠️ **Et la plateforme le savait déjà.** `datastore_active_lease` filtre sur
`claimed_until > NOW()`, et sa docstring dit « expiré compte pour libre » — sinon le
zombie de dix-huit jours aurait bloqué sa ligne pendant dix-huit jours. *La lecture,
elle, servait le nom sans regarder la date.*

> **Deux lectures voisines de la même donnée, et une seule connaissait la règle.**
> **Un champ servi affirmait ce que le système lui-même tenait pour faux.**

Ce que ça coûtait, et ce n'est pas théorique : un relevé qui compte les lignes réservées
en trouvait 495 sans qu'aucun travail ne tourne, et l'export destiné au client montrait
le nom d'un worker à côté de chaque ligne. *Le compteur de reprises porte déjà la trace
des tentatives : rien ne se perd à taire un bail que plus personne ne détient.*
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-test")
    st.set_schema(ns, {"key": "siren", "fields": [{"key": "siren", "type": "text"}]})
    row = st.append_row(ns, {"siren": "552032534"})
    return st, ns, ns_id, row["_id"]


def _poser_bail(ns_id: int, row_id: str, dans: str, qui: str = "mistral-2",
                run: str | None = None):
    """Écrit un bail directement, pour fabriquer un échu que plus rien ne pose.

    ⚠️ `dans` est un INTERVALLE relatif à l'horloge du SERVEUR (`'-18 days'`,
    `'2 hours'`) — jamais une date en dur, même lointaine. Un test qui fige un
    instant futur devient faux le jour où le futur arrive, et il ne prévient pas :
    il passe jusqu'à la veille. Le relevé statique de ces dates sur-déclare trop
    pour servir (77 candidats, 0 vraie) — seule une horloge décalée tranche. Écrit
    ainsi, décaler l'horloge de la suite ne change aucune couleur ici."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            "UPDATE datastore_rows SET claimed_by = %s, claimed_run = %s, "
            "       claimed_until = NOW() + %s::interval "
            " WHERE ns_id = %s AND row_id = %s", (qui, run, dans, ns_id, row_id))


# Les deux seuls états qui nous occupent, dits en RELATIF. « Dix-huit jours » est la
# mesure de production (la plus vieille des 495 lignes échues), pas un choix rond.
ECHU, EN_COURS = "-18 days", "2 hours"


def test_un_bail_EXPIRE_nest_pas_servi(table):
    """⚠️ LE cas : 495 lignes de production le portaient, toutes mortes."""
    st, ns, ns_id, rid = table
    _poser_bail(ns_id, rid, ECHU)

    servi = st.get_row(ns, rid) or {}
    assert "_claimed_by" not in servi, (
        f"un bail expiré ne se sert pas comme une réservation : {servi.get('_claimed_by')!r}")
    assert "_claimed_until" not in servi and "_claimed_run" not in servi


def test_un_bail_ACTIF_est_servi(table):
    """Le témoin négatif : on ne casse pas ce qui sert à la file."""
    st, ns, ns_id, rid = table
    _poser_bail(ns_id, rid, EN_COURS)

    servi = st.get_row(ns, rid) or {}
    assert servi.get("_claimed_by") == "mistral-2"
    assert servi.get("_claimed_until") is not None


def test_la_LECTURE_et_la_GARDE_disent_la_meme_chose(table):
    """⚠️ La règle, et c'est elle qui manquait : deux lectures voisines de la même
    donnée doivent la lire pareil. La garde savait ; la lecture non.

    Depuis ce lot elles ne se ressemblent plus, elles partagent le PRÉDICAT : c'est
    PostgreSQL qui tranche `claimed_until > NOW()`, aux deux endroits, sur la même
    horloge. Deux implémentations d'une même règle finissent toujours par diverger ;
    une seule ne le peut pas."""
    from oto_mcp.db.rowlock import datastore_active_lease
    st, ns, ns_id, rid = table

    for dans, actif in ((ECHU, False), (EN_COURS, True)):
        _poser_bail(ns_id, rid, dans)
        garde = datastore_active_lease(ns_id, rid) is not None
        lecture = "_claimed_by" in (st.get_row(ns, rid) or {})
        assert garde is actif, f"la garde se trompe sur un bail {dans}"
        assert lecture is garde, (
            f"la lecture dit {lecture} là où la garde dit {garde} — bail {dans}")


def test_une_ligne_JAMAIS_reservee_ne_porte_rien(table):
    st, ns, ns_id, rid = table
    servi = st.get_row(ns, rid) or {}
    assert "_claimed_by" not in servi and "_claimed_until" not in servi


def test_la_SUPERVISION_voit_le_bail_echu_que_la_LECTURE_tait(table):
    """⚠️ LE témoin qui manquait — sans lui, le correctif produit l'INVERSE de son but.

    La même ligne, au même instant, par deux chemins qui n'ont pas le même contrat :

    - **la lecture ordinaire** (`get_row`, `list_rows`, l'export client) doit la
      TAIRE : servir le nom d'un travailleur mort est le défaut qu'on ferme ;
    - **la file de supervision** (`queue`, `GET /api/datastores/…/queue`)
      doit la MONTRER telle quelle. Son contrat — « bail actif OU expiré, le
      consommateur tranche sur `_claimed_until` » — est écrit à trois endroits
      (`db.rowlock.datastore_claimed_rows`, `DatastorePg.queue`, la capacité
      `me.datastore.queue`) et il est ANTÉRIEUR à ce lot : c'est lui qui fait foi.

    Neutraliser le bail dans le sérialiseur PARTAGÉ le casse en silence : la requête
    de la file continue de rendre les lignes échues, mais dépouillées. L'écran les
    compte alors « sous bail » pendant que son compteur d'échus tombe à zéro, et le
    bouton « Libérer le bail » — gaté sur `_claimed_by` — disparaît précisément sur
    les lignes qu'il faut libérer. Un correctif qui rend l'inverse de son intention
    est pire que pas de correctif : il ferme le sujet."""
    st, ns, ns_id, rid = table
    _poser_bail(ns_id, rid, ECHU, run="run-de-la-campagne-close")

    lu = st.get_row(ns, rid) or {}
    assert "_claimed_by" not in lu and "_claimed_until" not in lu \
        and "_claimed_run" not in lu, "la lecture ordinaire doit taire un bail mort"

    file = [r for r in st.queue(ns) if r["_id"] == rid]
    assert file, ("la file de supervision ne rend plus la ligne échue — c'est "
                  "exactement ce qu'elle existe pour montrer")
    vue = file[0]
    assert vue["_claimed_by"] == "mistral-2", \
        "sans le titulaire, l'écran ne peut pas dire QUI tenait la ligne"
    assert vue["_claimed_until"] is not None, \
        "sans la date, le consommateur ne PEUT PAS trancher — c'est son contrat"
    assert vue["_claimed_run"] == "run-de-la-campagne-close", \
        "sans le run, la vue dit qu'un travail tenait une ligne, jamais lequel"


def test_la_file_montre_aussi_un_bail_EN_COURS(table):
    """L'autre moitié du contrat de la file : elle ne trie pas, elle rend les deux
    et laisse trancher. Sans ce témoin, « la file montre tout » pourrait être servi
    par une file qui ne montrerait QUE les échus."""
    st, ns, ns_id, rid = table
    _poser_bail(ns_id, rid, EN_COURS)
    vue = [r for r in st.queue(ns) if r["_id"] == rid]
    assert vue and vue[0]["_claimed_by"] == "mistral-2"
