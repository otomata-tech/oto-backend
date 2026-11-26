"""Lire le HTML d'une app PAR MORCEAUX (`oto_apps op=source`), pour l'éditer sans le lire en entier.

Une app grandit : on en a vu une à 510 000 caractères, quelque 150 000 jetons —
`op=get` la sert entière à l'agent qui veut en changer une ligne, ce qui remplit le tour
d'un modèle et, au-delà, ne passe plus. `op=patch` écrit déjà sans réémettre le HTML ;
il manquait de quoi LIRE juste ce qu'il faut pour écrire un `find` exact :

- **le plan** (ni `lines` ni `search`) : chaque `<style>` et `<script>`, chaque fonction
  et classe (au premier et au deuxième niveau d'imbrication), chaque grande déclaration,
  chaque commentaire de section (`// ── Backlinks ──`), chaque élément à `id` — avec la
  ligne où il commence, celle où il finit et sa taille, en TEXTE compact (une ligne par
  entrée : le plan d'une app de 500 000 caractères tient en quelques milliers de jetons) ;
- **la recherche** (`search`) : un TEXTE littéral, sans casse — jamais une regex
  d'agent sur un million de caractères, qui peut bloquer un worker partagé —, rendu en
  lignes numérotées avec `context` lignes autour ; `exact` compte les occurrences à la
  casse près, celles qu'un `find` égal à la requête rencontrerait ;
- **des lignes** (`lines` = [première, dernière]) : numérotées comme `cat -n`.

**Borné** : au plus `MAX_TEXTE` caractères rendus par réponse ; une ligne de plus de
`MAX_LIGNE` caractères est coupée (un HTML minifié tient sur une ligne : on y cherche,
la recherche y rend une fenêtre autour de chaque occurrence).

**Le plan est un indicateur, pas un parseur** : les débuts se lisent par motifs, ligne à
ligne ; les fins par un compte d'accolades qui saute chaînes, gabarits (`${…}` compris),
commentaires et littéraux d'expression régulière (reconnus au caractère qui les précède,
une heuristique). Le code écrit par un agent est souvent sans retrait : l'indentation ne
dit rien, les accolades si. Un compte qui se trompe fausse une FIN, jamais le texte rendu
par `lines` ou `search` ; une fonction écrite autrement n'y paraît pas, la recherche la
trouve. Linéaire, un seul passage.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any, Optional

# Ce qu'une réponse rend au plus, en caractères de texte (lignes numérotées comprises).
MAX_TEXTE = 40_000
# Une ligne plus longue est coupée à la lecture ; en recherche, fenêtrée.
MAX_LIGNE = 4_000
# Les lignes de contexte d'une recherche, coupées plus court.
MAX_CONTEXTE_LIGNE = 400
# Autour d'une occurrence dans une longue ligne.
FENETRE = 300
MAX_OCCURRENCES = 50
MAX_CONTEXTE = 10
CONTEXTE = 2
MAX_REQUETE = 300
# Les entrées du plan, au plus ; au-delà, les plus PETITES fonctions tombent (la
# recherche les trouve) — jamais la fin du fichier.
MAX_PLAN = 400
# Les fonctions au plan : niveau 0 (dans le <script>) et 1 (dans une fonction).
MAX_NIVEAU = 1
# Une déclaration qui n'est pas une fonction (`const CSS = \`…\``) n'entre au plan qu'au
# niveau 0 et si elle couvre au moins autant de lignes : les grands blocs, pas chaque
# constante.
MIN_LIGNES_DECLARATION = 20

_OUVRE = re.compile(r"<(script|style)\b[^<>]*>", re.I)
_IDENT = r"[A-Za-z_$][\w$]*"
_FONCTIONS = (
    re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*({_IDENT})\s*\("),
    re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_IDENT})\s*=\s*(?:async\s+)?"
               rf"(?:function\b|\([^()]*\)\s*=>|{_IDENT}\s*=>)"),
    re.compile(rf"^\s*({_IDENT})\s*:\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>)"),
)
_CLASSE = re.compile(rf"^\s*(?:export\s+)?class\s+({_IDENT})")
_DECLARATION = re.compile(rf"^\s*(?:const|let|var)\s+({_IDENT})\s*=")
# Un commentaire seul sur sa ligne, décoré (`// ── X ──`, `/* ==== X ==== */`,
# `<!-- --- X --- -->`) : un titre de section.
_SECTION = re.compile(r"^\s*(?://|/\*|<!--)\s*(.*?)\s*(?:\*/|-->)?\s*$")
_DECOR = re.compile(r"[=─━═—#*~]{2,}|-{3,}")
_ID = re.compile(r"<([A-Za-z][\w-]*)\b[^<>]*\bid\s*=\s*[\"']([^\"'<>]+)[\"']")
# Après ces caractères, un `/` ouvre une expression régulière, pas une division.
_AVANT_REGEX = frozenset("(,=:[!&|?{};+-*%<>~^")


class SourceRefusee(ValueError):
    """La lecture demandée ne se fait pas : `str(e)` dit pourquoi, à l'agent."""


def _numero(n: int, texte: str) -> str:
    return f"{n:>6}\t{texte}"


def _coupe(ligne: str, borne: int) -> str:
    if len(ligne) <= borne:
        return ligne
    return f"{ligne[:borne]}… [+{len(ligne) - borne} characters on this line: search inside it]"


# ── Le plan ─────────────────────────────────────────────────────────────────────

def _accolades(lignes: list[str]) -> list[tuple[int, int, bool]]:
    """Pour chaque ligne d'un `<script>` : (niveau au début, niveau à la fin, la ligne
    finit-elle DANS un gabarit ou un commentaire de bloc). Un seul passage."""
    out: list[tuple[int, int, bool]] = []
    niveau, mode, gabarits, avant = 0, "code", [], ""
    for ligne in lignes:
        debut = niveau
        i, n = 0, len(ligne)
        while i < n:
            c = ligne[i]
            if mode == "code":
                if c == "'" or c == '"':
                    mode = c
                elif c == "`":
                    mode = "`"
                elif c == "/" and i + 1 < n and ligne[i + 1] == "/":
                    break
                elif c == "/" and i + 1 < n and ligne[i + 1] == "*":
                    mode, i = "*", i + 1
                elif c == "/" and (not avant or avant in _AVANT_REGEX):
                    mode = "re"
                elif c == "{":
                    niveau += 1
                elif c == "}":
                    if gabarits and niveau == gabarits[-1] + 1:
                        gabarits.pop()
                        mode = "`"
                    niveau = max(0, niveau - 1)
                if mode == "code" and not c.isspace():
                    avant = c
            elif mode in ("'", '"'):
                if c == "\\":
                    i += 1
                elif c == mode:
                    mode, avant = "code", c
            elif mode == "`":
                if c == "\\":
                    i += 1
                elif c == "`":
                    mode, avant = "code", c
                elif c == "$" and i + 1 < n and ligne[i + 1] == "{":
                    gabarits.append(niveau)
                    niveau += 1
                    mode, avant, i = "code", "{", i + 1
            elif mode == "*":
                if c == "*" and i + 1 < n and ligne[i + 1] == "/":
                    mode, i = "code", i + 1
            elif mode == "re":
                if c == "\\":
                    i += 1
                elif c == "[":
                    mode = "re["
                elif c == "/":
                    mode, avant = "code", ")"
            elif mode == "re[":
                if c == "\\":
                    i += 1
                elif c == "]":
                    mode = "re"
            i += 1
        # Une chaîne ou une regex ne passe pas la ligne : une erreur de lecture s'y arrête.
        if mode in ("'", '"', "re", "re["):
            mode = "code"
        out.append((debut, niveau, mode in ("`", "*")))
    return out


def _js(entrees: list, lignes: list[str], premiere: int, fin_du_bloc: int) -> None:
    """Les fonctions, classes, grandes déclarations et sections d'un `<script>` dont le
    contenu va des lignes `premiere` à `fin_du_bloc` (numéros du HTML)."""
    corps = lignes[premiere - 1:fin_du_bloc]
    niveaux = _accolades(corps)

    def fin(k: int, niveau: int) -> int:
        for j in range(k, len(corps)):
            _, apres, ouvert = niveaux[j]
            if apres <= niveau and not ouvert:
                return premiere + j
        return fin_du_bloc

    sections = []
    for k, ligne in enumerate(corps):
        niveau = niveaux[k][0]
        nom, genre = None, None
        for motif in _FONCTIONS:
            m = motif.match(ligne)
            if m is not None:
                nom, genre = m.group(1), "function"
                break
        if nom is None:
            m = _CLASSE.match(ligne)
            if m is not None:
                nom, genre = m.group(1), "class"
        if nom is None:
            m = _DECLARATION.match(ligne)
            if m is not None and niveau == 0:
                nom, genre = m.group(1), "declaration"
        if nom is not None:
            if niveau > MAX_NIVEAU:
                continue
            bout = fin(k, niveau)
            if genre == "declaration" and bout - (premiere + k) + 1 < MIN_LIGNES_DECLARATION:
                continue
            entrees.append({"kind": genre, "name": nom, "line": premiere + k, "end": bout,
                            "depth": niveau + 1})
            continue
        s = _section(ligne, premiere + k)
        if s is not None:
            s["depth"] = niveau + 1
            sections.append(s)
            entrees.append(s)
    _fins_de_sections(sections, fin_du_bloc)


def _section(ligne: str, i: int) -> Optional[dict]:
    m = _SECTION.match(ligne)
    if m is None or not _DECOR.search(m.group(1) or ""):
        return None
    nom = _DECOR.sub(" ", m.group(1)).strip(" -:")
    return {"kind": "section", "name": nom[:80], "line": i, "end": i, "depth": 0} if nom else None


def _fins_de_sections(sections: list, fin_du_bloc: int) -> None:
    # Une section court jusqu'à la suivante du même bloc, ou à la fin du bloc.
    for a, b in zip(sections, sections[1:] + [None]):
        a["end"] = (b["line"] - 1) if b is not None else fin_du_bloc


def plan(html: str) -> dict[str, Any]:
    lignes = html.split("\n")
    entrees: list[dict[str, Any]] = []
    i, total = 1, len(lignes)
    sections_html: list[dict] = []
    while i <= total:
        ligne = lignes[i - 1]
        o = _OUVRE.search(ligne)
        if o is None:
            for m in _ID.finditer(ligne):
                entrees.append({"kind": "element", "name": f"{m.group(1).lower()}#{m.group(2)}",
                                "line": i, "end": i, "depth": 0})
            s = _section(ligne, i)
            if s is not None:
                sections_html.append(s)
                entrees.append(s)
            i += 1
            continue
        genre = o.group(1).lower()
        # La balise du MÊME bloc ferme : un `</style>` écrit dans une chaîne du script ne
        # termine pas le script.
        ferme = re.compile(rf"</{genre}\s*>", re.I)
        bout = i if ferme.search(ligne, o.end()) else next(
            (j for j in range(i + 1, total + 1) if ferme.search(lignes[j - 1])), total)
        bloc = {"kind": genre, "name": "", "line": i, "end": bout, "depth": 0}
        entrees.append(bloc)
        if bout > i + 1:
            if genre == "script":
                _js(entrees, lignes, i + 1, bout - 1)
            else:
                # Le premier commentaire d'une feuille la nomme (`/* v19: day list window */`).
                for j in range(i + 1, min(bout, i + 4)):
                    c = re.match(r"\s*/\*\s*(.*?)\s*\*/", lignes[j - 1])
                    if c and c.group(1):
                        bloc["name"] = c.group(1)[:80]
                        break
        i = bout + 1
    _fins_de_sections(sections_html, total)
    entrees.sort(key=lambda e: (e["line"], e["depth"]))
    debuts = [0]
    for l in lignes:
        debuts.append(debuts[-1] + len(l) + 1)
    for e in entrees:
        e["chars"] = debuts[e["end"]] - debuts[e["line"] - 1]
    omis = 0
    if len(entrees) > MAX_PLAN:
        # La charpente d'abord (blocs, sections), puis les plus GROSSES entrées : ce qui
        # tombe, ce sont les petites fonctions, que la recherche trouve.
        charpente = {"script", "style", "section"}
        gardees = sorted(entrees, key=lambda e: (e["kind"] not in charpente, -e["chars"]))
        omis = len(entrees) - MAX_PLAN
        entrees = sorted(gardees[:MAX_PLAN], key=lambda e: (e["line"], e["depth"]))
    rendu = []
    for e in entrees:
        lignes_de = str(e["line"]) if e["end"] == e["line"] else f"{e['line']}-{e['end']}"
        nom = f" {e['name']}" if e["name"] else ""
        rendu.append(f"{'  ' * e['depth']}{lignes_de} {e['kind']}{nom} ({e['chars']} ch)")
    out: dict[str, Any] = {"outline": "\n".join(rendu)}
    if omis:
        out["omitted"] = omis
    return out


# ── Des lignes ──────────────────────────────────────────────────────────────────

def plage(html: str, lines: list[int]) -> dict[str, Any]:
    if not isinstance(lines, list) or not 1 <= len(lines) <= 2 \
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in lines):
        raise SourceRefusee("`lines` is [first, last] (or [first]), 1-based line numbers.")
    lignes = html.split("\n")
    total = len(lignes)
    premiere = lines[0]
    derniere = lines[1] if len(lines) == 2 else total
    if premiere < 1 or derniere < premiere:
        raise SourceRefusee("`lines`: first ≥ 1 and last ≥ first.")
    if premiere > total:
        raise SourceRefusee(f"The text has {total} lines.")
    derniere = min(derniere, total)
    rendu, taille, fin = [], 0, premiere - 1
    for n in range(premiere, derniere + 1):
        morceau = _numero(n, _coupe(lignes[n - 1], MAX_LIGNE))
        if rendu and taille + len(morceau) + 1 > MAX_TEXTE:
            break
        rendu.append(morceau)
        taille += len(morceau) + 1
        fin = n
    out: dict[str, Any] = {"from": premiere, "to": fin, "text": "\n".join(rendu)}
    if fin < derniere:
        out["next"] = fin + 1
    return out


# ── La recherche ────────────────────────────────────────────────────────────────

def chercher(html: str, requete: str, context: Optional[int]) -> dict[str, Any]:
    if not isinstance(requete, str) or len(requete.strip()) < 2:
        raise SourceRefusee("`search` is a text of at least 2 characters.")
    if len(requete) > MAX_REQUETE:
        raise SourceRefusee(f"`search` is at most {MAX_REQUETE} characters.")
    autour = CONTEXTE if context is None else context
    if not isinstance(autour, int) or isinstance(autour, bool) or not 0 <= autour <= MAX_CONTEXTE:
        raise SourceRefusee(f"`context` is 0 to {MAX_CONTEXTE} lines.")
    lignes = html.split("\n")
    debuts = [0]
    for l in lignes[:-1]:
        debuts.append(debuts[-1] + len(l) + 1)
    # Un littéral échappé : linéaire, aucun retour arrière possible.
    motif = re.compile(re.escape(requete), re.I)
    occurrences: list[int] = []
    total = 0
    for m in motif.finditer(html):
        total += 1
        if len(occurrences) < MAX_OCCURRENCES:
            occurrences.append(m.start())
    out: dict[str, Any] = {"matches": total, "exact": html.count(requete)}
    if not total:
        return {**out, "hunks": []}
    # Les occurrences par ligne : ligne → positions dans la ligne.
    par_ligne: dict[int, list[int]] = {}
    for pos in occurrences:
        n = bisect_right(debuts, pos)
        par_ligne.setdefault(n, []).append(pos - debuts[n - 1])
    fenetres: list[list[int]] = []
    for n in sorted(par_ligne):
        a, b = max(1, n - autour), min(len(lignes), n + autour)
        if fenetres and a <= fenetres[-1][1] + 1:
            fenetres[-1][1] = max(fenetres[-1][1], b)
        else:
            fenetres.append([a, b])
    # Rempli LIGNE À LIGNE sous le budget, comme une lecture de `lines` : une fenêtre
    # s'arrête à sa dernière ligne entière, jamais au milieu d'une ligne, et `to` le dit.
    hunks, taille, coupe = [], 0, False
    for a, b in fenetres:
        rendu, fin = [], a - 1
        for n in range(a, b + 1):
            ligne = lignes[n - 1]
            if n in par_ligne:
                texte = _fenetre(ligne, par_ligne[n], len(requete))
            else:
                texte = _coupe(ligne, MAX_CONTEXTE_LIGNE)
            morceau = _numero(n, texte)
            if (hunks or rendu) and taille + len(morceau) + 1 > MAX_TEXTE:
                coupe = True
                break
            rendu.append(morceau)
            taille += len(morceau) + 1
            fin = n
        if rendu:
            hunks.append({"from": a, "to": fin, "text": "\n".join(rendu)})
        if coupe:
            break
    if coupe or total > len(occurrences):
        out["truncated"] = True
    return {**out, "lines_matched": len(par_ligne), "hunks": hunks}


def _fenetre(ligne: str, positions: list[int], longueur: int) -> str:
    """Une ligne qui porte l'occurrence : entière si elle tient dans `MAX_LIGNE`, sinon
    une fenêtre de `FENETRE` caractères autour de chaque occurrence — du texte EXACT, que
    l'agent peut reprendre tel quel dans un `find`."""
    if len(ligne) <= MAX_LIGNE:
        return ligne
    morceaux: list[tuple[int, int]] = []
    for p in positions:
        a, b = max(0, p - FENETRE), min(len(ligne), p + longueur + FENETRE)
        if morceaux and a <= morceaux[-1][1]:
            morceaux[-1] = (morceaux[-1][0], b)
        else:
            morceaux.append((a, b))
    return " ⋯ ".join(("…" if a > 0 else "") + ligne[a:b] + ("…" if b < len(ligne) else "")
                      for a, b in morceaux)


# ── L'entrée ────────────────────────────────────────────────────────────────────

def lire(html: str, *, lines: Optional[list[int]] = None, search: Optional[str] = None,
         context: Optional[int] = None) -> dict[str, Any]:
    """`op=source` : le plan, une recherche ou des lignes — jamais deux à la fois."""
    if lines is not None and search is not None:
        raise SourceRefusee("Give `lines` or `search`, not both.")
    if context is not None and search is None:
        raise SourceRefusee("`context` goes with `search`.")
    entete = {"chars": len(html), "lines": html.count("\n") + 1}
    if search is not None:
        return {**entete, **chercher(html, search, context)}
    if lines is not None:
        return {**entete, **plage(html, lines)}
    return {**entete, **plan(html)}
