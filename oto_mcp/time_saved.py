"""Le temps qu'un agent fait gagner — estimé depuis ce qu'il a FAIT, sans rien demander.

**Le parti** (page Agents d'un front, 17/10/2026) : aucune saisie, aucun réglage
obligatoire, aucune question à l'utilisateur. Le temps gagné d'un run est la somme
de ses RÉSULTATS × des minutes qu'un humain y passerait. Le type d'un résultat se
déduit de l'outil appelé — de ce que l'outil DÉCLARE déjà (`tools/lecture.py` :
`LECTURE` lit, `ENVOI` agit chez un tiers, le reste écrit), pas d'une table tenue à
la main : un connecteur ajouté demain a son temps gagné le jour même.

**Ce qui compte** — revu sur les agents réels d'une org (17/10/2026), qui
surcomptaient les balayages à vide et les outils multiplexés :
- seuls les runs CONCLUS `done` (un run en échec n'a rien fait gagner) ;
- les APPELS du journal (`tool_calls`, liés au run) : l'`op` de chaque appel, s'il a
  RÉUSSI, et l'enregistrement touché. À défaut de journal pour un run, les
  `tool_counts` du worker, moins précis (ni op, ni succès, ni enregistrement) ;
- une `op` qui LIT (`get`, `list`, `search`…) est une recherche, même sur un outil
  déclaré écrivain : un outil multiplexé ne compte que ses écritures ;
- un même enregistrement ne compte qu'UNE fois par jour et par agent : un balayage
  horaire qui revoit le même contact n'a rien gagné la deuxième fois ;
- la RECHERCHE ne compte que dans un run qui a PRODUIT quelque chose de neuf, et
  plafonnée par run : un balayage qui ne trouve rien n'a rien fait gagner ;
- un run qui a cherché puis seulement POSTÉ (un résumé, une alerte) est un RAPPORT —
  une fois par run, au lieu d'un message + une recherche plafonnée ;
- la tenue de la plateforme ne compte pas (`oto_*`, `run_start`/`run_finish`, les
  lectures et réservations du tableau de l'agent) ;
- un outil que le registre ne connaît pas ne compte pas — on ne devine pas.

**Les minutes par défaut** sont des points de départ, prudents, à confirmer avec des
clients ; une org les surcharge (`oto_org_settings domain=time_saved`, admin),
jamais l'agent un par un.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

#: Les types de résultat, dans l'ordre où l'écran les dit. `minutes` = défaut.
KINDS: dict[str, dict] = {
    "report": {"label": "report posted", "plural": "reports posted", "minutes": 15.0},
    "enrichment": {"label": "contact enriched", "plural": "contacts enriched", "minutes": 3.0},
    "action": {"label": "message or action sent", "plural": "messages or actions sent", "minutes": 4.0},
    "record": {"label": "record written", "plural": "records written", "minutes": 1.5},
    "research": {"label": "research step", "plural": "research steps", "minutes": 2.0},
}

#: La recherche d'UN run ne compte pas plus que ceci, en minutes.
RESEARCH_CAP_PER_RUN = 10.0

#: Bornes d'une surcharge d'org, en minutes par résultat.
OVERRIDE_MIN, OVERRIDE_MAX = 0.0, 240.0

#: La tenue de la plateforme : jamais un résultat.
_IGNORED_EXACT = frozenset({"run_start", "run_finish", "feedback"})
#: Les outils du tableau qui ÉCRIVENT un résultat — les autres `data_*` (lire,
#: réserver, relâcher, schéma) sont la tenue du travail.
_DATA_WRITES = frozenset({"data_write", "data_update_where"})

#: Les `op` qui LISENT, quel que soit ce que l'outil déclare : un outil multiplexé
#: (`<ns>_record op=get|update`) ne doit compter que ses écritures.
READ_OPS = frozenset({
    "get", "list", "search", "find", "read", "lookup", "query", "count", "history",
    "schema", "status", "describe", "preview", "fetch", "show", "inbox", "stats",
    "activities", "info", "check", "match", "resolve", "export", "download",
})

#: Les résultats qui font d'un run un run PRODUCTIF.
_PRODUCTS = frozenset({"enrichment", "action", "record"})


def _effet(name: str) -> Optional[str]:
    from . import tool_registry
    return tool_registry.effet_de(name)


def resolve(name: str) -> Optional[str]:
    """Le nom canonique d'un outil compté. Le worker peut préfixer (`acme_doc` pour
    `oto_doc`, `demo-connecteur_data_claim_next`) : on prend l'outil connu dont le nom
    TERMINE le nom compté, sur une frontière `_` — le plus long, pour qu'un suffixe
    court (`get`) ne gagne jamais sur le vrai nom."""
    from . import tool_registry
    connus = tool_registry.effets_connus()
    if name in connus:
        return name
    meilleur = None
    for n in connus:
        if name.endswith("_" + n) and (meilleur is None or len(n) > len(meilleur)):
            meilleur = n
    return meilleur


def kind_of(name: str, op: Optional[str] = None) -> Optional[str]:
    """Le type de résultat d'un appel à `name` (avec son `op`, connu par le journal),
    ou None quand il ne compte pas."""
    base = resolve(name) or name
    if base in _IGNORED_EXACT or base.startswith("oto_"):
        return None
    if base.startswith("data_"):
        return "record" if base in _DATA_WRITES else None
    effet = _effet(base)
    if effet is None:
        return None
    if "enrich" in base:
        return "enrichment"
    if op and str(op).lower() in READ_OPS:
        return "research"
    return {"read": "research", "act": "action", "write": "record"}.get(effet)


def standards(overrides: Optional[dict]) -> dict[str, float]:
    """Les minutes par type, défauts surchargés par l'org (clés inconnues ignorées)."""
    out = {k: v["minutes"] for k, v in KINDS.items()}
    for k, v in (overrides or {}).items():
        if k in out and isinstance(v, (int, float)):
            out[k] = float(v)
    return out


class Call(NamedTuple):
    """Un appel d'un run, tel que le journal le connaît (`tool_calls`)."""
    tool: str
    op: Optional[str] = None
    ok: bool = True
    #: L'enregistrement touché (id, email, url…) quand l'appel le nomme — None sinon.
    key: Optional[str] = None
    #: Le jour de l'appel, pour « une fois par jour ».
    day: Optional[str] = None


def calls_from_counts(tool_counts: dict) -> list[Call]:
    """Le repli d'un run sans journal : ses `tool_counts`, un appel réussi par compte,
    sans op ni enregistrement."""
    out: list[Call] = []
    for name, n in (tool_counts or {}).items():
        if isinstance(n, int) and n > 0:
            out.extend([Call(str(name))] * n)
    return out


def for_agent(runs: list[list[Call]], minutes: dict[str, float]) -> tuple[float, dict[str, dict]]:
    """(minutes gagnées, `{type: {count, minutes}}`) d'UN agent sur ses runs conclus,
    dans l'ordre où ils ont tourné. Les règles du module, appliquées run par run :
    appels réussis seulement, un enregistrement une fois par jour, la recherche dans
    un run productif seulement et plafonnée, le rapport une fois par run."""
    vus: set[tuple] = set()
    tally: dict[str, dict] = {}
    total = 0.0

    def ajoute(k: str, n: int, m: float) -> None:
        nonlocal total
        t = tally.setdefault(k, {"count": 0, "minutes": 0.0})
        t["count"] += n
        t["minutes"] += m
        total += m

    for run in runs:
        counts: dict[str, int] = {}
        for c in run:
            if not c.ok:
                continue
            k = kind_of(c.tool, c.op)
            if not k:
                continue
            if k in _PRODUCTS and c.key:
                marque = (k, resolve(c.tool) or c.tool, c.key, c.day)
                if marque in vus:
                    continue
                vus.add(marque)
            counts[k] = counts.get(k, 0) + 1
        produits = {k: n for k, n in counts.items() if k in _PRODUCTS}
        if not produits:
            continue          # un balayage qui n'a rien produit de neuf
        recherche = counts.get("research", 0)
        if recherche and set(produits) == {"action"}:
            ajoute("report", 1, minutes["report"])     # chercher puis poster : un rapport
            continue
        for k, n in produits.items():
            ajoute(k, n, n * minutes[k])
        if recherche:
            ajoute("research", recherche, min(recherche * minutes["research"], RESEARCH_CAP_PER_RUN))
    return total, tally


def for_run(tool_counts: dict, minutes: dict[str, float]) -> tuple[float, dict[str, int]]:
    """(minutes gagnées, résultats par type) d'UN run, depuis ses seuls `tool_counts`."""
    total, tally = for_agent([calls_from_counts(tool_counts)], minutes)
    return total, {k: v["count"] for k, v in tally.items()}
