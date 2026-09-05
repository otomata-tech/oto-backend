"""La couche écrite avec un souligné : la montrer, puis la nommer (oto#63).

**Le geste qui fabrique les colonnes fantômes**, mesuré sur une mission réelle : l'agent
lit sa ligne réservée à plat, y voit `effectif.comment`, veut écrire un commentaire — et
produit `effectif_comment`. Un point n'a pas l'air d'un nom de champ, et rien ne lui dit
qu'il est adressable. Cinq colonnes fantômes, **toutes des `comment`**, jamais des
origines ni des liens.

⚠️ **Ce n'est pas une consigne à améliorer.** La consigne servie disait déjà exactement
la bonne chose — le nom d'une couche s'écrit avec un point, jamais un souligné, avec le
contre-exemple nommé — dans la version même qui a produit les cinq cents fiches. Onze
lignes portent quand même la forme fautive. Ce qui gagne, c'est **ce que la plateforme
montre à chaque tour de boucle** contre une phrase lue une fois au début.

⚠️ **Et le durcissement a changé le prix sans changer le défaut** : sur un tableau qui
refuse les colonnes inconnues, cette faute ne crée plus une colonne invisible — elle
fait perdre la fiche entière. Plus on durcit, plus elle coûte.

Deux volets, dans cet ordre, et l'ordre compte : d'abord **montrer** la forme à écrire
(la réservation sert `layers`), ensuite **nommer** la colonne quand la faute est faite.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest

from oto_mcp.datastore import layers as dsl
from oto_mcp.datastore.schema import couche_mal_ecrite, off_schema_refusal

SCHEMA = {"key": "ref", "strict": True, "unknown_fields": "reject",
          "fields": [{"key": "ref", "type": "text"},
                     {"key": "effectif", "type": "text"}]}
DECLAREES = {"ref", "effectif"}


# ── volet 2 : la reconnaissance, exacte et sans devinette ────────────────────

@pytest.mark.parametrize("cle,attendu", [
    ("effectif_comment", ("effectif", "comment")),
    ("effectif_origine", ("effectif", "origine")),
    ("effectif_link", ("effectif", "link")),
])
def test_une_couche_soulignee_sur_colonne_DECLAREE_est_reconnue(cle, attendu):
    assert couche_mal_ecrite(cle, DECLAREES) == attendu


@pytest.mark.parametrize("cle", [
    "effectif_bidule",        # `bidule` n'est pas une couche connue
    "inconnue_comment",       # `inconnue` n'est pas déclarée
    "_comment",               # aucun nom de colonne devant
    "effectif",               # pas de suffixe du tout
    "comment",                # la couche seule
])
def test_rien_d_AUTRE_n_est_reconnu(cle):
    """⚠️ La doctrine du refus reste entière : on ne pointe JAMAIS la colonne la plus
    proche — une destination inventée envoie la valeur dans une colonne juste, pire
    qu'un refus sec. Ici on ne rapproche rien : on lit une décomposition exacte, ou on
    se tait."""
    assert couche_mal_ecrite(cle, DECLAREES) is None


# ── les trois refus, et le geste de réparation de chacun ─────────────────────

def test_couche_mal_ecrite_le_refus_NOMME_la_colonne():
    msgs, details = off_schema_refusal(SCHEMA, {"effectif_comment": "x"})
    assert len(msgs) == 1
    assert "`effectif.comment`" in msgs[0]
    assert "COUCHE" in msgs[0]
    assert details.get("expected_column") == "effectif.comment", details


def test_colonne_inconnue_sous_une_couche_BIEN_ecrite_dit_l_autre_geste():
    """⚠️ La distinction existe, et elle ne vient PAS d'ici — c'est ce que la
    comparaison avant/après a établi.

    `_refuse_dotted_names` (datastore/points.py) lève sur les CINQ chemins d'écriture,
    avant que le refus des colonnes inconnues ne soit consulté, et son message est
    meilleur que celui qu'on aurait écrit : il dit où il a cherché — « ni dans cette
    écriture, ni sur la ligne visée, ni au schéma » — et donne la forme imbriquée.
    Écrire notre version aurait ajouté du code que rien n'atteint, en laissant croire
    à un second chemin.

    Ce banc garde donc la distinction là où elle VIT, pas là où on aurait pu la
    dupliquer."""
    from oto_mcp.datastore.points import _refuse_dotted_names
    from oto_mcp.datastore.errors import RowValidationError

    with pytest.raises(RowValidationError) as e:
        _refuse_dotted_names({"inconnue.comment": "x"})
    message = str(e.value)
    assert "n'est aucune colonne" in message
    assert "ni au schéma" in message, "le refus doit dire OÙ il a cherché"
    assert '{"inconnue": {"comment"' in message, "…et donner la forme imbriquée"


def test_une_colonne_franchement_inconnue_garde_son_refus_SEC():
    """Aucune des deux reconnaissances ne s'applique : le message d'origine, qui ne
    suggère aucune destination — parce qu'il n'y en a pas."""
    msgs, details = off_schema_refusal(SCHEMA, {"totalement_inconnu": "x"})
    assert "aucune colonne déclarée ne porte ce nom" in msgs[0]
    assert details == {}


def test_la_forme_JUSTE_n_est_pas_refusee():
    """L'autre moitié : `effectif.comment` est la forme correcte, elle passe."""
    assert off_schema_refusal(SCHEMA, {"effectif.comment": "x"}) == ([], {})


# ── volet 1 : la réservation montre la forme qu'on écrit ─────────────────────

def test_les_deux_reservations_portent_layers():
    """⚠️ La réservation est le SEUL chemin qu'un agent emprunte pour lire ce qu'il va
    réécrire, et c'était le seul à ne pas porter l'option. Les deux lectures l'avaient
    déjà — la boucle d'écriture, non."""
    from oto_mcp.datastore.core import DatastorePg

    for nom in ("claim_next", "claim_row"):
        params = inspect.signature(getattr(DatastorePg, nom)).parameters
        assert "layers" in params, f"{nom} ne porte pas `layers`"
        assert params["layers"].default == dsl.DEFAUT


def test_la_face_agent_porte_layers_et_le_VALIDE():
    """Le tool sert `layers` et le passe au validateur commun — une valeur inconnue
    doit être refusée en nommant le paramètre, pas ignorée."""
    from oto_mcp.tools import datastore as tools_ds

    src = inspect.getsource(tools_ds)
    assert 'layers: str = "flat"' in src
    assert "layers=dsl.check(layers)" in src, (
        "la face agent doit valider `layers`, pas le passer brut")


@pytest.fixture(scope="module")
def live(pg_dsn):
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


def test_la_reservation_rend_la_forme_qu_on_ECRIT(live):
    """La mesure qui tient tout le volet 1 : à plat, l'agent voit une clé pointée
    (`effectif.comment`) qu'il transformera en souligné ; en `nested`, il voit
    exactement la forme dans laquelle il doit réécrire."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = "t-" + uuid.uuid4().hex[:6]
    db.create_datastore_namespace("user", "sub-c63", ns)
    st = make_store("sub-c63")
    st.set_schema(ns, {"key": "ref", "fields": [{"key": "ref", "type": "text"},
                                                {"key": "effectif", "type": "text"}]})
    st.write_rows(ns, [{"ref": "r1",
                        "effectif": {"valeur": "12", "comment": "estimé"}}])

    plat = st.claim_next(ns, worker="w1")
    assert plat["effectif"] == "12" and plat["effectif.comment"] == "estimé"

    st.release_claim(ns, plat["_id"], worker="w1")
    nested = st.claim_next(ns, worker="w1", layers=dsl.NESTED)
    assert nested["effectif"] == {"valeur": "12", "comment": "estimé"}
