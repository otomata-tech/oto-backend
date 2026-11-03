"""Text plumbing of the loop: a served tool result as text for the model, the announced
cut, the `<next>…</next>` chips taken out of the streamed answer, and the final answer
as plain prose for the voice."""
from __future__ import annotations

import json
import re

#: A tool result is cut here, and the model is TOLD so — never a silent fragment.
#: Every character kept is written to the prompt cache once (1.25 × the input price) and
#: read back at every later call of the conversation. Measured on stored conversations
#: (30 days): tool results are ~86 % of the replayed history, and the ~13 % of results
#: longer than 20 000 characters carry ~77 % of the characters of all results. This was
#: 80 000; at 20 000 the history shrinks by about 40 %.
PLAFOND_RESULTAT = 20_000
#: Tools whose result is PROSE read in one piece and that offer no way to read a part of it
#: (`oto_procedure op=get` has no section, field or outline; `oto_guide op=read` neither):
#: cut at 20 000 the model would hold half a procedure or half a guide with no way to ask
#: for the rest. They keep the old limit.
PLAFONDS_PAR_OUTIL = {"oto_procedure": 80_000, "oto_guide": 80_000}


def plafond_de(outil: str) -> int:
    """The cut for the result of `outil` (the TARGET tool, `oto_call` already unwrapped)."""
    return PLAFONDS_PAR_OUTIL.get(outil, PLAFOND_RESULTAT)
_OUVRANT = "<next>"


def texte_du_resultat(result) -> str:
    """A served result (`ToolResult`, or anything JSON-able) as the text the model reads:
    its text contents, else its structured content."""
    contenu = getattr(result, "content", None)
    if isinstance(contenu, list):
        parts = [getattr(c, "text", None) for c in contenu]
        parts = [p for p in parts if isinstance(p, str)]
        if parts:
            return "\n".join(parts)
        structure = getattr(result, "structured_content", None)
        if structure is not None:
            return json.dumps(structure, ensure_ascii=False, default=str)
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


_INDICE = "narrow the call (filter, fields, limit) to see the rest"
#: Room left for the `_cut` notice when a JSON list is cut by items.
_MARGE = 300


def _compact(x) -> str:
    """JSON re-written for the model: no space after `,` and `:` (the same data, fewer
    tokens)."""
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)


def _garder(items: list, budget: int):
    """The leading items that fit `budget` characters, and how many were left out;
    `(None, 0)` when none fits (one huge item) or all fit (nothing to cut)."""
    if budget <= 0:
        return None, 0
    total = n = 0
    for it in items:
        t = len(_compact(it)) + 1
        if total + t > budget:
            break
        total += t
        n += 1
    if n == 0 or n == len(items):
        return None, 0
    return items[:n], len(items) - n


def _couper_en_elements(texte: str, plafond: int):
    """A result that is a JSON list (or an object carrying a long list) is cut by WHOLE
    items: still valid JSON, and the model is told how many it does not have. `None` when
    the text is not that shape or no item boundary helps — the caller cuts characters."""
    try:
        obj = json.loads(texte)
    except (ValueError, TypeError):
        return None
    compact = _compact(obj)
    if len(compact) <= plafond:
        return compact
    if isinstance(obj, list):
        garde, reste = _garder(obj, plafond - _MARGE)
        if garde is None:
            return None
        return _compact([*garde, {"_cut": reste, "_hint": _INDICE}])
    if isinstance(obj, dict):
        listes = {k: v for k, v in obj.items() if isinstance(v, list) and len(v) > 1}
        if not listes:
            return None
        cle = max(listes, key=lambda k: len(_compact(listes[k])))
        autres = len(_compact({k: v for k, v in obj.items() if k != cle}))
        garde, reste = _garder(obj[cle], plafond - _MARGE - autres)
        if garde is None:
            return None
        return _compact({**obj, cle: garde, "_cut": {cle: reste}, "_hint": _INDICE})
    return None


#: Past this size a result is not re-parsed to be lightened: the cut handles it.
_ALLEGER_MAX = 2_000_000
_VIDES = (None, "", [], {})


def _sans_vides(x, profondeur: int = 0):
    """In every list of at least three rows (objects), drops the keys whose value is
    null, "" , [] or {}. Zero and false stay: they say something."""
    if profondeur > 3:
        return x
    if isinstance(x, list):
        lignes = [r for r in x if isinstance(r, dict)]
        if len(x) >= 3 and len(lignes) == len(x):
            return [{k: _sans_vides(v, profondeur + 1) for k, v in r.items()
                     if not any(v is vide or (type(v) is type(vide) and v == vide)
                                for vide in _VIDES)} for r in x]
        return [_sans_vides(r, profondeur + 1) for r in x]
    if isinstance(x, dict):
        return {k: _sans_vides(v, profondeur + 1) for k, v in x.items()}
    return x


def alleger(texte: str) -> str:
    """The same JSON for the model with fewer tokens: no space after `,` and `:`, and, in
    lists of rows, no empty keys. Text that is not a JSON object or list is returned as
    it came. Reads only: the caller does not use it on what a write answered."""
    if not texte or len(texte) > _ALLEGER_MAX or texte[0] not in "[{":
        return texte
    try:
        obj = json.loads(texte)
    except (ValueError, TypeError):
        return texte
    return _compact(_sans_vides(obj))


def couper(texte: str, plafond: int = PLAFOND_RESULTAT) -> str:
    """At most `plafond` characters, and the cut said in words to the model. A JSON list
    is cut by whole items (valid JSON, the count of what is missing); anything else by
    characters."""
    if len(texte) <= plafond:
        return texte or "(empty)"
    par_elements = _couper_en_elements(texte, plafond)
    if par_elements is not None:
        return par_elements
    reste = len(texte) - plafond
    return (f"{texte[:plafond]}\n[cut: {reste} more characters — {_INDICE}]")


class FiltreSuite:
    """The streamed answer minus a trailing `<next>a | b | c</next>` block, which becomes
    chips. What might be the start of the tag is held back until it can't be; nothing of
    the block ever reaches the screen."""

    def __init__(self, sortie):
        self._sortie = sortie
        self._retenu = ""
        self._dedans = False
        self._capte = ""

    def pousser(self, texte: str) -> None:
        if self._dedans:
            self._capte += texte
            return
        self._retenu += texte
        at = self._retenu.find(_OUVRANT)
        if at >= 0:
            if at > 0:
                self._sortie(self._retenu[:at])
            self._capte = self._retenu[at + len(_OUVRANT):]
            self._retenu = ""
            self._dedans = True
            return
        garde = 0
        for k in range(min(len(_OUVRANT) - 1, len(self._retenu)), 0, -1):
            if _OUVRANT.startswith(self._retenu[-k:]):
                garde = k
                break
        if len(self._retenu) > garde:
            self._sortie(self._retenu[:len(self._retenu) - garde])
            self._retenu = self._retenu[len(self._retenu) - garde:]

    def finir(self) -> list[str]:
        """Flushes what was held and returns up to three suggestions."""
        if not self._dedans:
            if self._retenu:
                self._sortie(self._retenu)
                self._retenu = ""
            return []
        corps = self._capte.split("</next>", 1)[0]
        items = []
        for morceau in corps.replace("\n", "|").split("|"):
            m = morceau.strip().lstrip("-* ").strip()
            if m:
                items.append(m)
        return items[:3]


#: The spoken answer is cut here, at the last sentence end before it.
PLAFOND_PAROLE = 1500
_SUITE = re.compile(r"<next>.*?(?:</next>|$)", re.S)
_BLOC_CODE = re.compile(r"```.*?(?:```|$)", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LIEN = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"<?https?://\S+>?")
_CODE = re.compile(r"`([^`]*)`")
_TITRE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_CITATION = re.compile(r"^\s{0,3}>\s?", re.M)
_PUCE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_REGLE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M)
_TABLEAU = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.M)
_EMPHASE = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_FIN_DE_PHRASE = re.compile(r"[.!?…](?:[\"'»)\]]*)(?=\s|$)")


def texte_a_dire(contenu) -> str:
    """The final answer as plain prose for the voice: the assistant message's text
    blocks, without the `<next>` chips, code, links' targets, bare URLs, nor markdown
    marks; at most `PLAFOND_PAROLE` characters, cut at the last sentence end before
    (else at a word). '' when nothing is left to say."""
    if isinstance(contenu, str):
        brut = contenu
    else:
        brut = "\n".join(b.get("text") or "" for b in contenu or []
                         if isinstance(b, dict) and b.get("type") == "text")
    t = _SUITE.sub(" ", brut)
    t = _BLOC_CODE.sub(" ", t)
    t = _IMAGE.sub(r"\1", t)
    t = _LIEN.sub(r"\1", t)
    t = _URL.sub(" ", t)
    t = _CODE.sub(r"\1", t)
    t = _REGLE.sub(" ", t)
    t = _TABLEAU.sub(" ", t)
    t = _TITRE.sub("", t)
    t = _CITATION.sub("", t)
    t = _PUCE.sub("", t)
    for _ in range(2):
        t = _EMPHASE.sub(r"\2", t)
    t = t.replace("|", " ")
    # A line that ends without punctuation (a title, a list item) still reads as a pause.
    lignes = [l.strip() for l in t.splitlines() if l.strip()]
    t = " ".join(l if re.search(r"[.!?:;…,]$", l) else f"{l}." for l in lignes)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= PLAFOND_PAROLE:
        return t
    tete = t[:PLAFOND_PAROLE]
    fins = [m.end() for m in _FIN_DE_PHRASE.finditer(tete)]
    if fins and fins[-1] >= PLAFOND_PAROLE // 3:
        return tete[:fins[-1]].strip()
    return tete.rsplit(" ", 1)[0].strip()
