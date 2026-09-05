"""Annoter une colonne DÉCLARÉE `json`, et un refus qui ne ment plus (#728).

Le cas remonté le 2026-09-01 : un agent écrit, **dans le même appel**,
`entreprise_social` (l'objet) et `entreprise_social.comment` (son annotation). Refus :

    « `entreprise_social` n'est aucune colonne de ce tableau : ni dans cette écriture,
      ni sur la ligne visée, ni au schéma. »

**Elle y était.** Et `effectif.comment` — même forme, colonne scalaire — passait dans la
MÊME écriture : c'est cette asymétrie qui rend le message impossible à croire.

La cause : le rangement (`ranger_les_couches`) exempte les colonnes déclarées `json`,
et l'exemption portait aussi sur l'ADRESSE. L'annotation d'une colonne-objet n'était
donc jamais rangée, restait pointée, et tombait dans le refus du cas 3 — qui annonce
« n'est aucune colonne » alors qu'elle en est une, et seulement exemptée. Un refus qui
nomme la mauvaise cause coûte une demi-journée à qui le lit ; celui-ci nommait une cause
FAUSSE, ce qui est pire : il envoie créer une colonne qui existe déjà.

**Décision (2026-09-01, #728) : l'exemption `json` protège le CONTENU de l'objet, pas le
droit d'annoter la colonne.** Le LECTEUR ne l'a jamais exemptée — `flat_layers` sert
`col.comment` pour toute colonne, `json` comprise, et le même fichier prouvait déjà
qu'une colonne `json` écrite en couches se relit à plat. Ce qu'on servait, on le
refusait en retour : l'aller-retour était ouvert précisément là. Restent exempts le
contenu de l'objet (`_ranger_les_items`) et la garde des couches mixtes (#329).

Second front, même mensonge : `_refuse_dotted_names` AFFIRMAIT avoir regardé trois
endroits alors qu'il n'en tient aucun — il ne reçoit que le payload. Un nom PROJETÉ
(`contact1_email`, servi pendant une migration) prenait la même phrase, dans `add_row`
où `_refuse_flat_writes` ne passe pas. Le refus ne dit plus que ce qu'il a vérifié.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_json_ann_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + name
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = prev_pool
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


# Le tableau du cas remonté : une colonne scalaire et une colonne-objet, annotées par
# le même geste. C'est la PAIRE qui porte la preuve — une seule des deux passait.
SCHEMA = {"key": "siren",
          "fields": [{"key": "siren", "type": "text"},
                     {"key": "effectif", "type": "number"},
                     {"key": "entreprise_social", "type": "json"}]}


@pytest.fixture
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-json-ann", ns)
    st = make_store("sub-json-ann")
    st.set_schema(ns, SCHEMA)
    return st, ns, ns_id


def _colonnes(ns_id: int) -> set:
    """Les noms de colonnes RÉELLEMENT en base — le seul juge de « aucune colonne
    littérale ». La forme servie ne le dirait pas : le service aplatit."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()
    return {k for l in lignes for k in (l["data"] or {})}


def _brut(ns_id: int) -> dict:
    """La ligne STOCKÉE. Ce qui distingue « rangé » de « enveloppé » n'est visible que
    là : les deux formes servent le même payload."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchone()["data"]


def _tel_quel(row: dict) -> dict:
    """Ce qu'un agent réémet : la fiche telle qu'il l'a lue (`_id` COMPRIS — c'est
    l'adresse de la ligne), les horodatages de plateforme mis à part."""
    return {k: v for k, v in row.items() if k not in ("_created_at", "_updated_at")}


def _sans_horodatage(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_updated_at"}


# ── Le cas remonté : la colonne EST dans l'écriture ──────────────────────────

def test_annoter_une_colonne_OBJET_passe_comme_une_colonne_SCALAIRE(table):
    """⚠️ **LE témoin de #728.** Les deux annotations sont écrites par le même geste,
    sur deux colonnes de types différents. Une seule passait, et le refus de l'autre
    accusait le tableau de ne pas porter une colonne qu'il déclare."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    geste = {"siren": "552032534",
             "effectif": 12, "effectif.comment": "bilan 2025",
             "entreprise_social": {"forme": "SAS", "capital": 10000},
             "entreprise_social.comment": "extrait du registre"}
    try:
        st.append_row(ns, geste)
    except RowValidationError as e:
        pytest.fail(f"le geste dominant est refusé, et le refus MENT : {e}")

    lu = st.list_rows(ns)[0]
    assert lu["entreprise_social"] == {"forme": "SAS", "capital": 10000}, (
        "le nom nu rend l'objet métier, intact")
    assert lu["entreprise_social.comment"] == "extrait du registre"
    assert lu["effectif.comment"] == "bilan 2025", "l'asymétrie est refermée"
    assert not any("." in c for c in _colonnes(ns_id)), "aucune colonne littérale"


def test_l_aller_retour_se_referme_sur_une_colonne_OBJET(table):
    """La règle du module, appliquée là où elle manquait : ce qu'on SERT doit pouvoir
    être réécrit tel quel. Une colonne `json` écrite en couches se relit à plat — donc
    sa réémission doit revenir à sa place, sans enveloppe de plus à chaque passage."""
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "1",
                       "entreprise_social": {"valeur": {"forme": "SAS"},
                                             "comment": "registre"}})
    lu = st.list_rows(ns)[0]
    assert lu["entreprise_social"] == {"forme": "SAS"}
    assert lu["entreprise_social.comment"] == "registre"

    st.append_row(ns, _tel_quel(lu))              # réémission EXACTE de la lecture

    assert _sans_horodatage(st.list_rows(ns)[0]) == _sans_horodatage(lu)
    assert _brut(ns_id)["entreprise_social"] == {"valeur": {"forme": "SAS"},
                                                 "comment": "registre"}
    assert not any("." in c for c in _colonnes(ns_id))


def test_annoter_SEULE_une_colonne_objet_que_la_ligne_porte_deja(table):
    """Le rattrapage (#326) sur une colonne-objet : l'annotation posée seule se dépose
    sur la valeur en place, sans y toucher."""
    st, ns, ns_id = table
    row = st.append_row(ns, {"siren": "1", "entreprise_social": {"forme": "SAS"}})
    st.update_row(ns, row["_id"], {"entreprise_social.origine": "registre"},
                  origine_override=True)
    lu = st.list_rows(ns)[0]
    assert lu["entreprise_social"] == {"forme": "SAS"}, "la valeur n'a pas bougé"
    assert lu["entreprise_social.origine"] == "registre"
    assert not any("." in c for c in _colonnes(ns_id))


# ── L'ambiguïté irréductible : un champ de l'objet nommé comme une couche ────

def test_un_champ_de_l_objet_qui_PORTE_le_nom_d_une_couche(table):
    """`flat_layers` ne regarde que le NOM : un objet métier qui a un champ `comment`
    voit ce champ servi à plat, comme une annotation. La réémission le renvoie alors
    deux fois, et le payload seul ne dit pas laquelle des deux formes stockées il vient
    de lire. Même valeur ⟹ c'est notre propre lecture : on ne touche à rien, surtout
    pas à la forme stockée. Valeur différente ⟹ deux écritures du même champ servi, et
    on ne fusionne jamais en silence."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "1",
                       "entreprise_social": {"comment": "champ métier", "n": 1}})
    lu = st.list_rows(ns)[0]
    assert lu["entreprise_social.comment"] == "champ métier", "servi à plat"

    st.append_row(ns, _tel_quel(lu))              # réémission EXACTE de la lecture

    assert _brut(ns_id)["entreprise_social"] == {"comment": "champ métier", "n": 1}, (
        "rien n'a changé : l'objet n'est pas enveloppé au passage")

    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1",
                           "entreprise_social": {"comment": "champ métier", "n": 1},
                           "entreprise_social.comment": "une autre valeur"})
    msg = str(e.value)
    assert "n'est aucune colonne" not in msg, "la colonne existe : ne pas mentir"
    assert "entreprise_social.comment" in msg and "`entreprise_social`" in msg, (
        "les DEUX formes sont nommées")


# ── Le refus ne dit que ce qu'il a VÉRIFIÉ ───────────────────────────────────

def test_le_refus_ne_dit_JAMAIS_ni_dans_cette_ecriture_quand_elle_y_EST(table):
    """Second front du même mensonge, par une autre porte : un nom PROJETÉ
    (`contact1_email`, servi en lecture pendant une migration, jamais stocké) reste
    pointé lui aussi. Dans `add_row`, `_refuse_flat_writes` ne passe pas — et le refus
    des noms pointés récitait sa phrase à trois sources sur un nom qui est, lui,
    littéralement dans l'écriture."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, _ = table
    st.set_schema(ns, {**SCHEMA, "fields": SCHEMA["fields"] + [
        {"key": "contacts", "type": "list", "flat_alias": "contact{n}_{attr}",
         "of": {"fields": [{"key": "email", "type": "email"}]}}]})
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1", "contact1_email": "jo@a.fr",
                           "contact1_email.comment": "vérifié"})
    msg = str(e.value)
    assert "ni dans cette écriture" not in msg, (
        "`contact1_email` EST dans l'écriture — le refus ne peut pas prétendre "
        "l'avoir cherchée sans la trouver")
    assert "`contacts`" in msg, "le refus dit d'où le nom est calculé"


def test_une_colonne_ABSENTE_PARTOUT_garde_le_refus_a_TROIS_sources(table):
    """Le témoin négatif : la phrase à trois sources reste, et elle est désormais vraie
    par construction — on ne l'atteint plus que lorsque les trois ont été consultées."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1", "inconnu.comment": "x"})
    assert ("ni dans cette écriture, ni sur la ligne visée, ni au schéma"
            in str(e.value))
    assert not any("." in c for c in _colonnes(ns_id))
