"""Édition PARTIELLE d'une page markdown par SECTION (oto/#6 top5 #3).

`op=update` remplaçait tout le corps → deux auteurs qui touchent des sections
DIFFÉRENTES s'écrasaient. `patch_section` cible UNE section par son titre (heading
markdown) et n'y touche que là : replace / append / prepend. Fonction PURE (pas
d'I/O) → le caller relit le doc, applique le patch, réécrit via `update_doc` (qui
garde révisions + backlinks + conflit optimiste).
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


class SectionNotFound(Exception):
    """Le titre visé n'existe pas dans le corps (le caller renvoie une erreur
    actionnable listant les sections disponibles)."""
    def __init__(self, heading: str, available: list[str]):
        self.heading = heading
        self.available = available
        super().__init__(f"section introuvable: {heading!r}")


def _norm(s: str) -> str:
    """Normalise un titre pour comparaison : sans `#`, sans casse, espaces réduits."""
    return re.sub(r"\s+", " ", s.lstrip("#").strip()).lower()


def headings(body: str) -> list[str]:
    """Titres de section (texte brut, sans `#`) présents dans le corps."""
    out: list[str] = []
    for line in (body or "").split("\n"):
        m = _HEADING.match(line)
        if m:
            out.append(m.group(2).strip())
    return out


def patch_section(body: str, heading: str, new_body: str, mode: str = "replace") -> str:
    """Retourne le corps COMPLET avec la section `heading` modifiée.

    `mode` : 'replace' (remplace le contenu SOUS le titre, garde le titre) /
    'append' (ajoute à la fin de la section) / 'prepend' (insère juste après le titre).
    La section court du titre jusqu'au PROCHAIN titre de niveau ≤ (ou la fin).
    Lève `SectionNotFound` si le titre n'existe pas."""
    if mode not in ("replace", "append", "prepend"):
        raise ValueError(f"mode invalide: {mode}")
    lines = (body or "").split("\n")
    target = _norm(heading)
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m or _norm(m.group(2)) != target:
            continue
        level = len(m.group(1))
        # Fin de section = prochain titre de niveau ≤ (une sous-section reste dedans).
        j = i + 1
        while j < len(lines):
            m2 = _HEADING.match(lines[j])
            if m2 and len(m2.group(1)) <= level:
                break
            j += 1
        head, inner, tail = lines[:i + 1], lines[i + 1:j], lines[j:]
        new_lines = (new_body or "").split("\n")
        if mode == "replace":
            # Une ligne vide encadre proprement le nouveau contenu sous le titre.
            section = [""] + new_lines + ([""] if tail else [])
        elif mode == "append":
            # Retire les vides de fin de section avant d'ajouter.
            while inner and not inner[-1].strip():
                inner.pop()
            section = inner + [""] + new_lines + ([""] if tail else [])
        else:  # prepend
            section = [""] + new_lines + [""] + inner
        return "\n".join(head + section + tail)
    raise SectionNotFound(heading, headings(body))
