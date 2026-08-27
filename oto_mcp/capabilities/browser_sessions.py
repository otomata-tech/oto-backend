"""Connexion par SESSION NAVIGATEUR (Live View Browserbase, ADR 0026).

Deux routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `POST /api/me/connectors/{name}/session/start`    → ouvre la Live View, rend son URL
- `POST /api/me/connectors/{name}/session/finalize` → vérifie le login, persiste au coffre

Le geste produit le même objet qu'un formulaire de credential — une ligne du coffre
scopée `(sub, org)` (ADR 0033) — mais par un login HUMAIN dans un navigateur hébergé,
là où l'autre voie dérive un formulaire du schéma du connecteur (celle-là est passée
en capacité le même jour : `capabilities/me_credentials.py`).

**Pas de face MCP** (`mcp=None`), pour deux raisons cumulatives. D'abord un `context_id`
Browserbase EST le credential : il ne passe pas en argument d'outil, il transiterait
dans le contexte du modèle. Ensuite le geste n'a aucun sens sans humain — c'est une
fenêtre de navigateur où quelqu'un tape un mot de passe. Le pendant agent existe et
c'est `me.connector_connect` (`POST /api/me/connectors/{name}/connect`), qui rend une
`FlowStart`.

⚠️ **L'autz déclarée est `SUB_ONLY`, et l'escalade de scope reste dans le handler.**
Ce n'est pas un contournement : le palier exigé DÉPEND du corps (`scope`), et surtout
l'ORDRE des refus est observable — `no_org_context` (400) avant `not_org_shareable`
(400) avant `forbidden` (403). Une règle d'autz qui trancherait en amont rendrait 403
là où la route rend 400 depuis toujours. Même choix, même raison, que
`me_credentials._clear`.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel

from .. import access, connectors, roles
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_BASE = "/api/me/connectors/{name}/session"
_SCOPES = ("member", "org", "group")


# --- Entrées ----------------------------------------------------------------

class SessionStartInput(BaseModel):
    name: str                       # placeholder {name} — le connecteur visé
    # Connecteur GÉNÉRIQUE (`browser`, oto-private#79) : le SITE vient de l'appel —
    # `?url=` ouvre la Live View sur la page de connexion demandée. Absent (les
    # connecteurs à site unique) ⇒ la `login_url` enregistrée.
    url: Optional[str] = None


class SessionFinalizeInput(BaseModel):
    name: str
    # ⚠️ Ces deux-là sont REQUIS mais déclarés facultatifs À DESSEIN : la route rend
    # `400 missing_params` quand ils manquent, pas le `400 invalid_input` de pydantic.
    # Les rendre obligatoires changerait le code d'erreur d'un chemin déjà servi.
    context_id: str = ""
    session_id: str = ""
    # Niveau de configuration de l'instance (ADR 0038/0044) : member (défaut, ma
    # session perso), org (partagée à toute l'org), group (partagée à l'équipe).
    scope: str = "member"
    # Compte du coffre visé — connecteur générique : le site (host).
    account: str = ""
    # Persister SANS la vérification générique de login. Refusé par le seam pour un
    # connecteur à site unique, dont le verify est une vraie sonde d'API.
    force: bool = False


# --- Sorties ----------------------------------------------------------------

class SessionOpened(BaseModel):
    """La Live View est une fenêtre de navigateur RÉELLE, à ouvrir dans un iframe ou un
    onglet : c'est l'utilisateur qui s'y connecte, à la main. `context_id` et
    `session_id` sont à rendre tels quels à `…/finalize` — ils sont liés au `sub` qui a
    ouvert la session (anti-IDOR) et expirent."""
    live_view_url: str
    context_id: str
    session_id: str


class SessionFinalized(BaseModel):
    """⚠️ **`connected: false` n'est pas une erreur.** C'est « pas encore logué » : la
    session vit toujours, l'utilisateur n'a simplement pas fini de se connecter, et
    l'appelant peut réessayer. Rien n'a été écrit au coffre dans ce cas."""
    connected: bool
    scope: str
    account: str


# --- Handlers ---------------------------------------------------------------

def _session_connector(name: str):
    from .. import browser_session
    if not browser_session.is_session_connector(name):
        raise AuthzDenied(404, "not_a_session_connector")
    return browser_session


async def _start(ctx: ResolvedCtx, inp: SessionStartInput) -> dict:
    browser_session = _session_connector(inp.name)
    url = (inp.url or "").strip() or None
    try:
        return await asyncio.to_thread(
            lambda: browser_session.start(ctx.sub, inp.name, login_url=url))
    except browser_session.SessionError as e:
        raise AuthzDenied(503, "browserbase_unavailable", str(e))


async def _finalize(ctx: ResolvedCtx, inp: SessionFinalizeInput) -> dict:
    browser_session = _session_connector(inp.name)
    if not inp.context_id or not inp.session_id:
        raise AuthzDenied(400, "missing_params")
    scope = (inp.scope or "member").strip()
    if scope not in _SCOPES:
        raise AuthzDenied(400, "invalid_scope")
    group_id = None
    if scope in ("org", "group"):
        # ⚠️ Ordre des refus PRÉSERVÉ : contexte manquant (400) avant partageabilité
        # du connecteur (400) avant droit d'admin (403).
        org_id = access.current_org(ctx.sub)
        if org_id is None:
            raise AuthzDenied(400, "no_org_context")
        if not connectors.is_org_shareable(inp.name):
            raise AuthzDenied(400, "not_org_shareable")
        if scope == "org":
            if not roles.is_org_admin(ctx.sub, org_id):
                raise AuthzDenied(403, "forbidden")
        else:
            group_id = access.current_group(ctx.sub)
            if group_id is None:
                raise AuthzDenied(400, "no_group_context")
            if not roles.can_admin_group(ctx.sub, group_id):
                raise AuthzDenied(403, "forbidden")
    account = (inp.account or "").strip()
    try:
        connected = await browser_session.finalize(
            ctx.sub, inp.name, inp.context_id, inp.session_id, scope=scope,
            group_id=group_id, account=account, force=bool(inp.force))
    except browser_session.SessionError as e:
        raise AuthzDenied(502, "session_verify_failed", str(e))
    return {"connected": connected, "scope": scope, "account": account}


_DOC_START = (
    "Ouvre une fenêtre de navigateur hébergée pour que JE me connecte à la main au site "
    "d'un connecteur à session (ADR 0026). Rend l'URL de la Live View à afficher, plus "
    "le couple `context_id`/`session_id` à rendre tel quel à `…/finalize`. `url` ne sert "
    "qu'au connecteur générique, dont le site vient de l'appel. `404 "
    "not_a_session_connector` si le connecteur ne se connecte pas ainsi ; `503 "
    "browserbase_unavailable` si le substrat navigateur ne répond pas."
)
_DOC_FINALIZE = (
    "Vérifie que la connexion a abouti dans la fenêtre ouverte par `…/start`, et, si "
    "oui, persiste la session au coffre — c'est un credential comme un autre. ⚠️ "
    "`connected: false` veut dire « pas encore logué », pas « échec » : rien n'a été "
    "écrit, on peut réessayer. `scope` pose l'instance au niveau `member` (défaut), "
    "`org` ou `group` — ces deux-là exigent d'être admin du palier ET un connecteur "
    "partageable. La session doit avoir été émise par `…/start` pour MOI : un "
    "`context_id` tiers n'est jamais persisté."
)

CAPABILITIES += [
    Capability(
        key="me.browser_session.start", handler=_start, Input=SessionStartInput,
        authz=SUB_ONLY, Output=SessionOpened, description=_DOC_START,
        mcp=None,   # un context_id EST le credential, et le geste exige un humain
        rest=RestBinding("POST", _BASE + "/start"),
    ),
    Capability(
        key="me.browser_session.finalize", handler=_finalize,
        Input=SessionFinalizeInput, authz=SUB_ONLY, Output=SessionFinalized,
        description=_DOC_FINALIZE,
        mcp=None,
        rest=RestBinding("POST", _BASE + "/finalize"),
    ),
]
