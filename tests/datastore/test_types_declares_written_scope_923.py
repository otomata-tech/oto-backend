"""oto-backend#923 (signal feedback) — `types_trahis` jugeait TOUTE colonne DÉCLARÉE
du row mergé, y compris celles que le geste n'écrit pas.

Mesuré : une écriture PARTIELLE par `id` (`row={"priorite": 3}`) sur une ligne dont
une AUTRE colonne (`ca`, déclarée `number`) porte déjà une valeur hors type
(`"5547719 (2024)"`, héritée d'avant que le type soit armé) était REFUSÉE — alors que
l'appel ne touche pas `ca`. Le message citait en plus `ca` comme si l'appel l'avait
envoyée, ce qui est trompeur.

`etats_trahis`/`couches_manquantes` respectaient déjà le partage `écrit / hérité`
(07-08/09/2026, même raison) : `types_trahis` ne l'avait jamais reçu — un oubli, pas
un choix, corrigé ici sur le même patron (`written` restreint le refus, `gelees`
recueille ce qui est hérité et invalide sans bloquer)."""
from __future__ import annotations

from oto_mcp.datastore import validation as V


def _schema(**extra):
    return {"strict": True,
            "fields": [{"key": "priorite", "type": "number"},
                       {"key": "ca", "type": "number"}],
            **extra}


def test_une_colonne_heritee_invalide_ne_gele_pas_un_patch_sans_rapport():
    """`ca` est déjà invalide EN BASE (hérité), le geste n'écrit que `priorite` :
    aucun refus — c'est le défaut mesuré par #923."""
    merged = {"priorite": 3, "ca": "5547719 (2024)"}
    gelees: list = []
    errors = V.validate_row(_schema(), merged, written={"priorite"}, gelees=gelees)
    assert errors == [], errors
    # `ca` doit apparaître en GELÉE (signalée, pas bloquante) — jamais en erreur.
    # Une entrée sœur peut venir du contrôle interne de `_row_errors` (déjà borné
    # depuis le 07/09), `_check_row` les déduplique par champ à l'exposition
    # (`off_geles.setdefault`) : ce test ne juge que `types_trahis`, donc seulement
    # la présence, pas l'unicité.
    champs_geles = {g["champ"] for g in gelees}
    assert champs_geles == {"ca"}, gelees
    assert any("`ca`" in g["refus"] and "refusé" in g["refus"] for g in gelees
              if g["champ"] == "ca"), gelees


def test_la_meme_valeur_est_refusee_si_le_geste_l_ecrit_lui_meme():
    """`ca` reste refusée quand c'est LE GESTE qui pose la valeur fautive — la
    restriction ne désarme rien, elle ne fait que juger le bon périmètre."""
    merged = {"priorite": 3, "ca": "5547719 (2024)"}
    errors = V.validate_row(_schema(), merged, written={"priorite", "ca"})
    assert any("`ca`" in e and "refusé" in e for e in errors), errors


def test_insert_sans_written_juge_toujours_la_row_entiere():
    """`written=None` (création/remplacement) : aucune colonne n'est « héritée »,
    tout ce qui est posé est jugé — comportement inchangé sur ce chemin."""
    merged = {"priorite": 3, "ca": "5547719 (2024)"}
    errors = V.validate_row(_schema(), merged, written=None)
    assert any("`ca`" in e and "refusé" in e for e in errors), errors
