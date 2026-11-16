"""Audit des liens d'un projet (ADR 0035, B5) — les liens vérifiés comme des refs.

Pendant projet des « refs mortes » de guide (0014) : un pointeur déclaré qui ne
correspond plus au réel devient une **alarme**, pas un silence. Quatre signaux,
tous NON bloquants (dérivation pure, best-effort) :
- **dead_links** : la cible ne résout plus (tableau sans nom résolu, procédure
  disparue, connecteur inconnu du registre) ;
- **unbound_slots** : une procédure liée déclare des slots que le projet ne binde
  pas (complétude, le pendant des refs mortes à l'écriture) ;
- **unresolvable_connectors** (#218/#219) : un projet ORG-owned lie un connecteur
  dont le credential n'existe qu'au niveau d'une ÉQUIPE — il ne résout pas en
  contexte projet (remède : transférer le projet à l'équipe, ou épingler une instance) ;
- **inert_procedures** : le projet a des runs mais une procédure liée n'a JAMAIS
  été déroulée (`runs.doctrine`) — un lien qui ne sert peut-être à rien.

Consommé par `oto_project op=get` (l'agent qui LIT le projet voit ses liens morts)
et `op=inventory` (curation), + le warning de complétude au `link`.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


#: Un lien vers une cible qui existe, mais hors de la portée du projet (posé avant la
#: garde d'`op=link`) : rien ne le sert, et le remède n'est pas celui d'un lien mort.
_HORS_PORTEE = ("hors de la portée du projet — rien ne le sert ; retire ce lien "
                "(`op=unlink`)")


def _resolved_procedures(links: list[dict]) -> dict[int, dict]:
    """Le slug et les slots des liens `procedure`, par `procedure_id`, en une requête —
    sans leur corps, que l'audit ne lit pas. Un lien sans `procedure_id` (réf non
    numérique legacy, guide disparu) n'y est pas. Import paresseux (pas de cycle au boot)."""
    from . import org_store
    # `procedure_id` = résolue DANS LA PORTÉE du projet (`db.list_project_links`) ;
    # sans elle, le lien est mort pour l'audit — et son slug ne se lit pas.
    return org_store.get_instruction_slots_by_ids(
        [int(l["procedure_id"]) for l in links
         if l.get("target_type") == "procedure" and l.get("procedure_id") is not None])


def _unresolvable_connector_why(name: str, org_id: int, link: dict,
                                groups: Optional[list[dict]] = None) -> Optional[str]:
    """Un lien connecteur d'un projet ORG-owned est-il « aveugle » (#218/#219) ? — le
    credential n'existe qu'au niveau d'une ÉQUIPE de l'org, donc il ne résout PAS en
    contexte projet (un projet org-owned ne co-pose pas de groupe ; seul un projet
    d'équipe le fait). Renvoie le message de remède, ou None si le lien résout bien.

    Conservateur (pas de faux positif sur les connecteurs BYO par-membre — ceux-là
    n'ont pas non plus de secret d'équipe → non signalés) : on ne signale QUE la forme
    exacte de #218 = secret présent au niveau d'un groupe, absent au niveau org.
    Un `instance_ref` épinglé (intention déclarée, RBAC gère) lève le signal.
    `groups` = les équipes de l'org, déjà lues par l'appelant (une lecture par audit,
    pas par lien). Les équipes qui détiennent la clé se lisent EN LOT
    (`group_store.groups_holding`, deux requêtes quel que soit leur nombre) : une
    lecture par équipe et par connecteur faisait de cette vérification l'essentiel des
    requêtes d'`op=get` (banc local : 5 connecteurs × 4 équipes = 40 requêtes sur 71)."""
    from . import access, group_store
    if (link.get("config") or {}).get("instance_ref"):
        return None
    if access.connector_resolvable_for_org(name, org_id):
        return None
    if groups is None:
        groups = group_store.list_groups(org_id)
    if not group_store.groups_holding([int(g["id"]) for g in groups], org_id, name):
        return None
    return (f"le credential `{name}` n'existe qu'au niveau d'une équipe de l'org — un "
            "projet d'org ne le résout pas en contexte projet. Transfère le projet à "
            "l'équipe (`oto_resource op=transfer new_owner_group=<id>`) pour qu'il hérite "
            "de ses credentials, ou épingle une instance (`instance_ref`).")


def unbound_slots_for(instr: dict, links: list[dict]) -> list[str]:
    """Slots déclarés par une procédure et NON bindés par le projet (noms triés).
    Le binding compte quel que soit le type du lien porteur (le nom est un
    vocabulaire du projet, B2)."""
    declared = {s["name"] for s in (instr.get("slots") or []) if s.get("name")}
    bound = {l.get("slot") for l in links if l.get("slot")}
    return sorted(declared - bound)


#: Ce que rend chaque profondeur d'audit — la réponse le dit (`checked`) quand elle n'est
#: pas complète : une liste ABSENTE n'a pas été vérifiée, elle ne vaut pas « rien à signaler ».
CHECKS_COMPLETS = ("dead_links", "unbound_slots", "unresolvable_connectors",
                   "inert_procedures")
CHECKS_LIENS = ("dead_links",)


def audit_project(project_id: int, links: Optional[list[dict]] = None, *,
                  light: bool = False, links_only: bool = False,
                  project: Optional[dict] = None) -> dict:
    """Audit des liens d'un projet. `links` = liens déjà chargés
    (`db.list_project_links`, qui résout namespace/title), sinon rechargés ; `project` =
    la ligne du projet déjà lue par l'appelant (sinon relue). Best-effort : toute erreur
    ⇒ audit partiel, jamais d'exception.

    L'audit COMPLET lit en lot (incident du 13/10/2026) : les procédures liées en une
    requête (slug et slots, sans corps), les équipes de l'org une fois, celles qui
    détiennent une clé en deux requêtes par connecteur distinct — pas une lecture par
    lien ni par équipe.

    `links_only=True` (lecture d'UN projet par l'écran qui l'ouvre) : les LIENS MORTS
    seuls, et tous — procédures comprises —, sans une requête de plus que `links`. Une
    procédure est morte quand `list_project_links` ne lui a pas posé de `procedure_id`
    (absente, ou hors de la portée du projet) : c'est exactement ce que l'audit complet
    juge, l'existence ayant déjà été lue par la requête des titres. Les trois autres
    listes ne sont PAS servies (ni vides ni calculées) et `checked` nomme ce qui l'a été.

    `light=True` (oto/#6 A7 — perf du LISTING) : ne fait que les checks EN MÉMOIRE
    (tableau dont le namespace ne résout plus, connecteur hors registre) → ZÉRO
    requête supplémentaire par projet. Coupe les vérifications coûteuses par-lien
    (résolution de procédure, résolvabilité de connecteur) et par-projet
    (`get_project_by_id`, `project_run_stats`) qui, sur une liste, produisaient un
    N+1 pathologique (timeout `op=list` à 180 s pour 6 projets). L'audit COMPLET
    reste servi par `op=get`."""
    from . import db, providers
    if links is None:
        links = db.list_project_links(int(project_id))
    complet = not light and not links_only
    # Org propriétaire (le signal `unresolvable_connector` ne vaut que pour un projet
    # ORG-owned). Résolu seulement en audit COMPLET (une requête par projet, aucune
    # quand l'appelant passe la ligne).
    project_org: Optional[int] = None
    # None : la lecture des procédures a échoué (journalisée) — leurs liens ne sont
    # alors pas jugés, plutôt que déclarés morts à tort.
    instrs: Optional[dict[int, dict]] = {}
    groups: Optional[list[dict]] = None
    # Un connecteur lié sous plusieurs identités ne se juge qu'une fois.
    pourquoi_connecteur: dict[str, Optional[str]] = {}
    if complet:
        try:
            proj = project if project is not None else db.get_project_by_id(int(project_id))
            if proj and proj.get("owner_type") == "org" and str(proj.get("owner_id", "")).isdigit():
                project_org = int(proj["owner_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_project(%s) owner: %s", project_id, e)
        try:
            instrs = _resolved_procedures(links)
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_project(%s) procédures: %s", project_id, e)
            instrs = None
    dead: list[dict] = []
    unbound: list[dict] = []
    unresolvable: list[dict] = []
    procedures: list[dict] = []   # (link, instr) résolus — pour l'inertie
    for l in links:
        t = l.get("target_type")
        try:
            if t == "tableau":
                # Lit la clé NEUVE : `namespace` est doublée jusqu'au 08/11/2026 et
                # disparaîtra — un lecteur interne qui s'y accroche casserait ce jour-là.
                if l.get("datastore_ambigu"):
                    # #365 : le nom désigne plusieurs tableaux dans la portée du projet.
                    # Il en existe trop, pas pas assez — mais rien ne le sert tant qu'il
                    # n'est pas re-lié par identifiant : pour qui s'en sert, il est mort.
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "slot": l.get("slot"),
                                 "why": "ce nom désigne plusieurs tableaux dans la portée "
                                        "du projet — re-lie-le par identifiant"})
                elif l.get("hors_portee"):
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "slot": l.get("slot"),
                                 "why": _HORS_PORTEE})
                elif not l.get("datastore"):
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "slot": l.get("slot"),
                                 "why": "le tableau pointé n'existe plus"})
            elif t == "procedure":
                if light or instrs is None:
                    continue   # la résolution de procédure est une requête → COMPLET seul
                pid = l.get("procedure_id")
                instr = instrs.get(int(pid)) if pid is not None else None
                if pid is None or (instr is None and not links_only):
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "why": (_HORS_PORTEE if l.get("hors_portee")
                                         else "la procédure pointée ne résout plus")})
                    continue
                if links_only:
                    continue   # vivante ; ses slots ne sont pas lus à cette profondeur
                procedures.append(instr)
                missing = unbound_slots_for(instr, links)
                if missing:
                    unbound.append({"procedure": instr["slug"],
                                    "ref": str(l.get("target_ref")),
                                    "slots": missing})
            elif t == "connecteur":
                ref = l.get("target_ref")
                if ref not in providers.REGISTRY:
                    dead.append({"target_type": t, "target_ref": ref,
                                 "why": "connecteur inconnu du registre"})
                elif complet and project_org is not None:
                    epingle = bool((l.get("config") or {}).get("instance_ref"))
                    if epingle:
                        continue   # instance épinglée : intention déclarée, rien à lire
                    if str(ref) not in pourquoi_connecteur:
                        if groups is None:
                            from . import group_store
                            groups = group_store.list_groups(project_org)
                        pourquoi_connecteur[str(ref)] = _unresolvable_connector_why(
                            str(ref), project_org, l, groups)
                    why = pourquoi_connecteur[str(ref)]
                    if why:
                        unresolvable.append({"target_ref": ref, "why": why})
        except Exception as e:  # noqa: BLE001 — un lien pourri n'avale pas l'audit
            logger.warning("audit_project(%s) lien %s: %s", project_id, l.get("target_ref"), e)
    if links_only:
        return {"dead_links": dead, "checked": list(CHECKS_LIENS)}
    inert: list[str] = []
    if complet:
        try:
            stats = db.project_run_stats(int(project_id))
            # Un projet SANS run ne rend rien d'inerte (jeune projet = bruit, pas signal).
            if stats.get("runs"):
                used = set(stats.get("doctrines") or [])
                inert = sorted(p["slug"] for p in procedures if p["slug"] not in used)
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_project(%s) runs: %s", project_id, e)
    return {"dead_links": dead, "unbound_slots": unbound,
            "unresolvable_connectors": unresolvable, "inert_procedures": inert}
