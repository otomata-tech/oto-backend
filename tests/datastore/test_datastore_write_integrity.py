"""Ce qu'une écriture EFFACE, et ce qu'un lot refusé dit de la ligne fautive.

Six signaux d'usage le 13/08/2026, tous sur `data_write`, org 226. Le banc les
sépare en trois familles, parce qu'ils ne disent pas tous la vérité :

**① « Une écriture partielle a mis à null un champ qu'elle ne nommait pas »**
(#407, #408, #409, trois signaux en 75 secondes, champ `moteur` du tableau
`edition-essais`). **C'est une erreur d'attribution, et le journal des appels le
prouve** : à 08:33 GMT la même session a écrit, ligne par ligne,
`data_write(id=…, row={'moteur': None, 'siren': …})` — le champ était NOMMÉ, avec
`null`. L'écriture d'enrichissement incriminée est arrivée huit minutes plus tard,
à 08:41, et n'y était pour rien (appels `tool_calls` 224531 puis 224704, même
ligne `019ffa3a-7696…`). Les deux premiers tests d'ici gravent donc le
comportement RÉEL — un champ omis survit, sur les deux chemins d'écriture —
puisque c'est lui qu'on a accusé.

**② Le défaut qui reste, et qui a produit la perte : `null` EFFACE en silence.**
Nommer un champ avec une valeur vide est un geste destructeur légitime (vider une
valeur fausse n'a pas d'autre porte), mais il est indiscernable, côté payload,
d'un `None` de sérialisation — une variable non peuplée, un gabarit à demi rempli,
un aller-retour de lecture. Le serveur répondait un succès ordinaire. Il nomme
désormais ce qu'il a vidé, et avec quelle valeur : c'est ce qui permet de
rétablir. Même patron que `hors_schema` (#294) et `hors_options` (#319) — on
n'empêche rien, on rend la chose visible.

**③ Le lot refusé ne nommait pas sa ligne fautive** (#412) : 8 910 lignes
importées par lots de 200, une adresse sans arobase dans le fichier client, et un
refus qui nomme le champ et la valeur mais jamais LAQUELLE des deux cents lignes.
⚠️ En l'écrivant, une hypothèse du signal tombe : **le lot n'est pas atomique**.
Il s'arrête à la ligne fautive et laisse écrites celles d'avant. Le refus le dit
maintenant, parce que c'est ce qui décide de la reprise.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Un PostgreSQL RÉEL, jetable. Un banc qui reconstitue le magasin mesure la
    représentation qu'on s'en fait — et c'est exactement ce qui a laissé passer
    ces défauts : le retour de l'appel était crédible, c'est la base qu'il fallait
    interroger."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_wint_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    prev_url, prev_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
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


# Le tableau des signaux, réduit à ce qui porte les règles : la clé métier `siren`
# et le format strict d'`edition-vivier`, plus les deux champs de l'incident.
#
# ⚠️ `moteur` est déclaré avec la clé `enum:` et SANS `options:` — la forme exacte
# que le signal #409 accusait d'être « mal reconnue par le chemin d'écriture ».
# Elle ne l'est pas : `enum` n'est pas lue (#316 le signale à la pose), l'énumération
# est donc LIBRE, et un champ libre se préserve comme les autres.
SCHEMA = {
    "key": "siren",
    "strict": True,
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text"},
        {"key": "entreprise_email", "type": "email"},
        {"key": "moteur", "type": "enum", "enum": ["mistral", "sonnet"]},
        {"key": "origine_ligne", "type": "text"},
    ],
}


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    """Un tableau `key` + `strict`, et UNE ligne témoin portant `moteur`."""
    from oto_mcp import db
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-test", ns)
    st = _store()
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "377768379", "raison_sociale": "TEMOIN",
                             "moteur": "sonnet", "origine_ligne": "fichier-client"})
    return st, ns, ns_id, row["_id"]


def _donnees(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _cardinal(ns_id: int) -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM datastore_rows WHERE ns_id = %s",
            (ns_id,)).fetchone()["n"]


# ══ ① ce qu'on a accusé : un champ OMIS ═════════════════════════════════════

def test_le_champ_omis_survit_a_lecriture_par_id(table):
    """Le geste incriminé par #408 et #409 : un patch par `id` qui ne nomme pas
    `moteur`. Il ne l'a jamais touché — le journal montre que le `null` était venu
    d'un appel antérieur de la même session."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"raison_sociale": "APRES ENRICHISSEMENT"})

    apres = _donnees(ns_id, rid)
    assert apres.get("moteur") == "sonnet", \
        "une écriture ne touche QUE ce qu'elle nomme (#322/#326)"
    assert apres.get("origine_ligne") == "fichier-client"
    assert apres.get("raison_sociale") == "APRES ENRICHISSEMENT"


def test_le_champ_omis_survit_au_lot_par_cle_metier(table):
    """Le second geste incriminé (#407) : l'upsert par clé métier. Le guide de
    fusion vaut sur CE chemin aussi — elle avait déjà manqué une fois (#322), d'où
    la vérification des deux."""
    st, ns, ns_id, rid = table

    recap = st.write_rows(ns, [{"siren": "377768379",
                                "raison_sociale": "PAR LE LOT"}], key="siren")

    assert (recap["updated"], recap["inserted"]) == (1, 0), "fusion, pas doublon"
    apres = _donnees(ns_id, rid)
    assert apres.get("moteur") == "sonnet"
    assert apres.get("origine_ligne") == "fichier-client"


# ══ ② le défaut réel : `null` efface, et le dit ═════════════════════════════

def test_le_null_nomme_efface_et_le_dit(table):
    """Le geste qui a RÉELLEMENT vidé `moteur` en production, le 13/08 à 08:33.

    L'effacement reste permis — c'est la seule façon de vider une valeur fausse —
    mais il ne peut plus être muet : la réponse nomme le champ, la ligne et la
    valeur PERDUE, seule information qui permette de rétablir."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"moteur": None, "siren": "377768379"})
    releve = st.off_schema_report()

    assert _donnees(ns_id, rid).get("moteur") is None, \
        "le geste est exécuté : on avertit, on ne refuse pas"
    efface = releve.get("valeurs_effacees")
    assert efface, "un effacement muet est une perte de données silencieuse"
    assert [(e["champ"], e["valeur"]) for e in efface] == [("moteur", "sonnet")], \
        "le champ ET la valeur perdue — sans elle, rien à rétablir"
    assert efface[0]["ligne"] == rid, "sur un lot, la ligne est ce qui manque"
    assert "null" in (releve.get("valeurs_effacees_hint") or "").lower()


def test_le_null_du_lot_efface_et_le_dit_aussi(table):
    """Le même relevé sur le chemin de fusion par clé métier — les deux chemins
    d'écriture ont déjà divergé une fois sur cette famille de règles (#322).

    ⚠️ Ce test portait `origine_ligne: ""` et concluait « vider avec une chaîne vide
    est un effacement comme un autre ». Il était VERT, et il gravait le défaut de
    #608 : son nom parlait de `null`, son corps prescrivait la chaîne vide. Un test
    qui décrit le geste qu'on croit équivalent, plutôt que celui qu'on veut, protège
    la divergence au lieu de la révéler."""
    st, ns, ns_id, rid = table

    st.write_rows(ns, [{"siren": "377768379", "origine_ligne": None}], key="siren")
    releve = st.off_schema_report()

    assert _donnees(ns_id, rid).get("origine_ligne") is None
    assert [(e["champ"], e["valeur"]) for e in releve.get("valeurs_effacees") or []] \
        == [("origine_ligne", "fichier-client")], \
        "`null` reste le geste qui vide — sinon la valeur serait prise en otage"


def test_ecrire_une_valeur_ne_signale_aucun_effacement(table):
    """Le bruit est le premier ennemi d'un avertissement : remplacer une valeur
    par une AUTRE valeur n'est pas un effacement, et ne doit rien déclencher."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"moteur": "mistral"})

    assert "valeurs_effacees" not in st.off_schema_report()


def test_le_null_sur_un_champ_deja_vide_ne_signale_rien(table):
    """L'autre source de bruit : un gabarit qui porte `null` sur des champs jamais
    renseignés. Rien n'est perdu, rien n'est dit."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"entreprise_email": None})

    assert "valeurs_effacees" not in st.off_schema_report()


# ══ ③ le lot refusé nomme sa ligne ══════════════════════════════════════════

def test_le_lot_nomme_la_ligne_quil_refuse(table):
    """#412 : le refus nommait le champ et la valeur, jamais LAQUELLE des deux
    cents lignes. Sur un fichier client de 8 910 lignes qu'on n'a pas produit,
    c'est le coût le plus lourd — pas les lignes perdues, le temps de trouver."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, _rid = table

    with pytest.raises(RowValidationError) as exc:
        st.write_rows(ns, [
            {"siren": "111111111", "raison_sociale": "SAINE"},
            {"siren": "552081317", "entreprise_email": "editions-galilee.com"},
            {"siren": "333333333", "raison_sociale": "JAMAIS ATTEINTE"},
        ], key="siren")

    msg = str(exc.value)
    assert "ligne 2/3" in msg, "l'index dans le lot — la ligne se retrouve"
    assert "552081317" in msg, "et sa clé métier, qui la nomme dans le fichier"
    assert "entreprise_email" in msg and "editions-galilee.com" in msg, \
        "sans rien perdre de ce que le refus disait déjà"


def test_le_lot_refuse_dit_ce_quil_a_deja_ecrit(table):
    """⚠️ L'hypothèse que le signal tenait pour acquise — « un lot d'écriture est
    atomique » — est FAUSSE : les lignes qui précèdent la fautive sont écrites, et
    le restent. C'est ce que le refus doit dire, parce que c'est ce qui décide de
    la reprise : reprendre le lot entier redouble les premières."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, _rid = table

    with pytest.raises(RowValidationError) as exc:
        st.write_rows(ns, [
            {"siren": "111111111", "raison_sociale": "SAINE"},
            {"siren": "552081317", "entreprise_email": "editions-galilee.com"},
        ], key="siren")

    assert _cardinal(ns_id) == 2, "le témoin + la première du lot : rien n'est annulé"
    assert "1 ligne" in str(exc.value), \
        "le refus dit combien de lignes il laisse derrière lui"


# ══ ④ la chaîne vide d'un gabarit de lot : une ABSENCE, pas un effacement ════
#
# #608, remonté par un client le 28/08/2026 (org 270, tableau `koncile-accounts`).
# Un lot de sourcing portait `best_signal: ""` dans son GABARIT de ligne — la forme
# normale d'un lot : un gabarit écrit une fois, réutilisé sur toutes les lignes. Sur
# la ligne appariée par la clé métier, la chaîne vide a effacé un signal de
# recrutement daté, réel, déjà en base. Il a été rétabli parce que `valeurs_effacees`
# (#407/#408/#409) l'a nommé — mais annoncer une perte n'est pas l'éviter.
#
# ⚠️ Le défaut n'est PAS celui d'hier. Hier on annonçait un effacement par `null`
# NOMMÉ, geste délibéré. Ici la question est en amont : **une chaîne vide est-elle
# une valeur ?** Le serveur répondait NON à la validation (`_is_empty` la traite en
# absence : elle ne subit aucun contrôle de type, et elle déclenche « champ requis
# manquant ») et OUI à la fusion (elle écrase). Deux réponses contradictoires sur la
# même donnée, dans le même appel.

def test_la_chaine_vide_du_lot_nefface_pas(table):
    """LE défaut de #608 : la chaîne vide d'un gabarit détruisait la valeur en place.

    Une source qui ne rend rien pour un champ ne dit pas « oublie ce que tu savais » :
    elle ne dit rien. Le geste qui vide reste disponible, il est explicite (`null`)."""
    st, ns, ns_id, rid = table

    st.write_rows(ns, [{"siren": "377768379", "origine_ligne": ""}], key="siren")

    assert _donnees(ns_id, rid).get("origine_ligne") == "fichier-client", \
        "un lot d'enrichissement dont une source est muette ne détruit rien"


def test_la_chaine_vide_ignoree_le_dit(table):
    """Le silence dans l'AUTRE sens serait le même défaut retourné : un appelant qui
    voulait vraiment vider doit apprendre que son geste n'a rien fait, et par quoi le
    remplacer. On n'empêche pas sans le dire — même patron que `valeurs_effacees`."""
    st, ns, ns_id, rid = table

    st.write_rows(ns, [{"siren": "377768379", "origine_ligne": ""}], key="siren")
    releve = st.off_schema_report()

    ignores = releve.get("valeurs_ignorees")
    assert ignores, "ignorer sans le dire, c'est le défaut de #608 dans l'autre sens"
    assert [(e["champ"], e["valeur"]) for e in ignores] \
        == [("origine_ligne", "fichier-client")], \
        "le champ, et la valeur qui a SURVÉCU — de quoi juger si c'est ce qu'on voulait"
    assert "null" in (releve.get("valeurs_ignorees_hint") or "").lower(), \
        "le relevé nomme le geste qui vide VRAIMENT"


def test_la_chaine_vide_sur_un_champ_deja_vide_ne_dit_rien(table):
    """Le bruit est le premier ennemi d'un avertissement : un gabarit qui porte `""`
    sur trente champs jamais renseignés ne doit pas produire trente lignes de relevé.
    Rien n'a été préservé, il n'y a rien à arbitrer."""
    st, ns, ns_id, rid = table

    st.write_rows(ns, [{"siren": "377768379", "entreprise_email": ""}], key="siren")

    assert "valeurs_ignorees" not in st.off_schema_report()


def test_la_chaine_vide_SEULE_par_id_est_REFUSEE_en_nommant_la_porte(table):
    """L'autre chemin d'écriture. Les deux ont déjà divergé une fois sur cette
    famille de règles (#322), et c'est le patch par `id` qui est le geste le plus
    courant d'un agent : une règle câblée d'un seul côté ne protège personne.

    ⚠️ Le vide est ici TOUT le geste : l'appel ne changerait rien et répondrait comme
    un succès. Depuis #724 il est REFUSÉ, et le refus ÉCRIT la porte en toutes lettres
    — la nommer dans un relevé n'a pas suffi (dix retraits perdus le 01/09 malgré un
    relevé qui disait déjà `null`). Ce que le test verrouille n'a pas changé : la
    valeur en place survit."""
    st, ns, ns_id, rid = table

    with pytest.raises(ValueError) as exc:
        st.update_row(ns, rid, {"origine_ligne": ""})

    assert "origine_ligne" in str(exc.value), exc.value
    assert '"origine_ligne": null' in str(exc.value), \
        f"le refus doit écrire le geste qui marche, pas seulement le nommer : {exc.value}"
    assert _donnees(ns_id, rid).get("origine_ligne") == "fichier-client", \
        "un refus n'écrit rien — surtout pas l'effacement qu'il refuse"


def test_la_chaine_vide_ACCOMPAGNEE_est_preservee_et_relevee_par_id_aussi(table):
    """LE test qui porte #608 sur ce chemin, et il vaut 104 appels par mois.

    Dès que l'écriture pose autre chose, c'est un gabarit à demi peuplé — le geste
    dominant des flottes, 98 % des écritures à liste vide mesurées sur 30 jours au
    2026-09-01 — et la valeur en place survit. Élargir l'effacement du vide SEUL
    (#724) à cette forme détruirait 104 valeurs clientes par mois."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"origine_ligne": "", "raison_sociale": "ACME"})

    assert _donnees(ns_id, rid).get("origine_ligne") == "fichier-client"
    assert _donnees(ns_id, rid).get("raison_sociale") == "ACME"
    assert st.off_schema_report().get("valeurs_ignorees"), "et il le dit ici aussi"


def test_la_valeur_vide_ecartee_nemporte_pas_lorigine_quelle_accompagne(table):
    """« Une écriture ne touche que ce qu'elle nomme » vaut aussi quand on ÉCARTE.

    Un rattrapage de socle pose `{"valeur": …, "origine": …}` sur toute une colonne ;
    si la source est muette, la valeur vide est écartée — mais l'origine, elle, a été
    nommée délibérément et doit s'écrire. Sauter la colonne entière rejouerait #326
    par la porte de derrière."""
    st, ns, ns_id, rid = table

    st.update_row(ns, rid, {"origine_ligne": {"valeur": "", "origine": "apollo"}}, origine_override=True)

    apres = _donnees(ns_id, rid)
    assert apres.get("origine_ligne") == {"valeur": "fichier-client",
                                          "origine": "apollo"}, \
        "la valeur survit, l'origine s'écrit — chacune selon ce qui a été nommé"
