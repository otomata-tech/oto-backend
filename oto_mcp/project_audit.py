"""Audit des liens d'un projet (ADR 0035, B5) — les liens vérifiés comme des refs.

Pendant projet des « refs mortes » de doctrine (0014) : un pointeur déclaré qui ne
correspond plus au réel devient une **alarme**, pas un silence. Quatre signaux,
tous NON bloquants (dérivation pure, best-effort) :
- **dead_links** : la cible ne résout plus (tableau sans namespace, procédure
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


def _resolved_procedure(link: dict):
    """L'instruction d'un lien `procedure`, ou None (réf non numérique legacy /
    doctrine disparue). Import paresseux (pas de cycle au boot)."""
    from . import org_store
    ref = str(link.get("target_ref") or "")
    if not ref.isdigit():
        return None
    return org_store.get_instruction_by_id(int(ref))


def _unresolvable_connector_why(name: str, org_id: int, link: dict) -> Optional[str]:
    """Un lien connecteur d'un projet ORG-owned est-il « aveugle » (#218/#219) ? — le
    credential n'existe qu'au niveau d'une ÉQUIPE de l'org, donc il ne résout PAS en
    contexte projet (un projet org-owned ne co-pose pas de groupe ; seul un projet
    d'équipe le fait). Renvoie le message de remède, ou None si le lien résout bien.

    Conservateur (pas de faux positif sur les connecteurs BYO par-membre — ceux-là
    n'ont pas non plus de secret d'équipe → non signalés) : on ne signale QUE la forme
    exacte de #218 = secret présent au niveau d'un groupe, absent au niveau org.
    Un `instance_ref` épinglé (intention déclarée, RBAC gère) lève le signal."""
    from . import access, group_store
    if (link.get("config") or {}).get("instance_ref"):
        return None
    if access.connector_resolvable_for_org(name, org_id):
        return None
    if not any(group_store.has_group_secret(int(g["id"]), name)
               for g in group_store.list_groups(org_id)):
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


def audit_project(project_id: int, links: Optional[list[dict]] = None, *,
                  light: bool = False) -> dict:
    """Audit des liens d'un projet. `links` = liens déjà chargés
    (`db.list_project_links`, qui résout namespace/title), sinon rechargés.
    Best-effort : toute erreur ⇒ audit partiel, jamais d'exception.

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
    # Org propriétaire (le signal `unresolvable_connector` ne vaut que pour un projet
    # ORG-owned). Résolu seulement en audit COMPLET (une requête par projet).
    project_org: Optional[int] = None
    if not light:
        try:
            proj = db.get_project_by_id(int(project_id))
            if proj and proj.get("owner_type") == "org" and str(proj.get("owner_id", "")).isdigit():
                project_org = int(proj["owner_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("audit_project(%s) owner: %s", project_id, e)
    dead: list[dict] = []
    unbound: list[dict] = []
    unresolvable: list[dict] = []
    procedures: list[dict] = []   # (link, instr) résolus — pour l'inertie
    for l in links:
        t = l.get("target_type")
        try:
            if t == "tableau":
                if not l.get("namespace"):
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "slot": l.get("slot"),
                                 "why": "le namespace pointé n'existe plus"})
            elif t == "procedure":
                if light:
                    continue   # la résolution de procédure est une requête → COMPLET seul
                instr = _resolved_procedure(l)
                if instr is None:
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "why": "la procédure pointée ne résout plus"})
                    continue
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
                elif not light and project_org is not None:
                    why = _unresolvable_connector_why(str(ref), project_org, l)
                    if why:
                        unresolvable.append({"target_ref": ref, "why": why})
        except Exception as e:  # noqa: BLE001 — un lien pourri n'avale pas l'audit
            logger.warning("audit_project(%s) lien %s: %s", project_id, l.get("target_ref"), e)
    inert: list[str] = []
    if not light:
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
