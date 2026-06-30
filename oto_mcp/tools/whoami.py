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

from fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, db, memento_oauth, org_store, session_org
from ..auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

_DASHBOARD = "https://dashboard.oto.ninja"


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


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def oto_whoami(ctx: Context) -> dict:
        """Identité MCP courante : sous quel compte et dans quelle org/groupe tu agis.

        Appelle-la quand tu as besoin de savoir POUR QUI tu travailles, ou avant une
        action sensible (écriture CRM, envoi de message, dépense de crédits) pour
        confirmer le contexte. C'est ce couple **compte × org active × groupe actif**
        qui détermine quelles clés API sont résolues et à quelles données tu accèdes.

        Renvoie : `account` (sub, email, name, rôle plateforme), `org` (org active —
        id, name, rôle ; tu es TOUJOURS dans une org), `group` (groupe actif éventuel),
        `knowledge` (base Memento connectée ?), `connectors` (résumé des connecteurs
        configurés), `onboarded`, et un `summary` lisible. Lecture seule.

        Pour basculer d'org dans la conversation en cours : `oto_use_org <org>`
        (override de session éphémère — recharge toolbox + credentials, n'affecte
        ni ton org maison ni les autres conversations). Pour changer ton org par
        défaut (maison, appliquée aux prochaines conversations) : `oto_set_home_org`.
        """
        sub = _require_sub()

        user = {}
        try:
            user = db.get_user(sub) or {}
        except Exception as e:
            logger.warning("whoami: get_user failed: %s", e)
        try:
            role = access.get_user_role(sub)
        except Exception:
            role = None

        # Org EFFECTIVE sous laquelle tu agis (ADR 0023) = org de session ?? maison.
        # `scope`='session' = override éphémère posé par oto_use_org (cette conversation) ;
        # 'home' = ton org maison (défaut). 0/None = espace perso.
        org_block = None
        active_org = None
        try:
            active_org = access.current_org(sub)
            has_override, _ = session_org.current_override()
            if active_org is not None:
                o = org_store.get_org(active_org)
                org_block = {
                    "id": active_org,
                    "name": o["name"] if o else None,
                    "role": org_store.get_org_role(active_org, sub),
                    "scope": "session" if has_override else "home",
                }
        except Exception as e:
            logger.warning("whoami: org lookup failed: %s", e)

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

        # Connecteurs configurés (résumé, pas le détail des clés).
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
            logger.warning("whoami: status_for failed: %s", e)

        memento_connected = False
        try:
            memento_connected = bool(memento_oauth.status_for(sub).get("connected"))
        except Exception as e:
            logger.warning("whoami: memento status failed: %s", e)

        onboarded = False
        try:
            onboarded = bool(db.get_account_profile(sub).get("onboarded"))
        except Exception as e:
            logger.warning("whoami: account profile failed: %s", e)

        who = user.get("name") or user.get("email") or sub
        has_override, _ = session_org.current_override()
        if org_block:
            scope = f"org « {org_block['name']} » (rôle {org_block['role']})"
            if group_block:
                scope += f", groupe « {group_block['name']} »"
        else:
            scope = "espace perso (aucune org active)"
        if has_override:
            scope += " — override de session (cette conversation ; oto_use_org)"
        summary = f"Tu agis pour {who} dans {scope}."

        return {
            "account": {
                "sub": sub,
                "email": user.get("email"),
                "name": user.get("name"),
                "role": role,
            },
            "org": org_block,
            "group": group_block,
            "knowledge": {"memento_connected": memento_connected},
            "connectors": {
                "configured": configured,
                "platform_available": platform_ready,
            },
            "onboarded": onboarded,
            "summary": summary,
            "dashboard_url": _DASHBOARD,
        }
