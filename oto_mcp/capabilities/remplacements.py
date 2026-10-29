"""Éditer un long texte par remplacements exacts, sans le réémettre.

Un agent qui change une ligne d'un texte de 40 000 caractères (le HTML d'une app, le
code d'une recette) devait jusqu'ici le renvoyer ENTIER : vingt mille jetons de sortie
pour une ligne, et un appel coupé net quand le texte dépasse ce qu'un tour de modèle
peut écrire. Ici, une liste de `{find, replace}` appliquée au texte courant, dans
l'ordre, tout ou rien.

**Exact et unique** : `find` doit se trouver tel quel, une seule fois (sauf `all`) —
un `find` absent ou ambigu refuse la liste ENTIÈRE en disant lequel et combien de fois
il apparaît, jamais un remplacement « le plus proche ». Chaque `find` se cherche dans
le texte déjà modifié par les précédents.
"""
from __future__ import annotations

from typing import Any

MAX_EDITS = 50


class RemplacementRefuse(ValueError):
    """La liste ne s'applique pas : `str(e)` dit pourquoi, à l'agent qui l'a écrite."""


def appliquer(texte: str, edits: Any) -> tuple[str, int]:
    """Le texte après `edits`, et le nombre de remplacements faits."""
    if not isinstance(edits, list) or not edits:
        raise RemplacementRefuse("`edits` is a non-empty list of {find, replace}.")
    if len(edits) > MAX_EDITS:
        raise RemplacementRefuse(f"At most {MAX_EDITS} edits per call.")
    faits = 0
    for i, e in enumerate(edits):
        quoi = f"edits[{i}]"
        if not isinstance(e, dict) or not isinstance(e.get("find"), str) \
                or not isinstance(e.get("replace"), str):
            raise RemplacementRefuse(f"{quoi} must be {{\"find\": str, \"replace\": str}}.")
        find, replace, tous = e["find"], e["replace"], e.get("all") is True
        if not find:
            raise RemplacementRefuse(f"{quoi}.find is empty.")
        n = texte.count(find)
        if n == 0:
            raise RemplacementRefuse(
                f"{quoi}.find isn't in the current text (after the edits before it). "
                "Copy it exactly from a fresh read, whitespace included. Nothing was written.")
        if n > 1 and not tous:
            raise RemplacementRefuse(
                f"{quoi}.find appears {n} times: add surrounding text until it is unique, "
                "or set \"all\": true. Nothing was written.")
        texte = texte.replace(find, replace)
        faits += n
    return texte, faits
