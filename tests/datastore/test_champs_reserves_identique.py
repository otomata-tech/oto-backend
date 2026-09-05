"""L'identique n'est pas une écriture — sur `readonly`, sur `.origine`, et au substrat.

Deux erreurs datées du 29/08/2026, dans la même journée :

1. **v1.165.0, trou éprouvé sur copie jetable** : sur une colonne `readonly`, réécrire la
   valeur EXACTE en place passait (« rien n'a changé ») et `adresse.comment` tombait avec
   — « une valeur nue réécrite emporte ses couches » jouait là où tout tient à la couche.
2. **L'erreur d'une heure (#623)** : la réparation a d'abord REFUSÉ l'identique. Huit
   charges d'écriture échantillonnées sur le terrain, toutes : le geste dominant RÉÉMET
   la fiche entière, valeurs verrouillées comprises (`{"valeur": <identique>, "origine":
   <la même>}` + vingt colonnes d'enrichissement). Chaque fiche aurait été refusée — une
   flotte à l'arrêt, pas un garde-fou.

La vraie réparation est au substrat : une valeur IDENTIQUE à celle en place est un no-op
qui préserve les couches ; le refus ne porte que sur un CHANGEMENT ; une `.origine` égale
à ce que le système poserait est acceptée. Le banc `banc` vit dans `champs_reserves_banc.py`.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2
from oto_mcp.datastore.errors import RowValidationError

from champs_reserves_banc import LIGNE as _LIGNE, banc  # noqa: F401


def test_la_valeur_IDENTIQUE_d_une_readonly_est_un_no_op_qui_garde_le_comment(banc):
    """29/08/2026, l'erreur d'une heure : #623 refusait l'identique sur `readonly`.
    Huit charges d'écriture échantillonnées sur le terrain, toutes : le geste dominant
    RÉÉMET la fiche entière, valeurs verrouillées comprises. « Identique compris »
    aurait arrêté la campagne — une flotte à l'arrêt, pas un garde-fou. L'identique
    n'est pas une écriture : no-op silencieux, couches préservées. Le refus ne porte
    que sur un CHANGEMENT."""
    st, etat = banc
    st.update_row("viviers", "r1", {"adresse": {"comment": "registre — 2 rue B"}})
    st.update_row("viviers", "r1", {"adresse": "1 rue A"})
    st.update_row("viviers", "r1", {"adresse": {"valeur": "1 rue A"}})
    assert etat["lignes"]["r1"]["adresse"] == {"valeur": "1 rue A",
                                              "comment": "registre — 2 rue B"}
    assert st.off_schema_report() == {}                     # silencieux


def test_le_round_trip_ENTIER_sur_une_readonly_PASSE(banc):
    """Relire → repousser la ligne entière porte la valeur nue de la colonne source,
    identique : la fiche passe, rien n'est perdu."""
    st, etat = banc
    st.update_row("viviers", "r1", {"adresse": {"comment": "registre — 2 rue B"}})
    st.update_row("viviers", "r1", dict(_LIGNE, libre="note"))
    assert etat["lignes"]["r1"]["libre"] == "note"
    assert etat["lignes"]["r1"]["adresse"]["comment"] == "registre — 2 rue B"


def test_le_lot_accepte_l_identique_sur_une_readonly_et_garde_le_comment(banc):
    st, etat = banc
    st.update_row("viviers", "r1", {"adresse": {"comment": "registre — 2 rue B"}})
    out = st._write_rows_to_ns(7, [{"siren": "552081317", "adresse": "1 rue A",
                                    "libre": "x"}], key="siren")
    assert out["updated"] == 1
    assert etat["lignes"]["r1"]["adresse"]["comment"] == "registre — 2 rue B"


def test_comment_et_link_accompagnant_une_valeur_identique_sont_ECRITS(banc):
    """Le geste utile : `{"valeur": <identique>, "comment": "…"}` annote sans toucher
    la valeur — le comment est écrit, le link existant reste (la valeur n'a pas
    changé, rien ne tombe)."""
    st, etat = banc
    st.update_row("viviers", "r1", {"adresse": {"link": "https://l"}})
    st.update_row("viviers", "r1", {"adresse": {"valeur": "1 rue A",
                                                "comment": "registre — 2 rue B"}})
    assert etat["lignes"]["r1"]["adresse"] == {"valeur": "1 rue A", "link": "https://l",
                                              "comment": "registre — 2 rue B"}


# ── #586 : une `.origine` égale à ce que le système poserait est un no-op ─────

def test_ecrire_l_origine_EGALE_a_la_valeur_en_place_est_acceptee(banc):
    """Le geste dominant du terrain sur une colonne système :
    `{"valeur": <identique>, "origine": <la même>}` — c'est exactement ce que le
    système poserait. Accepté ; rien de perdu, rien de refusé."""
    st, etat = banc
    st.update_row("viviers", "r1", {"raison_sociale": {"comment": "c"}})
    st.update_row("viviers", "r1", {"raison_sociale": {"valeur": "ACME", "origine": "ACME"}},
                  origine_override=True)
    assert etat["lignes"]["r1"]["raison_sociale"] == {"valeur": "ACME", "origine": "ACME",
                                                     "comment": "c"}
    # Puis l'agent modifie en réémettant l'origine STOCKÉE : accepté, jamais réécrite.
    st.update_row("viviers", "r1", {"raison_sociale": {"valeur": "ACME HOLDING",
                                                       "origine": "ACME"}},
                  origine_override=True)
    assert etat["lignes"]["r1"]["raison_sociale"] == {"valeur": "ACME HOLDING",
                                                     "origine": "ACME"}
    # Une origine DIFFÉRENTE reste refusée, avec le message existant.
    with pytest.raises(RowValidationError, match="raison_sociale.origine"):
        st.update_row("viviers", "r1", {"raison_sociale": {"valeur": "ACME GROUP",
                                                           "origine": "ACME GROUP"}},
                      origine_override=True)
    assert etat["lignes"]["r1"]["raison_sociale"]["valeur"] == "ACME HOLDING"


def test_a_la_creation_une_origine_egale_a_la_valeur_est_acceptee(banc):
    st, etat = banc
    st.append_row("viviers", {"siren": "389256712",
                              "raison_sociale": {"valeur": "X", "origine": "X"}},
                  origine_override=True)
    assert etat["creees"][0]["raison_sociale"] == {"valeur": "X", "origine": "X"}


# ── le substrat : une valeur nue IDENTIQUE est un no-op qui garde les couches ──

def test_une_valeur_nue_identique_PRESERVE_les_couches(banc):
    """Le round-trip relire → repousser (#390) ne doit jamais détruire un `comment`,
    un `link` ou une `origine` : la lecture sert la valeur nue (`flat_layers` met les
    couches à côté, sous `champ.couche`), donc un round-trip fidèle repousse la valeur
    nue — et la règle « réécrire la valeur emporte ses couches » la détruisait. Une
    valeur IDENTIQUE n'est pas une réécriture : rien ne bouge, rien ne tombe. Sur
    toute colonne, readonly ou non — ici `libre`, sur les trois chemins."""
    st, etat = banc
    st.update_row("viviers", "r1", {"libre": {"valeur": "x", "comment": "c",
                                              "link": "https://l", "origine": "o"}},
                  origine_override=True)
    couches = {"valeur": "x", "comment": "c", "link": "https://l", "origine": "o"}
    st.update_row("viviers", "r1", {"libre": "x"})
    assert etat["lignes"]["r1"]["libre"] == couches
    st.append_row("viviers", {"siren": "552081317", "libre": "x"})
    assert etat["lignes"]["r1"]["libre"] == couches
    st._write_rows_to_ns(7, [{"siren": "552081317", "libre": "x"}], key="siren")
    assert etat["lignes"]["r1"]["libre"] == couches
    # Une valeur DIFFÉRENTE, elle, reste une réécriture : comment/link tombent,
    # origine survit — la règle de #322/#326, inchangée.
    st.update_row("viviers", "r1", {"libre": "y"})
    assert etat["lignes"]["r1"]["libre"] == {"valeur": "y", "origine": "o"}


# ── la colonne-clé : de l'adressage, pas une écriture de valeur ──────────────

def test_la_pose_refuse_readonly_sur_la_cle_metier():
    """La clé figure dans CHAQUE écriture pour désigner la ligne : `readonly` dessus,
    identique refusé, fermerait toutes les écritures du tableau. Elle se protège par
    `key_required` (une autre valeur est une autre ligne), pas par `readonly`."""
    errs = dsv2.validate_schema_def(
        {"key": "siren", "fields": [{"key": "siren", "readonly": True}]})
    assert errs and any("clé métier" in e and "key_required" in e for e in errs), errs


def test_sur_un_schema_legacy_la_cle_identique_est_de_l_ADRESSAGE(banc):
    """Un schéma déjà en base qui porterait `readonly` sur la clé (posé avant ce
    garde, ou « complété » dans six mois) ne ferme pas le tableau : la valeur de clé
    IDENTIQUE passe comme toute valeur identique — c'est l'adresse de la ligne —, une
    valeur DIFFÉRENTE reste refusée (sur la ligne visée, c'est une réécriture)."""
    st, etat = banc
    etat["schema"] = {"key": "siren",
                      "fields": [{"key": "siren", "readonly": True},
                                 {"key": "adresse", "readonly": True},
                                 {"key": "libre"}]}
    st.append_row("viviers", {"siren": "552081317", "libre": "x",
                              "adresse": {"comment": "registre — 2 rue B"}})
    st.update_row("viviers", "r1", {"siren": "552081317", "libre": "y"})
    assert etat["lignes"]["r1"]["libre"] == "y" and etat["creees"] == []
    assert etat["lignes"]["r1"]["adresse"]["comment"] == "registre — 2 rue B"
    with pytest.raises(RowValidationError, match="`siren`"):
        st.update_row("viviers", "r1", {"siren": "999999999"})
    with pytest.raises(RowValidationError, match="`adresse`"):
        st.update_row("viviers", "r1", {"siren": "552081317", "adresse": "2 rue B"})


def test_identique_se_juge_au_TYPE_pres():
    """`0` et `False` sont égaux pour Python, pas pour une colonne : écrire `False`
    sur `0` est une réécriture, pas un no-op."""
    from oto_mcp.datastore.columns import _merge_column
    assert _merge_column({"valeur": 0, "comment": "c"}, False) is False   # couches tombées, scalaire nu
    assert _merge_column({"valeur": 0, "comment": "c"}, 0) == {"valeur": 0, "comment": "c"}
    assert _merge_column("x", "x") == "x"
    assert _merge_column(None, None) is None


