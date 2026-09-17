"""Whoami — l'identité sous laquelle Claude agit quand il appelle les outils.

`oto_whoami()` répond à la question « pour qui / dans quel contexte est-ce que
j'agis ? » : le **compte** (sub Logto + email + rôle plateforme) croisé avec
l'**org active** et l'éventuel **groupe actif** — exactement ce qui gouverne la
résolution des credentials et le scope des données (cf. badge « identité MCP » du
dashboard). Lecture seule, best-effort (jamais d'exception sur un hoquet DB).

Spine : chargé explicitement dans `register_all`, hors gate d'activation, toujours
visible (`PROTECTED_TOOLS`). Pas de dépendance externe.
"""
from __future__ import annotations

import logging
import os

from fastmcp import Context, FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, org_store, session_org
from ..db import tenants as db_tenants
from ..auth.hooks import current_user_sub_from_token
from .. import config

logger = logging.getLogger(__name__)

# ⚠️ PAS une constante de module : l'adresse dépend du TENANT du compte, donc de
# l'appel. La figer au chargement servirait la nôtre à tout le monde — y compris aux
# utilisateurs d'un partenaire, à qui elle propose un produit qui n'est pas le leur.


def _require_sub() -> str:
    sub = None
    try:
        sub = current_user_sub_from_token()
    # noqa: SILENT — dette déclarée : sub avalé (#424, verdict C — seam commun)
    except Exception:
        pass
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Auth requise — ces tools ne marchent que sur le transport HTTP authentifié.",
        ))
    return sub


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def oto_whoami(ctx: Context) -> dict:
        """Identité MCP courante : sous quel compte et dans quelle org/groupe tu agis.

        Appelle-la quand tu as besoin de savoir POUR QUI tu travailles, ou avant une
        action sensible (écriture CRM, envoi de message, dépense de crédits) pour
        confirmer le contexte. C'est ce couple **compte × org active × groupe actif**
        qui détermine quelles clés API sont résolues et à quelles données tu accèdes.

        Renvoie : `account` (sub, email, name, rôle plateforme), `org` (org active —
        id, name, rôle ; tu es TOUJOURS dans une org), `tenant` (`null` hors org active,
        sinon `{slug, is_ours}` — **le tenant est le compte de plus haut niveau isolé
        chez oto : le partenaire qui héberge cette org sous sa marque, AU-DESSUS de
        l'org qui lit, jamais confondu avec elle ; `is_ours=true` = notre propre
        tenant, aucun partenaire entre l'org et oto**), `group` (groupe actif éventuel),
        `connectors` (résumé des connecteurs
        configurés — dont `platform_quotas`, le quota du jour `{used, limit,
        remaining}` des connecteurs plateforme au quota plafonné : regarde-le avant
        un lot d'appels qui dépensent, pour arbitrer sans découvrir la limite au
        milieu du lot), et un `summary` lisible. Lecture seule.

        Pour agir sous une autre org/équipe/projet : passe le jeton `_org=` /
        `_group=` / `_project=` directement sur chaque appel de travail (aucun état
        de session, ADR 0038) — `oto_whoami(org=X)` montre le contexte résultant.
        L'org/équipe PAR DÉFAUT (maison) ne se change que dans le dashboard —
        l'agent ne mute jamais le défaut.
        """
        sub = _require_sub()

        user = {}
        try:
            user = db.get_user(sub) or {}
        except Exception as e:
            logger.warning("whoami: get_user failed: %s", e)
        try:
            role = access.get_user_role(sub)
        # noqa: SILENT — rôle non lisible ⇒ non affiché, jamais deviné
        except Exception:
            role = None

        # Org EFFECTIVE sous laquelle tu agis (ADR 0038) = jeton d'appel ?? maison.
        # `scope`='call' = org épinglée par le jeton de CET appel (org=/project=/group=) ;
        # 'home' = ton org maison (défaut de tout appel sans jeton). 0/None = perso.
        org_block = None
        active_org = None
        try:
            active_org = access.current_org(sub)
            has_call_pin = session_org.current_call_org() is not None
            if active_org is not None:
                o = org_store.get_org(active_org)
                org_block = {
                    "id": active_org,
                    "name": o["name"] if o else None,
                    "role": org_store.get_org_role(active_org, sub),
                    "scope": "call" if has_call_pin else "home",
                    # MFA obligatoire de l'org (le 2ᵉ facteur est imposé au login des
                    # membres, enforcé par Logto via l'org miroir — cf. mfa_mirror).
                    "require_mfa": org_store.get_org_mfa(active_org)["require_mfa"],
                }
        except Exception as e:
            logger.warning("whoami: org lookup failed: %s", e)

        # Le TENANT de l'org active (oto-backend#775) : « oto » (nous), ou le slug
        # du partenaire qui héberge cette org sous sa marque. C'est la seule
        # réponse à « duquel de vos partenaires relevez-vous ? » — aucune autre
        # surface servie ne la donnait, alors que `level: "tenant"` (ex.
        # connecteurs) suppose qu'on sache déjà lequel.
        tenant_block = None
        if active_org is not None:
            try:
                slug = db_tenants.org_tenant_slug(active_org)
                tenant_block = {
                    "slug": slug,
                    "is_ours": slug == db_tenants.tenancy.PRIMARY_SLUG,
                }
            except Exception as e:
                logger.warning("whoami: tenant lookup failed: %s", e)

        # Groupe actif (sous-palier ADR 0012) — invariant : appartient à l'org active.
        group_block = None
        try:
            from .. import group_store, roles
            active_group = access.current_group(sub)
            if active_group is not None:
                g = group_store.get_group(active_group)
                group_block = {
                    "id": active_group,
                    "name": g["name"] if g else None,
                    "role": roles.effective_group_role(sub, active_group),
                }
        except Exception as e:
            logger.warning("whoami: group lookup failed: %s", e)

        # Projet de l'appel (jeton project= — le bracelet de session est retiré, ADR 0038 B3b).
        project_block = None
        try:
            active_project = access.current_project()
            if active_project is not None:
                p = db.get_project_by_id(active_project)
                if p is not None:
                    project_block = {"id": active_project, "name": p.get("name")}
        except Exception as e:
            logger.warning("whoami: project lookup failed: %s", e)

        # Connecteurs configurés (résumé, pas le détail des clés). `platform_quotas`
        # réutilise le calcul déjà fait par `status_for` (aucune marche en plus) :
        # pour un connecteur en mode plateforme dont le quota jour est PLAFONNÉ
        # (ex. apollo — cf. `access.platform_quota_hint`), regarder ici AVANT un
        # lot d'appels qui dépensent évite de découvrir la limite au milieu d'un
        # lot (oto-backend#710). `over_quota` reste listé — masquer le connecteur
        # une fois épuisé dirait « pas configuré » à qui n'a que ça d'épuisé.
        configured: list[str] = []
        platform_ready: list[str] = []
        platform_quotas: dict[str, dict] = {}
        try:
            providers = access.status_for(sub).get("providers", {})
            for name, st in sorted(providers.items()):
                mode = st.get("mode")
                if mode in ("user", "group", "org"):
                    configured.append(name)
                elif mode in ("platform", "over_quota"):
                    platform_ready.append(name)
                    limit = st.get("quota_daily")
                    if limit:
                        used = st.get("quota_used_today") or 0
                        platform_quotas[name] = {
                            "used": used, "limit": limit,
                            "remaining": max(0, limit - used),
                        }
        except Exception as e:
            logger.warning("whoami: status_for failed: %s", e)

        # ⚠️ Plus de champ `knowledge` (retiré le 10/09/2026 avec le verbe `oto_kb`) : il
        # rendait l'id du projet de l'ex-« base de connaissance », et un agent qui lit
        # « ta KB est le projet N » y écrit — un recrutement par la RÉPONSE, le défaut
        # même qui a fait retirer le verbe. Ce projet reste un projet ordinaire.

        who = user.get("name") or user.get("email") or sub
        if org_block:
            scope = f"org « {org_block['name']} » (rôle {org_block['role']})"
            if group_block:
                scope += f", groupe « {group_block['name']} »"
        else:
            scope = "espace perso (aucune org active)"
        if org_block and org_block["scope"] == "call":
            scope += " — épinglée par le jeton de CET appel (org=/project=/group=)"
        if project_block:
            scope += f" — projet actif « {project_block['name']} »"
        summary = f"Tu agis pour {who} dans {scope}."

        return {
            "account": {
                "sub": sub,
                "email": user.get("email"),
                "name": user.get("name"),
                "role": role,
            },
            "org": org_block,
            "tenant": tenant_block,
            "group": group_block,
            "project": project_block,
            "connectors": {
                "configured": configured,
                "platform_available": platform_ready,
                # {name: {used, limit, remaining}} pour les seuls connecteurs
                # plateforme au quota PLAFONNÉ aujourd'hui — absent sinon (quota
                # illimité, ou org sur un plan `unmetered`, ADR 0043).
                "platform_quotas": platform_quotas,
            },
            "summary": summary,
            "dashboard_url": config.dashboard_url_for(sub),
        }
