"""Audit des liens d'un projet (ADR 0035, B5) — les liens vérifiés comme des refs.

Pendant projet des « refs mortes » de doctrine (0014) : un pointeur déclaré qui ne
correspond plus au réel devient une **alarme**, pas un silence. Trois signaux,
tous NON bloquants (dérivation pure, best-effort) :
- **dead_links** : la cible ne résout plus (tableau sans namespace, procédure
  disparue, connecteur inconnu du registre) ;
- **unbound_slots** : une procédure liée déclare des slots que le projet ne binde
  pas (complétude, le pendant des refs mortes à l'écriture) ;
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


def unbound_slots_for(instr: dict, links: list[dict]) -> list[str]:
    """Slots déclarés par une procédure et NON bindés par le projet (noms triés).
    Le binding compte quel que soit le type du lien porteur (le nom est un
    vocabulaire du projet, B2)."""
    declared = {s["name"] for s in (instr.get("slots") or []) if s.get("name")}
    bound = {l.get("slot") for l in links if l.get("slot")}
    return sorted(declared - bound)


def audit_project(project_id: int, links: Optional[list[dict]] = None) -> dict:
    """Audit complet des liens d'un projet. `links` = liens déjà chargés
    (`db.list_project_links`, qui résout namespace/title), sinon rechargés.
    Best-effort : toute erreur ⇒ audit partiel, jamais d'exception."""
    from . import db, providers
    if links is None:
        links = db.list_project_links(int(project_id))
    dead: list[dict] = []
    unbound: list[dict] = []
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
                if l.get("target_ref") not in providers.REGISTRY:
                    dead.append({"target_type": t, "target_ref": l.get("target_ref"),
                                 "why": "connecteur inconnu du registre"})
        except Exception as e:  # noqa: BLE001 — un lien pourri n'avale pas l'audit
            logger.warning("audit_project(%s) lien %s: %s", project_id, l.get("target_ref"), e)
    inert: list[str] = []
    try:
        stats = db.project_run_stats(int(project_id))
        # Un projet SANS run ne rend rien d'inerte (jeune projet = bruit, pas signal).
        if stats.get("runs"):
            used = set(stats.get("doctrines") or [])
            inert = sorted(p["slug"] for p in procedures if p["slug"] not in used)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit_project(%s) runs: %s", project_id, e)
    return {"dead_links": dead, "unbound_slots": unbound, "inert_procedures": inert}
