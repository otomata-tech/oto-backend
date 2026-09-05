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
"""
from __future__ import annotations

import hmac
import hashlib
import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .. import credentials_store, db
from . import flow as oauth_flow
from ..connectors import flow as connector_flow
from ..connectors import health as connector_health
from ..connectors import link as connector_link


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # Drive COMPLET (restricted) — gérer TOUS les fichiers du user (pas seulement
    # ceux créés par oto). Couvre aussi l'export datastore (#29). Supersede drive.file.
    "https://www.googleapis.com/auth/drive",
    # Gmail surface complète (read/send/reply/draft/archive/trash). Scope
    # RESTRICTED chez Google → audit CASA requis si l'écran de consentement
    # passe en published/external (OK en mode testing).
    "https://www.googleapis.com/auth/gmail.modify",
    # Google Tasks (read/write). Scope SENSIBLE (pas restricted comme Gmail) →
    # vérification Google requise si l'app passe en published, pas d'audit CASA.
    "https://www.googleapis.com/auth/tasks",
    # Google Calendar (read/write events). Scope SENSIBLE (pas restricted) →
    # vérification de marque à la publication, pas d'audit CASA.
    "https://www.googleapis.com/auth/calendar",
    # Google Chat (RESTRICTED) — lire les espaces + lire/poster des messages.
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages",
]

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_STATE_TTL = 600  # 10 min


def _client_id() -> str:
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


def _redirect_uri() -> str:
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/google/oauth/callback"


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


def make_state(sub: str, org_id: int, return_app: str = "") -> str:
    """HMAC-signed state : `<b64(payload)>.<b64(sig)>` — payload = {sub, org, ts, app}.

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
                          "app": return_app},
                         separators=(",", ":")).encode()
    sig = hmac.new(_state_secret(), payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def verify_state(state: str) -> Optional[tuple[str, int, str]]:
    """Renvoie (sub, org_id, return_app) si state valide et non expiré, sinon None.

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

    return sub, org, oauth_flow.resolve_return_app(data.get("app") or "")


def build_auth_url(sub: str, return_app: str = "") -> str:
    """L'URL de consentement Google.

    `return_app` : clé de front déclarée par l'APPELANT (ex. un front tiers), jamais
    un Origin sniffé — les capacités sont transport-agnostiques (ADR 0009). Validée
    ICI, une seule fois, AVANT `make_state` : `resolve_return_app` réduit toute
    valeur hors de sa liste fermée à `""`, donc le state ne porte jamais une valeur
    de client non vérifiée (pas de redirection ouverte)."""
    from urllib.parse import urlencode

    from . import flow as oauth_flow

    org_id = _ctx_org(sub)
    resolved_app = oauth_flow.resolve_return_app(return_app)
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # consent → force refresh_token ; select_account → laisse l'user choisir
        # quel compte Google connecter (clé du multi-compte).
        "prompt": "consent select_account",
        "state": make_state(sub, org_id, resolved_app),
        "include_granted_scopes": "true",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Échange le code OAuth contre tokens. Renvoie le dict de réponse Google.

    Clés attendues : `access_token`, `refresh_token`, `expires_in`, `scope`.
    """
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _fetch_email(access_token: str) -> str:
    """Récupère l'adresse du compte Google qui vient de consentir.

    Via le profil Gmail (scope gmail.modify déjà accordé) — évite d'ajouter
    un scope identité juste pour connaître l'email.
    """
    import requests
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


def persist_token(sub: str, org_id: int, token_response: dict) -> str:
    """Persiste les tokens (scope membre : l'org vient du state, capturée au
    démarrage du flow) et renvoie l'email du compte Google connecté."""
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
    scopes = token_response.get("scope") or " ".join(SCOPES)
    email = _fetch_email(access_token)
    db.set_google_oauth(
        sub,
        org_id,
        google_email=email,
        refresh_token=refresh_token,
        scopes=scopes,
        access_token=access_token,
        expires_at=expires_at,
    )
    return email


class GoogleReauthRequired(Exception):
    """Refresh token Google mort (invalid_grant) → l'user doit reconnecter."""


def _refresh_access_token(refresh_token: str) -> dict:
    import requests
    r = requests.post(
        _TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
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
    dash = "https://manage.oto.cx/ (section Google)"
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


def credentials_for(sub: str, account: Optional[str] = None):
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

    if needs_refresh:
        member_id = credentials_store.member_id(org_id, sub)
        account = row.get("google_email") or ""
        scope = (credentials_store.MEMBER, member_id, account)
        try:
            resp = _refresh_access_token(row["refresh_token"])
        except GoogleReauthRequired as e:
            # Grant mort : on MARQUE (aide partagée oto#25 lot b2), jamais de purge —
            # même garde de portée que atlassian/folk/salesforce/zoho. On relève
            # ENSUITE, sans changer le contrat de `credentials_for` (toujours des
            # `Credentials` valides ou une exception, jamais un `None` muet).
            connector_health.mark_rejected(
                credentials_store.MEMBER, member_id, "google", account, str(e) or None)
            raise
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

    return Credentials(
        token=access_token,
        refresh_token=row["refresh_token"],
        token_uri=_TOKEN_URL,
        client_id=_client_id(),
        client_secret=_client_secret(),
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
    label="Autoriser oto chez Google",
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
