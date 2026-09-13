"""Façade DCR devant Logto (technique éprouvée sur ytmusic MCP).

claude.ai (et d'autres clients MCP) exigent un `registration_endpoint` RFC 7591
(Dynamic Client Registration). Logto self-hosted n'en a pas → sans ça, l'user
doit coller le `client_id` à la main dans le connecteur (friction rédhibitoire
pour l'onboarding tiers).

On agit en **façade de l'authorization server** : le PRM pointe les clients vers
NOUS (cf. `_build_auth` → authorization_servers = OTO), on sert une métadonnée AS
augmentée (un `registration_endpoint` à nous et, pour NOTRE annuaire, l'autorisation —
oto#202, `authorize_consent` ; tous les autres endpoints = ceux de Logto), et un endpoint DCR qui renvoie un client Logto **pré-créé partagé**. Les
tokens restent émis et signés par Logto ; on ne fait que les vérifier.

Le redirect URI de claude.ai est fixe et déjà enregistré sur l'app Logto pré-créée
(`Claude (oto MCP)`), donc on peut renvoyer le même `client_id` à chaque
enregistrement sans risque.

⚠️ **« Partagé » vaut par ANNUAIRE, pas pour tout le monde.** Un host réclamé par un
tenant (`tenants.hosts`) est servi par la même façade, mais l'annuaire visé est le
sien : son client préparé (`tenants.oauth_client_id`), et le rappel posé chez LUI
(`tenants.logto_mgmt` → `Directory`). Faute de quoi la façade rendait 201 sans rien
enregistrer, et l'`/authorize` refusait deux secondes plus tard — oto-backend#909.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..json_body import InvalidJsonBody, read_json_body
from .authorize_consent import redirection

_log = logging.getLogger("oto_mcp.oauth_facade")


def _logto_issuer() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/") + "/oidc"


def _logto_public_oidc() -> str:
    """L'adresse de Logto **annoncée au client**, qui n'est pas celle qui signe.

    `LOGTO_ENDPOINT` est l'`issuer` des jetons et l'adresse de la Management API : il
    est gravé dans l'instance — le changer invalide toute session vivante — et reste
    donc `auth.oto.ninja`. Mais ce que le client LIT le conduit à une page de
    connexion : y annoncer l'adresse interne fait qu'un utilisateur qui autorise son
    client sur `mcp.oto.cx` se connecte sur `auth.oto.ninja`, alors que tout le reste
    du produit est en `.cx` (vécu le 10/09/2026). Dans Logto, tout suit l'en-tête
    `Host` SAUF l'`issuer` : les deux domaines servent les mêmes endpoints et les
    mêmes clés, un jeton obtenu par l'un est identique à un jeton obtenu par l'autre.

    `LOGTO_PUBLIC_ENDPOINT` absent = pas de domaine public distinct de celui qui
    signe ; on annonce alors le même, ce qu'attendent la preprod et l'on-premise.
    """
    public = os.environ.get("LOGTO_PUBLIC_ENDPOINT", "").strip().rstrip("/")
    return f"{public}/oidc" if public else _logto_issuer()


def as_metadata(public_url: str, logto: str = "") -> dict:
    """Métadonnée RFC 8414 servie sur NOTRE domaine : issuer = nous, le
    `registration_endpoint` est à nous, le jeton et les clés sont ceux de Logto.

    L'autorisation de NOTRE annuaire transite par la façade (oto#202) : elle y pose le
    consentement sans lequel Logto ne délivre aucun jeton de rafraîchissement. Celle
    d'un TENANT (`logto` fourni) reste chez lui : délivrer des jetons de
    rafraîchissement à ses utilisateurs est sa décision, pas la nôtre.

    ⚠️ RFC 8414 §3.3 : l'`issuer` retourné DOIT être IDENTIQUE à l'identifiant d'AS
    que le client a annoncé dans le PRM (`authorization_servers`) et dans lequel il
    a inséré le chemin well-known. Le PRM passe `public_base` par `AnyHttpUrl`
    (RemoteAuthProvider, server.py), qui NORMALISE en ajoutant un slash final
    (`https://x` → `https://x/`). On normalise l'issuer par le MÊME `AnyHttpUrl` →
    égalité byte-à-byte garantie. Sans ça, un client strict (Mistral) rejette le
    discovery pour issuer mismatch (claude.ai, lui, tolère le slash). Vécu 2026-06-25."""
    annuaire_tiers = bool(logto)
    logto = logto or _logto_public_oidc()
    autorisation = (f"{logto}/auth" if annuaire_tiers
                    else f"{str(public_url).rstrip('/')}/oauth/authorize")
    return {
        "issuer": str(AnyHttpUrl(public_url)),
        "authorization_endpoint": autorisation,
        "token_endpoint": f"{logto}/token",
        "jwks_uri": f"{logto}/jwks",
        "registration_endpoint": f"{public_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
    }


def as_oidc_metadata(public_url: str, logto: str = "") -> dict:
    """OIDC Discovery 1.0 servie sur NOTRE domaine (`/.well-known/openid-configuration`).

    Certains clients OAuth 2.1 (dont Mistral) sondent l'OIDC discovery EN PLUS de
    RFC 8414 (`oauth-authorization-server`) ; un 404 ici peut casser leur résolution
    d'AS. On réutilise `as_metadata` et on ajoute les champs OIDC OBLIGATOIRES
    (`subject_types_supported`, `id_token_signing_alg_values_supported` = ES384, ce
    que Logto self-hosted signe) + `userinfo_endpoint`. Même issuer (normalisé) →
    pas de mismatch."""
    base = as_metadata(public_url, logto)  # l'argument BRUT : il dit si l'annuaire est tiers
    logto = logto or _logto_public_oidc()
    return {
        **base,
        "userinfo_endpoint": f"{logto}/me",
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["ES384"],
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "email", "name"],
    }


def _cors() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
    }


# ⚠️ INVARIANT DE SÉCURITÉ (audit 2026-06-13). Le client DCR partagé
# (`OTO_MCP_CLAUDE_APP_ID`) n'est sûr QUE parce que l'app Logto correspondante
# n'enregistre QUE des redirect_uris étroits (callbacks claude.ai/.com + locaux).
# Logto valide le redirect au `/authorize` (enforcement réel) ; un redirect large
# ou wildcard ajouté à l'app Logto rendrait le DCR ouvert + client public
# exploitable (vol de code d'autorisation). On valide AUSSI ici (défense en
# profondeur, fail-fast à l'enregistrement) — sans dépendre uniquement de Logto.
# Hôtes claude.ai/.com en https (callback MCP) + hôtes locaux en http (Claude
# Code/desktop, port ignoré). Comparaison de host EXACTE après parsing : jamais
# de startswith sur l'URL brute (contournable via `http://localhost.evil.com`).
_ALLOWED_HTTPS_HOSTS = {"claude.ai", "claude.com"}
_ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CALLBACK_PATH = "/api/mcp/auth_callback"


def _extra_https_hosts() -> set[str]:
    extra = (os.environ.get("OTO_MCP_DCR_ALLOWED_REDIRECTS") or "").strip()
    return {h.strip().lower().rstrip(".") for h in extra.split(",") if h.strip()}


def _redirect_ok(uri: str) -> bool:
    if not isinstance(uri, str):
        return False
    try:
        p = urlparse(uri)
    # noqa: SILENT — fail-closed : un redirect_uri douteux est refusé, sans dire pourquoi
    except Exception:
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if p.scheme == "https" and host in (_ALLOWED_HTTPS_HOSTS | _extra_https_hosts()) \
            and p.path.startswith(_CALLBACK_PATH):
        return True
    # ChatGPT (connecteurs MCP) : DEUX formes de rappel, toutes deux documentées
    # par OpenAI (developers.openai.com/plugins/build/auth). Ce n'est pas le mode de
    # connexion qui tranche, c'est le serveur d'AUTORISATION : le rappel propre au
    # connecteur `https://chatgpt.com/connector/oauth/<id>` (<id> varie → préfixe de
    # path, pas l'URI exacte) quand l'AS n'annonce pas l'identification d'émetteur
    # RFC 9207 ; le rappel STABLE `connector_platform_oauth_redirect` quand il
    # l'annonce — et aussi pour tout connecteur créé avant l'apparition du premier,
    # qui garde la forme stable. Refuser la seconde suffit à rendre le connecteur
    # ininstallable, sans qu'aucune trace ne survive plus de 19 h.
    # Garde-fou réel = l'app Logto (redirect enregistré, exact).
    if p.scheme == "https" and host == "chatgpt.com" and (
            p.path.startswith("/connector/oauth/")
            or p.path == "/connector_platform_oauth_redirect"):
        return True
    # Mistral (Le Chat, connecteurs MCP) : redirect FIXE callback.mistral.ai.
    if p.scheme == "https" and host == "callback.mistral.ai" \
            and p.path.startswith("/v1/integrations_auth/"):
        return True
    if p.scheme == "http" and host in _ALLOWED_LOCAL_HOSTS:
        return True
    return False


# ── DCR réelle : enregistrement dynamique du redirect dans l'app Logto ────────
# Le client_id reste celui de l'annuaire visé, mais on ÉTEND la liste de
# redirectUris de son app à chaque DCR (le redirect de ChatGPT est propre à chaque
# connecteur → impossible à pré-enregistrer). Ainsi N'IMPORTE QUEL user installe sans
# intervention manuelle. Les redirects sont déjà validés par _redirect_ok
# (host allowlist) → on n'enregistre QUE des callbacks légitimes.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"  # vs WAF Cloudflare (1010)
_MGMT_RESOURCE = "https://default.logto.app/api"
# Le credential de management de NOTRE annuaire, sous la même convention que celle
# qu'un tenant déclare : `<credential>_ID` / `<credential>_SECRET`. Le primaire n'est
# donc pas un cas particulier — c'est le premier annuaire, pas le seul.
_PRIMARY_CREDENTIAL = "OTO_MCP_LOGTO_M2M"


@dataclass(frozen=True)
class Directory:
    """Un annuaire Logto **administrable** : où prendre le jeton, où poser les appels
    `/api`, et sous quel credential.

    ⚠️ Les deux endpoints sont un COUPLE, pas une base unique. Chez Logto le jeton de
    management s'obtient sur l'endpoint d'ADMINISTRATION et les appels `/api` vont sur
    l'endpoint PRINCIPAL ; l'inverse rend `401 aud check_failed`. Sur notre annuaire
    les deux coïncident, ce qui est précisément ce qui a permis de vivre longtemps
    avec une seule variable — et ce qui l'aurait rendue fausse en silence au premier
    annuaire où ils diffèrent (`auth.tulina.ai` / `logto-tulina.oto.zone`).

    ⚠️ `credential` est le NOM d'un couple de variables d'environnement, jamais une
    valeur : rien de secret n'entre dans cet objet, donc rien de secret ne peut sortir
    par un log, une trace d'exception ou une fiche de suivi.
    """
    label: str            # le tenant, pour les messages — jamais pour la sélection
    token_endpoint: str
    api_endpoint: str
    credential: str

    @property
    def key(self) -> str:
        """L'identité de ce qui PRODUIT le jeton — la clé de son cache.

        Le `label` en est exclu à dessein : deux tenants sur le même annuaire, sous le
        même credential, partagent légitimement un jeton. Tout le reste y entre, y
        compris l'endpoint d'API qui ne change pourtant pas le jeton : une différence
        quelque part doit donner un slot différent, jamais un jeton réutilisé au
        hasard d'une égalité partielle.
        """
        return f"{self.token_endpoint}|{self.api_endpoint}|{self.credential}"


# ⚠️ Un dict CLEFÉ par annuaire, jamais un singleton. Le cache était
# `{"value", "exp"}` au niveau du module, partagé par tous les appelants : brancher un
# second annuaire dessus ferait servir à l'un le jeton de l'autre — au mieux des refus
# intermittents, au pire une écriture dirigée vers le mauvais annuaire (oto#909).
_mgmt_toks: dict = {}


def _logto_base() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/")


def _primary_directory() -> Directory:
    """NOTRE annuaire — le défaut de tout appel de management non qualifié."""
    from ..tenancy import PRIMARY_SLUG
    base = _logto_base()
    return Directory(PRIMARY_SLUG, base, base, _PRIMARY_CREDENTIAL)


def directory_for_tenant(entry) -> "Directory | None":
    """L'annuaire administrable d'un tenant, ou **None** s'il n'en déclare pas.

    `None` est le défaut et le reste : authentifier les comptes d'un tenant ne donne
    aucun droit d'écrire dans son annuaire. N'en rend un que pour un tenant qui a
    DÉCLARÉ où frapper et sous quel nom — c'est-à-dire, en pratique, un tenant dont
    nous hébergeons l'annuaire.
    """
    mgmt = getattr(entry, "logto_mgmt", None)
    if not mgmt:
        return None
    return Directory(getattr(entry, "slug", "") or "?", mgmt["token_endpoint"],
                     mgmt["api_endpoint"], mgmt["credential"])


def _credential_present(d: Directory) -> bool:
    """Le process détient-il la clé de cet annuaire ? Déclaré en base ≠ injecté ici :
    l'écart est exactement ce qui doit se voir, pas se rattraper."""
    return bool(os.environ.get(f"{d.credential}_ID")
                and os.environ.get(f"{d.credential}_SECRET"))


def _mgmt_token(directory: "Directory | None" = None) -> str:
    import requests
    d = directory or _primary_directory()
    cid = os.environ.get(f"{d.credential}_ID")
    csec = os.environ.get(f"{d.credential}_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            f"annuaire {d.label} : credential de management absent de "
            f"l'environnement ({d.credential}_ID / {d.credential}_SECRET)")
    now = time.time()
    slot = _mgmt_toks.get(d.key)
    if slot and slot["value"] and slot["exp"] > now + 30:
        return slot["value"]
    r = requests.post(
        f"{d.token_endpoint}/oidc/token",
        data={"grant_type": "client_credentials", "resource": _MGMT_RESOURCE, "scope": "all"},
        auth=(cid, csec), headers={"User-Agent": _UA}, timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    _mgmt_toks[d.key] = {"value": j["access_token"],
                         "exp": now + int(j.get("expires_in", 3600))}
    return _mgmt_toks[d.key]["value"]


def logto_user_primary_email(sub: str) -> str | None:
    """Email primaire AUTORITATIF d'un user Logto (Management API). Dans Logto, le
    `primaryEmail` n'est posé qu'après vérification de l'adresse → sa présence vaut
    « email vérifié », et c'est la SOURCE DE VÉRITÉ (un claim de token, lui, peut
    mentir). Utilisé par la bascule de tenant pour décider d'un merge de comptes sans
    faire confiance au token. Renvoie None si user inconnu / Logto indispo (l'appelant
    ne migre alors PAS — fail-safe).

    ⚠️ **Lève `ForeignTenantDirectory` sur un sub qualifié** (`slug:sub`, tenant tiers)
    plutôt que d'interroger notre Logto avec un identifiant qui n'y existe pas : le
    None qui en sortirait est indistinguable d'un « email non vérifié », et l'échec se
    lirait à l'autre bout de la chaîne. Router la lecture vers l'émetteur du tenant
    demanderait ses credentials de management, qu'on n'a pas (oto-backend#274)."""
    import requests

    from ..tenancy import require_primary_tenant
    # AVANT le try : le message doit remonter à l'appelant, pas être absorbé par le
    # fail-safe ci-dessous (qui, lui, couvre les pannes réseau/Logto).
    require_primary_tenant(sub, "lecture de l'email primaire Logto")
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.get(
            f"{base}/api/users/{sub}",
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("primaryEmail") or None
    except Exception as e:
        _log.warning("lookup primaryEmail Logto échoué pour %s : %s", sub, e)
        return None


def reset_user_mfa(sub: str) -> list[str]:
    """Efface tous les facteurs de double authentification d'un user Logto (Management
    API) — geste de récupération de compte : perte de l'appli ET des codes de secours,
    aucun autre moyen de rentrer. Renvoie les types retirés (pour l'audit du geste
    admin, jamais les valeurs). Lève si Logto est injoignable (l'appelant décide)."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA}
    r = requests.get(f"{base}/api/users/{sub}/mfa-verifications", headers=h, timeout=15)
    r.raise_for_status()
    removed = []
    for v in r.json():
        d = requests.delete(f"{base}/api/users/{sub}/mfa-verifications/{v['id']}", headers=h, timeout=15)
        d.raise_for_status()
        removed.append(v["type"])
    return removed


# ── Magic link : one-time-token Logto (onboarding sans saisie de code) ────────
# Le backend mint un OTT pour l'email de l'invité (Management API) ; le lien le
# porte → la custom UI Logto le consomme (signIn extraParams) → auth silencieuse,
# compte créé/loggé avec l'email EXACT (pas de mismatch). Best-effort : si le mint
# échoue (M2M absent, Logto down), l'invitation reste valide via le flow code email.
def mint_one_time_token(email: str, *, expires_in: int = 7 * 24 * 3600) -> str | None:
    """Mint un one-time-token Logto (magic link) pour `email`. Renvoie le token,
    ou None si le mint échoue (l'appelant dégrade vers le lien sans magic-link)."""
    import requests
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.post(
            f"{base}/api/one-time-tokens",
            json={"email": email, "expiresIn": int(expires_in)},
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA,
                     "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:  # M2M non configuré, Logto indispo, expiry rejeté…
        _log.warning("OTT mint échoué pour %s : %s", email, e)
        return None


def magic_url(base_url: str, email: str, *, expires_in: int = 7 * 24 * 3600) -> str:
    """Augmente `base_url` d'un magic-link Logto pour `email` (params `otl` +
    `login_hint`). Le front fait `signIn({extraParams:{one_time_token: otl},
    loginHint})`. Si le mint échoue, renvoie `base_url` inchangé (dégradation
    gracieuse vers le flow code email)."""
    from urllib.parse import quote
    ott = mint_one_time_token(email, expires_in=expires_in)
    if not ott:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}otl={quote(ott, safe='')}&login_hint={quote(email, safe='')}"


def _refus_dcr(message: str, *args) -> None:
    """Journalise un refus d'enregistrement ET le pousse au suivi d'erreurs.

    Le journal systemd de la box tourne sur ~19 h et la lentille REST n'instrumente
    que `/api/…` : un refus de DCR n'a AUCUNE trace durable. Or c'est le seul
    événement qui NOMME un client qu'on ne sait pas encore accueillir — le perdre,
    c'est rejouer le diagnostic à l'aveugle. Volume attendu : quelques unités.
    """
    _log.warning(message, *args)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            # Clé de recherche stable (`has:oto.dcr`) ; le TEXTE porte le redirect
            # refusé, donc une issue par client inconnu, pas un fourre-tout.
            scope.set_tag("oto.dcr", "refus")
            sentry_sdk.capture_message(message % args if args else message,
                                       level="warning")
    # noqa: SILENT — une DCR ne casse pas parce que le suivi d'erreurs est indisponible
    except Exception:
        pass


class RedirectRegistrationFailed(RuntimeError):
    """L'annuaire a RÉPONDU, et le PATCH qui devait y poser un NOUVEAU redirect a
    échoué. On ne suppose pas : on sait que le callback n'est pas enregistré, donc
    que l'`/authorize` qui suivra sera refusé. À distinguer d'un annuaire
    injoignable, où l'état reste inconnu — cf. `dcr`."""


def _register_redirects(app_id: str, redirect_uris: list,
                        directory: "Directory | None" = None) -> None:
    """Ajoute les `redirect_uris` (déjà validés) à l'app Logto `app_id` (dédup) +
    l'origine CORS https correspondante. Idempotent ; no-op si tout est déjà là.
    Lève si la Management API échoue (l'appelant décide quoi en faire) —
    `RedirectRegistrationFailed` quand l'échec porte sur l'écriture elle-même.

    ⚠️ `app_id` et `directory` sont un COUPLE : une app d'un annuaire patchée dans un
    autre vise, au mieux, une application inexistante. L'appelant les choisit
    ensemble, ce module ne les rapproche jamais par défaut."""
    import requests
    d = directory or _primary_directory()
    base, tok = d.api_endpoint, _mgmt_token(d)
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA, "Content-Type": "application/json"}
    data = requests.get(f"{base}/api/applications/{app_id}", headers=h, timeout=15)
    data.raise_for_status()
    app = data.json()
    meta = app.get("oidcClientMetadata", {}) or {}
    custom = app.get("customClientMetadata", {}) or {}
    cur = list(meta.get("redirectUris", []))
    cors = list(custom.get("corsAllowedOrigins", []))
    new = [u for u in redirect_uris if u not in cur]
    for u in redirect_uris:
        pp = urlparse(u)
        origin = f"{pp.scheme}://{pp.netloc}"
        if pp.scheme == "https" and origin not in cors:
            cors.append(origin)
    if not new and set(cors) == set(custom.get("corsAllowedOrigins", []) or []):
        return
    meta["redirectUris"] = cur + new
    custom["corsAllowedOrigins"] = cors
    try:
        p = requests.patch(f"{base}/api/applications/{app_id}",
                           json={"oidcClientMetadata": meta, "customClientMetadata": custom},
                           headers=h, timeout=15)
        p.raise_for_status()
    except Exception as exc:
        raise RedirectRegistrationFailed(
            f"app {app_id} — {len(new)} redirect(s) non enregistré(s)") from exc
    _log.info("DCR: annuaire %s, app %s — +%d redirect(s), cors=%s",
              d.label, app_id, len(new), cors)


def ensure_api_resource(indicator: str, *, name: str | None = None) -> None:
    """Enregistre (idempotent) une API resource Logto pour `indicator` (le resource
    indicator = l'audience JWT, ex. `https://<slug>.mcp.oto.cx/mcp`). Sans ça, Logto
    émet un token OPAQUE pour ce sous-domaine (≠ resource enregistrée) → `invalid_token`
    (blocage historique #44). Best-effort côté appelant : lève si la Management API
    échoue (l'appelant loggue et n'empêche pas la publication). Réutilise le M2M partagé."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA, "Content-Type": "application/json"}
    existing = requests.get(f"{base}/api/resources", headers=h, timeout=15)
    existing.raise_for_status()
    if any((r.get("indicator") == indicator) for r in existing.json()):
        return
    r = requests.post(
        f"{base}/api/resources",
        json={"name": name or indicator, "indicator": indicator},
        headers=h, timeout=15,
    )
    r.raise_for_status()
    _log.info("Logto API resource créée : %s", indicator)


# ── PRM (RFC 9728) host-aware — la SEULE pièce de discovery à rendre host-aware ─
# Le PRM annonce le `resource` (= l'audience que le client demandera à Logto). Sur le
# sous-domaine d'un projet org publié, il DOIT annoncer le sous-domaine lui-même (sinon
# claude.ai reçoit un token opaque, blocage #44).
# Sécurité canonique : on construit le PRM via le MÊME modèle mcp lib que fastmcp, avec
# les MÊMES paramètres pour le host canonique → sortie identique byte-à-byte ; on ne
# diverge le `resource` que pour un sous-domaine org VÉRIFIÉ publié.
#
# ⚠️ **L'AS n'est plus canonique par principe** (lot L3). Le commentaire disait « l'AS
# reste canonique (RFC 8707 : resource indicator ≠ authorization server) » : c'était
# vrai tant qu'il n'y avait qu'un émetteur. Depuis le registre de tenants, un host peut
# être servi POUR un autre tenant, et lui annoncer NOTRE émetteur envoie son utilisateur
# natif sur NOTRE écran de connexion — où il n'a pas de compte (oto-private#83).
# Le host décide donc de l'AS et du nom ; un host qu'aucun tenant ne réclame garde le
# comportement d'avant, à l'octet près.
def _prm_handler(public_url: str, resource_url: str, *, as_url: str = "",
                 resource_name: str = "oto MCP"):
    from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
    from mcp.shared.auth import ProtectedResourceMetadata
    md = ProtectedResourceMetadata(
        resource=AnyHttpUrl(resource_url),
        authorization_servers=[AnyHttpUrl(as_url or public_url)],
        # Mêmes valeurs que _build_auth (RemoteAuthProvider) → PRM canonique identique.
        scopes_supported=["openid", "profile", "email", "offline_access"],
        resource_name=resource_name,
    )
    return ProtectedResourceMetadataHandler(md)


def tenant_discovery_for_host(host: str):
    """`(as_url, resource_name)` à annoncer pour ce host, ou **None** s'il n'est
    réclamé par aucun tenant.

    ⚠️ **L'`as_url` est le host LUI-MÊME, pas l'émetteur du tenant** — c'est-à-dire
    NOTRE façade, servie sur son domaine. Annoncer l'émetteur en direct paraît plus
    honnête et casse tout : la façade existe parce que Logto self-hosted ne sait pas
    enregistrer un client à la volée, et la retirer du chemin fait échouer le client
    sur « l'enregistrement automatique n'est pas pris en charge » (vécu en fenêtre, le
    13/08). La façade servie ici route ensuite vers l'annuaire du tenant et sert SON
    client préparé (`as_metadata`, `dcr`).

    Partagé par le PRM et le 401 pour qu'ils ne puissent pas diverger : annoncer un
    serveur dans l'un et un autre dans l'autre enverrait le client faire un aller-
    retour entre deux annuaires, panne bien plus obscure que celle qu'on corrige.
    """
    from .. import tenancy
    entry = tenancy.current().for_host(host)
    if entry is None:
        return None
    return f"https://{host}/", (entry.name or entry.slug)


def _host_of(request) -> str:
    return (request.headers.get("host") or "").split(":")[0].strip().lower()


def tenant_for_host(host: str):
    """L'entrée de registre servie par ce host, ou None. Sert les routes de la façade
    (métadonnée + enregistrement), qui doivent toutes viser le MÊME annuaire."""
    from .. import tenancy
    return tenancy.current().for_host(host)


def _refus_annuaire(entry, directory: "Directory | None", requested: list):
    """Ce que la façade REFUSE de promettre sur le host d'un tenant — ou `None` si la
    voie est libre.

    Le défaut historique tient en une phrase : sur un host de tenant, rien n'était
    enregistré et on répondait **201 quand même** (oto-backend#909). Le client
    recevait un succès puis `oidc.invalid_redirect_uri` deux secondes plus tard, à
    l'`/authorize` — l'étape suivante, chez un annuaire qui n'est pas le nôtre, avec
    un message qui n'accuse pas la bonne cause. Un 201 est une CRÉATION : ne pas le
    rendre quand on sait n'avoir rien créé n'est pas une sévérité nouvelle, c'est
    arrêter de mentir.

    Le refus **nomme sa destination** : ce qui manque, et à qui le demander. Les trois
    causes ne s'adressent pas au même monde — l'exploitant du tenant, son
    administrateur d'annuaire, nous — donc elles ne partagent pas leur message. Le
    JOURNAL porte le détail interne (slug, nom des variables d'environnement) ; le
    corps servi porte ce que le demandeur peut faire, et rien de notre nomenclature.

    503 partout : chacune de ces causes se lève par un geste d'exploitation, après
    quoi le même appel réussit. C'est aussi le code de la branche voisine (écriture
    ratée sur notre annuaire) — deux refus d'enregistrement, un seul code à connaître.
    """
    nom = getattr(entry, "name", "") or getattr(entry, "slug", "") or "ce domaine"
    slug = getattr(entry, "slug", "") or "?"
    uris = ", ".join(str(u) for u in requested)
    cible = f"le rappel {uris}" if uris else "le rappel demandé"

    if not getattr(entry, "oauth_client_id", ""):
        _refus_dcr(
            "DCR sur le host du tenant %r sans client OAuth déclaré — rien n'est "
            "enregistré et l'appel est refusé (rappels=%r). Renseigner "
            "`tenants.oauth_client_id`.", slug, requested)
        detail = (f"aucun client OAuth n'est préparé sur le serveur d'autorisation de "
                  f"{nom} : l'enregistrement automatique est impossible sur ce "
                  f"domaine. Demander à l'exploitant de {nom} de le déclarer.")
    elif directory is None:
        _refus_dcr(
            "DCR sur le host du tenant %r : aucun accès d'administration déclaré "
            "(`tenants.logto_mgmt`) — les rappels %r ne sont pas enregistrés dans son "
            "annuaire et l'appel est refusé.", slug, requested)
        detail = (f"{cible} n'a pas été enregistré auprès du serveur d'autorisation "
                  f"de {nom} : la plateforme n'a pas d'accès d'administration à cet "
                  f"annuaire. Demander à l'administrateur de {nom} d'ajouter ce "
                  f"rappel à l'application {entry.oauth_client_id}.")
    elif not _credential_present(directory):
        _refus_dcr(
            "DCR sur le host du tenant %r : accès d'annuaire DÉCLARÉ mais credential "
            "absent de l'environnement de ce process (%s_ID / %s_SECRET) — les "
            "rappels %r ne sont pas enregistrés et l'appel est refusé.",
            slug, directory.credential, directory.credential, requested)
        detail = (f"{cible} n'a pas été enregistré auprès du serveur d'autorisation "
                  f"de {nom} : l'accès d'administration de cet annuaire manque à ce "
                  f"serveur. L'exploitant est alerté ; réessayer ensuite.")
    else:
        return None

    return JSONResponse({"error": "temporarily_unavailable",
                         "error_description": detail},
                        status_code=503, headers=_cors())


def make_routes(public_url: str, claude_app_id: str) -> list[Route]:
    public_url = public_url.rstrip("/")

    async def as_meta(request: Request) -> JSONResponse:
        # Servie sur le host d'un tenant : l'issuer est CE host (la façade), et les
        # endpoints d'autorisation sont ceux de SON annuaire. Un host libre est servi
        # exactement comme avant.
        host = _host_of(request)
        entry = tenant_for_host(host)
        if entry is not None:
            return JSONResponse(as_metadata(f"https://{host}", entry.issuer))
        return JSONResponse(as_metadata(public_url))

    async def authorize(request: Request):
        # oto#202 — la destination est NOTRE annuaire, résolu ici, jamais la requête :
        # elle n'est recopiée qu'après le `?` (cf. authorize_consent). Un host de
        # tenant annonce son propre point d'autorisation : cette route n'y existe pas.
        if tenant_for_host(_host_of(request)) is not None:
            return JSONResponse({"error": "not_found", "error_description":
                                 "l'autorisation de cet hôte se fait chez son annuaire"},
                                status_code=404)
        return redirection(_logto_public_oidc(),
                           request.scope.get("query_string", b"").decode("latin-1"))

    async def oidc_meta(request: Request) -> JSONResponse:
        host = _host_of(request)
        entry = tenant_for_host(host)
        if entry is not None:
            return JSONResponse(as_oidc_metadata(f"https://{host}", entry.issuer))
        return JSONResponse(as_oidc_metadata(public_url))

    async def prm(request: Request):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        # Sous-domaine d'un projet org PUBLIÉ → resource = ce sous-domaine ; sinon
        # canonique (identique à fastmcp). valid_org_audience = motif + existence DB.
        candidate = f"https://{host}/mcp"
        from .. import subdomain_project
        from ..config import mcp_audience_alt_hosts
        # Host réclamé par un tenant → SON émetteur et SON nom ; sinon rien ne change.
        tenant = tenant_discovery_for_host(host)
        as_url, resource_name = tenant if tenant else ("", "oto MCP")
        # Host = domaine canonique SECONDAIRE (ex. mcp.oto.cx) → resource = ce host ;
        # sinon host d'un TENANT déclaré ; sinon sous-domaine d'un projet org publié ;
        # sinon canonique (mcp.oto.ninja).
        #
        # ⚠️ Le cran `tenant` n'est pas une redite de `mcp_audience_alt_hosts()` : c'est
        # ce qui rend le retrait de `MCP_AUDIENCE_ALT` POSSIBLE. Le host d'un partenaire
        # y vit aujourd'hui ; sans dérivation depuis ses hosts déclarés, le flip (retrait
        # du drain + de l'audience alt) ferait retomber sa `resource` sur NOTRE domaine —
        # le client demanderait alors un jeton pour une audience qui n'est pas la sienne,
        # et l'échec se lirait comme un problème d'émetteur.
        resource_url = candidate if (host in mcp_audience_alt_hosts()
                                     or tenant is not None
                                     or subdomain_project.valid_org_audience(candidate)) \
            else f"{public_url}/mcp"
        return await _prm_handler(public_url, resource_url, as_url=as_url,
                                  resource_name=resource_name).handle(request)

    async def dcr(request: Request) -> JSONResponse:
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=_cors())
        # Corps ABSENT ⇒ `{}` (comportement d'avant conservé) ; corps ILLISIBLE ⇒
        # 400 nommé plutôt qu'un client_id partagé émis sur des métadonnées devinées.
        try:
            body = await read_json_body(request)
        except InvalidJsonBody as e:
            _log.warning("DCR refusé — corps illisible (%s)", e.code)
            return JSONResponse({"error": "invalid_client_metadata",
                                 "error_description": e.detail},
                                status_code=400, headers=_cors())
        # Défense en profondeur (audit 2026-06-13) : on n'émet le client_id
        # partagé QUE pour des redirect_uris connus. L'enforcement réel reste
        # Logto au `/authorize` (cf. invariant ci-dessus), mais fail-fast ici
        # évite de tendre un client public à un redirect non prévu.
        requested = body.get("redirect_uris") or []
        if not isinstance(requested, list) or any(not _redirect_ok(u) for u in requested):
            _refus_dcr("DCR refusé — redirect_uris=%r client_name=%r grant_types=%r",
                       requested, body.get("client_name"), body.get("grant_types"))
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": "redirect_uri non autorisé pour ce serveur"},
                status_code=400,
                headers=_cors(),
            )
        # DCR réelle : on enregistre dynamiquement le(s) redirect(s) dans l'app Logto
        # de l'annuaire visé (le redirect de ChatGPT est propre à chaque connecteur).
        # Deux échecs possibles, qui ne se valent pas :
        #  — l'annuaire est INJOIGNABLE : on ignore ce qui y est déjà posé, donc on
        #    rend le client_id (un client dont le redirect est déjà enregistré, comme
        #    Claude, doit continuer de s'installer pendant un incident Logto) ;
        #  — l'annuaire a répondu et l'ÉCRITURE a échoué : on sait que le callback
        #    n'y est pas, donc que l'`/authorize` sera refusé. Rendre 201 serait
        #    annoncer une création qui n'a pas eu lieu, et déplacer le diagnostic
        #    d'un cran, chez un client qui ne lit pas nos journaux.
        # Sur le host d'un tenant, le client rendu doit être celui de SON annuaire :
        # c'est là que l'utilisateur va s'authentifier. Rendre le nôtre enverrait le
        # client se présenter chez l'un avec l'identité de l'autre — refus au
        # `/authorize`, et un message qui n'accuse pas la bonne cause.
        entry = tenant_for_host(_host_of(request))
        if entry is None:
            app_id, directory = claude_app_id, _primary_directory()
        else:
            app_id, directory = entry.oauth_client_id, directory_for_tenant(entry)
            refus = _refus_annuaire(entry, directory, requested)
            if refus is not None:
                return refus
        try:
            # Hors boucle : la Management API est un appel HTTP synchrone (15 s), et
            # cette route est `async def` — nûment, elle fige tout le processus le
            # temps de la réponse (oto-backend#867).
            await run_in_threadpool(_register_redirects, app_id, requested, directory)
        except RedirectRegistrationFailed:
            _log.exception("DCR: enregistrement Logto échoué (redirects=%r)", requested)
            return JSONResponse(
                {"error": "temporarily_unavailable",
                 "error_description": "l'enregistrement du redirect_uri auprès du "
                                      "serveur d'autorisation a échoué — réessayer"},
                status_code=503,
                headers=_cors(),
            )
        except Exception:
            _log.exception("DCR: annuaire injoignable (redirects=%r)", requested)
        # Logto valide le redirect contre l'app : on rend le client_id de l'annuaire
        # où le rappel vient d'être posé — le nôtre, ou celui du tenant.
        return JSONResponse(
            {
                "client_id": app_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": body.get("redirect_uris", []),
                "token_endpoint_auth_method": "none",
                "grant_types": body.get("grant_types", ["authorization_code", "refresh_token"]),
                "response_types": body.get("response_types", ["code"]),
                "client_name": body.get("client_name"),
            },
            status_code=201,
            headers=_cors(),
        )

    return [
        # Métadonnée AS servie à TOUTES les variantes de chemin que les clients
        # MCP tentent : racine (issuer sans path) ET path-suffixée par la
        # ressource `/mcp` (RFC 8414 path-insertion — claude.ai essaie les deux).
        Route("/.well-known/oauth-authorization-server", as_meta, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server/mcp", as_meta, methods=["GET"]),
        # OIDC discovery (en plus de RFC 8414) — sondé par les clients OAuth 2.1/OIDC
        # (Mistral) ; un 404 ici peut casser leur résolution d'AS. Mêmes 2 variantes.
        Route("/.well-known/openid-configuration", oidc_meta, methods=["GET"]),
        Route("/.well-known/openid-configuration/mcp", oidc_meta, methods=["GET"]),
        # PRM host-aware (RFC 9728) : shadow les routes fastmcp (insérées avant → priorité).
        # Canonique = identique à fastmcp ; sous-domaine org publié = resource = le sous-domaine.
        Route("/.well-known/oauth-protected-resource", prm, methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource/mcp", prm, methods=["GET", "OPTIONS"]),
        Route("/oauth/register", dcr, methods=["POST", "OPTIONS"]),
        # oto#202 : l'autorisation annoncée par la métadonnée de NOTRE annuaire passe par ici.
        Route("/oauth/authorize", authorize, methods=["GET"]),
    ]
