"""Les VERBES du consentement OAuth per-user : lire l'état, déconnecter (et, pour
`google` seul, démarrer — voir plus bas pourquoi les trois n'ont pas le même sort).

Dix routes écrites à la main jusqu'au 2026-08-27, portées en capacités (ADR 0009) —
mêmes chemins, mêmes codes, même corps sur le fil :

- `GET    /api/{atlassian,folkmcp,google}/oauth/start`  → `{auth_url}` à ouvrir
- `GET    /api/{atlassian,folkmcp,google}/oauth/status` → l'état du consentement
- `DELETE /api/{atlassian,folkmcp,google}/oauth`        → révoque
- `POST   /api/google/oauth/default`                    → élit le compte Google par défaut

⚠️ **`.start` d'atlassian et folkmcp RETIRÉ le 2026-09-04 (oto-dashboard#125), celui de
google GARDÉ.** Le dashboard démarre les trois par le chemin fixe générique depuis le
01/09 (`POST /api/me/connectors/{name}/connect`, commit dashboard `433d563`), mais ce
sont des routes REST PUBLIQUES : mesuré sur `tool_calls` (30 jours, toutes origines,
`oto_admin_monitoring op=rest route=…`) avant tout retrait, PAS supposé sur la seule
disparition de l'appelant chez nous. Résultat : `atlassian`/`folkmcp` à **0 appel**,
`google` à **2** (2 utilisateurs, dernier le 02/09 — un jour après le cutover dashboard,
compatible avec un cache navigateur pas encore rafraîchi, mais non confirmé). D'où le
retrait des deux premiers et le maintien du troisième — la mesure décide route par
route, jamais par famille.

⚠️ **`.disconnect` d'atlassian et folkmcp RETIRÉ le 2026-09-04 (même lot, même mesure)**
— `DELETE /api/{atlassian,folkmcp}/oauth` à **0 appel/30j, avant ET après** le bascule du
dashboard vers `me.connector_disconnect` (`oto-dashboard v1.56.0`, `capabilities/
connectors/oauth_status.py`) : zéro indépendant du timing, contrairement à `.status`
(3 appels folkmcp le 01/09, 3 jours AVANT le bascule — trafic réel, pas un artefact,
**gardé**, remesure prévue après le 04/10/2026 une fois 30 jours pleins écoulés depuis
le bascule). `me.connector_disconnect` ne dépend PAS de cette capacité : il rappelle
`atlassian_oauth.disconnect`/`folk_oauth.disconnect` directement
(`capabilities/connectors/oauth_status.py`), donc son retrait ne change AUCUN
comportement servi ailleurs.

⚠️ **Les CALLBACKS ne migrent pas** (`…/oauth/callback`, un par fournisseur) : le
fournisseur y redirige le NAVIGATEUR, sans en-tête d'auth, et la réponse est une **302**,
pas du JSON. L'adaptateur REST authentifie toujours et répond toujours en JSON : ils sont
hors du moule par construction, et restent classés par NATURE dans leurs modules.

**Deux familles, pas trois.** `atlassian` et `folkmcp` fédèrent un MCP distant : leur
jeton per-user vit au coffre et `tools/mount.py` l'injecte par requête. Leur surface est
identique au champ près, d'où des `Input`/`Output` partagés — le dashboard les pilote
d'ailleurs par un client GÉNÉRIQUE (`/api/${name}/oauth/…`), ce qui rend cette symétrie
contractuelle et pas cosmétique. `google` est à part : il porte PLUSIEURS comptes,
donc un statut plus riche et un verbe de plus (élire le défaut).

**Pas de face MCP** (`mcp=None`) : ouvrir une page de consentement demande un
navigateur, et le pendant agent générique existe déjà (`me.connector_connect`). Un
second chemin par fournisseur recréerait le doublon que ce chantier supprime.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import access, db
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


# --- Entrées ----------------------------------------------------------------

class OAuthStartInput(BaseModel):
    """Aucun paramètre : le consentement est demandé pour le porteur du jeton."""


class OAuthStatusInput(BaseModel):
    """Aucun paramètre."""


class GoogleRevokeInput(BaseModel):
    # Compte précis à révoquer ; ABSENT = TOUS les comptes Google du sub.
    account: Optional[str] = None


class GoogleDefaultInput(BaseModel):
    account: str = ""


# --- Sorties ----------------------------------------------------------------

class OAuthStart(BaseModel):
    """L'URL de consentement à OUVRIR dans un navigateur. Rien n'est connecté tant que
    l'utilisateur n'y est pas passé — c'est le callback du fournisseur qui persiste."""
    auth_url: str


class FederationStatus(BaseModel):
    """État d'une fédération MCP per-user. `set_at` = quand le jeton a été posé ; `null`
    avec `connected: false` est l'état normal d'un compte jamais connecté."""
    connected: bool
    set_at: Optional[str] = None


class FederationDisconnected(BaseModel):
    """`disconnected: false` = il n'y avait rien à déconnecter (idempotent), pas un échec.

    ⚠️ Plus utilisée par une capacité DE CE FICHIER depuis le retrait de `.disconnect`
    (2026-09-04, oto-dashboard#125) — importée telle quelle par
    `capabilities/connectors/oauth_status.py::me.connector_disconnect`, qui la
    réutilise comme contrat de sortie. Ne pas supprimer en la croyant morte."""
    ok: bool
    disconnected: bool


class GoogleAccount(BaseModel):
    email: Optional[str] = None
    is_default: bool = False
    scopes: list[str] = []
    granted_at: Optional[str] = None


class GoogleStatus(BaseModel):
    """⚠️ **Les champs racine décrivent le compte PAR DÉFAUT, pas l'union des comptes.**
    C'est un héritage du temps où Google était mono-compte, conservé pour compat :
    `granted_at`/`scopes` valent pour le défaut seul. La vérité multi-compte est
    `accounts`, et un intégrateur doit lire celle-là."""
    connected: bool
    granted_at: Optional[str] = None
    scopes: list[str] = []
    accounts: list[GoogleAccount]


class GoogleRevoked(BaseModel):
    """`account: null` veut dire que TOUS les comptes ont été révoqués, pas « aucun »."""
    ok: bool
    account: Optional[str] = None


class GoogleDefaultSet(BaseModel):
    ok: bool
    default: str


# --- Fédérations MCP per-user (atlassian, folkmcp) --------------------------

def _federation(nom: str, module_attr: str):
    """Le seul verbe restant d'une fédération (`status`), branché sur SON module OAuth.

    Le module est résolu à l'APPEL (import paresseux) : ces modules montent des clients
    HTTP et lisent la config au chargement, on ne les tire pas à l'import du registre.

    ⚠️ **Pas de `.start` ni de `.disconnect` ici** (retirés le 2026-09-04,
    oto-dashboard#125) : mesurés sur `tool_calls` (30 jours, toutes origines,
    `oto_admin_monitoring op=rest route=…`) à **0 appel** pour `atlassian` ET
    `folkmcp`, avant ET après le bascule dashboard (`.disconnect` était déjà à zéro
    dans les deux fenêtres — zéro indépendant du timing). `me.connector_disconnect`
    (`capabilities/connectors/oauth_status.py`) ne dépend pas de la capacité retirée :
    il rappelle `atlassian_oauth.disconnect`/`folk_oauth.disconnect` directement.
    `.status`, lui, est GARDÉ : du trafic réel a été mesuré 3 jours avant le bascule
    (folkmcp, 01/09) — remesure prévue après le 04/10/2026 (30 jours pleins depuis le
    bascule dashboard) avant de trancher son sort. `google` reste à part : cf.
    `_google_start` ci-dessous, la mesure décide route par route, jamais par famille."""
    def _module():
        from ..auth import atlassian as atlassian_oauth
        from ..auth import folk as folk_oauth
        return {"atlassian_oauth": atlassian_oauth, "folk_oauth": folk_oauth}[module_attr]

    def _status(ctx: ResolvedCtx, inp: OAuthStatusInput) -> dict:
        return _module().status_for(ctx.sub)

    base = f"/api/{nom}/oauth"
    return [
        Capability(
            key=f"me.federation.{nom}.status", handler=_status, Input=OAuthStatusInput,
            authz=SUB_ONLY, Output=FederationStatus, mcp=None,
            description=(f"Mon consentement {nom} est-il posé, et depuis quand. "
                         "`connected: false` avec `set_at: null` est l'état normal d'un "
                         "compte jamais connecté."),
            rest=RestBinding("GET", base + "/status"),
        ),
    ]


# --- Google (multi-compte) --------------------------------------------------

def _google_start(ctx: ResolvedCtx, inp: OAuthStartInput) -> dict:
    from ..auth import google as google_oauth
    try:
        url = google_oauth.build_auth_url(ctx.sub)
    except RuntimeError as e:
        # Forme historique EXACTE, espace compris : le code machine porte la cause.
        raise AuthzDenied(500, f"oauth_misconfigured: {e}")
    return {"auth_url": url}


def _google_status(ctx: ResolvedCtx, inp: OAuthStatusInput) -> dict:
    from ..auth import google as google_oauth
    accounts = google_oauth.list_accounts(ctx.sub)
    default = next((a for a in accounts if a.get("is_default")), None)
    return {
        "connected": bool(accounts),
        # Compat : champs au niveau racine = compte par défaut.
        "granted_at": default["granted_at"] if default else None,
        "scopes": default["scopes"].split() if default and default.get("scopes") else [],
        "accounts": [
            {
                "email": a.get("google_email"),
                "is_default": a.get("is_default", False),
                "scopes": a["scopes"].split() if a.get("scopes") else [],
                "granted_at": a.get("granted_at"),
            }
            for a in accounts
        ],
    }


def _google_revoke(ctx: ResolvedCtx, inp: GoogleRevokeInput) -> dict:
    from ..auth import google as google_oauth
    # ?account=<email> révoque un compte précis ; absent = tous.
    account = inp.account or None
    google_oauth.revoke(ctx.sub, account=account)
    return {"ok": True, "account": account}


def _google_set_default(ctx: ResolvedCtx, inp: GoogleDefaultInput) -> dict:
    account = (inp.account or "").strip()
    if not account:
        raise AuthzDenied(400, "missing_account")
    org_id = access.current_org(ctx.sub)
    if org_id is None or not db.set_default_google_account(ctx.sub, org_id, account):
        raise AuthzDenied(404, "unknown_account")
    return {"ok": True, "default": account}


_DOC_G_START = (
    "Rend l'URL de consentement Google à ouvrir. ⚠️ Un `500 oauth_misconfigured:` "
    "signale une app OAuth mal configurée CÔTÉ PLATEFORME, pas une erreur de l'appelant."
)
_DOC_G_STATUS = (
    "Mes comptes Google connectés. ⚠️ Les champs racine (`granted_at`, `scopes`) "
    "décrivent le compte PAR DÉFAUT seul — héritage du temps où Google était "
    "mono-compte. La vérité multi-compte est `accounts`."
)
_DOC_G_REVOKE = (
    "Révoque un compte Google précis (`?account=<email>`) ou, SANS paramètre, TOUS mes "
    "comptes Google. `account: null` dans la réponse veut donc dire « tous », pas « aucun »."
)
_DOC_G_DEFAULT = (
    "Élit le compte Google par défaut de mon org de contexte — celui qu'utilisent les "
    "outils quand aucun compte n'est nommé à l'appel."
)

CAPABILITIES += (
    _federation("atlassian", "atlassian_oauth")
    + _federation("folkmcp", "folk_oauth")
    + [
        Capability(
            key="me.federation.google.start", handler=_google_start,
            Input=OAuthStartInput, authz=SUB_ONLY, Output=OAuthStart, mcp=None,
            description=_DOC_G_START,
            rest=RestBinding("GET", "/api/google/oauth/start"),
        ),
        Capability(
            key="me.federation.google.status", handler=_google_status,
            Input=OAuthStatusInput, authz=SUB_ONLY, Output=GoogleStatus, mcp=None,
            description=_DOC_G_STATUS,
            rest=RestBinding("GET", "/api/google/oauth/status"),
        ),
        Capability(
            key="me.federation.google.revoke", handler=_google_revoke,
            Input=GoogleRevokeInput, authz=SUB_ONLY, Output=GoogleRevoked, mcp=None,
            description=_DOC_G_REVOKE,
            rest=RestBinding("DELETE", "/api/google/oauth"),
        ),
        Capability(
            key="me.federation.google.set_default", handler=_google_set_default,
            Input=GoogleDefaultInput, authz=SUB_ONLY, Output=GoogleDefaultSet, mcp=None,
            description=_DOC_G_DEFAULT,
            rest=RestBinding("POST", "/api/google/oauth/default"),
        ),
    ]
)
