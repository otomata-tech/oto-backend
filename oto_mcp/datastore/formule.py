"""Colonnes CALCULÉES par une formule OpenFormula (ISO/IEC 26300), déclarée dans le
schéma d'un tableau (oto-backend#1008).

Un sous-ensemble FERMÉ de fonctions : `IFS`, `IF`, `SWITCH`, `AND`, `OR`, `NOT`,
`LEFT`, `MID`, `LEN`, `TRUE`, `FALSE`, et les comparaisons (`= <> < > <= >=`). Toute
autre fonction est refusée À LA POSE de la formule (`definition.py`), en la nommant.

**Analyseur écrit pour cette grammaire précise — jamais `eval()`, jamais une lib de
formules généraliste.** Une lib tierce (`formulas`, `pycel`…) supporterait bien plus
que ce sous-ensemble, donc une surface non maîtrisée sur un chemin d'ÉCRITURE ; un
`eval()` sur une chaîne fournie par un tiers n'est simplement pas une option. La
grammaire fermée EST la garantie de sécurité : une fonction hors liste ne peut pas
s'exécuter, elle ne peut même pas se PARSER.

Une formule est une fonction PURE de la ligne qui la porte : elle ne lit QUE les
colonnes déclarées de SA PROPRE ligne (jamais un agrégat, jamais une autre ligne) —
c'est une propriété de la grammaire elle-même : aucune fonction du sous-ensemble ne
sait référencer autre chose qu'un champ nu de l'environnement d'évaluation. Une
formule ne peut pas non plus référencer une autre colonne qui est ELLE-MÊME une
formule (pas de chaînage) — vérifié à la pose, pas ici.

**Plage sur une colonne `list, of: {fields: [...]}`** (oto-backend#1008 v2) :
`contacts[].telephone` lit la valeur du sous-champ `telephone` de CHAQUE élément de
la colonne-liste `contacts` de la ligne — une PLAGE de valeurs, jamais un élément
isolé (pas d'index). Notation `champ[].sous_champ` reprise TELLE QUELLE du reste du
repo (`hors_schema.py`/`vocabulaire.py`/`effacements.py`/`validation.py` la
portent déjà pour la même notion) plutôt qu'un point nu — un point nu désignerait
une COUCHE (`champ.origine`/`champ.comment`), notion sans rapport. Une plage ne se
consomme QU'À L'INTÉRIEUR de `COUNTA(...)` — l'utiliser ailleurs (comparaison,
argument d'une autre fonction) est refusé à la pose, jamais silencieusement coercé.

La PROVENANCE (`<champ>.comment`) est dérivée automatiquement de l'AST : la branche
`IFS` gagnante est re-sérialisée vers sa forme OpenFormula canonique, jamais un label
écrit à la main.
"""
from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any, Optional, Union


class FormulaError(ValueError):
    """Une formule invalide — texte actionnable, nommant le problème exact."""


# ── AST ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Litteral:
    valeur: Union[str, bool, int]


@dataclass(frozen=True)
class Champ:
    nom: str


@dataclass(frozen=True)
class Plage:
    """`colonne[].sous_champ` — les valeurs de `sous_champ` sur chaque élément de
    la colonne-liste `colonne` de la ligne. Ne se consomme qu'en argument de
    `COUNTA` (vérifié à la pose, cf. `_verifier_usage_plages`)."""
    colonne: str
    sous_champ: str


@dataclass(frozen=True)
class Comparaison:
    op: str  # "=" "<>" "<" ">" "<=" ">="
    gauche: "Noeud"
    droite: "Noeud"


@dataclass(frozen=True)
class Appel:
    fonction: str  # nom canonique, MAJUSCULES
    args: tuple["Noeud", ...] = _dc_field(default_factory=tuple)


Noeud = Union[Litteral, Champ, Plage, Comparaison, Appel]


# ── Fonctions autorisées ─────────────────────────────────────────────────────

#: Le sous-ensemble FERMÉ — rien d'autre ne parse. Nom OpenFormula -> arité
#: (None = variadique).
FONCTIONS_AUTORISEES: dict[str, Optional[int]] = {
    "IFS": None, "IF": 3, "SWITCH": None, "AND": None, "OR": None, "NOT": 1,
    "LEFT": 2, "MID": 3, "LEN": 1, "TRUE": 0, "FALSE": 0, "COUNTA": 1,
}

_COMPARATEURS = ("<=", ">=", "<>", "=", "<", ">")


# ── Tokenizer ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Token:
    type: str  # "IDENT" "STR" "OP" "LPAREN" "RPAREN" "SEP" "LBRACKET" "RBRACKET" "DOT" "EOF"
    valeur: str


def _tokenize(texte: str) -> list[_Token]:
    out: list[_Token] = []
    i, n = 0, len(texte)
    while i < n:
        c = texte[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and texte[j] != '"':
                buf.append(texte[j])
                j += 1
            if j >= n:
                raise FormulaError(
                    f"chaîne non terminée (guillemet ouvrant à la position {i}) — "
                    "il manque un `\"` fermant.")
            out.append(_Token("STR", "".join(buf)))
            i = j + 1
            continue
        if c == "(":
            out.append(_Token("LPAREN", c)); i += 1; continue
        if c == ")":
            out.append(_Token("RPAREN", c)); i += 1; continue
        if c == ";":
            out.append(_Token("SEP", c)); i += 1; continue
        if c == "[":
            out.append(_Token("LBRACKET", c)); i += 1; continue
        if c == "]":
            out.append(_Token("RBRACKET", c)); i += 1; continue
        if c == ".":
            out.append(_Token("DOT", c)); i += 1; continue
        if c.isdigit():
            j = i
            while j < n and texte[j].isdigit():
                j += 1
            out.append(_Token("NUM", texte[i:j]))
            i = j
            continue
        matched = None
        for comp in _COMPARATEURS:
            if texte.startswith(comp, i):
                matched = comp
                break
        if matched:
            out.append(_Token("OP", matched)); i += len(matched); continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (texte[j].isalnum() or texte[j] == "_"):
                j += 1
            out.append(_Token("IDENT", texte[i:j]))
            i = j
            continue
        raise FormulaError(
            f"caractère inattendu {c!r} à la position {i} — une formule ne porte que "
            "des noms de fonction/colonne, des chaînes entre guillemets, `( ) ; [ ] .` "
            "et les comparateurs = <> < > <= >=.")
    out.append(_Token("EOF", ""))
    return out


# ── Parseur récursif-descendant ─────────────────────────────────────────────
#
# Grammaire (précédence croissante) :
#   expr        := comparaison
#   comparaison := primaire (COMPARATEUR primaire)?
#   primaire    := STR | NUM | appel | plage | champ
#   appel       := IDENT "(" (expr (";" expr)*)? ")"
#   plage       := IDENT "[" "]" "." IDENT    # colonne[].sous_champ
#   champ       := IDENT                      # si pas suivi de "(" ni de "["

class _Parseur:
    def __init__(self, tokens: list[_Token]):
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _avancer(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _attendre(self, type_: str) -> _Token:
        tok = self._peek()
        if tok.type != type_:
            raise FormulaError(
                f"attendu {type_}, trouvé {tok.type} ({tok.valeur!r}) — vérifie les "
                "parenthèses et les séparateurs `;` de la formule.")
        return self._avancer()

    def parser(self) -> Noeud:
        noeud = self._expr()
        self._attendre("EOF")
        return noeud

    def _expr(self) -> Noeud:
        return self._comparaison()

    def _comparaison(self) -> Noeud:
        gauche = self._primaire()
        tok = self._peek()
        if tok.type == "OP":
            op = self._avancer().valeur
            droite = self._primaire()
            return Comparaison(op=op, gauche=gauche, droite=droite)
        return gauche

    def _primaire(self) -> Noeud:
        tok = self._peek()
        if tok.type == "STR":
            self._avancer()
            return Litteral(valeur=tok.valeur)
        if tok.type == "NUM":
            self._avancer()
            return Litteral(valeur=int(tok.valeur))
        if tok.type == "IDENT":
            nom = tok.valeur
            nom_maj = nom.upper()
            suivant = self._tokens[self._pos + 1] if self._pos + 1 < len(self._tokens) else None
            if suivant is not None and suivant.type == "LPAREN":
                self._avancer()  # IDENT
                self._avancer()  # LPAREN
                if nom_maj not in FONCTIONS_AUTORISEES:
                    raise FormulaError(
                        f"fonction inconnue `{nom}` — le sous-ensemble autorisé est "
                        f"{', '.join(sorted(FONCTIONS_AUTORISEES))}. Aucune autre "
                        "fonction n'est acceptée.")
                args: list[Noeud] = []
                if self._peek().type != "RPAREN":
                    args.append(self._expr())
                    while self._peek().type == "SEP":
                        self._avancer()
                        args.append(self._expr())
                self._attendre("RPAREN")
                arite = FONCTIONS_AUTORISEES[nom_maj]
                if arite is not None and len(args) != arite:
                    raise FormulaError(
                        f"`{nom_maj}` attend {arite} argument(s), reçu {len(args)}.")
                return Appel(fonction=nom_maj, args=tuple(args))
            if suivant is not None and suivant.type == "LBRACKET":
                self._avancer()  # IDENT (la colonne-liste)
                self._avancer()  # LBRACKET
                self._attendre("RBRACKET")
                self._attendre("DOT")
                sous = self._attendre("IDENT")
                return Plage(colonne=nom, sous_champ=sous.valeur)
            self._avancer()
            return Champ(nom=nom)
        raise FormulaError(
            f"jeton inattendu {tok.type} ({tok.valeur!r}) — une expression commence "
            "par une chaîne entre guillemets, un nom de fonction suivi de `(`, ou un "
            "nom de colonne nu.")


def parse(texte: str) -> Noeud:
    """Parse une formule OpenFormula (texte, sans le `=` d'en-tête s'il est présent)
    vers son AST. Lève `FormulaError` avec un message actionnable — jamais un
    `eval()`, jamais un parseur généraliste."""
    if not isinstance(texte, str) or not texte.strip():
        raise FormulaError("formule vide — attendu une expression OpenFormula.")
    t = texte.strip()
    if t.startswith("="):
        t = t[1:]
    return _Parseur(_tokenize(t)).parser()


# ── Validation à la POSE ─────────────────────────────────────────────────────

def _champs_references(noeud: Noeud, out: set) -> None:
    if isinstance(noeud, Champ):
        out.add(noeud.nom)
    elif isinstance(noeud, Plage):
        # La colonne-LISTE porteuse est une référence comme une autre (« colonne
        # inconnue », déclenchement du recalcul à l'écriture — cf. docstring de
        # `champs_references`) ; le sous-champ, lui, n'est PAS un nom de colonne
        # de la ligne, il n'est jamais ajouté ici.
        out.add(noeud.colonne)
    elif isinstance(noeud, Comparaison):
        _champs_references(noeud.gauche, out)
        _champs_references(noeud.droite, out)
    elif isinstance(noeud, Appel):
        for a in noeud.args:
            _champs_references(a, out)


def _plages(noeud: Noeud, out: list) -> None:
    """Toutes les `Plage` de l'AST, DANS L'ORDRE — sert la validation à la pose
    (colonne list valide, sous-champ déclaré, usage restreint à `COUNTA`)."""
    if isinstance(noeud, Plage):
        out.append(noeud)
    elif isinstance(noeud, Comparaison):
        _plages(noeud.gauche, out)
        _plages(noeud.droite, out)
    elif isinstance(noeud, Appel):
        for a in noeud.args:
            _plages(a, out)


def champs_references(noeud: Noeud) -> set:
    """Les colonnes (nues) référencées par une formule — sert à la fois le refus
    « colonne inconnue » à la pose et le déclenchement du recalcul à l'écriture
    (une formule ne se recalcule que si une des colonnes qu'elle référence a
    changé)."""
    out: set = set()
    _champs_references(noeud, out)
    return out


def _verifier_usage_plages(noeud: Noeud, dans_counta_arg0: bool = False) -> None:
    """Une `Plage` (`col[].sous_champ`) ne se consomme QU'en argument de `COUNTA` —
    ailleurs (comparaison directe, argument d'une autre fonction), sa valeur serait
    une LISTE Python et non un scalaire : un comportement silencieusement faux
    (ex. une comparaison `=` toujours fausse) plutôt qu'un refus nommé. Vérifié ici,
    à la pose — pas laissé à l'évaluateur, qui ne verrait le problème qu'à la
    première ligne évaluée."""
    if isinstance(noeud, Plage):
        if not dans_counta_arg0:
            raise FormulaError(
                f"`{noeud.colonne}[].{noeud.sous_champ}` (une plage) ne peut être "
                "utilisée qu'en argument de COUNTA(...) — ailleurs, ce n'est pas "
                "une valeur scalaire.")
        return
    if isinstance(noeud, Appel):
        if noeud.fonction == "COUNTA" and not isinstance(noeud.args[0], Plage):
            raise FormulaError(
                "COUNTA(...) n'attend qu'une plage (`colonne[].sous_champ`) — "
                f"reçu {serialiser(noeud.args[0])!r}.")
        for k, a in enumerate(noeud.args):
            est_counta_arg0 = noeud.fonction == "COUNTA" and k == 0
            _verifier_usage_plages(a, dans_counta_arg0=est_counta_arg0)
        return
    if isinstance(noeud, Comparaison):
        _verifier_usage_plages(noeud.gauche)
        _verifier_usage_plages(noeud.droite)


def _valider_plages(plages: list, champs_def: list) -> None:
    """Une `Plage` doit référencer une colonne DÉCLARÉE `type: "list"` dont les
    éléments sont des objets (`of: {"fields": [...]}`), et un sous-champ DÉCLARÉ
    dans `of.fields` — trois refus distincts, chacun nommé."""
    par_cle = {f["key"]: f for f in champs_def
               if isinstance(f, dict) and isinstance(f.get("key"), str)}
    for p in plages:
        f = par_cle.get(p.colonne)
        if f is None:
            raise FormulaError(
                f"`{p.colonne}[]` référence une colonne inconnue — une plage ne "
                "lit qu'une colonne DÉCLARÉE du même tableau.")
        if f.get("type") != "list":
            raise FormulaError(
                f"`{p.colonne}[]` : `{p.colonne}` n'est pas de type `list` "
                f"(type déclaré : {f.get('type')!r}) — une plage ne s'écrit que "
                "sur une colonne-liste.")
        of = f.get("of")
        sous_champs = (of.get("fields") if isinstance(of, dict) else None) or []
        cles_sous_champs = {sf["key"] for sf in sous_champs
                             if isinstance(sf, dict) and isinstance(sf.get("key"), str)}
        if not cles_sous_champs:
            raise FormulaError(
                f"`{p.colonne}[].{p.sous_champ}` : `{p.colonne}` n'est pas une "
                "liste D'OBJETS (`of: {fields: [...]}`) — une plage lit un "
                "sous-champ, il en faut un à déclarer.")
        if p.sous_champ not in cles_sous_champs:
            raise FormulaError(
                f"`{p.colonne}[].{p.sous_champ}` : `{p.sous_champ}` n'est pas un "
                f"sous-champ déclaré de `{p.colonne}` — sous-champs connus : "
                f"{', '.join(sorted(cles_sous_champs))}.")


def valider(texte: str, colonnes_declarees: set, colonnes_formule: set,
            champs_def: Optional[list] = None) -> Noeud:
    """Parse ET valide une formule contre le schéma qui la porte. Lève
    `FormulaError` nommant précisément le problème — fonction inconnue (déjà géré
    par `parse`), colonne inconnue, chaînage (référence à une AUTRE colonne
    formule, interdit v1), ou plage invalide (colonne pas de type `list`,
    sous-champ non déclaré, usage hors `COUNTA`).

    `colonnes_declarees` = l'ensemble des clés de colonne du schéma (formule
    comprise). `colonnes_formule` = le sous-ensemble qui est LUI-MÊME une formule
    (pour détecter le chaînage). `champs_def` = la liste des field-def COMPLÈTES
    du schéma (pas seulement leurs clés) — nécessaire pour valider une `Plage`
    contre la forme déclarée de la colonne-liste ; omis (`None`), aucune plage
    n'est vérifiée en profondeur (mais son usage hors COUNTA l'est toujours)."""
    noeud = parse(texte)
    refs = champs_references(noeud)
    inconnues = refs - colonnes_declarees
    if inconnues:
        raise FormulaError(
            f"colonne(s) inconnue(s) référencée(s) par la formule : "
            f"{', '.join(sorted(inconnues))} — une formule ne lit que les colonnes "
            "DÉCLARÉES du même tableau.")
    chainees = refs & colonnes_formule
    if chainees:
        raise FormulaError(
            f"chaînage refusé : {', '.join(sorted(chainees))} — une formule ne peut "
            "pas référencer une autre colonne CALCULÉE (v1 : fonction pure sur des "
            "colonnes d'entrée uniquement).")
    _verifier_usage_plages(noeud)
    plages: list = []
    _plages(noeud, plages)
    if plages and champs_def is not None:
        _valider_plages(plages, champs_def)
    _valider_ifs_couvrant(noeud)
    return noeud


def _valider_ifs_couvrant(noeud: Noeud) -> None:
    """Un `IFS` de tête sans branche par défaut couvrante (`TRUE()`) est accepté
    (OpenFormula standard : aucune condition vraie -> erreur #N/A à l'évaluation),
    mais SIGNALÉ ici n'est pas la bonne réponse pour un refus à la pose — on ne
    connaît pas encore les données. On se contente donc de ne RIEN refuser sur ce
    point (posture documentée : la couverture est la responsabilité de l'auteur de
    la formule, comme dans un vrai tableur)."""
    return None


# ── Évaluation ────────────────────────────────────────────────────────────

class _Vide:
    """Marqueur : la valeur de la ligne pour ce champ est absente/vide — distinct
    de la chaîne vide `""` explicite posée par une formule."""


_VIDE = _Vide()


def _valeur_champ(row: dict, nom: str) -> str:
    v = row.get(nom)
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def _valeurs_plage(noeud: "Plage", row: dict) -> list:
    """Les valeurs du sous-champ `noeud.sous_champ` sur chaque élément de la
    colonne-liste `noeud.colonne` de `row` — une LISTE Python, jamais un scalaire.
    Défensif : une colonne absente/pas une liste rend une plage vide plutôt que de
    lever (la validation à la pose a déjà refusé ce cas ; ici on ne casse jamais
    une écriture pour une formule posée avant un durcissement de la validation)."""
    valeur = row.get(noeud.colonne)
    if not isinstance(valeur, list):
        return []
    out = []
    for element in valeur:
        if isinstance(element, dict):
            out.append(_valeur_champ(element, noeud.sous_champ))
        else:
            out.append("")
    return out


def _evaluer(noeud: Noeud, row: dict) -> Any:
    if isinstance(noeud, Litteral):
        return noeud.valeur
    if isinstance(noeud, Champ):
        return _valeur_champ(row, noeud.nom)
    if isinstance(noeud, Plage):
        # Ne devrait être atteint qu'en argument direct de COUNTA (vérifié à la
        # pose) — `_appeler("COUNTA", …)` évalue la plage lui-même sans repasser
        # ici ; ce chemin ne sert que si une formule antérieure au durcissement
        # de la validation l'a laissée passer ailleurs (défensif, jamais une
        # liste rendue comme valeur de colonne).
        return _valeurs_plage(noeud, row)
    if isinstance(noeud, Comparaison):
        g, d = _evaluer(noeud.gauche, row), _evaluer(noeud.droite, row)
        if noeud.op == "=":
            return g == d
        if noeud.op == "<>":
            return g != d
        if noeud.op == "<":
            return g < d
        if noeud.op == ">":
            return g > d
        if noeud.op == "<=":
            return g <= d
        if noeud.op == ">=":
            return g >= d
        raise FormulaError(f"comparateur inconnu {noeud.op!r}")  # pragma: no cover
    if isinstance(noeud, Appel):
        return _appeler(noeud, row)
    raise FormulaError(f"nœud AST inconnu {noeud!r}")  # pragma: no cover


def _appeler(appel: Appel, row: dict) -> Any:
    fn, args = appel.fonction, appel.args
    if fn == "TRUE":
        return True
    if fn == "FALSE":
        return False
    if fn == "AND":
        return all(bool(_evaluer(a, row)) for a in args)
    if fn == "OR":
        return any(bool(_evaluer(a, row)) for a in args)
    if fn == "NOT":
        return not bool(_evaluer(args[0], row))
    if fn == "LEFT":
        texte = str(_evaluer(args[0], row))
        n = int(_evaluer(args[1], row))
        return texte[:max(n, 0)]
    if fn == "MID":
        texte = str(_evaluer(args[0], row))
        debut = int(_evaluer(args[1], row))
        longueur = int(_evaluer(args[2], row))
        # OpenFormula/Excel : `debut` est 1-indexé.
        start = max(debut - 1, 0)
        return texte[start:start + max(longueur, 0)]
    if fn == "LEN":
        return len(str(_evaluer(args[0], row)))
    if fn == "COUNTA":
        # `args[0]` est TOUJOURS une `Plage` ici — `valider()` refuse à la pose
        # tout autre argument (cf. `_verifier_usage_plages`).
        valeurs = _valeurs_plage(args[0], row)
        return sum(1 for v in valeurs if v != "")
    if fn == "IF":
        cond, alors, sinon = args
        return _evaluer(alors, row) if bool(_evaluer(cond, row)) else _evaluer(sinon, row)
    if fn == "IFS":
        if len(args) % 2 != 0:
            raise FormulaError("IFS attend un nombre PAIR d'arguments "
                               "(cond;val;cond;val;…).")
        for k in range(0, len(args), 2):
            if bool(_evaluer(args[k], row)):
                return _evaluer(args[k + 1], row)
        return ""  # aucune condition vraie — pas de #N/A, une colonne vide plutôt
    if fn == "SWITCH":
        expr = _evaluer(args[0], row)
        reste = args[1:]
        i = 0
        while i + 1 < len(reste):
            if _evaluer(reste[i], row) == expr:
                return _evaluer(reste[i + 1], row)
            i += 2
        if i < len(reste):  # défaut impair final
            return _evaluer(reste[i], row)
        return ""
    raise FormulaError(f"fonction non évaluable {fn!r}")  # pragma: no cover — parse() l'a déjà refusée


# ── Provenance : re-sérialisation canonique de l'AST ────────────────────────

def serialiser(noeud: Noeud) -> str:
    """Re-sérialise un AST vers sa forme OpenFormula canonique — c'est CE texte
    qui devient la provenance (`<champ>.comment`) de la branche IFS gagnante. Pure,
    déterministe : deux appels sur le même AST rendent le même texte."""
    if isinstance(noeud, Litteral):
        if isinstance(noeud.valeur, bool):
            return "TRUE()" if noeud.valeur else "FALSE()"
        if isinstance(noeud.valeur, int):
            return str(noeud.valeur)
        return f'"{noeud.valeur}"'
    if isinstance(noeud, Champ):
        return noeud.nom
    if isinstance(noeud, Plage):
        return f"{noeud.colonne}[].{noeud.sous_champ}"
    if isinstance(noeud, Comparaison):
        return f"{serialiser(noeud.gauche)}{noeud.op}{serialiser(noeud.droite)}"
    if isinstance(noeud, Appel):
        return f"{noeud.fonction}({';'.join(serialiser(a) for a in noeud.args)})"
    raise FormulaError(f"nœud AST inconnu {noeud!r}")  # pragma: no cover


def _compte_counta(noeud: Noeud, row: dict) -> Optional[int]:
    """Le premier `COUNTA(plage)` trouvé dans `noeud` (parcours en profondeur),
    déjà évalué contre `row` — `None` si la condition n'en contient aucun. Sert
    la provenance : « combien de valeurs ont été trouvées » pour une branche
    gagnante qui teste une plage."""
    if isinstance(noeud, Appel):
        if noeud.fonction == "COUNTA":
            return len([v for v in _valeurs_plage(noeud.args[0], row) if v != ""])
        for a in noeud.args:
            trouve = _compte_counta(a, row)
            if trouve is not None:
                return trouve
        return None
    if isinstance(noeud, Comparaison):
        trouve = _compte_counta(noeud.gauche, row)
        return trouve if trouve is not None else _compte_counta(noeud.droite, row)
    return None


def _provenance_ifs(args: tuple[Noeud, ...], row: dict) -> Optional[tuple[Any, str]]:
    """Pour un `IFS` DE TÊTE : rend (valeur, provenance) de la branche gagnante, ou
    `None` si ce n'est pas un IFS. La provenance = la condition gagnante re-
    sérialisée, + si la valeur vient d'un SWITCH, la paire clé→résultat retenue."""
    if len(args) % 2 != 0:
        raise FormulaError("IFS attend un nombre PAIR d'arguments "
                           "(cond;val;cond;val;…).")
    for k in range(0, len(args), 2):
        cond = args[k]
        if bool(_evaluer(cond, row)):
            valeur_noeud = args[k + 1]
            valeur = _evaluer(valeur_noeud, row)
            prov = serialiser(cond)
            if isinstance(valeur_noeud, Appel) and valeur_noeud.fonction == "SWITCH":
                expr_val = _evaluer(valeur_noeud.args[0], row)
                prov += f" → {expr_val!r} → {valeur!r}"
            compte = _compte_counta(cond, row)
            if compte is not None:
                prov += f" — {compte} valeur(s) trouvée(s)"
            return valeur, prov
    return None


def evaluer_avec_provenance(noeud: Noeud, row: dict) -> tuple[Any, Optional[str]]:
    """Évalue la formule contre une ligne, rend `(valeur, provenance)`.
    `provenance` est `None` si la formule de tête n'est pas un `IFS` (rien à
    tracer branche par branche — cas rare, un `SWITCH`/littéral de tête n'a
    qu'une seule issue)."""
    if isinstance(noeud, Appel) and noeud.fonction == "IFS":
        res = _provenance_ifs(noeud.args, row)
        if res is not None:
            return res
        return "", None
    return _evaluer(noeud, row), None


# ── API de haut niveau ───────────────────────────────────────────────────────

def compute_row_formulas(schema: Optional[dict], row: dict) -> dict[str, dict]:
    """Calcule toutes les colonnes `type: "formula"` d'un schéma contre une ligne
    (dict de colonnes -> valeurs, DÉJÀ dépouillées de leurs couches — l'appelant
    fournit la vue « valeur seule »). Rend `{champ: {"valeur": ..., "comment": ...}}`
    — jamais rien sur `origine`, jamais touchée ici. `comment` absent de la sortie
    si la provenance est vide (formule dont la tête n'est pas un IFS)."""
    out: dict[str, dict] = {}
    for f in (schema or {}).get("fields") or []:
        if not isinstance(f, dict) or f.get("type") != "formula":
            continue
        cle = f.get("key")
        texte = f.get("formula")
        if not isinstance(cle, str) or not cle or not isinstance(texte, str):
            continue
        try:
            noeud = parse(texte)
            valeur, provenance = evaluer_avec_provenance(noeud, row)
        except FormulaError:
            # Une formule invalide a déjà été refusée À LA POSE (definition.py) —
            # si on en rencontre une ici quand même (schéma posé avant ce lot,
            # ou incohérence), on ne casse pas l'écriture : la colonne reste
            # simplement non calculée plutôt que de faire échouer toute la ligne.
            continue
        entree: dict = {"valeur": valeur}
        if provenance:
            entree["comment"] = provenance
        out[cle] = entree
    return out


def _formulas_by_key(schema: Optional[dict]) -> dict[str, str]:
    return {f["key"]: f["formula"] for f in (schema or {}).get("fields") or []
            if isinstance(f, dict) and f.get("type") == "formula"
            and isinstance(f.get("key"), str) and isinstance(f.get("formula"), str)}


def formules_neuves_ou_modifiees(avant: Optional[dict], apres: Optional[dict]) -> bool:
    """`True` si `apres` déclare au moins une colonne `type: "formula"` ABSENTE de
    `avant`, ou dont le texte `formula` a changé — le déclencheur du backfill
    (oto-backend#1008) : poser ou modifier une formule recalcule TOUTES les lignes
    existantes, une seule fois, pour ce changement de schéma. Une formule retirée,
    ou re-déclarée à l'identique, ne déclenche rien — `compute_row_formulas` calcule
    de toute façon TOUTES les colonnes formule du schéma courant à chaque passage :
    inutile de cibler la colonne précise, une seule détection suffit à armer le
    recalcul de la ligne entière."""
    anciennes = _formulas_by_key(avant)
    nouvelles = _formulas_by_key(apres)
    return any(nouvelles.get(k) != anciennes.get(k) for k in nouvelles)
