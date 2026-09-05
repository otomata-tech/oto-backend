"""Ce qu'on SERT doit pouvoir être réécrit tel quel (#684/#687).

**Le défaut, en une phrase : on servait une forme qu'on refusait en retour.** Une
colonne porte des annotations (`origine`, `comment`, `link`), stockées imbriquées et
servies **à plat** — une lecture rend `site_web` *et* `site_web.comment` comme deux clés
de premier niveau. C'est voulu : sans ça, `row["email"]` cesserait de rendre un e-mail
le jour où quelqu'un pose une source, et tout consommateur casserait en silence.

Le refus des clés pointées (#685) fermait une corruption réelle — une colonne littérale
`champ.comment` est invisible au filtre et au tri du même nom. Mais il ne distinguait pas
la clé fautive **de notre propre lecture réémise**. Trois gestes de première classe s'y
cassaient :

- un agent qui relit une fiche et la **réémet entière** (le geste dominant, #390) ;
- l'export CSV du tableau de bord, bâti sur les clés servies, **réimporté** (#687) ;
- un en-tête de tableur ordinaire — `N.SIREN` — qui n'a rien à voir avec nos annotations.

⚠️ **L'alternative — étiqueter les annotations à la lecture — a été écartée** : plus
propre, mais elle changerait le contrat de lecture de tous les consommateurs actuels.
*On ne change pas la forme servie pendant que quelqu'un écrit dessus.*

**LA preuve de ce fichier, et de tout le lot** : lire une ligne annotée, réémettre
exactement ce qu'on a lu, retrouver la ligne IDENTIQUE — annotation à sa place, aucune
colonne créée.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_points_" + uuid.uuid4().hex[:8]
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


SCHEMA = {"key": "siren",
          "fields": [{"key": "siren", "type": "text"},
                     {"key": "site_web", "type": "url"},
                     {"key": "contacts", "type": "list",
                      "of": {"fields": [{"key": "nom", "type": "text"},
                                        {"key": "email", "type": "email"}]}}]}


@pytest.fixture
def table(live):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-points", ns)
    st = make_store("sub-points")
    st.set_schema(ns, SCHEMA)
    return st, ns, ns_id


def _colonnes(ns_id: int) -> set:
    """Les noms de colonnes RÉELLEMENT en base — le seul juge de « aucune colonne
    créée ». Une assertion sur la forme servie ne le dirait pas : le service aplatit."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s", (ns_id,)).fetchall()
    return {k for l in lignes for k in (l["data"] or {})}


def _sans_horodatage(row: dict) -> dict:
    """⚠️ `_updated_at` est stampé par la PLATEFORME à chaque écriture, même quand le
    résultat mergé est identique : le comparer ferait dépendre la preuve de la seconde
    où le test tourne. Ce que l'aller-retour doit rendre identique, c'est la DONNÉE."""
    return {k: v for k, v in row.items() if k != "_updated_at"}


def _tel_quel(row: dict) -> dict:
    """Ce qu'un agent réémet : la fiche telle qu'il l'a lue, horodatage de plateforme
    mis à part (il ne s'écrit pas, et `_id` DOIT rester — c'est l'adresse de la ligne)."""
    return {k: v for k, v in row.items() if k not in ("_created_at", "_updated_at")}


# ── LA preuve : l'aller-retour se referme ────────────────────────────────────

def test_aller_retour_relire_puis_reemettre_rend_la_ligne_IDENTIQUE(table):
    """⚠️ **LE témoin du lot.** Sans lui, tout le reste n'est que du détail de forme."""
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "552032534",
                       "site_web": {"valeur": "https://a.fr",
                                    "origine": "registre",
                                    "comment": "site vérifié le 12/07"}},
                  origine_override=True)
    lu = st.list_rows(ns)[0]
    assert lu["site_web"] == "https://a.fr", "le nom nu rend la VALEUR"
    assert lu["site_web.comment"] == "site vérifié le 12/07", "les couches, à plat"

    st.append_row(ns, _tel_quel(lu))          # réémission EXACTE de ce qu'on a lu

    relu = st.list_rows(ns)[0]
    assert _sans_horodatage(relu) == _sans_horodatage(lu), (
        "la ligne relue doit être identique à la ligne lue")
    assert _colonnes(ns_id) == {"siren", "site_web"}, "aucune colonne créée"


def test_aller_retour_par_le_LOT_le_chemin_de_l_import(table):
    """Le chemin de l'export/réimport du tableau de bord (#687) : c'est le LOT qui
    porte les imports, donc c'est là que l'aller-retour doit se refermer aussi."""
    st, ns, ns_id = table
    st.write_rows(ns, [{"siren": "1", "site_web": {"valeur": "a.fr",
                                                  "comment": "trouvé au registre"}}])
    lu = st.list_rows(ns)[0]
    st.write_rows(ns, [{k: v for k, v in _tel_quel(lu).items() if k != "_id"}])
    assert _sans_horodatage(st.list_rows(ns)[0]) == _sans_horodatage(lu)
    assert not any("." in c for c in _colonnes(ns_id))


def test_aller_retour_dans_une_colonne_TABLEAU(table):
    """`_served_item` aplatit les couches DANS l'item (`item["email.origine"]`) : la
    règle du premier niveau, un cran plus bas. Elle doit donc se réécrire pareil —
    sinon la fiche relue fabrique un attribut littéral à chaque passage."""
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "1", "contacts": [
        {"nom": "Jo", "email": {"valeur": "jo@a.fr", "origine": "hunter"}}]})
    lu = st.list_rows(ns)[0]
    assert lu["contacts"] == [{"nom": "Jo", "email": "jo@a.fr",
                               "email.origine": "hunter"}]

    st.append_row(ns, _tel_quel(lu))

    assert _sans_horodatage(st.list_rows(ns)[0]) == _sans_horodatage(lu)
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        brut = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchone()["data"]
    assert brut["contacts"] == [{"nom": "Jo", "email": {"valeur": "jo@a.fr",
                                                       "origine": "hunter"}}], (
        "l'attribut est rangé dans sa couche, pas stocké en littéral pointé")


def test_les_QUATRE_PORTES_referment_l_aller_retour(table):
    """La règle est la cohérence, pas le cas. #685 s'est produit parce que trois
    portes refusaient et une acceptait ; le remède ne vaut que s'il est aux quatre."""
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "1", "site_web": {"valeur": "a.fr", "comment": "c"}})
    lu = st.list_rows(ns)[0]
    nu = {k: v for k, v in _tel_quel(lu).items() if k != "_id"}

    st.append_row(ns, _tel_quel(lu))                              # append (promu update)
    st.update_row(ns, lu["_id"], {"site_web": lu["site_web"],
                                  "site_web.comment": lu["site_web.comment"]})
    st.upsert_row(ns, lu["_id"], nu)
    st.write_rows(ns, [nu])

    assert st.list_rows(ns)[0]["site_web.comment"] == "c"
    assert not any("." in c for c in _colonnes(ns_id))


# ── « Colonne réelle » : du geste, de la LIGNE, ou du SCHÉMA ─────────────────

def test_annotation_seule_sur_une_colonne_DE_LA_LIGNE(table):
    """L'annotation posée seule, sur une colonne que seule la LIGNE porte. Le tableau
    ne la déclare pas et l'écriture ne la nomme pas — c'est la ligne qui l'atteste."""
    st, ns, ns_id = table
    row = st.append_row(ns, {"siren": "1", "libre": "en place"})   # hors schéma, souple
    st.update_row(ns, row["_id"], {"libre.comment": "posé après coup"})
    lu = st.list_rows(ns)[0]
    assert lu["libre"] == "en place", "la valeur n'a pas bougé"
    assert lu["libre.comment"] == "posé après coup"
    assert "libre.comment" not in _colonnes(ns_id)


def test_annotation_seule_sur_une_colonne_DU_SCHEMA(table):
    """Le rattrapage de socle (#326) : annoter une colonne DÉCLARÉE que la ligne n'a
    pas encore renseignée. Rien dans le geste ni dans la ligne ne la porte."""
    st, ns, ns_id = table
    row = st.append_row(ns, {"siren": "1"})
    st.update_row(ns, row["_id"], {"site_web.origine": "registre"},
                  origine_override=True)
    assert st.list_rows(ns)[0]["site_web.origine"] == "registre"
    assert "site_web.origine" not in _colonnes(ns_id)


# ── Cas 3 : une clé d'appel fautive est REFUSÉE, en nommant la forme ─────────

def test_une_annotation_INCONNUE_est_refusee(table):
    """`champ.inexistant` : le suffixe n'est pas une annotation. Le refus nomme celles
    qui existent — un message qui dit seulement « non » fait deviner."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1", "site_web": "a.fr",
                           "site_web.inexistant": "x"})
    msg = str(e.value)
    assert "n'est pas un nom de colonne" in msg
    assert "`comment`" in msg and "`origine`" in msg and "`link`" in msg
    assert not any("." in c for c in _colonnes(ns_id))


def test_une_annotation_sur_une_colonne_INCONNUE_est_refusee(table):
    """`inconnu.comment` : l'annotation est connue, la colonne n'existe nulle part —
    ni dans le geste, ni sur la ligne, ni au schéma. C'est une adresse fautive, et le
    refus dit la forme qui marcherait."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1", "inconnu.comment": "x"})
    msg = str(e.value)
    assert "`inconnu`" in msg and "n'est aucune colonne" in msg
    assert '{"inconnu": {"comment"' in msg, "le refus nomme la forme attendue"
    assert not any("." in c for c in _colonnes(ns_id))


def test_le_refus_vaut_AUX_QUATRE_PORTES(table):
    """Le rétrécissement du refus (#685 → ici) ne doit pas rouvrir le trou de #685 :
    ce qui reste sans adresse est refusé partout, pas seulement là où on regarde."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    row = st.append_row(ns, {"siren": "1"})
    gestes = (
        lambda: st.append_row(ns, {"siren": "2", "inconnu.comment": "x"}),
        lambda: st.update_row(ns, row["_id"], {"inconnu.comment": "x"}),
        lambda: st.upsert_row(ns, row["_id"], {"siren": "1", "inconnu.comment": "x"}),
        lambda: st.write_rows(ns, [{"siren": "3", "inconnu.comment": "x"}]),
    )
    for geste in gestes:
        with pytest.raises(RowValidationError):
            geste()
    assert not any("." in c for c in _colonnes(ns_id))


# ── Cas 4 : une collision se refuse en NOMMANT LES DEUX ──────────────────────

def test_deux_adresses_de_la_MEME_annotation_se_refusent(table):
    """⚠️ On ne fusionne jamais deux écritures d'un même champ en silence. Le geste
    nomme `comment` deux fois, par deux formes, avec deux valeurs : l'une écraserait
    l'autre selon l'ordre des clés — donc on refuse, en nommant les deux."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1",
                           "site_web": {"valeur": "a.fr", "comment": "imbriqué"},
                           "site_web.comment": "pointé"})
    msg = str(e.value)
    assert "site_web.comment" in msg and "`site_web`" in msg, "les DEUX sont nommés"
    assert _colonnes(ns_id) == set(), "rien n'a été écrit"


# ── Le témoin négatif : on ferme une forme, pas la primitive ─────────────────

def test_une_ecriture_EN_COUCHES_passe_toujours(table):
    """Sans ce cas, « ranger les points » pourrait se durcir en « refuser les couches »
    et rendre le datastore inutilisable là où il sert le plus."""
    st, ns, ns_id = table
    st.write_rows(ns, [{"siren": "1",
                        "site_web": {"valeur": "a.fr", "comment": "site"}}])
    assert _colonnes(ns_id) == {"siren", "site_web"}


def test_le_CONTENU_d_une_colonne_json_n_est_pas_reinterprete(table):
    """Une colonne déclarée `json` est un objet métier assumé : on ne range rien DEDANS
    — même exemption que `_refuse_mixed_layers` (#329).

    ⚠️ **Amendé le 2026-09-01 (#728), et le nom du test avec.** L'exemption portait
    aussi sur l'ADRESSE, et ce test y consentait : il exigeait un refus sur
    `{"brut": …, "brut.comment": …}` **sans jamais lire le texte servi**. Ce refus
    disait « `brut` n'est aucune colonne de ce tableau : ni dans cette écriture, ni sur
    la ligne visée, ni au schéma » — d'une colonne déclarée deux lignes plus haut, et
    nommée dans le geste. Un `pytest.raises` nu ne pouvait pas le voir : c'est
    exactement ce qui a laissé le mensonge en place. L'annotation d'une colonne objet
    se range désormais ; preuves dans `test_annotation_colonne_objet_libre_728.py`."""
    st, ns, ns_id = table
    st.set_schema(ns, {**SCHEMA,
                       "fields": SCHEMA["fields"] + [{"key": "brut", "type": "json"}]})
    st.append_row(ns, {"siren": "1", "brut": {"comment": "un champ métier", "a": 1}})
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        brut = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                            (ns_id,)).fetchone()["data"]
    assert brut["brut"] == {"comment": "un champ métier", "a": 1}, "intacte"

    st.append_row(ns, {"siren": "2", "brut": {"a": 1}, "brut.comment": "x"})
    lu = next(r for r in st.list_rows(ns) if r["siren"] == "2")
    assert lu["brut"] == {"a": 1}, "le nom nu rend l'objet, jamais l'enveloppe"
    assert lu["brut.comment"] == "x", "l'annotation est rangée, pas refusée"


# ── Bout en bout : l'export du tableau de bord, réimporté ────────────────────

def test_un_CSV_d_export_se_reimporte_et_referme_l_aller_retour(table):
    """⚠️ Le chemin qui a produit #687 / oto-dashboard#137. L'export bâtit ses colonnes
    sur les CLÉS SERVIES, donc il exporte `site_web.comment` ; le réimport en faisait
    une colonne littérale. Ici il retrouve son annotation, et rien de plus."""
    from oto_mcp import upload_tokens as ut
    st, ns, ns_id = table
    st.append_row(ns, {"siren": "1", "site_web": {"valeur": "a.fr",
                                                  "comment": "vérifié"}})
    lu = st.list_rows(ns)[0]
    entetes = [k for k in lu if not k.startswith("_")]
    csv = (",".join(entetes) + "\n"
           + ",".join(str(lu[k]) for k in entetes) + "\n").encode("utf-8")
    assert b"site_web.comment" in csv, "l'export porte bien la colonne pointée"

    res = ut.materialize("sub-points",
                         {"kind": "datastore", "ns_id": ns_id, "namespace": ns,
                          "format": "csv", "key": "siren"}, csv, None)

    assert "entetes_traduits" not in res, "rien à traduire : c'était une annotation"
    assert _sans_horodatage(st.list_rows(ns)[0]) == _sans_horodatage(lu), (
        "la ligne est identique après le tour complet")
    assert _colonnes(ns_id) == {"siren", "site_web"}


def test_un_CSV_a_en_tetes_ordinaires_est_traduit_ET_ANNONCE(table):
    """DITE, jamais silencieuse : sans l'annonce, le client reçoit une colonne qu'il ne
    peut plus retrouver par le nom qu'il lui a donné — le défaut qu'on ferme côté
    écriture, rouvert côté import."""
    from oto_mcp import upload_tokens as ut
    st, ns, ns_id = table
    res = ut.materialize("sub-points",
                         {"kind": "datastore", "ns_id": ns_id, "namespace": ns,
                          "format": "csv", "key": "siren"},
                         b"siren,N.SIREN\n1,552032534\n", None)
    assert res["entetes_traduits"] == {"N.SIREN": "N_SIREN"}
    assert "`N.SIREN` → `N_SIREN`" in res["entetes_traduits_hint"]
    assert "N_SIREN" in _colonnes(ns_id) and "N.SIREN" not in _colonnes(ns_id)


def test_aller_retour_sur_un_champ_a_ORIGINE_SYSTEME(table):
    """⚠️ L'interaction qui casserait le lot en production sans qu'un test du lot le
    dise. Un champ `origine: system` (#586) refuse qu'on lui écrive son origine — et la
    lecture sert justement `champ.origine`. Le rangement DOIT donc passer avant le refus
    des champs réservés : sinon celui-ci jugerait une adresse (`champ.origine`, qu'il ne
    reconnaît pas) au lieu d'une couche, et laisserait passer ce qu'il doit voir.

    Réémise à l'identique, l'origine système est un no-op (#623/#625) ; CHANGÉE, elle
    est refusée. Les deux dans le même test, parce que c'est la paire qui prouve que le
    refus regarde bien la bonne chose."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    st.set_schema(ns, {**SCHEMA, "fields": SCHEMA["fields"]
                       + [{"key": "score", "type": "number", "origine": "system"}]})
    row = st.append_row(ns, {"siren": "1", "score": 12})
    st.update_row(ns, row["_id"], {"score": 13})   # c'est la MODIF qui capture
    lu = st.list_rows(ns)[0]
    assert lu["score.origine"] == 12, "la plateforme a posé l'origine, elle est SERVIE"

    st.append_row(ns, _tel_quel(lu))                       # réémission à l'identique
    assert _sans_horodatage(st.list_rows(ns)[0]) == _sans_horodatage(lu)

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, lu["_id"], {"score.origine": "moi"})
    assert "score" in str(e.value), "changer l'origine système reste refusé"


# ── Ce que « paresseusement » veut dire, mesuré ──────────────────────────────

def test_la_ligne_n_est_LUE_QUE_si_un_nom_pointe_reste_irresolu():
    """⚠️ La lecture de la ligne est le seul coût que ce lot pourrait ajouter, et il
    tomberait sur le chemin le plus volumineux du serveur — le lot de huit mille lignes.
    On le mesure au lieu de l'affirmer : le compteur reste à zéro tant qu'aucune adresse
    n'a besoin de la ligne pour être tranchée."""
    from oto_mcp.datastore.points import ranger_les_couches
    schema = {"fields": [{"key": "site_web", "type": "url"}]}
    lectures = []

    def _lire():
        lectures.append(1)
        return {"libre"}

    ranger_les_couches(schema, {"siren": "1", "raison": "ACME"},
                       colonnes_en_place=_lire)
    assert lectures == [], "aucun nom pointé : la ligne n'est pas lue"

    ranger_les_couches(schema, {"site_web": "a.fr", "site_web.comment": "c"},
                       colonnes_en_place=_lire)
    assert lectures == [], "la colonne est dans le geste : la ligne n'est pas lue"

    ranger_les_couches(schema, {"site_web.comment": "c"}, colonnes_en_place=_lire)
    assert lectures == [], "la colonne est au schéma : la ligne n'est pas lue"

    out = ranger_les_couches(schema, {"libre.comment": "c"}, colonnes_en_place=_lire)
    assert lectures == [1], "là, et seulement là, il fallait consulter la ligne"
    assert out == {"libre": {"comment": "c"}}


def test_annoter_un_OBJET_METIER_est_refuse_pour_LA_BONNE_RAISON(table):
    """⚠️ Un refus qui nomme la mauvaise cause coûte une demi-journée à qui le lit.

    Ici la colonne EXISTE — le refus du cas 3 (« `brut` n'est aucune colonne ») serait
    faux. Ce qui bloque est ailleurs : le geste écrit un objet métier, et poser une
    annotation à côté l'envelopperait dans `valeur`. Le refus le dit, et donne la forme
    qui marche vraiment."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id = table
    with pytest.raises(RowValidationError) as e:
        st.append_row(ns, {"siren": "1", "brut": {"a": 1, "b": 2},
                           "brut.comment": "x"})
    msg = str(e.value)
    assert "n'est aucune colonne" not in msg, "la colonne existe : ne pas mentir"
    assert "n'est pas fait de couches" in msg
    assert '"brut": {"valeur"' in msg, "la forme qui marche est nommée"

    st.append_row(ns, {"siren": "1", "brut": {"valeur": {"a": 1}, "comment": "x"}})
    assert st.list_rows(ns)[0]["brut.comment"] == "x"
