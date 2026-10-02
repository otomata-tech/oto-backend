"""Meta Ads — obtenir l'autorisation (Facebook Login for Business).

Flux hébergé par oto, sur le patron commun (`connectors/flow` + `auth/flow`), copie
de celui d'`instagram_meta` :

1. « Connecter » sur la fiche → `_start_flow` rend l'URL du dialogue, avec un
   `state` signé qui porte l'identité ;
2. Meta ramène le navigateur sur `/api/meta_ads/oauth/callback`
   (`api/meta_ads.py`) ; le code y devient un jeton ;
3. le jeton part au coffre, palier MEMBRE.

**Pas de renouvellement.** La configuration attendue côté Meta émet un jeton
d'utilisateur système (BISU), sans échéance. Si elle émet un jeton à durée de vie,
l'échéance est rangée dans `meta.expires_at` et la fiche passe « à reconnecter » au
premier rejet — on ne clone pas la passe quotidienne d'`instagram_meta` pour un cas
qu'on déconseille.

⚠️ Les coordonnées de l'application Meta (App ID, secret, configuration) vivent en
base, scope PLATEFORME de `connector_settings`, posées par l'exploitant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import credentials_store, status_hints
from ..connectors import flow as connector_flow
from ..connectors import link as connector_link
from . import flow as oauth_flow

logger = logging.getLogger("oto_mcp.auth.meta_ads")

CONNECTOR = "meta_ads"

_AUD = "meta_ads"
_CALLBACK_PATH = "/api/meta_ads/oauth/callback"

#: `config_id` = la configuration Facebook Login for Business (permissions + type
#: de jeton). Seul `app_secret` est secret — et porte le suffixe `_secret` que la
#: console admin masque.
_REGLAGES = ("app_id", "app_secret", "config_id")

_COMMANDE = ('oto_admin_connector_setting(op="set", connector="meta_ads", '
             'key="<key>", value="<value>")')


def _coeur():
    """`oto.tools.meta_ads` d'oto-core, importé À L'APPEL — le connecteur reste
    monté (et refuse en le disant) quand le tag épinglé ne le porte pas encore.
    `importlib` : `oto` est un package d'espace de noms (cf. `instagram_meta`)."""
    import importlib

    try:
        return importlib.import_module("oto.tools.meta_ads")
    except ImportError as e:
        raise RuntimeError(
            "The `meta_ads` connector is not installed on this instance: its core "
            "lives in oto-core and the pinned tag does not ship it yet. This is an "
            "instance configuration issue, not your account — tell the operator. "
            f"Detail: {e}") from e


# --- les coordonnées de l'application ----------------------------------------

def _reglages() -> dict:
    """Lecture FROIDE, jamais sur le chemin d'un appel d'outil : seulement au
    démarrage d'un flux ou au retour d'un consentement."""
    from ..db import connector_settings as store

    return {r["key"]: (r["value"] or "").strip()
            for r in store.list_connector_settings()
            if r["scope_type"] == "platform" and r["connector"] == CONNECTOR
            and r["key"] in _REGLAGES}


def coordonnees_manquantes() -> list[str]:
    poses = _reglages()
    return [nom for nom in _REGLAGES if not poses.get(nom)]


def app():
    """La `MetaAdsApp` de l'instance, ou un refus qui NOMME ce qui manque."""
    manquantes = coordonnees_manquantes()
    if manquantes:
        raise RuntimeError(
            f"The `meta_ads` connector is not configured on this instance: missing "
            f"{', '.join(manquantes)}. Nothing to do on your account — the instance "
            f"operator sets them with {_COMMANDE}.")
    poses = _reglages()
    return _coeur().MetaAdsApp(app_id=poses["app_id"],
                               app_secret=poses["app_secret"],
                               config_id=poses["config_id"])


def app_disponible(sub: str) -> bool:
    del sub
    return not coordonnees_manquantes()


# --- le state signé ----------------------------------------------------------

def _ctx_org(sub: str) -> int:
    from .. import access  # lazy : évite tout cycle d'import au boot

    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "No org in context — cannot scope the Meta Ads connection. Sign in "
            "again and retry.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "") -> str:
    return oauth_flow.sign_state(_AUD, {"sub": sub, "org": org_id, "app": return_app})


def verify_state(state: str) -> Optional[tuple[str, int, str]]:
    """`(sub, org_id, return_app)` si le state est valide et émis POUR ce flux."""
    data = oauth_flow.read_state(_AUD, state)
    if not data:
        return None
    sub, org, return_app = data.get("sub"), data.get("org"), data.get("app")
    if not isinstance(sub, str) or not isinstance(org, int):
        return None
    return sub, org, return_app if isinstance(return_app, str) else ""


# --- démarrage du flux -------------------------------------------------------

def build_auth_url(sub: str, return_app: str = "") -> str:
    org_id = _ctx_org(sub)
    return _coeur().authorize_url(
        app(), oauth_flow.redirect_uri(_CALLBACK_PATH),
        make_state(sub, org_id, oauth_flow.resolve_return_app(return_app)))


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    from ..capabilities._types import AuthzDenied

    try:
        return connector_flow.FlowStart(
            auth_url=build_auth_url(ctx.sub, (values or {}).get("app") or ""))
    except RuntimeError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


connector_flow.declare(
    CONNECTOR,
    start=_start_flow,
    label="Authorize oto on Facebook",
    callback_path=_CALLBACK_PATH,
    app_ready=app_disponible,
)


# --- le coffre ---------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope(org_id: int, sub: str) -> tuple[str, str]:
    return credentials_store.MEMBER, credentials_store.member_id(org_id, sub)


def persist_grant(sub: str, org_id: int, grant) -> dict:
    """Le jeton est le secret (`secret_kind="oauth"`) ; le reste va dans `meta`.

    `client_business_id` n'existe que pour un jeton BISU : il dit QUEL portefeuille
    business a autorisé. `expires_at` n'est rangé que si Meta en a donné une."""
    maintenant = datetime.now(timezone.utc)
    meta = {"user_id": grant.user_id, "name": grant.name,
            "client_business_id": grant.client_business_id,
            "connected_at": _iso(maintenant)}
    if grant.expires_in:
        meta["expires_at"] = _iso(maintenant + timedelta(seconds=grant.expires_in))
    entity_type, entity_id = _scope(org_id, sub)
    credentials_store.set_credential(entity_type, entity_id, CONNECTOR,
                                     grant.access_token, set_by=sub, meta=meta)
    logger.info("meta_ads : compte connecté (org=%s, business=%s, échéance=%s)",
                org_id, grant.client_business_id or "-",
                meta.get("expires_at") or "aucune")
    return {"name": grant.name, "expires_at": meta.get("expires_at")}


def _row(org_id: int, sub: str) -> Optional[dict]:
    entity_type, entity_id = _scope(org_id, sub)
    return credentials_store.get_credential_with_meta(entity_type, entity_id, CONNECTOR)


# --- ce que la fiche affiche -------------------------------------------------

def _link_state(sub: str) -> connector_link.LinkState:
    from .. import access  # lazy

    org = access.current_org(sub)
    if org is None:
        return connector_link.LinkState(linked=False)
    row = _row(org, sub)
    if not row:
        return connector_link.LinkState(linked=False)
    meta = row.get("meta") or {}
    return connector_link.LinkState(
        linked=True, accounts=1, set_at=str(row.get("set_at") or "") or None,
        health_ko=True if meta.get("health_ko") else None,
        health_reason=meta.get("health_reason") or None)


connector_link.register(CONNECTOR, _link_state)


def _etape_manquante(sub: str, org, group, entry: dict) -> Optional[str]:
    """Ce qu'il reste à faire — et à QUI. Sans coordonnées d'application, ce n'est
    pas à l'utilisatrice de cliquer « Connecter » en boucle."""
    del org, group, entry
    if coordonnees_manquantes():
        return "Meta app to be configured by the operator"
    etat = _link_state(sub)
    if not etat.linked:
        return "Authorize oto on Facebook"
    if etat.health_ko:
        return "Authorization revoked — reconnect"
    return None


status_hints.register(CONNECTOR, _etape_manquante)


def avertir_au_demarrage() -> None:
    """Dit AU BOOT ce qui empêchera le connecteur de servir. Ne lève jamais."""
    try:
        manquantes = coordonnees_manquantes()
    except Exception as e:  # noqa: SILENT — au boot la base peut n'être pas prête
        logger.info("meta_ads : configuration non vérifiable au démarrage (%s) — "
                    "le premier flux tranchera.", type(e).__name__)
        return
    if manquantes:
        logger.warning(
            "meta_ads : connecteur monté mais NON configuré — %s manquante(s). "
            "Le bouton « Connecter » refusera en le disant. Poser : %s",
            ", ".join(manquantes), _COMMANDE)
