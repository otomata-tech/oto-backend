"""Messagerie hébergée Unipile, côté MEMBRE : connecter, réconcilier, lire, délier.

Quatre routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `POST   /api/me/unipile/connect`   → URL de hosted-auth (ou adoption d'un compte déjà lié)
- `POST   /api/me/unipile/reconcile` → poll-and-bind explicite
- `GET    /api/me/unipile`           → statut per-user, avec self-heal opportuniste
- `DELETE /api/me/unipile`           → soft-déconnexion DANS l'org courante

⚠️ **`/api/me/unipile/connect` est SUPERSÉDÉ et vit jusqu'à la bascule du front.** Son
successeur est la capacité générique `me.connector_connect`
(`POST /api/me/connectors/{name}/connect`), qui rend une `FlowStart` — le corps partagé
vit déjà dans `unipile_connect.hosted_auth_url`, appelé par les deux. On le porte quand
même en capacité : la dette se rembourse, et sa suppression future devient une ligne.

⚠️ **Le webhook (`POST /api/unipile/webhook`) NE migre pas et ne migrera pas** : Unipile
l'appelle server-to-server, sans en-tête d'auth — or l'adaptateur REST authentifie
toujours. Il est gardé par un **nonce** non devinable, et reste classé par NATURE.

**Pas de face MCP** (`mcp=None`). La face agent de ce geste existe déjà et c'est
`me.connector_connect` ; en ajouter une seconde ici recréerait, du côté MCP, exactement
le doublon que ce chantier supprime du côté REST.

⚠️ **Deux replis qui ressemblent à des bugs et n'en sont pas :**
- `GET /api/me/unipile` **réconcilie** avant de répondre (le webhook hosted-auth v2 n'est
  pas livré) — best-effort, jamais fatal pour le statut, et no-op sans pending, donc sans
  appel réseau ;
- `DELETE` est une **soft**-déconnexion : le compte survit chez Unipile et la ligne
  survit comme PREUVE DE PROPRIÉTÉ, ce qui rend le rebind déterministe à la reconnexion.
  Elle est par-ORG, comme l'affichage : ce qu'on voit est ce qu'on déconnecte.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from pydantic import BaseModel

from .. import access, db
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_ME = "/api/me/unipile"


# --- Entrées ----------------------------------------------------------------

class UnipileConnectInput(BaseModel):
    channel: str = "linkedin"
    # Passer outre le refus anti-doublon cross-org (#172) : reconnecter un login déjà
    # porté sous la clé d'une AUTRE org créerait un second compte.
    force: bool = False
    # 'recruiter' | 'sales_navigator' — produit LinkedIn à ACTIVER à la connexion,
    # sans quoi ces APIs répondent 403 (classic seul).
    premium: Optional[str] = None
    # Front d'origine : sans lui, la fin du wizard repart chez oto-dashboard quel que
    # soit le produit qui a demandé la connexion. Le dashboard ne l'envoie pas et
    # retombe donc sur sa propre destination, inchangée.
    app: Optional[str] = None


class UnipileReconcileInput(BaseModel):
    """Aucun paramètre : on lie ce que CE compte vient de connecter."""


class UnipileStatusInput(BaseModel):
    """Aucun paramètre : le statut est celui du porteur du jeton, dans son org active."""


class UnipileDisconnectInput(BaseModel):
    # Canal côté front ('linkedin', 'whatsapp'…) ; mis en MAJUSCULES pour le coffre.
    channel: str = "linkedin"


# --- Sorties ----------------------------------------------------------------

class UnipileChannel(BaseModel):
    connected: bool
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    connected_at: Optional[str] = None


class UnipileElsewhere(BaseModel):
    """Un compte à MOI, connecté sous une AUTRE org avec la même clé plateforme : il est
    adoptable ici en un clic — le bouton « Connecter » l'adopte côté backend, sans wizard."""
    account_id: str
    account_name: Optional[str] = None
    org_id: Optional[int] = None


class UnipileStatusView(BaseModel):
    """⚠️ **`channels` ne liste que les comptes liés à l'org COURANTE.** Le binding est un
    acte par org (modèle explicite) : un canal vu « déconnecté » ici peut être connecté
    ailleurs — c'est précisément ce que dit `elsewhere`, et pourquoi une résurgence
    cross-org n'est plus possible.

    `subscribed` est le gate du bouton « connecter » : vrai si l'org apporte sa propre
    clé (BYO) ou si l'option de messagerie hébergée lui a été accordée. `mode` en dit
    l'ORIGINE (`user`|`group`|`org`|`platform`|`over_quota`|`forbidden`)."""
    subscribed: bool
    mode: Optional[str] = None
    byo: bool
    channels: dict[str, UnipileChannel]
    elsewhere: dict[str, UnipileElsewhere]


class UnipileConnectView(BaseModel):
    """⚠️ **Deux issues, deux formes.** Le cas ordinaire rend `{url}` : la page de
    consentement à ouvrir. Le cas ADOPTION rend `{adopted, channel, account_name}` et
    **pas d'`url`** — le compte était déjà connecté sous cette identité dans une autre
    org, il vient d'être rattaché ici, il n'y a aucun consentement à donner. Le front
    doit rafraîchir plutôt qu'ouvrir une fenêtre."""
    url: Optional[str] = None
    adopted: Optional[bool] = None
    channel: Optional[str] = None
    account_name: Optional[str] = None


class UnipileReconcileView(BaseModel):
    """`bound: false` avec `accounts: []` = rien à lier, pas une panne (aucun pending)."""
    bound: bool
    accounts: list[Any]


class UnipileDisconnected(BaseModel):
    ok: bool


# --- Handlers ---------------------------------------------------------------

async def _connect(ctx: ResolvedCtx, inp: UnipileConnectInput) -> dict:
    """Hosted-auth Unipile : génère l'URL où l'user connecte SON compte sous
    l'abonnement partagé (clé de son org). Per-user (pas admin)."""
    # Corps partagé REST + MCP (`unipile_connect_start`) : gates + nonce +
    # hosted_auth_link vivent dans `unipile_connect`.
    from .. import unipile_connect
    try:
        out = await unipile_connect.hosted_auth_url(
            ctx.sub, str(inp.channel or "linkedin"),
            force=bool(inp.force),
            premium=(str(inp.premium).strip().lower() if inp.premium else None),
            app=(str(inp.app) if inp.app else None))
    except unipile_connect.ConnectRefused as e:
        # ⚠️ Forme HISTORIQUE conservée : 502 (échec amont) et 409 (doublon cross-org,
        # #172) portent un message actionnable, servi À LA PLACE du code machine dans
        # `error` ; les autres exposent leur code. Le champ `error` est donc de la PROSE
        # pour ces deux-là — c'est ce qui est servi depuis toujours.
        raise AuthzDenied(e.status, e.message if e.status in (409, 502) else e.code)
    # Adoption (binding-par-org) : le compte connecté ailleurs a été lié ICI sans
    # wizard → pas d'URL, le front rafraîchit ({adopted, account_name, channel}).
    if out.get("adopted"):
        return out
    return {"url": out["url"]}


async def _reconcile(ctx: ResolvedCtx, inp: UnipileReconcileInput) -> dict:
    """Poll-and-bind explicite (webhook v2 non livré) : lie le compte que `sub` vient
    de connecter. Le dashboard peut l'appeler au retour du hosted-auth. Idempotent."""
    from .. import unipile_connect
    return await asyncio.to_thread(unipile_connect.reconcile_pending, ctx.sub)


async def _status(ctx: ResolvedCtx, inp: UnipileStatusInput) -> dict:
    """Statut de connexion per-user. **Self-heal** : le webhook hosted-auth v2 n'étant
    pas livré, on réconcilie (poll-and-bind) les comptes fraîchement connectés au
    chargement du statut — no-op sans pending (donc sans appel Unipile). Best-effort :
    jamais fatal pour le statut."""
    from .. import unipile_connect
    try:
        await asyncio.to_thread(unipile_connect.reconcile_pending, ctx.sub)
    except Exception:  # noqa: BLE001 — réconciliation opportuniste, jamais bloquante
        logger.warning("unipile status: reconcile best-effort échoué", exc_info=True)
    from ..tools import unipile
    return unipile.status_for(ctx.sub)


def _disconnect(ctx: ResolvedCtx, inp: UnipileDisconnectInput) -> dict:
    """SOFT-déconnecte le canal DANS CETTE ORG (ne supprime pas le compte chez Unipile ;
    la ligne survit comme preuve de propriété → rebind déterministe à la reconnexion).
    Par-org : le binding est un acte par org — et l'affichage ne montrant QUE les
    bindings de l'org courante, ce qu'on voit est ce qu'on déconnecte (ex-#221)."""
    provider = str(inp.channel or "linkedin").upper()
    db.clear_unipile_account(ctx.sub, access.current_org(ctx.sub), provider)
    return {"ok": True}


_DOC_CONNECT = (
    "Démarre la connexion d'un compte de messagerie hébergée sous l'abonnement de mon "
    "org. Rend `{url}` : la page de consentement à ouvrir. ⚠️ Deuxième issue possible — "
    "`{adopted: true, channel, account_name}` **sans url** : le compte était déjà "
    "connecté sous mon identité dans une autre org et vient d'être rattaché ici, il n'y "
    "a rien à consentir, il faut rafraîchir. `premium` active un produit LinkedIn "
    "(`recruiter`, `sales_navigator`) sans lequel ces APIs répondent 403."
)
_DOC_RECONCILE = (
    "Lie explicitement le compte que je viens de connecter (poll-and-bind), à appeler au "
    "retour du consentement. Idempotent. `bound: false` avec `accounts: []` veut dire "
    "« rien à lier », pas « panne »."
)
_DOC_STATUS = (
    "L'état de ma messagerie hébergée DANS L'ORG COURANTE : canaux connectés, origine de "
    "la clé, et option débloquée ou non. ⚠️ `channels` ne montre que les comptes liés à "
    "CETTE org — un canal vu déconnecté peut être connecté ailleurs, et `elsewhere` le "
    "dit alors, avec ce qui est adoptable ici en un clic."
)
_DOC_DISCONNECT = (
    "Délie un canal DE CETTE ORG. ⚠️ Déconnexion SOUPLE : le compte survit chez le "
    "fournisseur et la ligne survit comme preuve de propriété, ce qui rend la "
    "reconnexion déterministe. Ce qui est affiché est ce qui est délié — jamais un "
    "binding d'une autre org."
)

CAPABILITIES += [
    Capability(
        key="me.unipile.connect", handler=_connect, Input=UnipileConnectInput,
        authz=SUB_ONLY, Output=UnipileConnectView, description=_DOC_CONNECT,
        mcp=None,   # la face agent est `me.connector_connect` — pas de second chemin
        rest=RestBinding("POST", _ME + "/connect"),
    ),
    Capability(
        key="me.unipile.reconcile", handler=_reconcile, Input=UnipileReconcileInput,
        authz=SUB_ONLY, Output=UnipileReconcileView, description=_DOC_RECONCILE,
        mcp=None,
        rest=RestBinding("POST", _ME + "/reconcile"),
    ),
    Capability(
        key="me.unipile.status", handler=_status, Input=UnipileStatusInput,
        authz=SUB_ONLY, Output=UnipileStatusView, description=_DOC_STATUS,
        mcp=None,
        rest=RestBinding("GET", _ME),
    ),
    Capability(
        key="me.unipile.disconnect", handler=_disconnect,
        Input=UnipileDisconnectInput, authz=SUB_ONLY, Output=UnipileDisconnected,
        description=_DOC_DISCONNECT,
        mcp=None,
        rest=RestBinding("DELETE", _ME),
    ),
]
