"""Les SECTIONS d'une app, retirées du HTML au serveur (`docs/apps.md` § Rôles d'une app).

Un rôle d'app déclare les sections qu'il ouvre (`roles.<r>.sections`, `apps_roles.py`).
L'auteur marque le HTML : tout élément qui porte `data-oto-section="nom"` (ou une
liste de noms séparés par des blancs : visible si le rôle en ouvre UN) n'appartient
qu'à ces sections. Servi à un lecteur borné par un rôle (`apps._view`, op=get), le HTML
perd **l'élément entier** de chaque section que ce rôle n'ouvre pas — balise, contenu,
`<script>` intérieurs compris : il ne part pas, même en « afficher la source ».

- Rôle sans `sections` (None = tout) : rien n'est retiré. `sections: []` : tout élément
  marqué part. Lecteur sans rôle (l'app n'en déclare pas, ou il peut la modifier) :
  rien n'est retiré.
- Sans marque, le HTML est rendu **tel quel** (le même objet) ; avec, seules les
  tranches retirées changent : le reste est recopié à l'octet.

**Comment on trouve la fin d'un élément marqué — fermé du côté sûr.** Un lecteur
maison, linéaire, qui suit le découpage du tokenizer HTML (commentaires et leurs
fermetures abruptes, `<!…>` / `<?…>` / `</…` faux commentaires, valeurs d'attribut
entre guillemets qui contiennent `>`, texte brut de `script` avec ses états
« échappés », `style`, `textarea`, `title`, `xmp`, `iframe`, `noembed`, `noframes`,
`noscript`, `plaintext`, CDATA et `/>` dans SVG/MathML). Dans l'élément marqué, il tient
la pile de ce qui s'y ouvre ; une fermeture n'y dépile que si le navigateur la suivrait
(`_bloque` : portée des tableaux, des listes, des `select`, éléments spéciaux), règles
prises AU LARGE. Il ne rejoue pas les fermetures implicites des balises ouvrantes
(`<li>` qui ferme un `<li>`) : l'élément marqué ne finit qu'à SA balise fermante, jamais
avant le navigateur. Jamais fermé — ou un élément marqué intérieur fermé par celle d'un
autre (le navigateur peut le garder ouvert ou le recloner) — : **tout jusqu'à la fin du
document part**. `html`, `head`, `body` marqués : jusqu'à la fin aussi. Retirer trop est
un défaut d'affichage ; retirer trop peu serait une fuite.

**Ce qui n'est PAS protégé** : le JavaScript et les données hors d'un élément marqué
(une constante dans un `<script>` non marqué, un gabarit HTML construit en JS, le CSS)
— le texte d'un script n'est jamais lu comme du balisage. Le contenu d'un `noscript`,
`noembed`, `noframes`, `iframe` et d'un `<script type="text/template|…html…">` est,
lui, relu (un niveau) et ses éléments marqués retirés.
"""
from __future__ import annotations

import html as _html
import re
from typing import Callable, Optional

ATTRIBUT = "data-oto-section"
MAX_AVERTISSEMENTS = 10

_ASCII_BAS = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_MARQUE = re.compile(re.escape(ATTRIBUT), re.I)
_BLANCS = re.compile(r"[\t\n\f\r ]*")
_NOM = re.compile(r"[^\t\n\f\r />]*")
_ATTR = re.compile(r"""([^\t\n\f\r />][^\t\n\f\r /=>]*)(?:[\t\n\f\r ]*=[\t\n\f\r ]*"""
                   r"""(?:"([^"]*)"|'([^']*)'|(?!["'])([^\t\n\f\r >]*)))?""")
_BLANCS_HTML = re.compile(r"[\t\n\f\r ]+")
_FIN_COMMENTAIRE = re.compile(r"--!?>")
_JALONS_SCRIPT = re.compile(r"<!--|-->|<(/?)script(?=[\t\n\f\r />])", re.I)
_FIN_BRUT: dict[str, re.Pattern] = {}

VIDES = frozenset({"area", "base", "basefont", "bgsound", "br", "col", "embed", "frame",
                   "hr", "image", "img", "input", "keygen", "link", "meta", "param",
                   "source", "track", "wbr"})
BRUTS = frozenset({"script", "style", "textarea", "title", "xmp", "iframe", "noembed",
                   "noframes", "noscript", "plaintext"})
# Contenu jamais exécuté comme du JS : relu, un niveau, pour ses propres marques.
_RELUS = frozenset({"noscript", "noembed", "noframes", "iframe"})
# La portée du navigateur (« has an element in scope ») : une fermeture ne traverse
# pas ces éléments ouverts au-dessus d'elle — elle est ignorée. Pris au large : ignorer
# à tort retire trop, jamais trop peu.
PORTEE = frozenset({"applet", "caption", "html", "table", "td", "th", "marquee", "object",
                    "template", "svg", "math", "foreignobject", "desc", "title", "mi", "mo",
                    "mn", "ms", "mtext", "annotation-xml"})
_TABLEAU = frozenset({"table", "caption", "colgroup", "tbody", "thead", "tfoot", "tr", "td",
                      "th"})
# Les éléments « spéciaux » du HTML : la fermeture d'un élément ordinaire (`span`, un
# élément maison) ou de mise en forme (`b`, `a`…) ne les traverse pas.
SPECIAUX = PORTEE | _TABLEAU | frozenset({
    "address", "article", "aside", "blockquote", "body", "button", "center", "dd", "details",
    "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "frameset", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "li",
    "listing", "main", "menu", "nav", "ol", "p", "pre", "search", "section", "select",
    "summary", "ul"})
# La profondeur suivie (ce qui est ouvert dans une zone, les SVG/MathML ouverts), et ce
# que coûtent au plus les fermetures examinées : au-delà, la zone part jusqu'à la fin —
# la lecture reste linéaire.
MAX_PROFONDEUR = 256
_JUSQU_A_LA_FIN = frozenset({"html", "head", "body"})
# Une de ces balises ouvrantes dans SVG/MathML ramène au HTML (« breakout »).
_SORTIES = frozenset({"b", "big", "blockquote", "body", "br", "center", "code", "dd", "div",
                      "dl", "dt", "em", "embed", "h1", "h2", "h3", "h4", "h5", "h6", "head",
                      "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol", "p",
                      "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup",
                      "table", "tt", "u", "ul", "var"})
_INTEGRATION = {"svg": frozenset({"foreignobject", "desc", "title"}),
                "math": frozenset({"mi", "mo", "mn", "ms", "mtext", "annotation-xml"})}
_NS_NOMS = frozenset({"svg", "math"}) | _INTEGRATION["svg"] | _INTEGRATION["math"]


def _bas(s: str) -> str:
    return s.translate(_ASCII_BAS)


def _balise(s: str, i: int, fermante: bool):
    """La balise qui commence en `s[i]` (`<x` ou `</x`) : (fin, nom, attributs,
    auto-fermante) ; None si le texte s'arrête dans la balise."""
    n = len(s)
    m = _NOM.match(s, i + (2 if fermante else 1))
    nom, p = _bas(m.group(0)), m.end()
    attrs: list[tuple[str, str]] = []
    while True:
        p = _BLANCS.match(s, p).end()
        if p >= n:
            return None
        c = s[p]
        if c == ">":
            return p + 1, nom, attrs, False
        if c == "/":
            if p + 1 < n and s[p + 1] == ">":
                return p + 2, nom, attrs, True
            p += 1
            continue
        a = _ATTR.match(s, p)
        p = a.end()
        dq, sq, nu = a.group(2), a.group(3), a.group(4)
        if dq is None and sq is None and nu is None:
            q = _BLANCS.match(s, p).end()
            if q < n and s[q] == "=":
                return None     # une valeur entre guillemets jamais refermée
            val = ""
        else:
            val = dq if dq is not None else sq if sq is not None else nu
        attrs.append((_bas(a.group(1)), val))


def _fin_brut(s: str, p: int, nom: str) -> int:
    """L'indice du `</nom` qui clôt le texte brut ouvert en `p`, -1 s'il n'y en a pas."""
    if nom == "plaintext":
        return -1
    if nom != "script":
        rx = _FIN_BRUT.get(nom)
        if rx is None:
            rx = _FIN_BRUT[nom] = re.compile(r"</" + nom + r"(?=[\t\n\f\r />])", re.I)
        m = rx.search(s, p)
        return m.start() if m else -1
    # Les états « script data » : `<!--` échappe, `<script` y double l'échappement,
    # où `</script` ne ferme pas ; `-->` revient au texte ordinaire.
    etat, n = 0, len(s)
    for m in _JALONS_SCRIPT.finditer(s, p):
        t = m.group(0)
        if t == "<!--":
            k = m.end()
            while k < n and s[k] == "-":
                k += 1
            if k < n and s[k] == ">":
                etat = 0
            elif etat == 0:
                etat = 1
        elif t == "-->":
            etat = 0
        elif m.group(1):            # </script
            if etat == 2:
                etat = 1
            else:
                return m.start()
        elif etat == 1:             # <script
            etat = 2
    return -1


def _bloque(fermee: str, ouvert: str) -> bool:
    """`</fermee>` est-elle IGNORÉE par le navigateur quand `ouvert` est ouvert entre
    elle et son élément ? Chaque règle de l'algorithme d'arbre, prise au large."""
    if fermee in ("template", "select"):
        return False
    if ouvert == "select" and fermee not in ("option", "optgroup"):
        return True     # dans un select, toute autre fermeture est ignorée
    if fermee in _TABLEAU:
        return ouvert in ("table", "template", "html")
    if fermee == "form":
        return True     # l'élément sort de la pile, ce qu'il contient reste ouvert
    if fermee == "p":
        return ouvert in PORTEE or ouvert == "button"
    if fermee == "li":
        return ouvert in PORTEE or ouvert in ("ul", "ol")
    if fermee in SPECIAUX:
        return ouvert in PORTEE
    return ouvert in SPECIAUX   # mise en forme et élément ordinaire


def _noms(attrs: list[tuple[str, str]]) -> Optional[list[list[str]]]:
    """Les noms de section de chaque occurrence de l'attribut, None sans attribut."""
    occ = [_BLANCS_HTML.split(_html.unescape(v).strip("\t\n\f\r ")) for k, v in attrs
           if k == ATTRIBUT]
    occ = [[x for x in noms if x] for noms in occ]
    return occ or None


class _Parcours:
    def __init__(self) -> None:
        self.coupes: list[tuple[int, int, str]] = []
        self.marques: list[tuple[int, list[list[str]]]] = []
        self.non_fermes: list[tuple[int, str]] = []


def _parcourir(s: str, ouvre: Optional[Callable[[list[list[str]]], bool]],
               relu: bool = False) -> _Parcours:
    """Un passage linéaire sur `s`. `ouvre(noms)` dit si un élément marqué reste ;
    None = tout reste (on relève seulement les marques)."""
    out = _Parcours()
    n = len(s)
    ns: list[str] = []          # svg / math / points d'intégration ouverts
    # Dans un `select`, le navigateur ignore `style`, `title`, `iframe`, `xmp`… : seul `script`
    # y passe en texte brut ; `input`, `keygen`, `textarea` le ferment.
    dans_select = False
    # L'élément marqué en cours de retrait : son début, et la PILE de ce qui y est ouvert
    # (lui compris), chacun avec « marqué et retiré ». La zone finit quand la pile est
    # vide. Une fermeture n'y dépile que si le navigateur la suit (`_bloque`).
    zone: Optional[dict] = None
    budget = 4 * n + 100_000     # pas examinés par les fermetures, au plus
    i = 0

    def etranger() -> bool:
        return bool(ns) and ns[-1] in ("svg", "math")

    def fin_zone(fin: int) -> None:
        nonlocal zone
        out.coupes.append((zone["debut"], fin, ""))
        zone = None

    while True:
        j = s.find("<", i)
        if j < 0 or j + 1 >= n:
            break
        c = s[j + 1]
        if c == "!":
            if s.startswith("<!--", j):
                k = j + 4
                if s.startswith(">", k):
                    i = k + 1
                elif s.startswith("->", k):
                    i = k + 2
                else:
                    m = _FIN_COMMENTAIRE.search(s, k)
                    i = m.end() if m else n
                continue
            if etranger() and s.startswith("<![CDATA[", j):
                k = s.find("]]>", j + 9)
                i = k + 3 if k >= 0 else n
                continue
            k = s.find(">", j + 2)
            i = k + 1 if k >= 0 else n
            continue
        if c == "?":
            k = s.find(">", j + 2)
            i = k + 1 if k >= 0 else n
            continue
        if c == "/":
            if j + 2 >= n:
                break
            d = s[j + 2]
            if d == ">":
                i = j + 3
                continue
            if not ("a" <= d <= "z" or "A" <= d <= "Z"):
                k = s.find(">", j + 2)
                i = k + 1 if k >= 0 else n
                continue
            r = _balise(s, j, True)
            if r is None:
                break
            fin, nom, _, _ = r
            if nom in _NS_NOMS and nom in ns:
                del ns[len(ns) - 1 - ns[::-1].index(nom):]
            if nom == "select":
                dans_select = False
            if zone is not None and zone["compte"].get(nom):
                pile, k = zone["pile"], len(zone["pile"]) - 1
                while k >= 0 and pile[k][0] != nom:
                    if _bloque(nom, pile[k][0]):
                        k = -2
                        break
                    k -= 1
                budget -= len(pile) - k
                if budget < 0:
                    out.non_fermes.append((zone["debut"], zone["nom"]))
                    fin_zone(n)
                    return out
                if k >= 0:
                    # Un élément MARQUÉ fermé en silence par celle d'un autre : le
                    # navigateur peut le garder ouvert (balise ignorée) ou le recloner plus
                    # loin, marque comprise (mise en forme) — tout part jusqu'à la fin.
                    if any(cache for _, cache in pile[k + 1:]):
                        out.non_fermes.append((zone["debut"], zone["nom"]))
                        fin_zone(n)
                        return out
                    for x, _ in pile[k:]:
                        zone["compte"][x] -= 1
                    del pile[k:]
                    if not pile:
                        fin_zone(fin)
            i = fin
            continue
        if not ("a" <= c <= "z" or "A" <= c <= "Z"):
            i = j + 1
            continue

        r = _balise(s, j, False)
        if r is None:
            # Le texte s'arrête dans la balise : le navigateur la jette ; marquée, elle
            # part aussi (la source, elle, se lit).
            if zone is None and ouvre is not None and _MARQUE.search(s, j):
                out.coupes.append((j, n, ""))
            break
        fin, nom, attrs, auto = r
        # L'espace de noms de l'élément, comme le navigateur le choisit.
        if etranger():
            if nom in _SORTIES or (nom == "font" and any(
                    k in ("color", "face", "size") for k, _ in attrs)):
                while etranger():
                    ns.pop()
                est_etranger = False
            else:
                est_etranger = True
        else:
            est_etranger = nom in ("svg", "math")
        ferme_seul = nom in VIDES or (auto and est_etranger)
        if est_etranger and not ferme_seul:
            ctx = next((x for x in reversed(ns) if x in ("svg", "math")), None)
            if nom in ("svg", "math") or (ctx and nom in _INTEGRATION[ctx]):
                if len(ns) < MAX_PROFONDEUR:
                    ns.append(nom)
        if not est_etranger:
            if dans_select and nom in ("select", "input", "keygen", "textarea"):
                dans_select = False
            elif nom == "select":
                dans_select = True
        brut = (not est_etranger and nom in BRUTS and not ferme_seul
                and not (relu and nom in _RELUS)
                and not (dans_select and nom != "script"))
        fin_contenu = _fin_brut(s, fin, nom) if brut else None
        apres = fin
        if brut:
            if fin_contenu < 0:
                apres = n
            else:
                r2 = _balise(s, fin_contenu, True)
                apres = r2[0] if r2 else n

        noms = _noms(attrs)
        if noms is not None and zone is None:
            out.marques.append((j, noms))
        if zone is None and noms is not None and ouvre is not None and not ouvre(noms):
            if ferme_seul:
                out.coupes.append((j, fin, ""))
                i = fin
            elif brut:
                out.coupes.append((j, apres, ""))
                if fin_contenu < 0:
                    out.non_fermes.append((j, nom))
                i = apres
            elif nom in _JUSQU_A_LA_FIN:
                out.coupes.append((j, n, ""))
                out.non_fermes.append((j, nom))
                return out
            else:
                zone = {"debut": j, "nom": nom, "pile": [(nom, True)], "compte": {nom: 1}}
                i = fin
            continue

        if zone is not None:
            cache = noms is not None and ouvre is not None and not ouvre(noms)
            if (cache and nom in _JUSQU_A_LA_FIN) or len(zone["pile"]) >= MAX_PROFONDEUR:
                out.non_fermes.append((zone["debut"], zone["nom"]))
                fin_zone(n)
                return out
            if not ferme_seul and not brut:
                zone["pile"].append((nom, cache))
                zone["compte"][nom] = zone["compte"].get(nom, 0) + 1
        elif brut and not relu and fin_contenu is not None and ouvre is not None:
            # Un contenu jamais exécuté comme du JS : relu pour ses propres marques.
            typ = next((_bas(v) for k, v in attrs if k == "type"), "")
            if nom in _RELUS or (nom == "script" and ("template" in typ or "html" in typ)):
                bout = fin_contenu if fin_contenu >= 0 else n
                dedans = s[fin:bout]
                if _MARQUE.search(dedans):
                    neuf = retirer_avec(dedans, ouvre, relu=True)
                    if neuf is not dedans:
                        out.coupes.append((fin, bout, neuf))
        i = apres if brut else fin

    if zone is not None:
        out.non_fermes.append((zone["debut"], zone["nom"]))
        fin_zone(n)
    return out


def retirer_avec(html: str, ouvre: Callable[[list[list[str]]], bool],
                 relu: bool = False) -> str:
    p = _parcourir(html, ouvre, relu)
    if not p.coupes:
        return html
    morceaux, fin = [], 0
    for a, b, neuf in p.coupes:
        morceaux += [html[fin:a], neuf]
        fin = b
    morceaux.append(html[fin:])
    return "".join(morceaux)


def retirer(html: Optional[str], sections: Optional[list[str]]) -> Optional[str]:
    """`html` tel que le voit un rôle qui ouvre `sections` (None = toutes : rendu tel
    quel). Un élément marqué reste si, pour CHAQUE `data-oto-section` qu'il porte, le
    rôle ouvre l'un des noms ; une marque vide n'est ouverte à aucun rôle."""
    if sections is None or not html or not _MARQUE.search(html):
        return html
    ouverts = set(sections) | {COMMUN}
    return retirer_avec(html, lambda occ: all(ouverts.intersection(x) for x in occ))


def avertissements(manifest: dict, html: Optional[str]) -> list[str]:
    """À l'écriture (`apps._avertir`) : les sections que les rôles ouvrent sans
    qu'aucun élément ne les porte, les marques qu'aucun rôle ne déclare, et les éléments
    marqués jamais fermés. Jamais un refus."""
    roles = (manifest or {}).get("roles") or {}
    declarees: dict[str, list[str]] = {}
    for r, spec in roles.items():
        for x in (spec or {}).get("sections") or []:
            declarees.setdefault(x, []).append(r)
    if not html or (not declarees and not _MARQUE.search(html)):
        return []
    ligne = lambda pos: html.count("\n", 0, pos) + 1  # noqa: E731
    marques = _parcourir(html, None).marques if _MARQUE.search(html) else []
    portees: dict[str, int] = {}
    vides: list[int] = []
    for pos, occ in marques:
        for noms in occ:
            if not noms:
                vides.append(pos)
            for x in noms:
                portees.setdefault(x, pos)
    out: list[str] = []
    if not roles:
        if marques:
            noms = sorted(portees)
            liste = ", ".join(noms[:10]) + (", …" if len(noms) > 10 else "")
            out.append(f"The HTML marks sections ({liste or 'empty'}) "
                       f"with {ATTRIBUT}, but the manifest declares no `roles`: nothing is "
                       "removed, every viewer gets the whole HTML.")
        return out
    for x in sorted(set(declarees) - set(portees))[:MAX_AVERTISSEMENTS]:
        out.append(f'Role {", ".join(declarees[x])} opens section "{x}", but no element '
                   f'carries {ATTRIBUT}="{x}": whatever the page hides for it in script '
                   "still ships to every viewer. Mark that markup with "
                   f'{ATTRIBUT}="{x}" so it is removed for the other roles.')
    for x in sorted(set(portees) - set(declarees) - {COMMUN}, key=portees.get)[:MAX_AVERTISSEMENTS]:
        if len(out) >= MAX_AVERTISSEMENTS:
            break
        out.append(f'Line {ligne(portees[x])}: {ATTRIBUT}="{x}" names a section no role '
                   "declares: it is removed for every role that lists `sections`.")
    for pos in vides[:1]:
        out.append(f"Line {ligne(pos)}: an empty {ATTRIBUT} is removed for every role "
                   "that lists `sections`.")
    for pos, nom in _parcourir(html, lambda occ: False).non_fermes[:MAX_AVERTISSEMENTS]:
        out.append(f"Line {ligne(pos)}: <{nom} {ATTRIBUT}> is never closed: for a role that "
                   f"doesn't open it, everything after it is removed. Close it with </{nom}>.")
    return out[:MAX_AVERTISSEMENTS]


# ── Le mode STRICT : rien de non classé ne part chez un lecteur borné ────────────────
#
# Le retrait ne retire que ce qui est MARQUÉ. Un `<script>` ou un `<style>` non marqué part
# chez tout le monde, avec ce qu'il porte (la logique « admin », une constante, un
# gabarit) ; un gestionnaire `on…=` aussi. Dès qu'un rôle déclare `sections`, le mode
# strict EXIGE que chacun soit classé : marqué d'une section, ou déclaré COMMUN
# (`data-oto-section="*"`). Jugé à CHAQUE écriture (l'auteur voit le problème, refus
# 400 `sections_not_classified`), et au service en dernier recours (refus neutre).
# `strict_sections: false` au manifeste l'éteint, en connaissance de cause.
#
# Ce qu'il ne couvre PAS : le balisage et le texte non marqués (ils sont communs par
# définition), les attributs `style=`, les URLs. Ce qu'il tient : aucun code ni aucune
# feuille de style ne part chez un lecteur sans que l'auteur ait dit à qui il est.

#: La section COMMUNE : toujours servie, à tous les rôles. Réservée (aucun rôle ne la
#: déclare, `apps_roles.forme`).
COMMUN = "*"
_TOUTE_BALISE = re.compile(r"<[a-zA-Z][^\t\n\f\r />]*")


def strict(manifest: Optional[dict]) -> bool:
    """Le mode strict s'applique-t-il ? Dès qu'un rôle déclare `sections`, sauf
    `strict_sections: false`."""
    m = manifest or {}
    if m.get("strict_sections") is False:
        return False
    return any("sections" in (r or {}) for r in (m.get("roles") or {}).values())


def _commun(attrs: list[tuple[str, str]]) -> bool:
    occ = _noms(attrs)
    return bool(occ) and all(COMMUN in x for x in occ)


def non_classes(html: Optional[str]) -> list[str]:
    """Ce qui partirait chez TOUT lecteur sans être classé : chaque `<script>` / `<style>`
    et chaque balise à gestionnaire `on…=` hors de tout élément marqué d'une section,
    qui ne porte pas `data-oto-section="*"`. Lu sur ce qui RESTE une fois retirés tous
    les éléments marqués d'une section (le commun gardé). Pris au large : un faux
    positif refuse (l'auteur reclasse), un oubli fuirait."""
    if not html:
        return []
    reste = retirer_avec(html, lambda occ: all(COMMUN in x for x in occ)) if _MARQUE.search(html) else html
    ligne = lambda pos: reste.count("\n", 0, pos) + 1  # noqa: E731
    out: list[str] = []
    i = 0
    while len(out) < MAX_AVERTISSEMENTS:
        m = _TOUTE_BALISE.search(reste, i)
        if m is None:
            break
        b = _balise(reste, m.start(), False)
        if b is None:
            # Une balise que le texte coupe : on ne sait pas ce qu'elle porte — refusé.
            out.append(f"Line {ligne(m.start())}: a tag is never closed (`>`): "
                       "it can't be classified.")
            break
        fin, nom, attrs = b[0], b[1], b[2]
        commun = _commun(attrs)
        if nom in ("script", "style") and not commun:
            out.append(f"Line {ligne(m.start())}: <{nom}> is in no section: it ships to "
                       f"every viewer. Mark it {ATTRIBUT}=\"<section>\", or "
                       f"{ATTRIBUT}=\"{COMMUN}\" if every role may read it.")
        elif not commun and any(_bas(k).startswith("on") for k, _ in attrs):
            out.append(f"Line {ligne(m.start())}: an on… handler on <{nom}> is in no "
                       f"section: it ships to every viewer. Move it into a script, or mark "
                       f"the element {ATTRIBUT}=\"{COMMUN}\".")
        i = fin
        if nom in BRUTS:
            # Le texte d'un script ou d'une feuille n'est pas du balisage : sauté.
            k = _fin_brut(reste, fin, nom)
            i = k if k >= 0 else len(reste)
    return out


def noms_dans_le_commun(manifest: dict, html: Optional[str]) -> list[str]:
    """Avertissements : une section nommée en clair dans un script qui part chez tous
    (commun ou non marqué) — sa logique part avec lui."""
    roles = (manifest or {}).get("roles") or {}
    noms = {x for r in roles.values() for x in (r or {}).get("sections") or []} - {COMMUN}
    if not html or not noms:
        return []
    reste = retirer_avec(html, lambda occ: all(COMMUN in x for x in occ)) if _MARQUE.search(html) else html
    out = []
    for x in sorted(noms):
        m = re.search(r"""(["'`])""" + re.escape(x) + r"\1", reste)
        if m:
            out.append(f'Line {reste.count(chr(10), 0, m.start()) + 1}: section "{x}" is named '
                       "in markup or script every viewer gets: whatever that code does for it "
                       f'ships to all. Move it into a <script {ATTRIBUT}="{x}">.')
    return out[:MAX_AVERTISSEMENTS]
