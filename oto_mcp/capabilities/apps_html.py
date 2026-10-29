"""Ce que le HTML d'une app demande et que son manifeste ne lui ouvre pas.

Une app ouverte trie, filtre, regroupe et mesure sur les seuls champs que son
manifeste déclare (`apps_appel.py`, et le pont du front avant lui) : le reste est
refusé À L'OUVERTURE — « An app can only sort on a field it declares. » —, devant
le premier lecteur, quand l'agent qui l'a écrite est parti. On lit donc le HTML À
L'ÉCRITURE (`create`, `update`, `restore`, `validate`) et on rend ces écarts en
`warnings` à qui écrit, pour qu'il les corrige tout de suite.

**Des avertissements, jamais un refus** : c'est une lecture du texte, pas une
exécution. Seuls les appels écrits en clair se lisent — `tulina.rows("leads",
{orderBy: "score"})`, le pont pouvant porter un autre nom (`const T = window.tulina`) ;
un nom de table ou des options passés par variable échappent
à la lecture (l'appel les jugera). Un `orderBy` / `groupBy` écrit hors d'un tel
appel (une fonction d'aide qui reçoit ses options) est jugé contre TOUS les
tableaux déclarés : il n'avertit que si aucun ne le lit.

Mêmes règles que l'appel : un champ déclaré, ou l'élément d'une liste par sa
colonne (`tags[]`, `contacts[].role`) ; sans liste de champs, tout ; `_created_at`
et `_updated_at` en plus pour trier et filtrer ; la recherche (`q`) seulement sans
liste de champs.
"""
from __future__ import annotations

import re
from typing import Optional

_DATES = frozenset({"_created_at", "_updated_at"})
# Au-delà, on s'arrête : un agent n'a pas besoin de cent fois la même phrase.
MAX_WARNINGS = 20

# `const T = window.tulina` : le pont sous un autre nom.
_ALIAS = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:window\s*\.\s*)?tulina\b(?!\s*\.)")
_CHAINE = re.compile(r"""\s*(['"`])([^'"`\\$]*)\1\s*$""", re.S)
_LIBRE = re.compile(r"""\b(orderBy|groupBy)\s*:\s*(['"`])([^'"`\\$]+)\2""")
_CLE = re.compile(r"""^\s*(?:(['"`])([^'"`]+)\1|([A-Za-z_$][\w$]*))\s*(?::|$)""", re.S)
_SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>|$)", re.S | re.I)


def _blanc(texte: str) -> str:
    """Le même texte en blanc, retours à la ligne gardés : les lignes ne bougent pas."""
    return re.sub(r"[^\n]", " ", texte)


def _code(html: str) -> str:
    """Le JavaScript de `html`, à la même place : hors des `<script>` et dans les
    commentaires, du blanc. Une apostrophe du texte de la page (« don't ») n'ouvre
    ainsi aucune chaîne, et un `)` commenté ne ferme rien. Sans `<script>`, tout est
    du code."""
    if _SCRIPT.search(html):
        morceaux, fin = [], 0
        for m in _SCRIPT.finditer(html):
            morceaux += [_blanc(html[fin:m.start(2)]), m.group(2)]
            fin = m.end(2)
        html = "".join(morceaux) + _blanc(html[fin:])
    out, i, n = [], 0, len(html)
    while i < n:
        c = html[i]
        if c in "'\"`":
            j = i + 1
            # Une chaîne '…' ou "…" s'arrête à la ligne (une regex /'/ ne mange pas la
            # suite) ; un gabarit `…` court sur plusieurs.
            while j < n and html[j] != c and (html[j] != "\n" or c == "`"):
                j += 2 if html[j] == "\\" else 1
            out.append(html[i:j + 1])
            i = j + 1
        elif html.startswith("//", i):
            j = html.find("\n", i)
            j = n if j < 0 else j
            out.append(_blanc(html[i:j]))
            i = j
        elif html.startswith("/*", i):
            j = html.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(_blanc(html[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _fin(src: str, i: int, ferme: str) -> int:
    """L'indice juste après le `ferme` qui équilibre l'ouvrant en `src[i]`, en
    sautant les chaînes ; -1 si le texte s'arrête avant."""
    prof, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == "\\" else 1
        elif c in "([{":
            prof += 1
        elif c in ")]}":
            prof -= 1
            if prof == 0:
                return i + 1 if c == ferme else -1
        i += 1
    return -1


def _morceaux(src: str) -> list[str]:
    """Les morceaux de `src` séparés par les virgules de premier niveau."""
    out, prof, debut, i, n = [], 0, 0, 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == "\\" else 1
        elif c in "([{":
            prof += 1
        elif c in ")]}":
            prof -= 1
        elif c == "," and prof == 0:
            out.append(src[debut:i])
            debut = i + 1
        i += 1
    out.append(src[debut:])
    return [m for m in out if m.strip()]


def _objet(src: str) -> Optional[dict[str, str]]:
    """Un littéral objet `{a: …, "b": …, c}` → {clé: texte de la valeur} ; None si
    `src` n'en est pas un (une variable, un appel)."""
    s = src.strip()
    if not s.startswith("{") or _fin(s, 0, "}") != len(s):
        return None
    out: dict[str, str] = {}
    for m in _morceaux(s[1:-1]):
        if m.strip().startswith("..."):
            continue
        k = _CLE.match(m)
        if k:
            cle = k.group(2) or k.group(3)
            out[cle] = m[m.find(":") + 1:] if ":" in m[k.start():k.end()] else cle
    return out


def _chaine(src: Optional[str]) -> Optional[str]:
    m = _CHAINE.match(src or "")
    return m.group(2) if m else None


def _lisible(t: dict, champ: str) -> bool:
    fields = t.get("fields")
    return fields is None or champ.split("[")[0].split(".")[0] in fields


def avertissements(manifest: dict, html: Optional[str]) -> list[str]:
    """Les écarts entre ce que `html` demande et ce que `manifest` (normalisé par
    `apps_manifeste.forme`, ou tel que rangé) déclare. Vide = rien vu."""
    if not html:
        return []
    html = _code(html)
    tables: dict[str, dict] = manifest.get("tables") or {}
    out: list[str] = []
    lus: list[tuple[int, int]] = []

    def ligne(i: int) -> int:
        return html.count("\n", 0, i) + 1

    def dire(i: int, msg: str) -> None:
        phrase = f"Line {ligne(i)}: {msg}"
        if phrase not in out:
            out.append(phrase)

    def champs(alias: str) -> str:
        f = tables[alias].get("fields")
        return ", ".join(f) if f else "none"

    noms = {"tulina"} | set(_ALIAS.findall(html))
    appel = re.compile(r"(?<![\w$.])(?:window\s*\.\s*)?(?:" + "|".join(map(re.escape, sorted(noms)))
                       + r")\s*\.\s*(rows|aggregate)\s*\(")
    for m in appel.finditer(html):
        quoi, ouvre = m.group(1), m.end() - 1
        ferme = _fin(html, ouvre, ")")
        if ferme < 0:
            continue
        args = _morceaux(html[ouvre + 1:ferme - 1])
        alias = _chaine(args[0]) if args else None
        if alias is None:
            continue
        if alias not in tables:
            dire(m.start(), f'tulina.{quoi}("{alias}") names a table the manifest doesn\'t '
                            f"declare (declared: {', '.join(tables) or 'none'}): add it under "
                            "`tables`, or use one of those names.")
            continue
        t = tables[alias]
        o = _objet(args[1]) if len(args) > 1 else None
        if o is None:
            continue
        lus.append((ouvre, ferme))
        avec = f"tables.{alias}.fields"
        if quoi == "rows":
            tri = _chaine(o.get("orderBy"))
            if tri is not None and tri not in _DATES and not _lisible(t, tri):
                dire(m.start(), f'sorts "{alias}" on "{tri}", which {avec} doesn\'t declare '
                                f"(declared: {champs(alias)}). Add \"{tri}\" to {avec}, or sort "
                                "on a declared field or on _created_at / _updated_at — "
                                "otherwise the app is refused when it opens.")
            if "q" in o and t.get("fields") is not None:
                dire(m.start(), f'searches "{alias}" (`q`), but search runs over every field: '
                                f"it needs {avec} left out. Drop `q`, or filter on declared "
                                "fields instead.")
        else:
            groupe = _chaine(o.get("groupBy"))
            if groupe is not None and not _lisible(t, groupe):
                dire(m.start(), f'groups "{alias}" by "{groupe}", which {avec} doesn\'t '
                                f"declare (declared: {champs(alias)}). Add \"{groupe}\" to {avec}.")
            for metrique in _morceaux((o.get("metrics") or "").strip()[1:-1]):
                mo = _objet(metrique) or {}
                f = _chaine(mo.get("field"))
                if f is not None and not _lisible(t, f):
                    dire(m.start(), f'measures "{f}" on "{alias}", which {avec} doesn\'t '
                                    f"declare (declared: {champs(alias)}). Add \"{f}\" to {avec}.")
        filtre = _objet(o["filter"]) if "filter" in o else None
        caches = [k for k in (filtre or {}) if k not in _DATES and not _lisible(t, k)]
        if caches:
            dire(m.start(), f'filters "{alias}" on {", ".join(caches)}, which {avec} doesn\'t '
                            f"declare (declared: {champs(alias)}). Add them to {avec}.")
        if len(out) >= MAX_WARNINGS:
            return out[:MAX_WARNINGS]

    # Un tri ou un regroupement écrit hors d'un appel lu : jugé contre tous les tableaux.
    for m in _LIBRE.finditer(html):
        if any(a < m.start() < b for a, b in lus):
            continue
        quoi, champ = m.group(1), m.group(3)
        if quoi == "orderBy" and champ in _DATES:
            continue
        if tables and not any(_lisible(t, champ) for t in tables.values()):
            dire(m.start(), f'{quoi}: "{champ}" isn\'t a declared field of any table in the '
                            "manifest: add it to the `fields` of the table this call reads"
                            + (", or sort on _created_at / _updated_at." if quoi == "orderBy"
                               else "."))
        if len(out) >= MAX_WARNINGS:
            break
    return out[:MAX_WARNINGS]
