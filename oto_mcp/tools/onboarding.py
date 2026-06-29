"""Onboarding — accueil + self-onboarding piloté par l'agent.

Au démarrage d'un compte, Claude appelle `oto_onboarding()` : il reçoit (1) une
explication d'Oto, (2) l'état découvert de la configuration (org active, base de
connaissance, clés de connecteurs, doctrine), (3) la **fiche « situation avec
oto »** déjà remplie + ce qu'il reste à collecter, et (4) un **script de
self-onboarding** (doctrine d'accueil) à dérouler avec l'utilisateur.

L'agent interviewe l'utilisateur en suivant le script et persiste les réponses via
`oto_onboarding_update(fields=…)` (data model nourri au fil de l'eau, table
`user_account_profile`). Quand l'accueil est terminé, il valide le booléan
`onboarded` (`oto_onboarding_update(onboarded=True)`). Tant que ce booléan est
faux, l'accueil reste à reprendre à la session suivante.

Spine : chargé explicitement dans `register_all`, hors gate d'activation, toujours
visible (`PROTECTED_TOOLS`). Pas de dépendance externe.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, memento_oauth, org_store
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

_DASHBOARD = "https://dashboard.oto.ninja"

_INTRO = (
    "Oto est ta boîte à outils d'automatisation branchée dans Claude : prospection "
    "B2B, données d'entreprise France (open data + INSEE), CRM, email, messagerie, "
    "recherche web et base de connaissance. Les outils `*_` AGISSENT directement sur "
    "les comptes et données de l'utilisateur — sers-t'en sans qu'on te le demande "
    "explicitement dès qu'une tâche entre dans ce périmètre."
)

# Data model de la fiche « situation avec oto » : ce que le self-onboarding cherche
# à remplir. Chaque champ = une clé persistée dans `user_account_profile.profile`.
# `question` guide l'interview ; `why` explique à l'agent à quoi sert la donnée.
_PROFILE_FIELDS: list[dict] = [
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

# Script de self-onboarding (doctrine d'accueil servie à l'agent). Volontairement
# court et opératoire : l'agent le déroule en conversation et persiste au fur et à
# mesure. On le sert depuis le serveur (« forké » côté compte = la fiche
# `user_account_profile` qu'il nourrit) plutôt que de dépendre d'une org.
_SELF_ONBOARDING_DOCTRINE = """\
# Self-onboarding Oto

Objectif : accueillir un nouvel utilisateur, lui expliquer Oto, et remplir sa
fiche « situation avec oto » — puis valider `onboarded`. Reste bref, une question
à la fois, ne bombarde pas.

1. **Souhaite la bienvenue** et explique Oto en 2 phrases (cf. `intro`). Annonce ce
   qui est déjà prêt sur son compte (cf. `setup` : org active, connecteurs,
   memento, doctrine).
2. **Interviewe** pour remplir la fiche (cf. `profile_fields`). Demande seulement
   les champs encore `missing`. Une réponse → persiste tout de suite avec
   `oto_onboarding_update(fields={"<clé>": "<valeur>"})`. N'invente jamais une
   réponse ; si l'utilisateur passe, laisse le champ vide.
3. **Propose de configurer** les étapes `todo` du `setup` (clés de connecteurs,
   memento, org) en pointant les liens dashboard — ne tente pas de poser les
   secrets toi-même.
4. **Clôture** quand l'essentiel est couvert (au moins identité + objectifs) :
   récapitule, puis valide avec `oto_onboarding_update(onboarded=True)`. Enchaîne
   ensuite sur `oto_get_doctrine()` et la première tâche de l'utilisateur.

Si l'utilisateur veut passer l'accueil, valide `onboarded=True` sans insister.
"""


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


def _step(key: str, label: str, status: str, detail: str, action_url: str | None = None) -> dict:
    s = {"key": key, "label": label, "status": status, "detail": detail}
    if action_url:
        s["action_url"] = action_url
    return s


def _discover_setup(sub: str) -> tuple[list[dict], dict, list[str], list[str]]:
    """État découvert du compte (best-effort, jamais d'exception)."""
    # Identité + org active
    email = role = None
    try:
        email = (db.get_user(sub) or {}).get("email")
    except Exception as e:
        logger.warning("onboarding: get_user failed: %s", e)
    try:
        role = access.get_user_role(sub)
    except Exception:
        role = None
    active_org = active_org_name = org_role = None
    try:
        active_org = org_store.get_active_org(sub)
        if active_org is not None:
            o = org_store.get_org(active_org)
            active_org_name = o["name"] if o else None
            org_role = org_store.get_org_role(active_org, sub)
    except Exception as e:
        logger.warning("onboarding: org lookup failed: %s", e)

    # Connecteurs prêts
    configured: list[str] = []
    platform_ready: list[str] = []
    try:
        providers = access.status_for(sub).get("providers", {})
        for name, st in sorted(providers.items()):
            mode = st.get("mode")
            if mode in ("user", "group", "org"):
                configured.append(name)
            elif mode == "platform":
                platform_ready.append(name)
    except Exception as e:
        logger.warning("onboarding: status_for failed: %s", e)

    memento_connected = False
    try:
        memento_connected = bool(memento_oauth.status_for(sub).get("connected"))
    except Exception as e:
        logger.warning("onboarding: memento status failed: %s", e)

    has_doctrine = False
    try:
        if active_org is not None:
            base = org_store.get_instruction(active_org, org_store.BASE_SLUG)
            has_doctrine = bool(base and (base.get("body_md") or "").strip())
    except Exception as e:
        logger.warning("onboarding: doctrine lookup failed: %s", e)

    steps = [
        _step("active_org", "Espace de travail",
              "done" if active_org is not None else "todo",
              (f"Org active : « {active_org_name} » (rôle {org_role})."
               if active_org is not None else
               "Aucune org active — espace perso. Crée/rejoins une org pour partager "
               "clés, doctrine et crédits (`oto_create_org` ou dashboard)."),
              _DASHBOARD),
        _step("connectors", "Connecteurs / clés API",
              "done" if (configured or platform_ready) else "todo",
              (f"{len(configured)} avec ta clé"
               f"{' : ' + ', '.join(configured[:8]) if configured else ''}"
               f"{'…' if len(configured) > 8 else ''}. "
               f"{len(platform_ready)} dispo(s) via la plateforme."
               if (configured or platform_ready) else
               "Aucun connecteur configuré. Pose tes clés API depuis le dashboard."),
              f"{_DASHBOARD}/console/connectors"),
        _step("linkedin", "LinkedIn & messagerie", "optional",
              "LinkedIn (Unipile) + WhatsApp/Telegram/Instagram se connectent depuis le "
              "dashboard (option messagerie) pour les outils `unipile_*`/`whatsapp_*`/etc.",
              f"{_DASHBOARD}/console/connectors"),
        _step("knowledge", "Base de connaissance (Memento)",
              "done" if memento_connected else "todo",
              ("Memento connecté (outils `memento_*`)." if memento_connected else
               "Connecte ta base Memento pour une mémoire durable de l'agent."),
              f"{_DASHBOARD}/console/knowledge"),
        _step("doctrine", "Doctrine d'organisation",
              "done" if has_doctrine else "optional",
              ("Doctrine d'org présente — `oto_get_doctrine()`." if has_doctrine else
               "Pas de doctrine d'org. Un org_admin peut l'écrire (`oto_set_doctrine`)."),
              f"{_DASHBOARD}/console/doctrine"),
    ]
    account = {"email": email, "role": role, "active_org": active_org,
               "active_org_name": active_org_name, "org_role": org_role}
    return steps, account, configured, platform_ready


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def oto_onboarding(ctx: Context) -> dict:
        """Accueil / self-onboarding du compte Oto. APPELLE-LA EN DÉBUT DE SESSION
        tant que le compte n'est pas onboardé (`onboarded=false`).

        Tu obtiens : une explication d'Oto (`intro`), l'état découvert du compte
        (`setup` + `account`), la fiche « situation avec oto » déjà remplie
        (`profile`) avec les champs restant à collecter (`missing` / `profile_fields`),
        et le script de self-onboarding à dérouler (`doctrine`).

        Marche à suivre : déroule `doctrine`. Pose les questions des champs `missing`
        une à une et persiste chaque réponse avec `oto_onboarding_update(fields=…)`.
        Quand l'accueil est terminé, valide avec `oto_onboarding_update(onboarded=True)`,
        puis enchaîne sur `oto_get_doctrine()`. Lecture seule — ne modifie rien ici.
        """
        sub = _require_sub()
        steps, account, configured, platform_ready = _discover_setup(sub)

        state = db.get_account_profile(sub)
        profile = state["profile"]
        missing = [f["key"] for f in _PROFILE_FIELDS if not str(profile.get(f["key"]) or "").strip()]

        todo = [s["key"] for s in steps if s["status"] == "todo"]
        if state["onboarded"]:
            nxt = ("Compte déjà onboardé — pas besoin de refaire l'accueil. Appelle "
                   "`oto_get_doctrine()` et enchaîne sur la tâche de l'utilisateur.")
        elif missing:
            nxt = ("Déroule le self-onboarding (`doctrine`) : pose les champs `missing` "
                   "un par un et persiste avec `oto_onboarding_update`. Valide "
                   "`onboarded=True` à la fin.")
        else:
            nxt = ("Fiche complète — récapitule à l'utilisateur puis valide avec "
                   "`oto_onboarding_update(onboarded=True)`, et appelle `oto_get_doctrine()`.")

        return {
            "intro": _INTRO,
            "onboarded": state["onboarded"],
            "account": account,
            "profile": profile,
            "profile_fields": _PROFILE_FIELDS,
            "missing": missing,
            "doctrine": _SELF_ONBOARDING_DOCTRINE,
            "setup": steps,
            "configured_connectors": configured,
            "platform_connectors": platform_ready,
            "next": nxt,
            "dashboard_url": _DASHBOARD,
        }

    @mcp.tool()
    def oto_onboarding_update(
        ctx: Context,
        fields: Optional[dict] = None,
        onboarded: Optional[bool] = None,
    ) -> dict:
        """Met à jour la fiche d'onboarding du compte (« situation avec oto »).

        Utilise-la pendant le self-onboarding pour persister les réponses de
        l'utilisateur au fil de l'eau, puis pour valider la fin de l'accueil.

        Args:
            fields: dict de paires clé→valeur à enregistrer dans la fiche (shallow-merge
                avec l'existant). Clés attendues (cf. `profile_fields` de
                `oto_onboarding`) : full_name, role, company, goals, crm,
                connectors_wanted, tone. D'autres clés libres sont acceptées.
                N'enregistre que des valeurs réellement données par l'utilisateur —
                jamais d'inventions.
            onboarded: passe à `true` quand l'accueil est terminé (le booléan reste
                vrai ensuite) ; `false` pour le rouvrir. Omis = inchangé.

        Renvoie l'état résultant : {onboarded, profile, missing}.
        """
        sub = _require_sub()
        if fields is not None and not isinstance(fields, dict):
            raise McpError(ErrorData(
                code=INVALID_PARAMS, message="`fields` doit être un objet clé→valeur."))
        if not fields and onboarded is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="Rien à mettre à jour : fournis `fields` et/ou `onboarded`."))
        # Ne persiste que des valeurs non vides (garde la fiche propre).
        clean = {k: v for k, v in (fields or {}).items() if v not in (None, "", [])} or None
        state = db.update_account_profile(sub, clean, onboarded)
        profile = state["profile"]
        missing = [f["key"] for f in _PROFILE_FIELDS if not str(profile.get(f["key"]) or "").strip()]
        return {"onboarded": state["onboarded"], "profile": profile, "missing": missing}
