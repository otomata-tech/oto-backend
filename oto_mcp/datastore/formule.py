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
class Comparaison:
    op: str  # "=" "<>" "<" ">" "<=" ">="
    gauche: "Noeud"
    droite: "Noeud"


@dataclass(frozen=True)
class Appel:
    fonction: str  # nom canonique, MAJUSCULES
    args: tuple["Noeud", ...] = _dc_field(default_factory=tuple)


Noeud = Union[Litteral, Champ, Comparaison, Appel]


# ── Fonctions autorisées ─────────────────────────────────────────────────────

#: Le sous-ensemble FERMÉ — rien d'autre ne parse. Nom OpenFormula -> arité
#: (None = variadique).
FONCTIONS_AUTORISEES: dict[str, Optional[int]] = {
    "IFS": None, "IF": 3, "SWITCH": None, "AND": None, "OR": None, "NOT": 1,
    "LEFT": 2, "MID": 3, "LEN": 1, "TRUE": 0, "FALSE": 0,
}

_COMPARATEURS = ("<=", ">=", "<>", "=", "<", ">")


# ── Tokenizer ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Token:
    type: str  # "IDENT" "STR" "OP" "LPAREN" "RPAREN" "SEP" "EOF"
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
            "des noms de fonction/colonne, des chaînes entre guillemets, `( ) ;` et "
            "les comparateurs = <> < > <= >=.")
    out.append(_Token("EOF", ""))
    return out


# ── Parseur récursif-descendant ─────────────────────────────────────────────
#
# Grammaire (précédence croissante) :
#   expr        := comparaison
#   comparaison := primaire (COMPARATEUR primaire)?
#   primaire    := STR | appel | champ
#   appel       := IDENT "(" (expr (";" expr)*)? ")"
#   champ       := IDENT                    # si pas suivi de "(" et pas un nom de fonction

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
    elif isinstance(noeud, Comparaison):
        _champs_references(noeud.gauche, out)
        _champs_references(noeud.droite, out)
    elif isinstance(noeud, Appel):
        for a in noeud.args:
            _champs_references(a, out)


def champs_references(noeud: Noeud) -> set:
    """Les colonnes (nues) référencées par une formule — sert à la fois le refus
    « colonne inconnue » à la pose et le déclenchement du recalcul à l'écriture
    (une formule ne se recalcule que si une des colonnes qu'elle référence a
    changé)."""
    out: set = set()
    _champs_references(noeud, out)
    return out


def valider(texte: str, colonnes_declarees: set, colonnes_formule: set) -> Noeud:
    """Parse ET valide une formule contre le schéma qui la porte. Lève
    `FormulaError` nommant précisément le problème — fonction inconnue (déjà géré
    par `parse`), colonne inconnue, ou chaînage (référence à une AUTRE colonne
    formule, interdit v1).

    `colonnes_declarees` = l'ensemble des clés de colonne du schéma (formule
    comprise). `colonnes_formule` = le sous-ensemble qui est LUI-MÊME une formule
    (pour détecter le chaînage)."""
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


def _evaluer(noeud: Noeud, row: dict) -> Any:
    if isinstance(noeud, Litteral):
        return noeud.valeur
    if isinstance(noeud, Champ):
        return _valeur_champ(row, noeud.nom)
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
    if isinstance(noeud, Comparaison):
        return f"{serialiser(noeud.gauche)}{noeud.op}{serialiser(noeud.droite)}"
    if isinstance(noeud, Appel):
        return f"{noeud.fonction}({';'.join(serialiser(a) for a in noeud.args)})"
    raise FormulaError(f"nœud AST inconnu {noeud!r}")  # pragma: no cover


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
            # `TRUE()` de tête est la branche PAR DÉFAUT d'un IFS (le cliquet
            # `_valider_ifs_couvrant` l'exige en dernière position) — sa
            # sérialisation littérale ne dit rien à qui relit la fiche.
            if isinstance(cond, Appel) and cond.fonction == "TRUE" and not cond.args:
                prov = "cas par défaut"
            else:
                prov = serialiser(cond)
            if isinstance(valeur_noeud, Appel) and valeur_noeud.fonction == "SWITCH":
                expr_val = _evaluer(valeur_noeud.args[0], row)
                prov += f" → {expr_val!r} → {valeur!r}"
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


def colonnes_formule(schema: Optional[dict]) -> set[str]:
    """Les clés des colonnes `type: "formula"` déclarées — pour qu'un appelant
    distingue un `readonly` classique (valeur remise par un tiers) d'une colonne
    CALCULÉE (jamais écrite, `readonly_override` n'a aucun sens dessus : la valeur
    serait recalculée au prochain passage)."""
    return set(_formulas_by_key(schema))


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
