"""Éditer un long texte par remplacements exacts, sans le réémettre.

Un agent qui change une ligne d'un texte de 40 000 caractères (le HTML d'une app, le
code d'une recette) devait jusqu'ici le renvoyer ENTIER : vingt mille jetons de sortie
pour une ligne, et un appel coupé net quand le texte dépasse ce qu'un tour de modèle
peut écrire. Ici, une liste de `{find, replace}` appliquée au texte courant, dans
l'ordre, tout ou rien.

**Exact et unique** : `find` doit se trouver tel quel, une seule fois (sauf `all`) —
un `find` absent ou ambigu refuse la liste ENTIÈRE en disant lequel et combien de fois
il apparaît, jamais un remplacement « le plus proche ». Deux occurrences qui se
chevauchent (« aa » dans « aaa ») sont ambiguës aussi. Chaque `find` se cherche dans
le texte déjà modifié par les précédents.

**Un remplacement qui ne change rien est refusé** : `replace` égal à `find`, ou une liste
dont le texte final est le texte de départ (« a » → « b » puis « b » → « a ») — jamais
une version enregistrée identique à la précédente.

**Borné avant d'allouer** : avec `max_len`, la longueur que le texte AURAIT est
calculée avant chaque remplacement — un `all: true` sur un caractère avec un `replace`
de 100 Ko refuse sans fabriquer le texte géant.
"""
from __future__ import annotations

from typing import Any, Optional

MAX_EDITS = 50


# Les lignes nommées d'un `find` ambigu, au plus : de quoi relire autour (`op=source`).
MAX_LIGNES_NOMMEES = 5


def _lignes(texte: str, find: str) -> str:
    """« lines 12, 480 » : où se trouvent les premières occurrences de `find`, dans le
    texte tel qu'il est à cet edit (après les précédents). Un seul passage."""
    lignes, pos, ligne, vu = [], texte.find(find), 1, 0
    while pos >= 0 and len(lignes) < MAX_LIGNES_NOMMEES:
        ligne += texte.count("\n", vu, pos)
        vu = pos
        if not lignes or lignes[-1] != ligne:
            lignes.append(ligne)
        pos = texte.find(find, pos + 1)
    suite = ", …" if pos >= 0 else ""
    return ("line " if len(lignes) == 1 and not suite else "lines ") \
        + ", ".join(map(str, lignes)) + suite


class RemplacementRefuse(ValueError):
    """La liste ne s'applique pas : `str(e)` dit pourquoi, à l'agent qui l'a écrite."""


def appliquer(texte: str, edits: Any, *, max_len: Optional[int] = None) -> tuple[str, int]:
    """Le texte après `edits`, et le nombre de remplacements faits. `max_len` : la
    longueur au-delà de laquelle la liste est refusée."""
    if not isinstance(edits, list) or not edits:
        raise RemplacementRefuse("`edits` is a non-empty list of {find, replace}.")
    if len(edits) > MAX_EDITS:
        raise RemplacementRefuse(f"At most {MAX_EDITS} edits per call.")
    faits, depart = 0, texte
    for i, e in enumerate(edits):
        quoi = f"edits[{i}]"
        if not isinstance(e, dict) or not isinstance(e.get("find"), str) \
                or not isinstance(e.get("replace"), str):
            raise RemplacementRefuse(f"{quoi} must be {{\"find\": str, \"replace\": str}}.")
        find, replace, tous = e["find"], e["replace"], e.get("all") is True
        if not find:
            raise RemplacementRefuse(f"{quoi}.find is empty.")
        # `replace` égal à `find` : un remplacement qui ne change rien, une version
        # enregistrée pour rien. Le plus souvent l'agent n'a pas vu le texte à changer.
        if replace == find:
            raise RemplacementRefuse(
                f"{quoi}.replace is the same as its find: it changes nothing. "
                "Read the text again and put the change in `replace`. Nothing was written.")
        n = texte.count(find)
        if n == 0:
            raise RemplacementRefuse(
                f"{quoi}.find isn't in the current text (after the edits before it). "
                "Copy it exactly from a fresh read, whitespace included. Nothing was written.")
        # `count` ne voit pas les chevauchements (« aa » dans « aaa » = 1) : ambigu aussi.
        if not tous and (n > 1 or texte.find(find, texte.find(find) + 1) >= 0):
            fois = f"{n} times" if n > 1 else "more than once (overlapping)"
            raise RemplacementRefuse(
                f"{quoi}.find appears {fois} ({_lignes(texte, find)}): add surrounding "
                "text until it is unique, or set \"all\": true. Nothing was written.")
        if max_len is not None and len(texte) + n * (len(replace) - len(find)) > max_len:
            raise RemplacementRefuse(
                f"{quoi} would make the text longer than {max_len} characters. "
                "Nothing was written.")
        texte = texte.replace(find, replace)
        faits += n
    if texte == depart:
        raise RemplacementRefuse(
            "These edits leave the text exactly as it was: nothing to save. "
            "Nothing was written.")
    return texte, faits
