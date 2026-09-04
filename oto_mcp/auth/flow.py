"""Fabrique d'acquisition OAuth2 — la danse `authorization_code`, écrite UNE fois.

Le registre de connecteurs sait déclarer un credential (`providers/`), le coffre
le chiffrer, la cascade le résoudre. Mais rien ne savait l'**ACQUÉRIR** : chaque
connecteur OAuth réécrivait le flux de bout en bout. Mesuré le 2026-07-28 :
`verify_state` ×6, `exchange_code` ×5, `build_auth_url` ×5, `_state_secret` ×5 —
cinq modules, ~1200 lignes, la même mécanique. Chaque copie a re-découvert les
mêmes pièges (secrets en query string, TTL, erreurs opaques).

Ce module porte le noyau réellement commun et stable :

- **state signé** — HMAC-SHA256, base64url, TTL ;
- **échange du code** — POST form-encodé, erreurs rédigées ;
- **URI de redirection** — dérivée de l'URL publique du serveur.

Ce qui reste au connecteur : l'URL d'autorisation (ses paramètres varient), ses
scopes, et le rangement du credential obtenu. C'est le bon partage : le mécanisme
ici, la politique là-bas.

**AUDIENCE — la correction de fond.** Les implémentations séparées signaient toutes
avec le MÊME secret d'env, au même format, sans discriminant : un `state` émis pour
un flux était structurellement valide sur le callback d'un autre (`folk` et
`atlassian` partageaient jusqu'à la forme exacte du payload). Ici le state est
**lié à son audience** et `read_state` rejette celui d'un autre flux — un rejeu
inter-connecteurs devient impossible par construction, pas par convention.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

import requests
from .. import config, deprecations

DEFAULT_STATE_TTL = 600   # 10 min — le temps de lire un écran de consentement


class OAuthFlowError(ValueError):
    """Échec d'acquisition. Le message est SÛR à afficher : jamais de secret, jamais
    l'URL de la requête (cf. l'incident #284 — des credentials en query string se
    retrouvaient dans les messages d'erreur)."""


class OAuthExchangeRefused(OAuthFlowError):
    """Le serveur d'autorisation a REFUSÉ l'échange — condition d'entrée/config de
    l'utilisateur, jamais un bug backend : code expiré ou déjà consommé, verifier PKCE
    qui ne correspond pas, client_id/secret faux, scopes absents, callback URL
    divergente, appel bloqué par une restriction IP.

    Sous-classe distincte parce que `OAuthFlowError` couvre aussi de VRAIS bugs de
    notre côté (`OTO_MCP_OAUTH_STATE_SECRET` absent) : les deux ne doivent pas partir
    au même endroit. Celle-ci est droppée de Sentry (`error_taxonomy`), l'autre non —
    sinon une misconfiguration serveur disparaîtrait derrière le bruit des refus
    normaux (2026-07-31 : 3 events de refus de grant Salesforce en tête du tableau,
    alors qu'ils décrivaient une Connected App mal réglée côté client)."""


# --- state signé --------------------------------------------------------------

def _secret() -> bytes:
    v = os.environ.get("OTO_MCP_OAUTH_STATE_SECRET")
    if not v:
        raise OAuthFlowError("OTO_MCP_OAUTH_STATE_SECRET env var manquante")
    return v.encode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_state(audience: str, payload: dict) -> str:
    """`<b64(payload)>.<b64(hmac)>`. `audience` (ex. « zoho », « google ») est
    INCLUS dans le payload signé : un state d'un flux ne vaut que pour ce flux."""
    body = json.dumps({**payload, "aud": audience, "ts": int(time.time())},
                      separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{_b64url(body)}.{_b64url(sig)}"


def read_state(audience: str, state: Optional[str], *,
               ttl: int = DEFAULT_STATE_TTL) -> Optional[dict]:
    """Payload si la signature est valide, l'audience correspond et le TTL tient.
    `None` sinon — un callback ne doit jamais distinguer les causes d'un refus."""
    if not state or "." not in state:
        return None
    b64_body, b64_sig = state.split(".", 1)
    try:
        body, sig = _b64url_decode(b64_body), _b64url_decode(b64_sig)
    # noqa: SILENT — fail-closed : un callback ne distingue jamais les causes d'un refus
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(sig, hmac.new(_secret(), body, hashlib.sha256).digest()):
        return None
    try:
        data = json.loads(body)
    # noqa: SILENT — fail-closed : un callback ne distingue jamais les causes d'un refus
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("aud") != audience:
        return None            # state d'un AUTRE flux → refus (anti-rejeu)
    if int(time.time()) - int(data.get("ts", 0)) > ttl:
        return None
    return data


# --- URI de redirection --------------------------------------------------------

def redirect_uri(path: str) -> str:
    """URL publique + `path`. À enregistrer AU BYTE PRÈS chez le fournisseur."""
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# --- retour vers le front qui a demandé la connexion ----------------------------

# Le callback (`/api/<connecteur>/oauth/callback`) est appelé PAR LE FOURNISSEUR
# (Salesforce/Zoho/…) — à ce stade il n'y a plus d'Origin utile à lire. Le SEUL
# moment où on sait QUEL front a demandé la connexion, c'est `/start` : la capacité
# REST peut recevoir une clé d'app explicite du client (`app=`), portée ensuite dans
# le state signé jusqu'au retour (même mécanique que `org`/`group` côté Salesforce).
# Pas de sniff d'Origin ici : les capacités sont transport-agnostiques par construction
# (ADR 0009, aucune Request ne descend jusqu'au handler) — un champ déclaré par le
# client est la seule donnée disponible à ce niveau, et elle ne vaut de toute façon
# que ce que `resolve_return_app` en valide.
#
# Gabarit de CHEMIN par app — le connecteur fournit lui-même son SUFFIXE (la query
# string `?connector=x&x=connected` ne change pas d'un front à l'autre, seul le
# chemin avant le `?` diffère). `{org}` substitué depuis le state, déjà porté pour
# scoper le credential (cf. `salesforce_oauth.make_state`).
RETURN_APPS: dict[str, tuple[str, str]] = {
    # ⚠️ Le CHEMIN suit les routes du front, pas nos souvenirs : `/network/[orgId]`
    # a été renommé `/org/[orgId]` (front tiers, commit 6921521) et le
    # segment `/network` n'existe plus du tout. Tant que ce patron est resté
    # périmé, un retour d'OAuth « réussi » déposait la personne sur un 404 —
    # panne silencieuse, puisque le consentement, lui, avait bien eu lieu.
    # noqa: CLIENT — coordonnées FONCTIONNELLES d'un front tiers : les déplacer
    # casse une redirection OAuth réelle. Leur relocalisation vers la config privée
    # est le second volet de oto-private#85, pas le lot du garde-fou.
    "tulina": ("https://app.tulina.ai", "/org/{org}/connectors"),  # noqa: CLIENT — cf. ci-dessus
    "tulina-preprod": ("https://tulina.oto.zone", "/org/{org}/connectors"),  # noqa: CLIENT — cf. ci-dessus
}

# Défaut historique (oto-dashboard) — byte-à-byte ce que chaque `_app_url()` de
# route callback faisait seul avant ce module.
_DEFAULT_RETURN_BASE_ENV = "OTO_APP_URL"
# Une redirection doit TOUJOURS aboutir : sans patron chez le tenant, on sert la
# nôtre (cf. `links.redirect_for`). Voir notre marque une fois vaut mieux qu'une page
# blanche au milieu d'une connexion.
_DEFAULT_RETURN_BASE_FALLBACK = config.dashboard_url()
_DEFAULT_RETURN_PATH = "/connectors"


def resolve_return_app(app: Optional[str]) -> str:
    """Clé à ranger dans le state signé (`make_state`-side). Une valeur absente ou
    hors de `RETURN_APPS` devient chaîne vide, qui fait retomber `return_url` sur
    le défaut oto-dashboard : on ne fait JAMAIS confiance à une valeur de client
    au-delà d'un lookup dans une liste fermée — jamais une origine prise telle
    quelle (ce serait un open redirect)."""
    return app if app in RETURN_APPS else ""


def return_url(app: Optional[str], suffix: str, *, org: Optional[int] = None) -> str:
    """URL de retour après consentement : base + chemin de l'app portée par le state
    (vide/inconnue → `OTO_APP_URL`/oto-dashboard, comportement historique), suivi de
    `suffix` — la query string propre au connecteur (ex.
    `?connector=salesforce&salesforce=connected`), inchangée d'un front à l'autre."""
    if app in RETURN_APPS:
        base, path_tmpl = RETURN_APPS[app]
    else:
        base = os.environ.get(_DEFAULT_RETURN_BASE_ENV, _DEFAULT_RETURN_BASE_FALLBACK).rstrip("/")
        path_tmpl = _DEFAULT_RETURN_PATH
    path = path_tmpl.format(org=org if org is not None else "")
    return f"{base}{path}{suffix}"


# --- LA convention unique de retour OAuth (oto-backend#670) --------------------
#
# Avant ce fabricant, chaque callback (`api/salesforce.py`, `api/zoho.py`,
# `api/atlassian.py`, `api/folk.py`, `api/datastore.py`) composait SA propre
# query string de retour, à la main : cinq formes différentes, dont deux replis
# cassés (une f-string à accolades doublées qui rendait une chaîne littérale au
# lieu d'une URL). La forme salesforce — `?connector=<nom>&connect=<etat>`,
# `etat ∈ {connected, error, forbidden}` — devient LA convention, fabriquée ici
# une fois plutôt que recopiée cinq fois.

def connector_return_suffix(connector: str, etat: str, *,
                            legacy: Optional[tuple] = None) -> str:
    """`?connector=<connector>&connect=<etat>`.

    `legacy` — un couple `(clé, valeur)` — double une ANCIENNE forme encore LUE par
    un consommateur aujourd'hui (le dashboard sur `?zoho=connected`/`?google=
    connected`) : ajoutée à la suite, dans le MÊME suffixe, tant que
    `deprecations.dans_le_preavis_retour_oauth()` le dit — jamais deux
    redirections, une seule URL qui porte les deux jeux de clés. Ne rien passer
    pour un connecteur dont l'ancien statut n'a JAMAIS eu de lecteur (atlassian,
    folk, et les branches d'échec de google qui ne redirigeaient pas du tout) :
    doubler une valeur que personne n'a jamais pu lire ne protège personne, et
    grossit l'URL pour rien."""
    suffix = f"?connector={connector}&connect={etat}"
    if legacy and deprecations.dans_le_preavis_retour_oauth():
        cle, valeur = legacy
        suffix += f"&{cle}={valeur}"
    return suffix


def connector_return_url(app: Optional[str], connector: str, etat: str, *,
                         org: Optional[int] = None,
                         legacy: Optional[tuple] = None) -> str:
    """URL de retour complète : base+chemin de `return_url` (app tierce connue,
    sinon défaut oto-dashboard) + `connector_return_suffix`. Le point d'entrée pour
    un connecteur dont le retour suit déjà le gabarit `return_url`/`RETURN_APPS`
    (salesforce, zoho, google) ; atlassian/folk résolvent leur base autrement
    (`links.link_for`, patron par tenant) et utilisent `avec_connect` à la place."""
    return return_url(app, connector_return_suffix(connector, etat, legacy=legacy), org=org)


def avec_connect(url: str, etat: str) -> str:
    """Ajoute `connect=<etat>` à une URL de retour qui porte déjà `connector=`
    (le patron `links.connector_return`, utilisé par atlassian/folk) — jamais une
    seconde redirection, le même paramètre rejoint la même URL, qu'elle ait ou non
    déjà une query string."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}connect={etat}"


# --- échange du code -----------------------------------------------------------

def exchange_code(token_url: str, *, code: str, client_id: str, client_secret: str,
                  redirect: str, extra: Optional[dict] = None,
                  timeout: int = 30) -> dict:
    """Code éphémère → tokens.

    ⚠️ Les credentials partent en **`data=`** (corps form-encodé, RFC 6749 §2.3.1),
    JAMAIS en `params=` : en query string ils entrent dans l'URL, donc dans le
    message de toute exception `requests`, dans les logs et chez le fournisseur.
    Et pas de `raise_for_status()` — son message embarque l'URL. On construit
    l'erreur nous-mêmes.
    """
    payload = {"grant_type": "authorization_code", "code": code,
               "client_id": client_id, "client_secret": client_secret,
               "redirect_uri": redirect, **(extra or {})}
    r = requests.post(token_url, data=payload, timeout=timeout)
    host = (token_url or "").split("//")[-1].split("/")[0]
    try:
        body = r.json()
    except ValueError:
        raise OAuthExchangeRefused(
            f"Réponse illisible du serveur d'autorisation ({host}, HTTP {r.status_code}).")
    # Beaucoup de fournisseurs (Zoho…) répondent HTTP 200 avec l'erreur dans le corps.
    if r.status_code >= 400 or "error" in body:
        # `error` seul ne suffit PAS à diagnostiquer : `invalid_grant` est le code
        # fourre-tout d'OAuth 2 (code expiré, déjà consommé, verifier PKCE qui ne
        # correspond pas, appel depuis une IP non autorisée, jeton révoqué…). Le
        # fournisseur dit lequel dans `error_description` ; le jeter obligeait à
        # deviner à la main, une hypothèse à la fois. Vécu le 31/07 sur Salesforce.
        detail = " : ".join(str(v).strip() for v in (
            body.get("error") or body.get("message") or f"HTTP {r.status_code}",
            body.get("error_description"),
        ) if v)
        raise OAuthExchangeRefused(f"Échec de l'échange OAuth ({host}) : {detail}.")
    return body


# --- le grant est-il MORT, ou est-ce ma config qui est fausse ? ----------------

def grant_is_dead(status_code: int, body_text: str) -> bool:
    """`True` seulement si le serveur d'autorisation dit que le GRANT est mort
    (`invalid_grant`, RFC 6749 §5.2) — donc qu'une ré-autorisation est requise.

    ⚠️ Cette distinction garde une opération DESTRUCTRICE. Les appelants purgent le
    credential sur « réauth requise » ; or un AS répond 400 aussi pour `invalid_client`,
    `invalid_request`, `unauthorized_client` — c'est-à-dire pour une CONFIG fausse. Tant
    que tout 400/401 valait « grant mort », un client_secret mal saisi effaçait
    irréversiblement un refresh_token parfaitement valide, et l'utilisateur devait tout
    reconnecter pour une faute de frappe. Un incident de configuration doit remonter,
    pas détruire.

    Le corps prime sur le code : c'est lui qui porte le verdict de l'AS."""
    low = (body_text or "").lower()
    if "invalid_grant" in low:
        return True
    # Un 401 NU (sans corps exploitable) reste un rejet d'identifiants côté grant :
    # les AS qui ne renvoient pas de corps sur refresh révoqué existent. Un 400 nu,
    # lui, est trop ambigu pour justifier une purge.
    return status_code == 401 and not low.strip()
