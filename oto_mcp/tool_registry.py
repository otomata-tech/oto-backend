"""Résolution des références d'outils d'un guide (ADR 0014).

Source UNIQUE partagée par les deux faces :
- REST `/api/me/tools/registry` (le dashboard résout les `<tool:slug>` côté UI) ;
- MCP `oto_procedure` (manifeste « referenced_tools »
  appended à la livraison, pour que l'AGENT voie les noms canoniques, la
  description tirée de l'outil, et le **drift** d'une référence morte).

« derive don't duplicate » : la logique « marqueur → outil réel » ne vit qu'ici.
"""
from __future__ import annotations

import re

from . import providers
from .search import fold
from .tool_visibility import namespace_of

_MARKER = re.compile(r"<tool:([a-z0-9_]+)>")

# Instance FastMCP du serveur, liée au boot (`server._build_mcp` → `bind`). Les
# handlers de la couche capacité ne reçoivent que `(ctx, inp)` — pas l'instance —
# et la face REST n'a pas de contexte MCP : ce singleton leur sert de défaut pour
# résoudre le registre d'outils (manifeste « referenced_tools », ADR 0014).
_INSTANCE = None


def bind(instance) -> None:
    """Mémorise l'instance FastMCP servie (appelée une fois au boot)."""
    global _INSTANCE
    _INSTANCE = instance


def bound_instance():
    """L'instance FastMCP liée au boot (ou None hors serveur, ex. tests). Permet à
    la face REST de réutiliser la logique de visibilité MCP (`compute_hidden_tools`,
    qui attend `ctx.fastmcp.list_tools`) sans contexte MCP."""
    return _INSTANCE


def ref_names(text: str) -> list[str]:
    """Noms d'outils cités via `<tool:slug>`, dédupliqués, dans l'ordre."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _MARKER.finditer(text or ""):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def namespaces_in(text: str) -> set[str]:
    """Namespaces des outils référencés `<tool:slug>` dans `text`. Sert le compteur
    « référencé par N guides » (posture guide-only, ADR 0024) — dérivation pure,
    sans toucher au registre live.

    ⚠️ Résolu par `namespace_of`, pas par le premier token (oto#260) : deux
    connecteurs distincts partagent le premier mot (`linkedin_unipile_*`,
    `linkedin_aiark_*`), et découper au premier `_` les rangeait tous deux sous
    `linkedin` — un connecteur qui n'existe pas. Le résolveur était déjà importé
    ici ; il n'était simplement pas appelé."""
    return {namespace_of(n) for n in ref_names(text)}


# Une docstring d'outil est enveloppée à ~80 colonnes : sa 1ʳᵉ LIGNE coupe presque
# toujours au milieu d'une phrase (« Full company profile by SIREN: identity (siège, »).
# On prend donc le 1er PARAGRAPHE, replié en une ligne, borné à la 1ʳᵉ phrase complète
# qui tient dans le budget — sinon coupe au dernier mot entier.
_BLURB_CHARS = 140


def blurb(description: str | None, limit: int = _BLURB_CHARS) -> str:
    """Résumé d'une ligne d'un outil, borné. Pur — testable sans registre.

    Règle unique du produit pour « décrire un outil en une ligne » : le catalogue
    (`oto_list_my_tools`) et le manifeste de guide s'en servent tous les deux, avec
    des budgets différents. Le budget compte : le catalogue rend ~350 entrées d'un coup."""
    para = (description or "").strip().split("\n\n", 1)[0]
    text = " ".join(para.split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    # Une phrase complète vaut mieux qu'une coupe : on la garde si elle occupe au moins
    # la moitié du budget (sinon on rendrait un fragment inutilement court).
    cut = text.rfind(". ", 0, limit + 1)
    if cut >= limit // 2:
        return text[: cut + 1]
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 0 else limit].rstrip(" ,;:") + "…"


def _matcher(token: str):
    """Prédicat « ce mot est-il dans la botte de foin ? », compilé une fois par mot.

    Deux régimes, parce qu'un seul se trompe dans un sens ou dans l'autre :
    - **mot court (≤3)** → MOT ENTIER. En sous-chaîne, `fr` touche « **fr**om your
      organisation » et rend `email_send` sur une requête `fr`.
    - **mot long** → sous-chaîne, plus un `s`/`x` final retiré : le catalogue dit
      « donnée entreprise FR », l'agent tape « entreprises ». Sans cette tolérance la
      recherche rate exactement les requêtes en langue naturelle qu'elle sert."""
    if len(token) <= 3:
        rx = re.compile(rf"\b{re.escape(token)}\b")
        return lambda hay: rx.search(hay) is not None
    stem = token[:-1] if token[-1] in "sx" else None
    return lambda hay: token in hay or (stem is not None and stem in hay)


def match(query: str, entries: list[dict]) -> list[dict]:
    """Classe des entrées `{name, description, namespace_help?}` contre une requête en
    mots. Pur — testable sans registre.

    Parcourir 356 noms d'outils n'est pas une stratégie d'accès : un agent en mode
    différé (`oto_list_my_tools` → `oto_tool_schema` → `oto_call`) doit pouvoir demander
    « entreprises françaises » et recevoir les `fr_*` (issue #275).

    **C'est du lexical, pas du sémantique** — assumé : le classement est par nombre de
    mots retrouvés, puis nom avant description (qui tape `unipile` veut le connecteur, pas
    les outils qui le citent en passant). Un mot introuvable ne disqualifie pas l'entrée,
    il la fait juste descendre : une requête en langue naturelle porte des mots de liaison
    qu'aucune docstring ne contient, et un ET strict rendait zéro résultat là où l'agent
    avait raison de chercher. L'appelant doit traiter « zéro résultat » comme « reformule
    ou prends le catalogue entier », jamais comme « oto ne sait pas faire ».

    Les docstrings d'outils sont en ANGLAIS (contrat LLM) : le pont vers une requête
    française passe par `namespace_help`, la ligne de catalogue du connecteur, curée en
    français — d'où son inclusion dans la botte de foin."""
    # Découpe sur tout non-alphanumérique : une requête est aussi bien « facturation
    # client » qu'un nom d'outil collé (`fr_get`), et le `_` doit alors séparer comme
    # une espace. Mots de 1-2 lettres écartés (« un », « de », « my ») — sauf si la
    # requête n'est QUE ça : `fr` ou `rh` sont de vraies requêtes.
    words_q = [t for t in re.split(r"[^a-z0-9]+", fold(query or "")) if t]
    tokens = [t for t in words_q if len(t) >= 3] or words_q
    if not tokens:
        return list(entries)
    exact = fold((query or "").strip())
    hits = [_matcher(t) for t in tokens]
    out: list[tuple[tuple, dict]] = []
    for e in entries:
        name = fold(e.get("name", ""))
        words = name.replace("_", " ")
        # `own` = ce que dit l'OUTIL (nom + docstring) ; `hay` y ajoute la ligne de
        # catalogue de son connecteur, PARTAGÉE par tous ses outils. Sans départager les
        # deux, « recherche web » remonterait n'importe quel `serper_*` (tous héritent de
        # « Serper : recherche web ») avant `serper_search`.
        own = f"{words} {fold(e.get('description', ''))}"
        hay = f"{own} {fold(e.get('namespace_help', ''))}"
        score = sum(1 for h in hits if h(hay))
        if not score:
            continue
        # Trois paliers de preuve, du plus fort au plus faible : le NOM porte le mot,
        # puis la docstring de l'outil, puis la ligne de catalogue du connecteur. Sans le
        # palier « nom », « envoyer un email » sortait `data_share` avant `email_send` —
        # les deux citent « email », seul l'un des deux s'appelle ainsi.
        name_score = sum(1 for h in hits if h(words))
        own_score = sum(1 for h in hits if h(own))
        if name == exact:
            rank = 0
        elif name_score == len(tokens):
            rank = 1
        elif tokens[0] == name.split("_", 1)[0]:   # le namespace, cité en tête
            rank = 2
        else:
            rank = 3
        out.append(((-score, -name_score, rank, -own_score, len(name), name), e))
    out.sort(key=lambda t: t[0])
    return [e for _, e in out]


def _entry(tool) -> dict:
    """Entrée registre d'un tool MCP : nom + résumé d'une ligne (`blurb`) + source.

    ⚠️ **`source` vaut désormais TOUJOURS `"native"`**, et le champ `mcp` n'est plus
    jamais posé : la fédération MCP est retirée (2026-09-09, ADR 0069), donc plus
    aucun outil servi ne vient d'un serveur tiers. Le champ SURVIT parce qu'il est
    au contrat servi (dashboard), pas parce qu'il discrimine encore quelque chose —
    le retirer est une rupture cross-repo, à faire côté front d'abord."""
    return {
        "name": tool.name,
        "description": blurb(tool.description),
        "source": "native",
    }


# Registre boot mis en cache, réchauffé au DÉMARRAGE hors de tout contexte de
# session (`warm_registry`, appelé au lifespan). Le manifeste « referenced_tools »
# doit répondre « cet outil existe-t-il dans le produit ? » (fait BOOT), jamais
# « m'est-il visible dans CETTE session ? » : `list_tools(run_middleware=False)`
# saute le middleware mais applique QUAND MÊME `apply_session_transforms` (fastmcp) ;
# l'appeler depuis un handler de session polluait donc le manifeste (faux
# `status=missing` sur un outil masqué par la session, ex. `bridge_*` post-
# `oto_use_org` — otomata-private#75). Le cache coupe cette contamination.
_REGISTRY: dict[str, dict] | None = None

# Ce que chaque outil FAIT, déduit de ce qu'il déclare (`tools/lecture.py`) : `read`
# (`LECTURE`), `act` (agit chez un tiers : `ENVOI`), `write` (le reste). Réchauffé avec
# le registre boot, à part de lui : le registre est au contrat servi, l'effet non. Lu
# par le temps gagné des agents (`time_saved.py`).
_EFFETS: dict[str, str] = {}


def _effet(tool) -> str:
    a = getattr(tool, "annotations", None)
    if getattr(a, "readOnlyHint", None) is True:
        return "read"
    if getattr(a, "destructiveHint", None) is True and getattr(a, "openWorldHint", None) is True:
        return "act"
    return "write"


def effet_de(name: str) -> str | None:
    """`read` | `act` | `write` pour un outil du registre boot ; None s'il est inconnu
    (ou le registre pas réchauffé — tests)."""
    return _EFFETS.get(name)


def effets_connus() -> dict[str, str]:
    """Tous les effets connus, par nom d'outil canonique."""
    return _EFFETS


def boot_tool_names() -> list[str]:
    """Noms de TOUS les tools du registre BOOT (réchauffé hors session au lifespan,
    immunisé à la visibilité, #75) — tri stable ; [] si non réchauffé (tests).
    Sert la découverte post-activation (#186 : donner les NOMS à oto_call)."""
    return sorted(_REGISTRY or {})


async def _build_registry_live(mcp_instance=None) -> dict[str, dict]:
    """Construit la map nom → entrée à la volée. ⚠️ Si appelée DANS un contexte de
    session, la visibilité de session filtre le résultat (cf. `_REGISTRY`)."""
    mcp_instance = mcp_instance or _INSTANCE
    if mcp_instance is None:
        return {}
    tools = await mcp_instance.list_tools(run_middleware=False)
    _EFFETS.update({t.name: _effet(t) for t in tools})
    return {t.name: _entry(t) for t in tools}


async def warm_registry(mcp_instance=None) -> dict[str, dict]:
    """Construit et met en cache le registre boot. À appeler au DÉMARRAGE, hors de
    tout contexte de session (lifespan) → `apply_session_transforms` ne trouve
    aucune règle de visibilité et renvoie le registre complet. Idempotent."""
    global _REGISTRY
    reg = await _build_registry_live(mcp_instance)
    if reg:
        _REGISTRY = reg
    return _REGISTRY or {}


async def build_registry(mcp_instance=None) -> dict[str, dict]:
    """Map nom → entrée pour tous les tools boot. Sert le cache réchauffé au
    démarrage (immunisé à la visibilité de session, #75) ; à défaut (tests, cache
    non réchauffé) construit à la volée."""
    if _REGISTRY is not None:
        return _REGISTRY
    return await _build_registry_live(mcp_instance)


def resolve_refs(names: list[str], registry: dict[str, dict]) -> list[dict]:
    """Manifeste : pour chaque nom, l'outil résolu (`status=ok`) ou un signal de
    drift (`status=missing`) — la référence n'existe plus dans le registre."""
    out: list[dict] = []
    for name in names:
        entry = registry.get(name)
        out.append({**entry, "status": "ok"} if entry else {"name": name, "status": "missing"})
    return out


async def manifest_for(*texts: str, mcp_instance=None) -> list[dict]:
    """Manifeste « outils référencés » des corps `texts` (base + groupe, ou un
    skill). **Court-circuit zéro-coût** : aucune liste de tools n'est construite
    si les corps ne citent aucun outil (cas des guides legacy en backticks).
    `mcp_instance` omise = l'instance liée au boot (`bind`)."""
    names: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for n in ref_names(t):
            if n not in seen:
                seen.add(n)
                names.append(n)
    if not names:
        return []
    registry = await build_registry(mcp_instance)
    return resolve_refs(names, registry)


async def write_check(body_md: str, mcp_instance=None) -> dict:
    """Validation à l'écriture (ADR 0014) : résout les `<tool:slug>` du corps et
    renvoie le manifeste + les références non résolues. **Non bloquant** —
    l'écriture a lieu, mais l'auteur (IA ou UI) reçoit le signal de drift avant
    que l'agent n'échoue sur l'appel. `mcp_instance` omise = instance liée au boot."""
    manifest = await manifest_for(body_md, mcp_instance=mcp_instance)
    return {
        "referenced_tools": manifest,
        "unresolved_tools": [t["name"] for t in manifest if t.get("status") == "missing"],
    }
