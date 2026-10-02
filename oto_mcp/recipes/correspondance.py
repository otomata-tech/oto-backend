"""Lire un élément, fabriquer une ligne : chemins, gabarits, filtres, `where`. Pur.

**Un langage volontairement petit.** Un chemin pointé (`profile.title`,
`email[0].email`), un gabarit (`{{params.company|slug}}::{{item.link.linkedin}}`) et
cinq filtres. Tout ce qui demande davantage — expression régulière, condition, calcul —
va dans une fonction (`oto_function`), jamais dans une syntaxe qu'on ferait grandir ici.

**Tolérant à la forme, jamais inventif.** Un chemin qui ne mène nulle part rend `None`
(une API tierce change de forme sans prévenir, et une ligne à moitié vide se voit au
remplissage par colonne), mais rien ici ne devine une valeur.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

_SEGMENT = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_GABARIT = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
FILTRES = ("slug", "lower", "upper", "strip", "unaccent")


class GabaritInvalide(ValueError):
    """Un gabarit cite une portée ou un filtre inconnus."""


def _segments(chemin: str) -> list:
    out: list = []
    for nom, index in _SEGMENT.findall(chemin or ""):
        out.append(int(index) if index else nom)
    return out


def lire(obj: Any, chemin: str) -> Any:
    """La valeur au bout de `chemin` dans `obj`, ou `None`. Chemin vide = `obj`."""
    cur = obj
    for seg in _segments(chemin):
        if isinstance(seg, int):
            if not isinstance(cur, list) or seg >= len(cur):
                return None
            cur = cur[seg]
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
        if cur is None:
            return None
    return cur


def sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in decompose if not unicodedata.combining(c))


def slug(texte: Any) -> str:
    """Minuscules, accents retirés, chaque suite de caractères non alphanumériques
    réduite à `_`, et aucun `_` aux bords : `Café Lumière` → `cafe_lumiere`, `4B
    Conseil` → `4b_conseil`, `Acme Group (Ex : Old Acme)` → `acme_group_ex_old_acme`.
    C'est la forme des clés déjà écrites par les procédures de sourcing : la changer
    dédoublerait chaque ligne au premier passage d'une recette."""
    base = sans_accents(str(texte if texte is not None else "")).lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def _filtrer(valeur: Any, filtre: str) -> Any:
    if valeur is None:
        return None
    if filtre == "slug":
        return slug(valeur)
    texte = str(valeur)
    if filtre == "lower":
        return texte.lower()
    if filtre == "upper":
        return texte.upper()
    if filtre == "strip":
        return texte.strip()
    if filtre == "unaccent":
        return sans_accents(texte)
    raise GabaritInvalide(f"unknown filter `{filtre}` (known: {', '.join(FILTRES)})")


def _expression(expr: str, portees: dict) -> Any:
    tete, *filtres = [p.strip() for p in expr.split("|")]
    portee, _, chemin = tete.partition(".")
    if portee not in portees:
        raise GabaritInvalide(
            f"unknown scope `{portee}` in `{{{{{expr}}}}}` (known: {', '.join(portees)})")
    valeur = lire(portees[portee], chemin)
    for f in filtres:
        valeur = _filtrer(valeur, f)
    return valeur


def rendre(gabarit: Any, portees: dict) -> Any:
    """Rend un gabarit, récursivement dans les listes et les objets.

    Une chaîne qui n'est QU'UN gabarit garde le type de sa valeur (une liste reste une
    liste, un nombre un nombre) ; mêlé à du texte, chaque morceau devient du texte et
    une valeur absente un texte vide."""
    if isinstance(gabarit, dict):
        return {k: rendre(v, portees) for k, v in gabarit.items()}
    if isinstance(gabarit, list):
        return [rendre(v, portees) for v in gabarit]
    if not isinstance(gabarit, str) or "{{" not in gabarit:
        return gabarit
    seul = _GABARIT.fullmatch(gabarit.strip())
    if seul:
        return _expression(seul.group(1), portees)

    def _morceau(m: re.Match) -> str:
        v = _expression(m.group(1), portees)
        return "" if v is None else str(v)
    return _GABARIT.sub(_morceau, gabarit)


def portees_citees(gabarit: Any) -> set[str]:
    """Les portées qu'un gabarit cite (`params`, `item`…) — pour refuser à l'écriture
    une recette qui cite une portée inconnue, plutôt qu'à la première page."""
    if isinstance(gabarit, dict):
        return set().union(*(portees_citees(v) for v in gabarit.values()), set())
    if isinstance(gabarit, list):
        return set().union(*(portees_citees(v) for v in gabarit), set())
    if not isinstance(gabarit, str):
        return set()
    return {m.group(1).split("|")[0].strip().partition(".")[0]
            for m in _GABARIT.finditer(gabarit)}


def _plie(v: Any) -> str:
    return sans_accents(str(v)).casefold()


def _vide(v: Any) -> bool:
    return v is None or (isinstance(v, (str, list, dict)) and not v)


def garde(item: Any, clauses: list[dict], params: dict) -> bool:
    """L'élément passe-t-il TOUTES les clauses `where` ? Comparaisons de texte sans
    casse ni accents : `Spain` = `spain`, `Côte` = `cote`."""
    for c in clauses or []:
        v = lire(item, c["path"])
        attendu = rendre(c.get("value"), {"params": params})
        op = c["op"]
        if op == "empty":
            ok = _vide(v)
        elif op == "not_empty":
            ok = not _vide(v)
        elif op in ("eq", "ne"):
            egal = v is not None and _plie(v) == _plie(attendu)
            ok = egal if op == "eq" else not egal
        elif op in ("in", "not_in"):
            dedans = v is not None and _plie(v) in {_plie(a) for a in attendu or []}
            ok = dedans if op == "in" else not dedans
        else:  # contains_any — validé par le contrat
            texte = "" if v is None else _plie(v)
            ok = any(_plie(a) in texte for a in attendu or [])
        if not ok:
            return False
    return True


def cellule(item: Any, spec: Any, params: dict) -> Any:
    """La valeur d'une colonne pour un élément, d'après sa spécification."""
    if isinstance(spec, str):
        return lire(item, spec)
    if "const" in spec:
        return spec["const"]
    if "template" in spec:
        v = rendre(spec["template"], {"params": params, "item": item})
    else:
        v = lire(item, spec["path"])
    if v is None:
        v = spec.get("default")
    borne: Optional[int] = spec.get("max")
    if borne and isinstance(v, str) and len(v) > borne:
        v = v[:borne]
    return v


def ligne(item: Any, correspondance: dict, valeurs: dict, params: dict) -> dict:
    """La ligne d'un élément : ses colonnes, puis les valeurs fixes de la recette."""
    out = {col: cellule(item, spec, params) for col, spec in (correspondance or {}).items()}
    for col, gab in (valeurs or {}).items():
        out[col] = rendre(gab, {"params": params})
    return out


def cle(item: Any, spec: dict, rangee: dict, params: dict) -> Optional[str]:
    """La valeur de clé d'un élément : son gabarit, sinon la colonne-clé de la ligne."""
    if spec.get("template"):
        portees = {"params": params, "item": item}
        # Un morceau absent annule la clé : `acme::` n'identifie personne, et deux
        # éléments sans profil se fondraient en une seule ligne.
        if any(_vide(_expression(m.group(1), portees))
               for m in _GABARIT.finditer(spec["template"])):
            return None
        v = rendre(spec["template"], portees)
    else:
        v = rangee.get(spec["column"])
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return str(v)
