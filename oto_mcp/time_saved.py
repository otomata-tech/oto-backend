"""Le temps qu'un agent fait gagner — compté depuis ce qu'il a FAIT, sans rien demander.

**La règle** (décision d'Alexis, 17/10/2026) : SEUL ce qu'un outil DÉCLARE compte. Le
type d'un résultat vient de l'annotation que l'outil porte (`tools/lecture.py`) :
`LECTURE` lit (une recherche), `SIMULATION` lit ou prépare ce que personne ne voit (ses
lectures sont des recherches, sa préparation ne compte pas), `ACTE`/`ENVOI` agit chez un
tiers. Un outil qui ne déclare rien ne rapporte RIEN — on ne déduit jamais une écriture
de l'absence de déclaration, ni un type du nom de l'outil.

**Ce qui compte** :
- seuls les runs CONCLUS `done` ET liés au journal (`tool_calls`) : un run sans journal
  n'est pas compté, et le résumé le dit (`not_counted`), il ne retombe sur rien ;
- de chaque appel : son `op`, s'il a RÉUSSI, et l'enregistrement touché ;
- sur un outil multiplexé, l'`op` dit le geste : une `op` de `READ_OPS` est une
  recherche ; sur un outil qui agit, seule une `op` dont le verbe est dans `SEND_OPS`
  (`send`, `send_email`) est un envoi —
  ranger, archiver ou supprimer (`gmail_act op=archive|trash`) n'est pas un envoi et
  ne rapporte rien ;
- un même enregistrement ne compte qu'UNE fois par jour et par agent ;
- la RECHERCHE ne compte que dans un run qui a PRODUIT quelque chose de neuf, et
  plafonnée par run ;
- un run qui a cherché puis seulement ENVOYÉ (un résumé, une alerte) est un RAPPORT —
  une fois par run ;
- la tenue de la plateforme ne compte pas (`oto_*`, `run_start`/`run_finish`, les
  lectures et réservations du tableau de l'agent) ; seules les écritures du tableau
  nommées ici (`_DATA_WRITES`) sont des enregistrements écrits.

**Les minutes par défaut** sont des ESTIMATIONS — des points de départ prudents, pas
des mesures. Chaque org les surcharge (`oto_org_settings domain=time_saved`, admin),
jamais l'agent un par un.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

#: Les types de résultat, dans l'ordre où l'écran les dit. `minutes` = défaut.
KINDS: dict[str, dict] = {
    "report": {"label": "report posted", "plural": "reports posted", "minutes": 15.0},
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
#: Les outils du tableau qui ÉCRIVENT un résultat, nommés un à un — les autres `data_*`
#: (lire, réserver, relâcher, schéma) sont la tenue du travail. Les capacités de la
#: plateforme ne portent pas d'annotation MCP : cette liste EST leur déclaration.
_DATA_WRITES = frozenset({"data_write", "data_update_where"})

#: Les `op` qui LISENT, quel que soit ce que l'outil déclare : un outil multiplexé
#: (`gmail op=search|get|attachment|drafts`) n'y compte que des recherches.
READ_OPS = frozenset({
    "get", "list", "search", "find", "read", "lookup", "query", "count", "history",
    "schema", "status", "describe", "preview", "fetch", "show", "inbox", "stats",
    "activities", "info", "check", "match", "resolve", "export", "download",
    "attachment", "drafts",
})

#: Les `op` d'un outil qui AGIT (`ACTE`/`ENVOI`) qui atteignent un tiers : un envoi.
#: Les autres gestes d'un tel outil (ranger, archiver, supprimer) ne rapportent rien.
SEND_OPS = frozenset({
    "send", "reply", "forward", "publish", "broadcast", "invite", "remind", "post",
    "comment",
})

#: Les résultats qui font d'un run un run PRODUCTIF.
_PRODUCTS = frozenset({"action", "record"})


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
    """Le type de résultat d'un appel à `name` (avec son `op`, lu dans le journal), ou
    None quand il ne compte pas — dont tout outil qui ne déclare rien."""
    base = resolve(name) or name
    if base in _IGNORED_EXACT or base.startswith("oto_"):
        return None
    if base.startswith("data_"):
        return "record" if base in _DATA_WRITES else None
    effet = _effet(base)
    geste = str(op).lower() if op else None
    if effet == "read":
        return "research"
    if effet == "simulation":
        return "research" if geste in READ_OPS else None
    if effet == "act":
        if geste is None:
            return "action"
        if geste in READ_OPS:
            return "research"
        # Une op de `<ns>_act` porte son objet (`send_email`, `publish_post`) : c'est son
        # VERBE qui dit l'envoi.
        return "action" if geste.split("_", 1)[0] in SEND_OPS else None
    return None


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


def classe(name: str, op: Optional[str] = None) -> Optional[tuple[str, str]]:
    """`(type, outil canonique)` d'un appel compté, None s'il ne compte pas. Le
    canonique est l'outil dont on marque l'enregistrement (« une fois par jour ») : un
    nom préfixé par le worker et son nom nu touchent le même enregistrement."""
    k = kind_of(name, op)
    return (k, resolve(name) or name) if k else None


class Forme(NamedTuple):
    """Ce qu'UN run a produit une fois ses appels comptés (réussis, typés, un
    enregistrement une fois par jour et par agent) — et combien de runs l'ont eu.
    `for_shapes` en tire le temps gagné : les règles du run vivent là, une fois."""
    actions: int
    records: int
    research: int
    runs: int = 1


def formes_des_runs(runs: list[list[Call]]) -> list[Forme]:
    """Les formes des runs conclus d'UN agent, dans l'ordre où ils ont tourné : appels
    réussis seulement, un enregistrement (action ou écriture qui le nomme) une fois par
    jour et par agent — la première occurrence compte, les suivantes non.

    La référence en mémoire : la page Agents compte la même chose en SQL
    (`db.formes_des_runs_conclus`), sans décoder un appel ; le banc de parité les
    compare (`tests/test_temps_gagne_borne_live.py`)."""
    vus: set[tuple] = set()
    out: list[Forme] = []
    for run in runs:
        counts: dict[str, int] = {}
        for c in run:
            if not c.ok:
                continue
            classee = classe(c.tool, c.op)
            if not classee:
                continue
            k, canonique = classee
            if k in _PRODUCTS and c.key:
                marque = (k, canonique, c.key, c.day)
                if marque in vus:
                    continue
                vus.add(marque)
            counts[k] = counts.get(k, 0) + 1
        out.append(Forme(counts.get("action", 0), counts.get("record", 0),
                         counts.get("research", 0)))
    return out


def for_shapes(formes: list[Forme], minutes: dict[str, float]) -> tuple[float, dict[str, dict]]:
    """(minutes gagnées, `{type: {count, minutes}}`) d'UN agent depuis les formes de
    ses runs : la recherche dans un run productif seulement et plafonnée, le rapport
    une fois par run. Une forme vaut `runs` runs identiques."""
    tally: dict[str, dict] = {}
    total = 0.0

    def ajoute(k: str, n: int, m: float) -> None:
        nonlocal total
        t = tally.setdefault(k, {"count": 0, "minutes": 0.0})
        t["count"] += n
        t["minutes"] += m
        total += m

    for f in formes:
        produits = {k: n for k, n in (("action", f.actions), ("record", f.records)) if n}
        if not produits or f.runs <= 0:
            continue          # un balayage qui n'a rien produit de neuf
        if f.research and set(produits) == {"action"}:
            ajoute("report", f.runs, f.runs * minutes["report"])  # chercher puis poster
            continue
        for k, n in produits.items():
            ajoute(k, f.runs * n, f.runs * (n * minutes[k]))
        if f.research:
            ajoute("research", f.runs * f.research,
                   f.runs * min(f.research * minutes["research"], RESEARCH_CAP_PER_RUN))
    return total, tally


def for_agent(runs: list[list[Call]], minutes: dict[str, float]) -> tuple[float, dict[str, dict]]:
    """(minutes gagnées, `{type: {count, minutes}}`) d'UN agent sur ses runs conclus,
    dans l'ordre où ils ont tourné. Les règles du module, appliquées run par run :
    appels réussis seulement, un enregistrement une fois par jour, la recherche dans
    un run productif seulement et plafonnée, le rapport une fois par run."""
    return for_shapes(formes_des_runs(runs), minutes)
