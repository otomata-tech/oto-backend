"""Google OAuth — web flow, per-user, tokens persistés en SQLite.

Flow :
1. User authentifié (Logto JWT) appelle `GET /api/google/oauth/start` →
   on renvoie une URL Google avec un `state` HMAC-signé contenant son `sub`.
2. User redirigé vers Google, consent, redirect vers
   `/api/google/oauth/callback?code=…&state=…`.
3. On vérifie le state, échange le code contre refresh+access token,
   persiste dans le coffre chiffré (`connector_credentials`, connector='google').

Pour utiliser les credentials (côté tools datastore) : `credentials_for(sub)`
charge depuis SQLite, refresh transparent si expiré, renvoie un
`google.oauth2.credentials.Credentials` valide.

Setup ops :
- Env `GOOGLE_WORKSPACE_CLIENT_ID` + `GOOGLE_WORKSPACE_CLIENT_SECRET` —
  OAuth client de type **Web application** dans Google Cloud Console. Le backend
  émet `{OTO_MCP_PUBLIC_URL}/api/google/oauth/callback` comme redirect URI : cette
  URL EXACTE doit figurer dans les « Authorized redirect URIs » du client, sinon
  Google renvoie « requête invalide » (redirect_uri_mismatch). Depuis le cutover
  ADR 0040 (2026-07-06) le client est partagé prod + preprod → déclarer les deux :
    - `https://mcp.oto.cx/api/google/oauth/callback`    (PROD)
    - `https://mcp.oto.ninja/api/google/oauth/callback` (PREPROD)
- Env `OTO_MCP_PUBLIC_URL` (déjà utilisée pour Logto) — base pour le
  redirect URI ; en local on peut override pour pointer sur localhost.
- Env `OTO_MCP_OAUTH_STATE_SECRET` — secret HMAC pour signer le state
  anti-CSRF (générer avec `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).

**L'app d'un TENANT** (23/09/2026) — un partenaire qui veut SON écran de consentement
(sa marque, son projet Google Cloud, ses scopes vérifiés sous son nom) pose son client
comme **app d'éditeur** du connecteur `google`, dans l'espace de noms des tenants
(`editor:tenant:<slug>`, `credentials_store.tenant_app_key`) : depuis SON tableau de bord
(`tenant_apps`, `PUT /api/admin/tenants/{slug}/apps/google`) ou par l'opérateur
(`POST /api/admin/editor-apps {connector: "google", data_center: "tenant:<slug>", …}`) —
REST seulement, coffre chiffré, jamais l'env. Dès qu'elle est posée, `app_for(sub)` la
sert à tout compte qualifié sous ce tenant, et le rappel passe sur le premier host que le
tenant TIENT (`tenancy.callback_host`, ex. `https://<host>/api/google/oauth/callback`) :
c'est CETTE URL que le partenaire déclare chez Google — son client n'accepte que ses
domaines, pas les nôtres. Sans app posée, le tenant reste sur la nôtre et sur notre
rappel : l'état d'avant, à l'octet près.
⚠️ **Un jeton ne se rafraîchit qu'avec le client qui l'a émis.** Poser, changer ou retirer
l'app d'un tenant rend inutilisables les jetons émis par l'app d'avant. Le client émetteur
est noté sur le jeton (`persist_token` → meta `client_id` ; absent = notre app, celle de
tous les jetons d'avant ce lot) : un jeton d'un autre client est refusé AVANT tout appel
réseau, compte marqué, « reconnecte ce compte » — jamais purgé. Un client refusé par
Google au refresh (`unauthorized_client`/`invalid_client`) est une `GoogleClientRejected`
nommée, pas une erreur interne — et pas un grant mort (config ≠ révocation).
⚠️ Le host du tenant route vers UNE instance (la prod) : un consentement démarré en
preprod avec l'app du tenant rappelle en prod — le state y est vérifié avec le secret de
la prod. Tester l'app d'un tenant, c'est le faire là où son host arrive.
"""
from __future__ import annotations

import hmac
import hashlib
import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .. import credentials_store, db
from . import flow as oauth_flow
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link


# Scopes d'IDENTITÉ — demandés à CHAQUE consentement, quel que soit le service : c'est
# par eux que le compte se NOMME (`userinfo` → email), sans dépendre du scope Gmail
# comme avant le split (un consentement Drive seul n'a pas de profil Gmail à lire).
# Non sensibles chez Google.
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")

# Les scopes de CHAQUE service — un connecteur par service depuis le split du
# 2026-09-26 (`providers/google.service`) : connecter Drive ne demande que Drive, en
# autorisation incrémentale sur le même compte (`include_granted_scopes`). Un tenant
# n'a plus à faire vérifier chez Google un service qu'il n'offre pas, et une personne
# qui ne veut que son agenda ne livre pas sa boîte mail.
SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    # Scope SENSIBLE → vérification Google à la publication, pas d'audit CASA.
    "sheets": ("https://www.googleapis.com/auth/spreadsheets",),
    # Drive COMPLET (RESTRICTED) — gérer TOUS les fichiers du user (pas seulement
    # ceux créés par oto). Couvre aussi l'export datastore (#29). Supersede drive.file.
    "drive": ("https://www.googleapis.com/auth/drive",),
    # Gmail surface complète (read/send/reply/draft/archive/trash). RESTRICTED →
    # audit CASA requis si l'écran de consentement passe en published.
    "gmail": ("https://www.googleapis.com/auth/gmail.modify",),
    # Google Tasks (read/write). SENSIBLE, pas restricted.
    "tasks": ("https://www.googleapis.com/auth/tasks",),
    # Google Calendar (read/write events). SENSIBLE, pas restricted.
    "calendar": ("https://www.googleapis.com/auth/calendar",),
    # Google Chat (RESTRICTED) — lire les espaces + lire/poster des messages.
    "chat": ("https://www.googleapis.com/auth/chat.spaces.readonly",
             "https://www.googleapis.com/auth/chat.messages"),
}
SERVICES: tuple[str, ...] = tuple(SERVICE_SCOPES)
SERVICE_LABELS = {"gmail": "Gmail", "drive": "Google Drive", "sheets": "Google Sheets",
                  "calendar": "Google Calendar", "tasks": "Google Tasks",
                  "chat": "Google Chat"}

# TOUS les scopes de service — ce que le COMPTE (`google`) demande sous notre app :
# l'état d'avant le split, pour un tableau de bord à carte Google unique.
SCOPES = [scope for svc in SERVICES for scope in SERVICE_SCOPES[svc]]
# Ce qu'une ligne du coffre peut porter : rien d'autre n'y entre (`persist_token`),
# même si le client d'un partenaire a d'autres scopes accordés ailleurs.
KNOWN_SCOPES = frozenset(IDENTITY_SCOPES) | frozenset(SCOPES)


def services_granted(scopes) -> list[str]:
    """Les services dont TOUS les scopes figurent dans `scopes` (chaîne ou liste) —
    ce qu'un compte a réellement autorisé, dans l'ordre des services."""
    have = set(scopes.split() if isinstance(scopes, str) else (scopes or ()))
    return [svc for svc in SERVICES if set(SERVICE_SCOPES[svc]) <= have]


def scopes_for(connector: str, app: "OAuthApp") -> list[str]:
    """Les scopes que CE consentement demande — l'identité, puis :

    - un service : les siens, et rien d'autre ;
    - le compte (`google`) : sous NOTRE app, les six services (l'état d'avant le
      split, pour un tableau de bord à carte unique) ; sous l'app d'un TENANT,
      rien de plus que l'identité — un partenaire ne demande jamais un scope que
      son projet Google ne déclare pas, ses services les ajoutent un à un.

    Un connecteur inconnu est refusé : rien ici ne devine un scope."""
    if connector == "google":
        base = list(SCOPES) if app.origin == "env" else []
    elif connector in SERVICE_SCOPES:
        base = list(SERVICE_SCOPES[connector])
    else:
        raise RuntimeError(f"« {connector} » n'est pas un service Google connu.")
    return list(IDENTITY_SCOPES) + base

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
    """Notre client — l'env, repli quand le tenant de l'appelant n'a pas posé le sien."""
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_ID")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_ID env var manquante")
    return v


def _client_secret() -> str:
    v = os.environ.get("GOOGLE_WORKSPACE_CLIENT_SECRET")
    if not v:
        raise RuntimeError("GOOGLE_WORKSPACE_CLIENT_SECRET env var manquante")
    return v


def _state_secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise RuntimeError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


_CALLBACK_PATH = "/api/google/oauth/callback"


def _redirect_uri() -> str:
    """NOTRE rappel — sur l'adresse publique de l'instance, jamais devinée
    (`config.public_base_url()` lève, tripwire `test_url_publique_sans_repli`)."""
    return oauth_flow.redirect_uri(_CALLBACK_PATH)


@dataclass(frozen=True)
class OAuthApp:
    """L'app OAuth qui demande le consentement POUR CE COMPTE, et son rappel exact.

    Les trois vont ensemble : le code s'échange et le jeton se rafraîchit avec le client
    qui a demandé le consentement, et Google n'accepte le rappel qu'au byte près chez
    CE client. Les séparer, c'est un `redirect_uri_mismatch` ou un `invalid_client`
    opaque — d'où une seule valeur, résolue une fois par geste.
    `origin` dit d'où elle vient (`tenant:<slug>` ou `env`) : pour le journal et les
    tests, jamais pour décider."""
    client_id: str
    # Hors du `repr` : une app finit dans un message d'assertion, un log de débogage,
    # une trace — le secret n'a rien à y faire.
    client_secret: str = field(repr=False)
    redirect_uri: str
    origin: str = "env"


def app_for(sub: str) -> OAuthApp:
    """L'app à employer pour ce sub : celle de SON tenant si elle est posée, la nôtre sinon.

    Le tenant se lit sur le sub qualifié (`tenancy.tenant_of`, par préfixe, jamais par
    découpe) — dérivé du jeton, un appelant ne peut pas revendiquer l'app d'un tenant
    auquel il n'appartient pas. L'app du tenant est l'app d'éditeur du connecteur
    `google` rangée dans l'espace de noms des tenants
    (`credentials_store.tenant_app_key(slug)`), et son rappel est posé sur le premier
    host que le tenant TIENT (`tenancy.callback_host`) — jamais sur un host qu'il a
    déclaré mais qu'un autre tient : le code et le state signé partiraient chez lui.
    ⚠️ Ce host doit ROUTER vers ce backend (condition de sa déclaration,
    `docs/tenants.md`) : ce n'est pas vérifiable d'ici.

    ⚠️ Une erreur de coffre REMONTE, elle ne fait pas retomber sur l'env : sinon un
    partenaire dont l'app devient illisible verrait ses utilisateurs consentir sous
    NOTRE marque sans que rien ne le dise (même leçon que `zoho_oauth.app_fields`,
    inventaire des silences du 2026-08-27, site B7). Seule l'ABSENCE (`None`) est un
    repli légitime — c'est l'état de tout tenant qui n'a rien posé.

    **Le tenant primaire ne sonde jamais le coffre** : son app EST l'env, comme ses
    clés partagées sont les instances plateforme (`tenant_vault.rung_tenant`, même
    règle, même raison — une ligne `editor:oto` que personne ne lit serait un second
    mécanisme pour la même fonction, #409). Conséquence mesurable : à 99 % du trafic,
    ce cran ne coûte AUCUNE lecture — chaque refresh de jeton passe ici.
    """
    from .. import tenancy  # lazy : évite tout cycle d'import au boot
    registre = tenancy.current()
    slug = registre.tenant_of(sub)
    app = (credentials_store.get_editor_app("google", credentials_store.tenant_app_key(slug))
           if slug != tenancy.PRIMARY_SLUG else None)
    if app:
        host = registre.callback_host(slug)
        return OAuthApp(client_id=app["client_id"], client_secret=app["client_secret"],
                        redirect_uri=oauth_flow.redirect_uri(_CALLBACK_PATH, host=host),
                        origin=f"tenant:{slug}")
    return OAuthApp(client_id=_client_id(), client_secret=_client_secret(),
                    redirect_uri=_redirect_uri(), origin="env")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ctx_org(sub: str) -> int:
    """Org de contexte (seam `current_org`, ADR 0023) — le scope MEMBRE des comptes
    Google (ADR 0033 B3). Lève une erreur actionnable plutôt qu'un scope silencieux."""
    from .. import access  # lazy : évite tout cycle d'import au boot
    org = access.current_org(sub)
    if org is None:
        raise RuntimeError(
            "Aucune org de contexte — impossible de scoper le compte Google. "
            "Reconnecte-toi et réessaie.")
    return org


def make_state(sub: str, org_id: int, return_app: str = "",
               connector: str = "google") -> str:
    """HMAC-signed state : `<b64(payload)>.<b64(sig)>` — payload = {sub, org, ts, app, c}.

    `connector` (`c`) : QUELLE carte a demandé le consentement — le compte, ou l'un de
    ses six services (split du 2026-09-26). Le callback y renvoie ; sans lui, une
    personne qui autorisait Drive atterrissait sur la carte du compte.

    L'org du DÉMARRAGE voyage jusqu'au callback (qui vient de Google, sans les
    headers de consultation) : le compte est scopé à l'org où l'user a cliqué
    « connecter » (ADR 0033 B3).

    `return_app` porte quel FRONT a demandé la connexion. Même raison que l'org :
    le callback arrive DEPUIS Google, sans en-tête ni session — ce que le state ne
    porte pas est perdu. Sans lui, un utilisateur venu d'un front tiers atterrissait
    chez nous après avoir consenti (oto-backend#877).

    ⚠️ La valeur est validée par l'APPELANT (`resolve_return_app`) avant d'arriver
    ici : le state ne doit jamais porter une clé de front non vérifiée, sinon il
    signe une redirection ouverte."""
    payload = json.dumps({"sub": sub, "org": org_id, "ts": int(time.time()),
                          "app": return_app, "c": connector},
                         separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[tuple[str, int, str, str]]:
    """Renvoie (sub, org_id, return_app, connector) si state valide et non expiré, sinon None.

    `connector` retombe sur `"google"` pour un state émis AVANT le split : le compte,
    exactement la carte qui existait alors.

    `return_app` retombe sur `""` pour un state émis AVANT ce lot : ils vivent
    quelques minutes, il y en a en vol au déploiement, et les casser renverrait
    une erreur à quelqu'un qui vient d'autoriser correctement."""
    if not state or "." not in state:
        return None
    p_b64, sig_b64 = state.split(".", 1)
    try:
        payload = _b64url_decode(p_b64)
        sig = _b64url_decode(sig_b64)
    # noqa: SILENT — fail-closed : un callback ne distingue jamais les causes d'un refus
    except Exception:
        return None
    expected = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(payload)
    # noqa: SILENT — fail-closed : un callback ne distingue jamais les causes d'un refus
    except Exception:
        return None
    if int(time.time()) - int(data.get("ts", 0)) > _STATE_TTL:
        return None
    sub, org = data.get("sub"), data.get("org")
    if not isinstance(sub, str) or not isinstance(org, int):
        return None
    # `app` absent = state émis avant oto-backend#877 : retour au front par défaut,
    # jamais un refus. Re-validé ici bien qu'il ait déjà été filtré au départ : le
    # state est signé, mais une clé retirée de `RETURN_APPS` entre le clic et le
    # retour ne doit pas ressusciter par sa signature.
    from . import flow as oauth_flow

    connector = data.get("c") or "google"
    if connector != "google" and connector not in SERVICE_SCOPES:
        return None
    return sub, org, oauth_flow.resolve_return_app(data.get("app") or ""), connector


def build_auth_url(sub: str, return_app: str = "", connector: str = "google") -> str:
    """L'URL de consentement Google — pour le compte, ou pour UN service (ses scopes
    seulement, cf. `scopes_for`).

    `return_app` : clé de front déclarée par l'APPELANT (ex. un front tiers), jamais
    un Origin sniffé — les capacités sont transport-agnostiques (ADR 0009). Validée
    ICI, une seule fois, AVANT `make_state` : `resolve_return_app` réduit toute
    valeur hors de sa liste fermée à `""`, donc le state ne porte jamais une valeur
    de client non vérifiée (pas de redirection ouverte)."""
    from urllib.parse import urlencode

    from . import flow as oauth_flow

    org_id = _ctx_org(sub)
    resolved_app = oauth_flow.resolve_return_app(return_app)
    app = app_for(sub)
    params = {
        "client_id": app.client_id,
        "redirect_uri": app.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes_for(connector, app)),
        "access_type": "offline",
        # consent → force refresh_token ; select_account → laisse l'user choisir
        # quel compte Google connecter (clé du multi-compte).
        "prompt": "consent select_account",
        "state": make_state(sub, org_id, resolved_app, connector),
        # Consentement INCRÉMENTAL : le jeton porte aussi les scopes déjà accordés à ce
        # client. C'est ce qui fait tenir le split — autoriser Drive après Gmail rend UN
        # jeton qui sait les deux, sur la même ligne du coffre. Sous le client d'un
        # partenaire aussi : son client est dédié à ce produit, et ce qui entre au coffre
        # est de toute façon filtré sur `KNOWN_SCOPES` (`persist_token`).
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, sub: str) -> dict:
    """Échange le code OAuth contre tokens. Renvoie le dict de réponse Google.

    `sub` (qualifié, relu du state signé) désigne l'app qui a demandé le consentement :
    le code ne s'échange qu'avec ELLE et son rappel exact.
    Clés attendues : `access_token`, `refresh_token`, `expires_in`, `scope`.
    """
    import requests
    app = app_for(sub)
    r = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": app.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _fetch_email(access_token: str, scopes=()) -> str:
    """Récupère l'adresse du compte Google qui vient de consentir.

    Par `userinfo` dès que le scope d'identité est là (tout consentement depuis le
    split le demande) — un consentement Drive seul n'a pas de profil Gmail à lire.
    Repli sur le profil Gmail pour un jeton d'avant, qui n'a que `gmail.modify`.
    """
    import requests
    if IDENTITY_SCOPES[1] in set(scopes or ()):
        r = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        r.raise_for_status()
        email = r.json().get("email")
        if not email:
            raise RuntimeError("userinfo sans email — impossible d'identifier le compte.")
        return email
    r = requests.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    r.raise_for_status()
    email = r.json().get("emailAddress")
    if not email:
        raise RuntimeError("Profil Gmail sans emailAddress — impossible d'identifier le compte.")
    return email


def persist_token(sub: str, org_id: int, token_response: dict,
                  client_id: Optional[str] = None) -> str:
    """Persiste les tokens (scope membre : l'org vient du state, capturée au
    démarrage du flow) et renvoie l'email du compte Google connecté.

    `client_id` : le client qui a ÉMIS ce jeton, noté sur lui — un jeton ne se
    rafraîchit qu'avec son émetteur (cf. `credentials_for`). Absent, c'est l'app que
    `app_for(sub)` sert à cet instant, celle qui vient d'échanger le code."""
    refresh_token = token_response.get("refresh_token")
    if not refresh_token:
        # `build_auth_url` impose `prompt=consent` + `access_type=offline`,
        # donc Google DOIT émettre un refresh_token. Si on arrive ici, c'est
        # un problème côté Google → on remonte plutôt que de masquer.
        raise RuntimeError(
            "Google n'a pas émis de refresh_token malgré prompt=consent. "
            "Vérifie la config du client OAuth dans GCP."
        )
    access_token = token_response.get("access_token")
    expires_in = int(token_response.get("expires_in", 0) or 0)
    expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat() if expires_in else None
    # Ce qui entre au coffre : les scopes que le jeton PORTE (l'union, par
    # `include_granted_scopes`), bornés à ceux qu'on connaît — jamais ceux d'un autre
    # produit du même client. Un jeton sans champ `scope` date d'avant : il a tout.
    brut = token_response.get("scope")
    granted = ([sc for sc in brut.split() if sc in KNOWN_SCOPES] if brut
               else list(SCOPES))
    scopes = " ".join(granted)
    email = _fetch_email(access_token, granted)
    db.set_google_oauth(
        sub,
        org_id,
        google_email=email,
        refresh_token=refresh_token,
        scopes=scopes,
        access_token=access_token,
        expires_at=expires_at,
        client_id=client_id or app_for(sub).client_id,
    )
    return email


class GoogleReauthRequired(RuntimeError):
    """Refresh token Google mort (invalid_grant) → l'user doit reconnecter.

    `RuntimeError` et non `Exception` (#875/#876) : les six outils Google traduisent
    les `RuntimeError` de `credentials_for` en refus lisible, et seulement elles. Un
    grant mort finissait donc en « Erreur interne du serveur » — le seul cas où
    l'appelant a un geste précis à faire (reconnecter CE compte) était celui où on
    ne lui disait rien."""


class GoogleClientRejected(RuntimeError):
    """Google refuse le CLIENT OAuth au refresh (`unauthorized_client`,
    `invalid_client`) : le jeton a été émis par un autre client, ou la configuration du
    client est fausse (identifiant, secret).

    `RuntimeError` pour la même raison que `GoogleReauthRequired` (les outils Google ne
    traduisent qu'elles en refus lisible) — mais PAS une sous-classe : ce n'est pas un
    grant mort, et le compte n'est pas marqué (`oauth_flow.grant_is_dead` : une config
    fausse ne doit rien détruire ni faire accuser le compte)."""


def _emis_par_un_autre_client(row: dict, app: "OAuthApp") -> bool:
    """Le jeton de cette ligne a-t-il été émis par un autre client que `app` ?

    Un jeton sans client noté date d'avant cette note : il vient de NOTRE app, la seule
    qui existait alors — donc d'un autre client dès que l'app servie est celle d'un
    tenant."""
    emetteur = row.get("client_id")
    if not emetteur:
        return app.origin != "env"
    return emetteur != app.client_id


def config_dashboard(sub) -> str:
    """Le tableau de bord de SON produit — où une carte de service se connecte."""
    from .. import config
    return config.dashboard_url_for(sub)


def _reconnecter(sub) -> str:
    """Où CE compte va reconnecter son Google — le tableau de bord de SON produit.

    C'était une constante pointant le nôtre. Servie telle quelle, elle envoyait l'agent
    d'un partenaire chez nous pour un geste qu'il doit faire chez lui : le défaut du
    socle d'accueil (13/08), retrouvé dans un recoin qu'aucune garde ne regardait —
    le tripwire des adresses en dur ne surveillait alors que la préproduction.
    """
    from .. import config
    return f"{config.dashboard_url_for(sub)}/ (section Google)"


def _refresh_access_token(refresh_token: str, sub: str,
                          app: Optional[OAuthApp] = None) -> dict:
    """`app` : l'app déjà résolue par l'appelant (une lecture de coffre de moins) ;
    absente, celle que `app_for(sub)` sert."""
    import requests
    app = app or app_for(sub)
    r = requests.post(
        _TOKEN_URL,
        data={
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    # `invalid_grant` SEUL vaut « réauth » (même règle que atlassian/folk/zoho,
    # `oauth_flow.grant_is_dead`) — un autre 4xx (client mal configuré) doit
    # remonter, pas se confondre avec un grant mort.
    body = (r.text or "")[:300]
    if r.status_code in (400, 401) and oauth_flow.grant_is_dead(r.status_code, body):
        raise GoogleReauthRequired(body)
    if r.status_code in (400, 401) and any(
            code in body.lower() for code in ("unauthorized_client", "invalid_client")):
        raise GoogleClientRejected(body)
    r.raise_for_status()
    return r.json()


def _no_account_message(sub: str, org_id: Optional[int], account: Optional[str]) -> str:
    """« Aucun compte connecté » — en nommant les comptes qui LE SONT, et la forme attendue.

    Le message ne disait ni l'un ni l'autre, alors qu'il sait déjà que l'appelant s'est
    trompé de valeur et que `list_google_accounts` sait la bonne réponse. Coût mesuré le
    14/08 : quatre essais à chercher un paramètre inexistant, l'appel recomposé à neuf
    pour repartir sur de bonnes bases — et le paramètre `mode=draft` oublié au passage.
    Trois mails partis chez une cliente.

    La confusion précise à fermer : `otomata` est un ALIAS de la convention CLI
    (`oto -a otomata`), pas un email. Ici on attend l'email du compte Google."""
    try:
        connectes = [a["google_email"] for a in db.list_google_accounts(sub, org_id)
                     if a.get("google_email")]
    # noqa: SILENT — message d'aide : liste de comptes connectés absente plutôt que fausse
    except Exception:      # jamais transformer une erreur d'entrée en panne
        connectes = []
    dash = _reconnecter(sub)
    if not account:
        return (f"Aucun compte Google connecté. Connecte-en un sur {dash}."
                if not connectes else
                "Aucun compte Google par défaut. Passe `account` — comptes connectés : "
                f"{', '.join(connectes)}.")
    if not connectes:
        return (f"Aucun compte Google connecté (tu as demandé `{account}`). "
                f"Connecte-en un sur {dash}.")
    return (f"Aucun compte Google connecté pour `{account}`. Comptes connectés : "
            f"{', '.join(connectes)} — `account` attend l'EMAIL du compte, pas un alias "
            "ni un nom d'organisation. La liste complète : gmail_list_accounts().")


def credentials_for(sub: str, account: Optional[str] = None,
                    service: Optional[str] = None):
    """Renvoie un `google.oauth2.credentials.Credentials` valide pour ce sub.

    `account` (email) cible un compte précis ; None = compte par défaut. Si aucun
    compte n'est demandé explicitement, un **projet actif** (bracelet de session,
    ADR 0032 §4) peut épingler le compte à utiliser (surcharge préfaite du lien
    connecteur) ; sinon repli sur le `is_default` du coffre.
    Charge depuis la DB, refresh transparent si access_token absent ou expiré.
    Lève RuntimeError actionnable si pas de compte connecté.
    """
    if account is None:
        from .. import access  # lazy : évite tout cycle d'import au boot
        account = access.project_pinned_identity("google")
    org_id = _ctx_org(sub)
    row = db.get_google_oauth(sub, org_id, account=account)
    if not row:
        raise RuntimeError(_no_account_message(sub, org_id, account))
    if service and service not in services_granted(row.get("scopes")):
        # Le compte existe mais n'a pas autorisé CE service (split du 2026-09-26) :
        # nommer la carte à ouvrir, pas « reconnecte-toi » — l'API Google, elle,
        # répondrait un 403 `insufficientPermissions` sans dire lequel.
        label = SERVICE_LABELS.get(service, service)
        raise RuntimeError(
            f"Le compte Google {row.get('google_email') or account or ''} n'a pas "
            f"encore autorisé {label} : connecte {label} depuis sa carte sur "
            f"{config_dashboard(sub)}. Rien n'a été fait.")

    from google.oauth2.credentials import Credentials

    access_token = row.get("access_token")
    expires_at = row.get("expires_at")
    needs_refresh = not access_token
    if not needs_refresh and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            # 60s d'avance pour éviter de cracher en plein appel
            if exp.timestamp() - time.time() < 60:
                needs_refresh = True
        # noqa: SILENT — credential illisible ⇒ refresh forcé, jamais un jeton périmé servi
        except Exception:
            needs_refresh = True

    # UNE lecture de l'app par appel (coffre + déchiffrement pour un compte tenant) :
    # elle sert au contrôle d'émetteur, au refresh et à l'objet rendu.
    app = app_for(sub)
    member_id = credentials_store.member_id(org_id, sub)
    email = row.get("google_email") or ""
    if _emis_par_un_autre_client(row, app):
        # L'app servie a changé depuis la connexion (posée, changée ou retirée) : ce
        # jeton ne se rafraîchira plus. On le dit avant tout appel réseau — et avant
        # qu'un access_token encore valide ne masque la panne pour une heure.
        connector_health.mark_rejected(
            credentials_store.MEMBER, member_id, "google", email,
            "jeton émis par un autre client OAuth que l'app servie")
        raise GoogleReauthRequired(
            f"Le compte Google {email or '(sans email)'} a été connecté sous une autre app "
            "OAuth que celle servie aujourd'hui (l'app de ton organisation a changé) : "
            f"reconnecte ce compte sur {_reconnecter(sub)}. Rien n'a été fait.")

    if needs_refresh:
        account = email
        scope = (credentials_store.MEMBER, member_id, account)
        try:
            resp = _refresh_access_token(row["refresh_token"], sub, app)
        except GoogleClientRejected as e:
            raise GoogleClientRejected(
                f"Google refuse le client OAuth au rafraîchissement du compte "
                f"{account or '(sans email)'} ({str(e)[:120]}) : la configuration de l'app "
                "(identifiant, secret) est à vérifier par un administrateur. "
                "Rien n'a été fait.") from e
        except GoogleReauthRequired as e:
            # Grant mort : on MARQUE (aide partagée oto#25 lot b2), jamais de purge —
            # même garde de portée que atlassian/folk/salesforce/zoho. On relève
            # ENSUITE, sans changer le contrat de `credentials_for` (toujours des
            # `Credentials` valides ou une exception, jamais un `None` muet).
            connector_health.mark_rejected(
                credentials_store.MEMBER, member_id, "google", account, str(e) or None)
            raise GoogleReauthRequired(
                f"Le jeton du compte Google {account or '(sans email)'} est expiré ou "
                "révoqué (Google répond invalid_grant) : reconnecte ce compte sur "
                f"{_reconnecter(sub)}. Rien n'a été fait.") from e
        access_token = resp["access_token"]
        expires_in = int(resp.get("expires_in", 0) or 0)
        new_exp = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc).isoformat()
        db.update_google_access_token(sub, org_id, row.get("google_email"), access_token, new_exp)
        # `update_google_access_token` MERGE le meta (`update_meta`, JSONB ||) :
        # un `health_ko` posé par un refresh mort précédent ne serait jamais
        # effacé par ce chemin sans cet appel explicite (oto#25 lot b3, même
        # raison que la rotation Salesforce — contrairement à atlassian/folk, dont
        # le refresh REMPLACE tout le meta et démarque déjà pour ce seul fait).
        connector_health.record_health("google", scope, True, None)

    # Le client Google rafraîchit aussi DE LUI-MÊME (googleapiclient) : il lui faut
    # l'app qui a délivré le jeton — vérifié plus haut, c'est `app`.
    return Credentials(
        token=access_token,
        refresh_token=row["refresh_token"],
        token_uri=_TOKEN_URL,
        client_id=app.client_id,
        client_secret=app.client_secret,
        scopes=row["scopes"].split() if row.get("scopes") else SCOPES,
    )


def list_accounts(sub: str) -> list[dict]:
    """Comptes Google connectés du user DANS l'org de contexte (email, défaut, scopes)."""
    from .. import access  # lazy
    return db.list_google_accounts(sub, access.current_org(sub))


def _link_state(sub: str) -> connector_link.LinkState:
    """État de lien pour `/api/me`. Google est MULTI-COMPTE : une ligne de coffre par
    adresse (`account = email`), avec ses satellites dans `meta`. Une boucle générique
    qui chercherait « la » ligne du membre n'en trouverait aucune."""
    accounts = list_accounts(sub)
    return connector_link.LinkState(
        linked=bool(accounts), accounts=len(accounts),
        set_at=max((a.get("set_at") or "" for a in accounts), default="") or None)


connector_link.register("google", _link_state)


def _link_state_for(service: str):
    """L'état de lien d'UN service : les comptes qui l'ont AUTORISÉ, pas tous les
    comptes du porteur — sinon la carte Drive dirait « connecté » à qui n'a
    consenti que Gmail, et le premier `drive_file` échouerait."""
    def read(sub: str) -> connector_link.LinkState:
        accounts = [a for a in list_accounts(sub)
                    if service in services_granted(a.get("scopes"))]
        return connector_link.LinkState(
            linked=bool(accounts), accounts=len(accounts),
            set_at=max((a.get("set_at") or "" for a in accounts), default="") or None)
    return read


for _svc in SERVICES:
    connector_link.register(_svc, _link_state_for(_svc))


def _start_flow(ctx, values: dict) -> "connector_flow.FlowStart":
    """Le geste « connecter », déclaré comme celui de tout autre connecteur (#300).

    Il existait — mais **hors du point de passage** : une route REST écrite à la main
    rendait `{auth_url}` par coïncidence, sans que rien ne l'y oblige, et le garde-fou
    qui impose la forme commune ne voit que les capacités.

    ⚠️ Une configuration OAuth absente lève ici un `RuntimeError` que la route
    traduisait en 500. Sous le seam, c'est un refus d'ENTRÉE (la plateforme n'a pas
    d'app Google configurée), pas une panne : traduit en erreur nommée, l'appelant
    saura que réessayer n'y changera rien.
    """
    from ..capabilities._types import AuthzDenied
    try:
        # `app` est une clé CACHÉE, pas un `FlowParam` déclaré : le front la passe
        # hors formulaire (le client sait qui il est), elle ne doit jamais devenir
        # un champ visible à l'utilisateur. Même convention que les quatre autres
        # connecteurs OAuth — Google était le seul à l'ignorer (oto-backend#877).
        return connector_flow.FlowStart(
            auth_url=build_auth_url(ctx.sub, (values or {}).get("app") or ""))
    except RuntimeError as e:
        raise AuthzDenied(503, "oauth_misconfigured", str(e))


connector_flow.declare(
    "google",
    start=_start_flow,
    label="Lier un compte Google",
    callback_path="/api/google/oauth/callback",
)


def _start_flow_for(service: str):
    """Le geste « connecter » d'UN service (split du 2026-09-26) : le même flux que
    le compte, borné à ses scopes (`scopes_for`), et un state qui nomme la carte —
    le callback y ramène."""
    def start(ctx, values: dict) -> "connector_flow.FlowStart":
        from ..capabilities._types import AuthzDenied
        try:
            return connector_flow.FlowStart(
                auth_url=build_auth_url(ctx.sub, (values or {}).get("app") or "",
                                        connector=service))
        except RuntimeError as e:
            raise AuthzDenied(503, "oauth_misconfigured", str(e))
    return start


for _svc in SERVICES:
    connector_flow.declare(
        _svc,
        start=_start_flow_for(_svc),
        label=f"Autoriser {SERVICE_LABELS[_svc]}",
        callback_path="/api/google/oauth/callback",
    )


def revoke(sub: str, account: Optional[str] = None) -> None:
    """Révoque côté Google + supprime de la DB.

    `account` (email) cible un compte ; None révoque tous les comptes du user.
    """
    import requests

    org_id = _ctx_org(sub)
    if account is None:
        rows = db.list_google_accounts(sub, org_id)
        targets = [r.get("google_email") for r in rows]
    else:
        targets = [account]

    for email in targets:
        # Révoquer côté Google est best-effort : un credential indéchiffrable
        # (ligne chiffrée avec une master key périmée → InvalidTag) ne doit PAS
        # empêcher la suppression. Le contrat de revoke = supprimer en DB.
        try:
            row = db.get_google_oauth(sub, org_id, account=email)
        # noqa: SILENT — dette déclarée : credential indéchiffrable ⇒ on supprime quand même (#424)
        except Exception:
            row = None
        if row and row.get("refresh_token"):
            try:
                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    # `data=` (corps) et non `params=` : en query string le refresh
                    # token part dans l'URL → breadcrumbs Sentry, logs de proxy.
                    data={"token": row["refresh_token"]},
                    timeout=10,
                )
            # noqa: SILENT — dette déclarée : le refresh_token reste vivant chez Google (#424, verdict C)
            except Exception:
                pass  # on supprime quand même en DB
    db.delete_google_oauth(sub, org_id, account=account)
