"""`/shell` v0 — le CHROME de l'application, en un appel, en lecture seule.

Ce que le front rend en permanence sur ses huit écrans et qui ne change pas quand on
navigue : l'entreprise, la personne, le rail rangé en sections, les compteurs, et un
index court de connecteurs pour la palette. Contracté dans `shell-contract.md` (front)
et accepté le 16/08 (`reponse-shell-contract-2026-08-16.md`) — **surface PRÉCOCE et
déclarée provisoire** : les FORMES se contractent, le stockage reste variable.

**Ce n'est ni un index, ni une recherche, ni une liste paginable.** L'argument du front
est le bon et il commande tout le reste : une coquille partielle ne rendrait pas « la
suite au prochain appel », elle rendrait « ce nœud est introuvable » pour un nœud qui
existe. D'où : aucune pagination, et c'est la PROFONDEUR qui se coupe (compteurs `more`,
le patron de l'épine), jamais la liste.

**Les sections sont CALCULÉES à l'appel, jamais stockées** — elles sont la projection
de l'ownership (ADR 0049) sur une personne : `everyone` = l'org, `team` = une par équipe
dont elle est membre, `private` = ce qu'elle possède, `shared` = ce qu'on lui a partagé
en direct et qu'aucune équipe ne couvre déjà.

Trois garanties du contrat, tenues ici :

1. **Pas de doublon** — un nœud est dans EXACTEMENT une section. Le rangement est un
   ordre de priorité (org > équipe > soi > partage direct), pas une union : un nœud
   accessible par son équipe ET partagé nominativement se range sous l'équipe. Sans
   ça le rail montre deux fois la même page, et on en déduit qu'il y en a deux.
2. **`shared` est conditionnelle et sans `context`** — absente quand vide, et le SEUL
   cas où une section n'a pas de ligne de contexte : une section de partages reçus ne
   range rien, donc elle n'a pas de « + ».
3. **L'ordre vient de nous** — le front n'en fabrique aucun.

⚠️ **`private` = TOUS les nœuds que la personne possède, y compris ceux qu'elle a
partagés.** Le contrat dit « ses nœuds à lui, partagés avec personne » ; on sert plus
large, et c'est délibéré : c'est SON rail. Une page qu'elle a écrite puis partagée
disparaîtrait de son propre rail au moment où elle la partage — le partage se lit
alors comme une perte. La nuance est ici pour qu'elle soit vue, pas subie.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import access, group_store, org_store, run_status
from ..connectors import selection as connector_selection
from ..db import project_nodes
from ..db import shell as db_shell
from ._authz import ORG_MEMBER
from ._types import Capability, NotModified, ResolvedCtx, RestBinding
from .node_keys import doc_id_de
from .node_procedure_ref import ProcedureRef, procedure_ref_of
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

# Le patron de l'épine, aux mêmes chiffres : la carte se lit d'un coup d'œil ou elle
# ne sert à rien. Ce qui dépasse est COMPTÉ (`more`), jamais coupé en silence.
_PROFONDEUR = 2
_BUDGET_PAR_SECTION = 200

# `type` choisit le GLYPHE de la ligne, pas l'écran (contrat §2).
#
# ⚠️ **La nature est DÉRIVÉE, elle n'est pas un `kind` de plus.** Le genre dit ce que
# l'objet EST (une page, un tableau) ; ce qu'il JOUE — procédure, agent — est un RÔLE
# porté en propriété (0054-D5/D6), exactement comme le rôle de présentation d'un bloc au
# lot ⑦. Créer `kind='agent'` rouvrirait le second axe qu'on a passé deux lots à fermer.
#
# `execution` (0054-D7) n'a toujours pas de source et n'est pas inventé ici : un
# nœud-conteneur par run est une décision de VOLUMÉTRIE, pas un défaut à combler.
_TYPE_PAR_KIND = {"page": "page", "tableau": "table"}
# Rôle porté en props → nature servie au rail. Un rôle inconnu retombe sur le genre :
# une nature inventée serait pire qu'une nature générique.
_TYPE_PAR_ROLE = {"procedure": "agent"}


class ShellInput(BaseModel):
    # La version que le client porte déjà. Égale à la nôtre ⟹ 304, et il garde son
    # cache. `rev` et non un `ETag` : c'est NOTRE convention (celle d'`oto_doc`), et
    # le front a demandé la nôtre plutôt que d'imposer la sienne.
    rev: Optional[str] = None


class RailNode(BaseModel):
    """Une ligne du rail de navigation.

    ⚠️ **`id` ne désigne pas la même chose selon `type`, et c'est le piège de cette
    surface.** Sur `page`, `table` et `agent`, c'est l'identifiant d'un nœud, celui
    que `GET /api/me/nodes/{node_id}` prend. Sur `execution`, c'est un **identifiant
    de déroulé** : ces lignes sont projetées depuis le journal, sans aucune ligne en
    base (~60 000 par an seraient créées pour des objets qui sont des JOURNAUX), et
    `GET /api/me/nodes/{cet id}` rend donc **404**. Un client qui traite toutes les
    lignes du rail de la même façon casse sur celle-là ; l'identifiant d'une
    exécution se porte aux surfaces de run.
    """
    id: str = Field(description=(
        "Identifiant de la ligne. ⚠️ Sa NATURE dépend de `type` : identifiant de "
        "nœud pour `page` / `table` / `agent` (accepté par "
        "`GET /api/me/nodes/{node_id}`), identifiant de DÉROULÉ pour `execution` — "
        "ce dernier n'a pas de fiche de nœud et rend 404 sur cette route."))
    name: str
    type: Literal["page", "table", "agent", "execution"]
    badge: Optional[str] = None
    sharedBy: Optional[str] = None
    children: Optional[list["RailNode"]] = None
    # Nombre d'enfants NON rendus (profondeur ou budget). Compté, jamais tu : une
    # branche coupée sans compteur se lit comme une branche vide.
    more: Optional[int] = None
    # La procédure qu'un nœud `agent` exécute — id stable, slug, scope (#417). Absente
    # sur toute autre nature, et sur un agent sans référence lisible : jamais devinée.
    procedure: Optional[ProcedureRef] = None
    # La poignée vers la page d'origine, quand la ligne en vient (#650). La colonne
    # était DÉJÀ lue par la requête du rail (`db/shell._COLS`) pour la référence de
    # procédure, et jetée pour tout le reste : la servir ne coûte aucune requête de
    # plus, et épargne un aller-retour `GET /api/me/nodes/{id}` par ligne à qui veut
    # seulement ouvrir ce qu'il vient de créer.
    # ⚠️ **Absente** (le rail omet les `None`) quand la ligne n'a pas de page derrière
    # elle — un projet, un tableau natif, une exécution. Jamais devinée.
    doc_id: Optional[int] = None


class RailContext(BaseModel):
    name: str


class RailSection(BaseModel):
    id: str
    name: str
    kind: Literal["everyone", "team", "private", "shared"]
    # Absent sur `shared` SEULEMENT (contrat §3, garantie 2).
    context: Optional[RailContext] = None
    # L'objet dont la section dérive (l'équipe). Servi, JAMAIS rendu : il sert à
    # l'invalidation et à la navigation. Absent hors `team`.
    origin: Optional[str] = None
    nodes: list[RailNode] = []


class ShellCompany(BaseModel):
    name: Optional[str] = None
    logoUrl: Optional[str] = None


class ShellUser(BaseModel):
    name: Optional[str] = None


class ShellConnector(BaseModel):
    id: str
    name: str
    # Le pont outil → branchement. Sans lui, aucun type ne voyait le 404 (le front a
    # eu le bug) — c'est la raison d'être de cette liste, pas un ornement.
    connectionId: str


class ShellOut(BaseModel):
    company: ShellCompany
    user: ShellUser
    sections: list[RailSection]
    # Des choses qui ATTENDENT la personne, jamais des volumes. Les compteurs sans
    # source sont ABSENTS plutôt que faux : un `0` affirme « rien ne t'attend ».
    counters: dict[str, int]
    connectors: list[ShellConnector]
    rev: str
    # Partages reçus qu'aucun nœud ne porte encore (les procédures — cf. `db/shell`).
    # Rendu pour qu'une section « Partagé » incomplète ne se lise pas comme vide.
    grants_sans_noeud: Optional[int] = None


def _type_of(kind: str, props: Optional[dict] = None) -> str:
    role = (props or {}).get("role")
    if role in _TYPE_PAR_ROLE:
        return _TYPE_PAR_ROLE[role]
    return _TYPE_PAR_KIND.get(kind or "", "page")


def _arbre(lignes: list[dict], *, shared_par: Optional[dict] = None) -> list[RailNode]:
    """Les nœuds d'une section en ARBRE, bornés en profondeur ET en nombre.

    Même patron que l'épine d'un projet : une seule lecture, assemblage en mémoire
    (les arbres d'une section sont petits), et ce qui dépasse est compté. Les racines
    sont les nœuds dont le parent n'appartient PAS à la section — sinon une page dont
    le parent vit ailleurs (autre propriétaire) disparaîtrait du rail au lieu d'y
    remonter à la racine.
    """
    par_parent: dict = {}
    presents = {l["id"] for l in lignes}
    for l in lignes:
        par_parent.setdefault(l["parent_id"], []).append(l)
    budget = {"n": 0}

    def _sous_arbre(node_id) -> int:
        n, pile = 0, [node_id]
        while pile:
            for k in par_parent.get(pile.pop(), []):
                n += 1
                pile.append(k["id"])
        return n

    def _noeud(l: dict, prof: int) -> RailNode:
        budget["n"] += 1
        nature = _type_of(l.get("kind"), l)
        out = RailNode(id=l["public_id"], name=l.get("title") or "", type=nature,
                       procedure=procedure_ref_of(nature, l.get("owner_type"), l),
                       doc_id=doc_id_de(l.get("legacy"), l.get("legacy_id")))
        if shared_par is not None:
            out.sharedBy = shared_par.get(l["public_id"])
        enfants = par_parent.get(l["id"], [])
        if not enfants:
            return out
        if prof <= 0 or budget["n"] >= _BUDGET_PAR_SECTION:
            out.more = _sous_arbre(l["id"])
            return out
        rendus = []
        for k in enfants:
            if budget["n"] >= _BUDGET_PAR_SECTION:
                out.more = len(enfants) - len(rendus)
                break
            rendus.append(_noeud(k, prof - 1))
        out.children = rendus
        return out

    racines = [l for l in lignes if l["parent_id"] not in presents]
    # ⚠️ Le budget borne AUSSI les racines, pas seulement les enfants. L'épine d'un
    # projet n'a qu'une racine, donc la question ne s'y posait pas ; une section en a
    # autant que l'org a de pages de premier niveau. Sans cette borne, `everyone` rend
    # les 400 racines d'une org fournie et le plafond ne protège rien — c'est le cas
    # que le budget existe pour couvrir.
    rendues = []
    for r in racines:
        if budget["n"] >= _BUDGET_PAR_SECTION:
            break
        rendues.append(_noeud(r, _PROFONDEUR))
    coupees = len(racines) - len(rendues)
    if coupees and rendues:
        # Le reste est COMPTÉ sur la dernière racine rendue : le contrat interdit une
        # pagination, pas un compteur — et une liste tronquée sans compteur se lit
        # comme une liste complète.
        rendues[-1].more = (rendues[-1].more or 0) + coupees
    return rendues


# Ce qu'on lit pour trouver les exécutions en cours. Large à la lecture, étroit au
# rendu : le filtre « ouvert et non périmé » ne se pousse pas en SQL (la fraîcheur vient
# du JOURNAL, pas de la table), donc on lit une tranche récente et on trie ici.
_RUNS_LUS = 60
_EXECUTIONS_MAX = 20


def _executions(sub: str, org_id: Optional[int]) -> list[RailNode]:
    """Les exécutions EN COURS de la personne — projetées, jamais stockées.

    ⚠️ **Aucun nœud n'est créé pour un run**, et c'est une décision mesurée, pas une
    facilité. Mesuré le 21/08 : 166 runs par jour, soit ~60 000 nœuds par an —
    l'équivalent de toute la table de contenu d'aujourd'hui, ajouté chaque année, pour
    des objets qui sont des JOURNAUX. Ils paieraient au passage les deux index GIN de
    recherche (99 % du coût d'écriture d'un vivier au banc M0) sans qu'on cherche jamais
    un run par son texte, et l'index d'ownership partiel ne les exclurait pas. 0058 a
    déjà tranché que le journal est la vérité d'un run : un nœud-conteneur serait une
    seconde vérité qui vieillit mal. Le contrat l'autorise — « les FORMES se contractent,
    le STOCKAGE reste variable » : `type: execution` est un champ de RailNode, pas une
    exigence de table.

    ⚠️ **La borne n'est PAS une fenêtre de jours, et c'est là que la mesure a tranché.**
    Sept jours donnaient **1 023 runs pour un seul compte** (dont 941 d'une même
    campagne de flotte) : illisible, et contraire à ce que le rail EST — le chrome, pas
    un journal. Le rail montre ce qui ATTEND la personne, jamais des volumes : c'est la
    règle que le contrat pose déjà pour ses compteurs, et elle vaut ici.

    Donc : **ouverts ET non périmés**, au sens que `run_status` porte déjà (24 h sans
    signe de vie ⟹ un run cesse d'être annoncé « en cours » — #311, seuil re-daté par
    #666). Mesuré : 26 runs sur toute la plateforme, médiane 1 par personne, maximum 24.
    Réutiliser ce seam plutôt que d'inventer une fenêtre évite d'avoir deux définitions
    de « en cours » — et un run périmé affiché ici serait le miroir exact du défaut que
    #311 a fermé.
    """
    if org_id is None:
        return []
    try:
        runs = db_shell.recent_runs(sub, org_id, limit=_RUNS_LUS)
    except Exception:      # le rail ne tombe pas parce que le journal hoquette
        logger.warning("shell: exécutions en cours indisponibles", exc_info=True)
        return []
    vivants = [r for r in runs
               if not r.get("outcome")
               and not run_status.is_stale(r.get("outcome"), r.get("last_seen_at"))]
    out = [RailNode(id=str(r["run_id"]), name=r.get("label") or "Exécution",
                    type="execution")
           for r in vivants[:_EXECUTIONS_MAX]]
    if out and len(vivants) > _EXECUTIONS_MAX:
        # Coupé, donc COMPTÉ — même règle que l'arbre : une liste tronquée sans
        # compteur se lit comme une liste complète.
        out[-1].more = len(vivants) - _EXECUTIONS_MAX
    return out


def _connecteurs(sub: str, org_id: Optional[int]) -> list[ShellConnector]:
    """Le VERDICT EFFECTIF, borné aux connecteurs INSTALLÉS de la personne.

    ⚠️ **Jamais le catalogue.** Résoudre les ~90 connecteurs déclarés fait marcher la
    cascade pour chacun — c'est la promenade qui a gelé le handshake le 15/08. Sa
    toolbox en compte une poignée, et c'est elle que la palette doit proposer : un
    outil que la personne n'a pas installé n'a rien à faire dans SA palette.

    Un connecteur sans branchement résoluble n'entre pas dans la liste (contrat §7) —
    la palette ne propose que ce qui marcherait si on cliquait.

    **Deux économies, mesurées avant d'être écrites (33 connecteurs, compte réel) :**

    1. **Le contexte est résolu UNE FOIS et passé.** `credential_mode_for` le résout
       sinon à chaque appel : `current_org` coûtait 73 % du temps total, appelé une
       fois par connecteur pour rendre trente-trois fois la même valeur. 1 213 → 554 ms
       à lui seul. On passe l'org et l'équipe de L'APPELANT — c'est le kwarg prévu
       pour ça, et le seam reste scopé sur l'acteur (jamais `current_org(autre_sub)`).
    2. **Une sonde PRÉCHARGÉE** remplace la marche par connecteur : l'inventaire des
       credentials à portée est lu une fois, la cascade répond en mémoire. Le walker
       n'est pas touché — c'est une sonde de plus, pas un second chemin.
    """
    if org_id is None:
        return []
    installes = sorted(nom for nom, etat
                       in connector_selection.list_selection(sub, org_id).items()
                       if etat == "active")
    if not installes:
        return []
    # Une fois, pas trente-trois. `_UNSET` ≠ `None` : passer explicitement suffit à
    # court-circuiter la résolution par appel.
    org_eff = access.current_org(sub)
    grp_eff = access.current_group(sub)
    try:
        equipes = group_store.list_groups_for_user(sub, org_eff) if org_eff else []
        sonde = access.preloaded_presence_probe(sub, org=org_eff, groups=equipes)
    except Exception:      # l'inventaire n'est qu'une accélération, jamais un prérequis
        logger.warning("shell: préchargement des credentials indisponible", exc_info=True)
        sonde = access.PRESENCE_PROBE

    out: list[ShellConnector] = []
    for nom in installes:
        try:
            # PAR le seam, jamais à côté : `credential_mode_for` fait plus que rendre
            # le barreau gagnant — c'est lui qui traduit un grant plateforme épuisé en
            # `over_quota`. L'appeler avec la sonde préchargée garde ce contrôle ; le
            # court-circuiter aurait listé comme utilisable un connecteur hors quota.
            mode = access.credential_mode_for(sub, nom, org=org_eff, group=grp_eff,
                                              probe=sonde)
        except Exception:      # un connecteur qui ne résout pas ne casse pas le rail
            logger.warning("shell: verdict connecteur %s indisponible", nom, exc_info=True)
            continue
        if mode in ("forbidden", "over_quota"):
            continue
        # `connectionId` = le palier qui RÉSOUT. C'est ce que le front appelle « le
        # branchement » : deux personnes du même outil peuvent résoudre par des
        # paliers différents, et l'identifiant doit le refléter.
        out.append(ShellConnector(id=nom, name=nom, connectionId=f"{mode}:{nom}"))
    return out


def _rev(corps: dict) -> str:
    """Empreinte du CORPS canonique — la version que le client renvoie pour un 304.

    Sur le corps sérialisé trié, pas sur un `updated_at` max : le rail dépend d'un
    nom d'org, d'une appartenance d'équipe et d'un verdict de connecteur, dont aucun
    ne touche `nodes.updated_at`. Une empreinte de contenu ne peut pas rater un
    changement qu'elle ne sait pas nommer.
    """
    return hashlib.sha256(
        json.dumps(corps, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:32]


def _compose(ctx: ResolvedCtx) -> dict:
    """Tout le travail SYNCHRONE du rail. Appelé hors boucle (cf. `_shell`)."""
    sub, org_id = ctx.sub, ctx.org_id
    org = org_store.get_org(org_id) if org_id is not None else None
    equipes = group_store.list_groups_for_user(sub, org_id) if org_id is not None else []

    proprios = ([("org", str(org_id))] if org_id is not None else [])
    proprios += [("group", str(g["group_id"])) for g in equipes]
    proprios += [("user", sub)]
    # Les projets et leurs pages sont LUS dans leurs tables (`db/project_nodes`), pas dans
    # leurs anciennes copies : une copie restée en base porterait un contenu figé.
    lignes = [l for l in db_shell.nodes_for_owners(proprios)
              if not project_nodes.est_une_copie(l)]
    lignes += project_nodes.lignes_pour_proprietaires(proprios)

    par_proprio: dict = {}
    for l in lignes:
        par_proprio.setdefault((l["owner_type"], str(l["owner_id"])), []).append(l)

    sections: list[RailSection] = []
    if org_id is not None:
        sections.append(RailSection(
            id="g-everyone", name="Tout le monde", kind="everyone",
            context=RailContext(name="Contexte — Tout le monde"),
            nodes=_arbre(par_proprio.get(("org", str(org_id)), []))))
    for g in equipes:
        gid = str(g["group_id"])
        nom = g.get("name") or "Équipe"
        sections.append(RailSection(
            id=f"g-{gid}", name=nom, kind="team", origin=gid,
            context=RailContext(name=f"Contexte — {nom}"),
            nodes=_arbre(par_proprio.get(("group", gid), []))))
    prive = _arbre(par_proprio.get(("user", sub), []))
    prive += _executions(sub, org_id)
    sections.append(RailSection(
        id="sec-private", name="Privé", kind="private",
        context=RailContext(name="Contexte — Privé"),
        nodes=prive))

    # ── `shared` : les partages DIRECTS, moins ce qu'une autre section range déjà ──
    grants = db_shell.direct_grants(sub)
    par_id, sans_noeud = db_shell.resolve_grant_nodes(grants)
    deja_rangés = {l["public_id"] for l in lignes}
    candidats = [pid for pid in par_id if pid not in deja_rangés]
    partages = [l for l in db_shell.nodes_by_public_id(candidats)
                if not project_nodes.est_une_copie(l)]
    if partages:
        auteurs = db_shell.names_of(
            (par_id[l["public_id"]] or {}).get("granted_by") for l in partages)
        shared_par = {l["public_id"]:
                      auteurs.get((par_id[l["public_id"]] or {}).get("granted_by"))
                      for l in partages}
        sections.append(RailSection(
            id="sec-shared", name="Partagé", kind="shared",
            nodes=_arbre(partages, shared_par=shared_par)))

    corps = {
        "company": ShellCompany(name=(org or {}).get("name"),
                                logoUrl=(org or {}).get("logo_url")).model_dump(),
        "user": ShellUser(name=_nom_de(sub)).model_dump(),
        "sections": [s.model_dump(exclude_none=True) for s in sections],
        "counters": _compteurs(ctx),
        "connectors": [c.model_dump() for c in _connecteurs(sub, org_id)],
    }
    if sans_noeud:
        corps["grants_sans_noeud"] = sans_noeud
    corps["rev"] = _rev({k: v for k, v in corps.items() if k != "rev"})
    return corps


def _nom_de(sub: Optional[str]) -> Optional[str]:
    if not sub:
        return None
    return db_shell.names_of([sub]).get(sub)


def _compteurs(ctx: ResolvedCtx) -> dict:
    """Ce qui ATTEND la personne — rien n'est compté aujourd'hui : aucune clé, jamais un zéro.

    Un `0` affirme « rien ne t'attend » ; une clé absente dit « on ne sait pas encore
    compter ça ». `home` comptait l'inbox, retirée avec elle (oto#191) ; `agents` et
    `connectors` naîtront avec les surfaces qui savent les compter (accord du 16/08),
    pas avant. La map reste servie, VIDE : un front la lit sans garde.
    """
    return {}


async def _shell(ctx: ResolvedCtx, inp: ShellInput):
    """Le chrome, ou un 304 si le client porte déjà notre version.

    ⚠️ **Tout le corps part au THREADPOOL.** Le serveur est mono-loop et cette lecture
    fait plusieurs requêtes DB plus une marche de cascade par connecteur installé —
    exécutée sur la boucle, elle gèlerait toutes les autres sessions. C'est la leçon
    du 15/08, prise à la construction plutôt qu'après l'incident.
    """
    corps = await run_in_threadpool(_compose, ctx)
    if inp.rev and inp.rev == corps.get("rev"):
        return NotModified(corps["rev"])
    return corps


CAPABILITIES += [
    Capability(
        key="me.shell", handler=_shell, Input=ShellInput, Output=ShellOut,
        authz=ORG_MEMBER,
        description=(
            "The application CHROME in one call: company, user, the rail in ordered "
            "SECTIONS (everyone / one per team / private / shared-when-not-empty), "
            "`counters` of things AWAITING you (nothing is counted today, so it is `{}` — a "
            "missing key means « not counted », never zero), and a short connector index for the "
            "command palette. Read-only, never paginated — depth is capped instead "
            "(`more` counts what was cut). Pass `rev` from a previous answer for a "
            "conditional read: unchanged returns `{not_modified: true, rev}` (HTTP 304 "
            "on REST) so you keep your cached copy. PROVISIONAL surface: the shape is "
            "contracted, not frozen."),
        mcp="oto_shell",
        rest=RestBinding("GET", "/api/me/shell", provisoire=True),
    ),
]
