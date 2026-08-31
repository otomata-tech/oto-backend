"""Un en-tête de tableur qui porte un point devient un nom de colonne (#684).

**Le cas qui l'impose.** Le refus des clés pointées, posé sur le chemin du lot, ferme
une corruption réelle — mais il ferme aussi la porte à un fichier client parfaitement
ordinaire : `N.SIREN`, `Tel.mobile`, `contact.email` sont des en-têtes de tableur
courants. Un fichier de production en portait deux, et il devenait irrechargeable.

⚠️ **La distinction qui tient tout, et sans laquelle quelqu'un étendra ceci à l'appel
programmatique dans six mois :**

| | |
|---|---|
| **en-tête CSV** | une **étiquette** — chaîne humaine écrite par un tiers, à TRADUIRE |
| **clé d'appel** | une **adresse** — `{"champ.comment": …}` DÉSIGNE une couche |

*L'import traduit déjà les types, les vides, l'encodage : traduire un point est de la
même famille.* Une adresse fautive, elle, doit lever. **Le refus protège le magasin, la
traduction protège l'ingestion — ce ne sont pas les mêmes portes.**

Sans cette traduction, notre convention d'adressage interne fuirait jusque dans le
fichier du client, à qui on demanderait de renommer ses colonnes pour se conformer à
notre modèle de stockage.
"""
from __future__ import annotations

import pytest

from oto_mcp.upload_tokens import UploadError, _parse_rows, traduire_entetes


def _csv(texte: str) -> bytes:
    return texte.encode("utf-8")


# ── La traduction, et elle est DITE ──────────────────────────────────────────

def test_un_entete_pointe_devient_une_colonne():
    lignes, traduits = _parse_rows(
        _csv("N.SIREN,Tel.mobile\n552032534,0102030405\n"), "csv")
    assert traduits == {"N.SIREN": "N_SIREN", "Tel.mobile": "Tel_mobile"}
    assert lignes == [{"N_SIREN": "552032534", "Tel_mobile": "0102030405"}]


def test_un_entete_SANS_point_n_est_pas_touche():
    """Le témoin négatif : on traduit ce qui doit l'être, rien d'autre."""
    lignes, traduits = _parse_rows(_csv("siren,raison_sociale\n1,ACME\n"), "csv")
    assert traduits == {}, "aucun renommage à signaler"
    assert lignes == [{"siren": "1", "raison_sociale": "ACME"}]


def test_le_renommage_est_DETERMINISTE():
    """⚠️ Sinon un second import du même fichier vise d'autres colonnes et duplique."""
    entetes = ["N.SIREN", "a.b.c", "Tel.mobile"]
    assert traduire_entetes(entetes) == traduire_entetes(entetes)
    assert traduire_entetes(entetes)["a.b.c"] == "a_b_c"


# ── La collision : on refuse, on ne fusionne jamais ──────────────────────────

def test_collision_avec_un_AUTRE_ENTETE_du_fichier(): 
    """⚠️ Le vrai risque du renommage. `N.SIREN` et `N_SIREN` dans le même fichier
    sont DEUX colonnes ; les fusionner en ferait perdre une en silence."""
    with pytest.raises(UploadError) as e:
        traduire_entetes(["N.SIREN", "N_SIREN"])
    msg = str(e.value.detail if hasattr(e.value, "detail") else e.value)
    assert "N.SIREN" in msg and "N_SIREN" in msg, "le refus nomme LES DEUX"


def test_collision_avec_une_colonne_DECLAREE_au_schema():
    """La cible peut être prise sans être dans le fichier : l'import écrirait alors
    dans une colonne du tableau que le client n'a jamais visée."""
    with pytest.raises(UploadError):
        traduire_entetes(["N.SIREN"], frozenset({"N_SIREN"}))


def test_deux_entetes_pointes_qui_visent_la_MEME_cible():
    with pytest.raises(UploadError):
        traduire_entetes(["a.b", "a_b"])


# ── Le NDJSON n'est pas concerné, et c'est un choix ──────────────────────────

def test_le_NDJSON_ne_traduit_RIEN():
    """⚠️ Il porte des CLÉS, pas des étiquettes. Une clé pointée y est une adresse
    fautive, et le store la refusera — à raison. Traduire ici masquerait un bug
    d'appelant au lieu de le lui dire."""
    lignes, traduits = _parse_rows(b'{"champ.comment": "x"}\n', "ndjson")
    assert traduits == {}
    assert lignes == [{"champ.comment": "x"}], "la clé arrive INTACTE au store"
