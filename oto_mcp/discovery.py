"""Projet « Découverte » — l'onboarding EST un projet (ADR 0032 §7), pas un mode
spécial. Quand l'org perso d'un utilisateur est créée (`org_store.ensure_personal_org`),
on y sème un projet d'accueil porteur d'un brief. Il remonte ensuite à l'agent comme
n'importe quel projet (bloc C « Projets récents » du handshake) ; aucun tool, aucun
gate, aucun état dédié. Best-effort : un échec de seed ne casse pas la création d'org.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_NAME = "Découverte"

# Brief d'accueil porté par le projet. Aucune mention d'un tool d'onboarding : l'agent
# l'ouvre comme un projet normal (`oto_use_project`) et s'en sert pour guider l'accueil.
BRIEF = """\
# Bienvenue sur Oto — projet « Découverte »

Ce projet est ton point d'entrée. Oto est ta boîte à outils d'automatisation branchée
dans Claude : prospection B2B, données entreprise France (open data + INSEE), CRM,
email, messagerie, recherche web et base de connaissance. Les outils `*_` agissent
directement sur les comptes et données de l'utilisateur.

Pour bien l'accueillir :

1. **Souhaite la bienvenue** et explique Oto en deux phrases.
2. **Apprends à le connaître** : qui il est, son métier, ce qu'il veut accomplir, son
   CRM, les connecteurs prioritaires, ses préférences de ton. Une question à la fois —
   ne bombarde pas. Persiste ce que tu apprends avec `oto_profile(op="update", …)` (sa
   fiche « situation avec oto », relue à chaque session) ; n'invente jamais une réponse.
3. **Propose de configurer** ce qui manque (clés de connecteurs, base de connaissance,
   doctrine d'org) en pointant le dashboard — ne pose pas les secrets toi-même.

Quand il a pris ses marques, crée des projets pour ses vrais cas d'usage (prospection,
veille, suivi client…) — chaque projet regroupe son but, ses tableaux, ses connecteurs
préconfigurés et ses procédures.
"""


def seed_for_org(sub: str, org_id: int) -> Optional[int]:
    """Crée le projet « Découverte » dans l'org `org_id` (best-effort). Renvoie l'id du
    projet créé, ou None si la création échoue. Appelé une fois, à la création de l'org
    perso — pas idempotent par lui-même (l'appelant ne sème qu'à la création)."""
    try:
        from . import db
        pid = db.create_project("org", str(org_id), PROJECT_NAME, BRIEF, created_by=sub)
        db.log_project_activity(pid, sub, "project.create", PROJECT_NAME)
        logger.info("projet Découverte #%s semé dans l'org #%s pour %s", pid, org_id, sub)
        return pid
    except Exception:
        logger.warning("seed du projet Découverte échoué (org=%s, sub=%s)", org_id, sub,
                       exc_info=True)
        return None
