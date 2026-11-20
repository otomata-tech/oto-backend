"""Une colonne hors schéma est REFUSÉE sur TOUS les chemins d'écriture (#647, oto#124).

**Le fait, campagne du 31/08/2026.** Une colonne non déclarée est née pendant un
passage sur un tableau `strict` — absente de l'instantané de départ, présente en base
à l'arrivée. **Cent trois travaux ont relevé `hors_schema` : tous à zéro.** Trois
lectures indépendantes disent zéro, la table dit un.

⚠️ **Ce que ça coûtait, et pourquoi ce banc existe.** La valeur écrite est une donnée
que le contrat de la campagne interdit. Rangée hors schéma, elle est **invisible à tous
les contrôles qui lisent le schéma**. Le relevé a laissé place au refus (oto#124) : une
colonne non déclarée ne naît plus. Une garde qui mord par intermittence est pire
qu'une garde absente.

Ce banc pose donc la question sur la MATRICE, pas sur le cas vu : chaque chemin
d'écriture × colonne neuve / colonne déjà là × forme de la valeur. Il tourne sur une
vraie base, par les mêmes appels que la campagne, et il interroge le STOCKAGE pour
établir que la colonne n'existe pas — jamais le retour de l'écriture.
"""
from __future__ import annotations

import uuid

import pytest

from oto_mcp.datastore.errors import ColonneNonDeclaree


# Le tableau de la campagne, réduit à ce qui porte la règle : une clé métier, et une
# colonne-liste (les contacts, où le défaut du 29/08 s'était logé).
SCHEMA = {
    "key": "siren",
    "fields": [
        {"key": "siren", "type": "text"},
        {"key": "raison_sociale", "type": "text"},
        {"key": "contacts", "type": "list",
         "of": {"type": "object", "fields": [{"key": "nom"}, {"key": "fonction"}]}},
    ],
}

HORS = "entreprise_instagram"   # le nom réel de la colonne née le 31/08


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


@pytest.fixture
def table(live):
    from oto_mcp import db
    ns_id = db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = _store()
    st.set_schema(ns, SCHEMA)
    row = st.append_row(ns, {"siren": "552032534", "raison_sociale": "TEMOIN"})
    return st, ns, ns_id, row["_id"]


def _colonnes_en_base(ns_id: int, row_id: str) -> set:
    """Ce que porte LA BASE, jamais ce que l'écriture a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id)).fetchone()
    return set((r or {}).get("data") or {})


def _colonnes_du_tableau(ns_id: int) -> set:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        rs = conn.execute("SELECT data FROM datastore_rows WHERE ns_id = %s",
                          (ns_id,)).fetchall()
    return {k for r in rs for k in (r["data"] or {})}


def _ligne_salie(ns_id: int) -> str:
    """Une ligne qui porte DÉJÀ la colonne hors schéma — écrite avant le refus."""
    from oto_mcp import db
    db.datastore_insert_row(ns_id, "r-salie", {"siren": "301234567", HORS: "ancien"})
    return "r-salie"


# ── La matrice : chaque chemin, colonne NEUVE ────────────────────────────────

def test_patch_par_id_refuse_la_colonne(table):
    """⚠️ LE chemin de l'incident : `data_write(id=…)` — le geste le plus courant
    d'un agent, et celui que l'alias de réservation emprunte."""
    st, ns, ns_id, rid = table
    with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
        st.update_row(ns, rid, {HORS: "https://example.invalid/x"})
    assert HORS not in _colonnes_en_base(ns_id, rid), "la colonne n'est PAS née en base"


def test_append_refuse_la_colonne(table):
    st, ns, ns_id, _ = table
    with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
        st.append_row(ns, {"siren": "301234567", HORS: "x"})
    assert HORS not in _colonnes_du_tableau(ns_id)
    assert st.count_rows(ns) == 1, "aucune ligne créée"


def test_le_lot_refuse_la_colonne(table):
    """Le lot fusionne sur la clé métier : c'est le chemin `write_rows`."""
    st, ns, ns_id, _ = table
    with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
        st.write_rows(ns, [{"siren": "552032534", HORS: "x"}], key="siren")
    assert HORS not in _colonnes_du_tableau(ns_id)


# ── La matrice : colonne DÉJÀ hors schéma ────────────────────────────────────

def test_la_colonne_DEJA_hors_schema_se_refuse_a_chaque_ecriture(table):
    """⚠️ Une colonne déjà en base n'en devient pas déclarée : chaque écriture qui la
    POSE est refusée, la première comme les suivantes."""
    st, ns, ns_id, _ = table
    rid = _ligne_salie(ns_id)
    for valeur in ("premier", "second"):
        with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
            _store().update_row(ns, rid, {HORS: valeur})


def test_une_ecriture_qui_ne_TOUCHE_pas_la_colonne_passe(table):
    """La borne du refus, et elle est voulue : il juge ce que LE GESTE pose, pas ce
    que la ligne porte (#284). Sinon une ligne déjà salie deviendrait inécrivable."""
    st, ns, ns_id, _ = table
    rid = _ligne_salie(ns_id)
    st.update_row(ns, rid, {"raison_sociale": "AUTRE"})
    assert {"raison_sociale", HORS} <= _colonnes_en_base(ns_id, rid)


# ── Un attribut hors schéma DANS un composite déclaré ────────────────────────

def test_une_cle_hors_schema_DANS_un_contact_est_REFUSEE(table):
    """Le défaut du cinquième passage (`contacts[].email_pattern`) ne peut plus se
    produire : un composite déclaré ferme ses attributs. *Contrairement au premier
    niveau, un attribut inconnu ne crée AUCUNE colonne libre* — il serait stocké là
    où rien ne le lit."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, rid = table
    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {"contacts": [{"nom": "Jo", "email_pro": "jo@x.fr"}]})
    assert "email_pro" in str(e.value) and "Nothing was written" in str(e.value)
    assert "email_pro" not in str(_colonnes_en_base(ns_id, rid))


# ── Le témoin négatif : pas de faux positif ──────────────────────────────────

def test_une_ecriture_entierement_declaree_passe(table):
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": "ACME",
                            "contacts": [{"nom": "Jo", "fonction": "Gérante"}]})
    assert {"raison_sociale", "contacts"} <= _colonnes_en_base(ns_id, rid)


# ── Les FORMES de l'écriture, et c'est là que le silence se loge ─────────────
# ⚠️ Ce n'est pas seulement le chemin qui varie, c'est la FORME de ce qu'on écrit.
# Une colonne du datastore s'écrit de trois façons — valeur nue, objet à couches, clé
# plate pointée — et une garde qui n'en connaît qu'une se tait sur les deux autres
# sans que rien ne le dise.

def test_colonne_hors_schema_ecrite_en_OBJET_A_COUCHES(table):
    """La forme que la campagne emploie partout : `{valeur, comment}`."""
    st, ns, ns_id, rid = table
    with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
        st.update_row(ns, rid, {HORS: {"valeur": "https://x.invalid/a",
                                       "comment": "site — pied de page"}})
    assert HORS not in _colonnes_en_base(ns_id, rid), "la colonne n'est PAS née"


def test_une_cle_PLATE_POINTEE_ne_fabrique_JAMAIS_de_colonne(table):
    """La clé littérale pointée, des deux côtés du schéma — et **le lot du 01/09
    (#684/#687) a changé le sort de chacun des deux, sans changer la garantie.**

    `raison_sociale` est DÉCLARÉE : `raison_sociale.comment` est donc l'adresse de son
    annotation, et elle est désormais rangée — c'est ce qui permet à une fiche relue
    d'être repoussée telle quelle. `entreprise_instagram` n'est déclarée nulle part et
    n'est pas sur la ligne : son adresse ne désigne rien, elle reste refusée.

    Ce qui ne bouge pas, et c'est le seul invariant qui compte ici : **aucune colonne
    dont le nom porte un point ne naît en base**, ni par l'un ni par l'autre chemin —
    elle serait invisible au filtre et au tri du même nom."""
    from oto_mcp.datastore.core import RowValidationError
    st, ns, ns_id, rid = table

    with pytest.raises(RowValidationError) as e:
        st.update_row(ns, rid, {f"{HORS}.comment": "site — pied de page"})
    assert "is no column" in str(e.value)

    st.update_row(ns, rid, {"raison_sociale.comment": "site — pied de page"})

    assert not any("." in k for k in _colonnes_en_base(ns_id, rid))


# ── La quatrième FORME, et c'est celle du terrain ────────────────────────────
# ⚠️ Écrit le 31/08 après avoir enfin LU la colonne née en production : ses huit
# occurrences portent toutes `null`. *Un banc qui couvre toute la matrice sauf la case
# où l'incident a eu lieu certifie le silence qu'il devait expliquer.*

def test_colonne_hors_schema_ecrite_a_NULL(table):
    """La forme observée en production : la clé est posée, sa valeur est vide. Un
    `null` EFFACE, il ne crée rien : il n'est pas refusé, et la colonne ne naît pas —
    *ce qui compte est que la colonne N'EXISTE PAS, pas ce que dit la réponse.*"""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {HORS: None})
    assert HORS not in _colonnes_en_base(ns_id, rid), "la colonne ne naît pas à null"


def test_colonne_hors_schema_a_null_DANS_un_objet_a_couches(table):
    """L'autre vide plausible : la couche est là, la valeur est vide — la couche est
    une valeur posée, la colonne est refusée."""
    st, ns, ns_id, rid = table
    with pytest.raises(ColonneNonDeclaree, match=f"`{HORS}`"):
        st.update_row(ns, rid, {HORS: {"valeur": None, "comment": "site — rien trouvé"}})
    assert HORS not in _colonnes_en_base(ns_id, rid)


# ── Ce que la PRODUCTION porte, et que le banc affirmait impossible ──────────
# ⚠️ Le 31/08, la lecture REST d'un lot de production rend 321 clés pointées
# (`site_web.comment`, `qualification.comment`…) alors que le cas ci-dessus prouve
# qu'ÉCRIRE une clé pointée est refusé. Les deux sont vrais, et la raison n'est ni
# l'une ni l'autre de celles qu'on suppose : **la base stocke la couche IMBRIQUÉE,
# la face REST la SERT à plat.** Écriture, stockage et lecture ont trois formes.
#
# *Un relevé bâti sur ce que la lecture rend compte des colonnes qui n'existent pas.*
# C'est ce qui a produit un faux positif de comptage le 31/08 : un contrôle de
# campagne lisait la face REST et prenait 304 couches servies à plat pour autant de
# colonnes inventées. La garde du serveur juge ce que le geste POSE, et le geste pose
# un objet.

def test_la_couche_est_STOCKEE_IMBRIQUEE_sous_sa_colonne(table):
    """Le geste servi écrit un objet ; la base porte UNE colonne, pas deux."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": {"valeur": "ACME",
                                               "comment": "site — pied de page"}})

    en_base = _colonnes_en_base(ns_id, rid)
    assert "raison_sociale" in en_base
    assert not [k for k in en_base if "." in k], (
        f"aucune clé pointée en base — la couche vit SOUS sa colonne : {sorted(en_base)}")


def test_une_couche_n_est_JAMAIS_une_colonne_non_declaree(table):
    """Le corollaire côté garde : les quatre couches d'une colonne déclarée sont le
    format normal, jamais une colonne inventée — l'écriture passe."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": {"valeur": "A", "comment": "c",
                                               "origine": "o", "link": "https://x.fr"}},
                  origine_override=True)
    assert not [k for k in _colonnes_en_base(ns_id, rid) if "." in k]


def test_la_LECTURE_aplatit_ce_que_la_base_garde_imbrique():
    """L'autre moitié du fait, et c'est elle qui piège les instruments.

    La même colonne a deux formes selon le côté où on se place. Sans ce témoin, la
    mise en garde n'est qu'une affirmation dans une docstring —
    et c'est exactement le genre d'affirmation qui devient fausse en silence."""
    from oto_mcp.datastore import schema as dsv2
    stocke = {"valeur": "https://x.fr", "comment": "site — pied de page"}

    assert dsv2.served_value(stocke, origine=False) == "https://x.fr", "le nom nu rend la VALEUR"
    assert dsv2.flat_layers("site_web", stocke, origine=False) == {
        "site_web.comment": "site — pied de page"}, "la couche est servie À CÔTÉ"


def test_le_RELEVE_et_la_LECTURE_ne_comptent_pas_les_memes_cles(table):
    """⚠️ Le contre-témoin du piège du 31/08, sur une seule ligne : la row servie
    porte une clé de plus que la row écrite, et cette clé n'est PAS une colonne."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": {"valeur": "ACME", "comment": "c"}})

    ecrit = _colonnes_en_base(ns_id, rid)
    servi = set(st.get_row(ns, rid) or {})
    assert "raison_sociale.comment" in servi, "servie : la couche est une clé"
    assert "raison_sociale.comment" not in ecrit, "écrite : elle n'en est pas une"


# ── Retirer une COUCHE : refusé, et le refus nomme sa destination ────────────

def test_drop_column_sur_une_COUCHE_refuse_en_nommant_sa_colonne(table):
    """⚠️ Mesuré le 31/08, alors qu'une purge de ~190 noms hors schéma s'apprêtait à
    partir sur un fichier de production. **La bonne nouvelle d'abord** : viser une
    couche ne détruit AUCUNE provenance — `drop_column` travaille sur les clés
    stockées, et une couche n'en est pas une, elle vit sous sa colonne.

    Le défaut était ailleurs, et c'était le motif de la journée : *la réponse ne
    nommait pas ce qu'elle constatait.* Elle rendait `rows: 0`, exactement comme pour
    une colonne réelle que personne ne portait. Sur un lot de 190 retraits,
    l'opérateur lisait « c'était déjà vide » là où la phrase juste est « ce nom n'est
    pas une colonne, c'est l'annotation de `raison_sociale` ».

    Corrigé le 01/09 (#680) : la version précédente de ce banc FIGEAIT le zéro et
    disait qu'il faudrait la mettre à jour sciemment le jour du correctif — c'est ce
    jour. Ce qu'il mesure maintenant : le refus, l'intégrité de la couche, et **que
    la destination nommée par le refus fonctionne**. Nommer un geste sans l'éprouver
    fabriquerait une case où ranger la chose, pas une porte de sortie."""
    st, ns, ns_id, rid = table
    st.update_row(ns, rid, {"raison_sociale": {"valeur": "ACME", "comment": "c"}})
    assert "raison_sociale.comment" in set(st.get_row(ns, rid) or {})

    with pytest.raises(ValueError) as e:
        st.drop_column(ns, "raison_sociale.comment", confirm=True)
    assert "`raison_sociale`" in str(e.value), "le refus NOMME la colonne porteuse"

    servi = st.get_row(ns, rid) or {}
    assert servi.get("raison_sociale.comment") == "c", "la provenance est INTACTE"
    assert servi.get("raison_sociale") == "ACME", "et la valeur aussi"

    # La destination que le refus indique, éprouvée ICI : écrire la couche nulle en
    # forme imbriquée la retire pour de bon, et ne touche pas la valeur.
    st.update_row(ns, rid, {"raison_sociale": {"comment": None}})
    servi = st.get_row(ns, rid) or {}
    assert "raison_sociale.comment" not in servi, "la couche est retirée"
    assert servi.get("raison_sociale") == "ACME", "sans emporter la valeur"


def test_drop_column_sur_un_nom_INCONNU_refuse_sans_inventer_de_colonne(table):
    """L'autre moitié du même zéro : une faute de frappe. Le refus doit dire « aucune
    colonne de ce nom » — et surtout PAS « c'est la couche de `zzz` », qui nommerait
    une colonne que personne n'a jamais écrite."""
    st, ns, ns_id, rid = table

    with pytest.raises(ValueError) as e:
        st.drop_column(ns, "zzz.comment", confirm=True)
    msg = str(e.value)
    assert "aucune colonne" in msg
    assert "annotation" not in msg, "aucune destination inventée"
