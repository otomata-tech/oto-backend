"""Profil utilisateur — la fiche « situation avec oto » (qui est l'utilisateur, son
métier, ses objectifs, son CRM, les connecteurs voulus, ses préférences de ton).

`oto_profile(op=…)` lit (`get`) ou met à jour (`update`) cette fiche, persistée dans
`user_account_profile.profile` (data model libre, shallow-merge). Elle est **relue à
chaque session** (injectée au handshake, bloc C) — l'agent personnalise son adresse,
ses workflows et son style à partir d'elle. Ce n'est PAS un mode d'accueil scripté
(l'onboarding est un projet, ADR 0032 §7) : l'agent entretient la fiche au fil de l'eau,
notamment depuis le projet « Découverte ».

Spine : chargé explicitement dans `register_all`, hors gate d'activation, toujours
visible (`PROTECTED_TOOLS`). Pas de dépendance externe.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import db
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

# Data model de la fiche : chaque champ = une clé persistée dans `profile`. `question`
# guide l'agent pour la remplir naturellement ; `why` explique à quoi sert la donnée.
# Les clés libres hors de cette liste sont acceptées (data model ouvert).
PROFILE_FIELDS: list[dict] = [
    {"key": "full_name",
     "question": "Comment s'appelle l'utilisateur (prénom/nom) ?",
     "why": "Personnaliser l'adresse et signer les emails/messages."},
    {"key": "role",
     "question": "Quel est son rôle / poste ?",
     "why": "Adapter le niveau et les workflows (commercial, fondateur, ops…)."},
    {"key": "company",
     "question": "Pour quelle entreprise / structure travaille-t-il, et dans quel secteur ?",
     "why": "Contextualiser la prospection et les recherches d'entreprise."},
    {"key": "goals",
     "question": "Qu'est-ce qu'il veut accomplir avec Oto (2-3 cas d'usage concrets) ?",
     "why": "Prioriser les connecteurs et proposer les bons workflows."},
    {"key": "crm",
     "question": "Quel CRM utilise-t-il (Attio, Folk, HubSpot, Pennylane, aucun…) ?",
     "why": "Router les écritures CRM vers le bon connecteur."},
    {"key": "connectors_wanted",
     "question": "Quels connecteurs/outils sont prioritaires pour lui (LinkedIn, email, "
                 "données entreprise FR, messagerie…) ?",
     "why": "Guider la configuration des clés et de la visibilité des outils."},
    {"key": "tone",
     "question": "Y a-t-il des préférences de ton/langue ou des contraintes à respecter ?",
     "why": "Aligner le style de rédaction et les gardes-fous."},
]


def _require_sub() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    except Exception:
        pass
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.",
        ))
    return sub


def _missing(profile: dict) -> list[str]:
    return [f["key"] for f in PROFILE_FIELDS if not str(profile.get(f["key"]) or "").strip()]


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def oto_profile(
        ctx: Context,
        op: str = "get",
        fields: Optional[dict] = None,
    ) -> dict:
        """Fiche « situation avec oto » de l'utilisateur — qui il est, son métier, ses
        objectifs, son CRM, les connecteurs voulus, ses préférences de ton. Cette fiche
        est relue à chaque session pour personnaliser ton aide ; entretiens-la au fil de
        l'eau quand tu apprends quelque chose d'utile sur l'utilisateur (notamment dans
        le projet « Découverte »).

        Args:
            op: `"get"` (défaut) = lire la fiche + les champs encore à remplir ;
                `"update"` = enregistrer des champs (requiert `fields`).
            fields: pour `op="update"`, un dict clé→valeur à enregistrer (shallow-merge
                avec l'existant). Clés attendues (cf. `profile_fields`) : full_name, role,
                company, goals, crm, connectors_wanted, tone. D'autres clés libres sont
                acceptées. N'enregistre QUE des valeurs réellement données par
                l'utilisateur — jamais d'inventions.

        Renvoie : {profile, profile_fields, missing}.
        """
        sub = _require_sub()
        op = (op or "get").strip().lower()

        if op == "update":
            if not isinstance(fields, dict) or not fields:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message="`fields` (objet clé→valeur non vide) requis pour op=update."))
            # Ne persiste que des valeurs non vides (garde la fiche propre).
            clean = {k: v for k, v in fields.items() if v not in (None, "", [])} or None
            state = db.update_account_profile(sub, clean)
        elif op == "get":
            state = db.get_account_profile(sub)
        else:
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="`op` doit être 'get' ou 'update'."))

        profile = state["profile"]
        return {"profile": profile, "profile_fields": PROFILE_FIELDS,
                "missing": _missing(profile)}
