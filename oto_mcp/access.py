"""Rôles + résolution de clé API + quotas par tool.

Le rôle `users.role` ne sert plus qu'à décider qui voit l'admin UI :

- **admin** : peut gérer `platform_keys` + grants via `/api/admin/*`.
  Bootstrap : env `OTO_MCP_ADMIN_SUB` force ce sub en admin quoi qu'il y
  ait en DB.
- **member** / **guest** : alias historiques sans effet sur l'accès aux
  tools. L'accès se décide via les `user_grants` (cf. ci-dessous).

Résolution d'une clé API par appel (`resolve_api_key`) :

1. Si user key posée par le user lui-même sur `/account` → on la prend,
   sans quota.
2. Sinon, on cherche un grant explicite dans `user_grants` (admin a posé
   une autorisation) → on prend la `platform_keys.api_key` la plus
   récemment grantée.
3. Sinon (et y compris pour un admin sans grant) → McpError actionnable.

Quota daily : chaque grant porte un `daily_quota` optionnel (per-user,
posé par l'admin au moment du grant). Si null, fallback sur
`OTO_MCP_QUOTA_<PROVIDER>_DAILY` env ou `_QUOTA_DEFAULTS`.

Les clés plateforme sont importées au boot (`bootstrap_env_keys`) mais
ne sont PAS auto-grantées — l'admin doit accorder l'accès explicitement
via l'API admin.
"""
from __future__ import annotations

import os
from typing import Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from . import db
from .auth_hooks import current_user_sub_from_token

# Rôles
GUEST = "guest"
MEMBER = "member"
ADMIN = "admin"
ROLES = (GUEST, MEMBER, ADMIN)

_QUOTA_DEFAULTS = {
    "serper": 50,
    "hunter": 10,
    "sirene": 200,
    "attio": 200,
    "kaspr": 5,
}

# Providers dont le secret peut être POSSÉDÉ par une org et partagé à ses
# membres (org_secret). Sous-ensemble de db.KEY_PROVIDERS : exclut `slack`
# (user token xoxp → poste en as_user = identité du propriétaire du token, pas
# du membre appelant). linkedin/google/whatsapp/crunchbase ne passent pas par
# resolve_api_key (sessions per-user). Cf. project_oto_mcp_org_tier — un
# org_secret ne s'applique qu'aux credentials de compte fongibles.
ORG_SHAREABLE_PROVIDERS = frozenset({
    "serper", "hunter", "sirene", "attio", "lemlist", "kaspr", "pennylane", "fullenrich",
})

_ACCOUNT_URL = "https://oto.ninja/account"


def get_user_role(sub: str) -> str:
    """Rôle effectif du user — env override > DB > défaut guest."""
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        return ADMIN
    user = db.get_user(sub)
    role = (user or {}).get("role") or GUEST
    return role if role in ROLES else GUEST


def current_user_sub_or_raise() -> str:
    sub = current_user_sub_from_token()
    if not sub:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Unauthenticated — no user identity on the request.",
        ))
    return sub


def quota_for(provider: str) -> int:
    raw = os.environ.get(f"OTO_MCP_QUOTA_{provider.upper()}_DAILY")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _QUOTA_DEFAULTS.get(provider, 0)


def resolve_api_key(provider: str, env_secret_name: Optional[str] = None) -> tuple[str, bool]:
    """Renvoie `(api_key, is_platform)` ou lève McpError actionnable."""
    sub = current_user_sub_or_raise()

    user_key = db.get_user_api_key(sub, provider)
    if user_key:
        return user_key, False

    # Palier org : secret partagé possédé par l'org active du user. Sauté tant
    # que l'user n'a pas d'org active (get_active_org -> None) → comportement
    # strictement identique à avant pour tout user flat. is_platform=False : le
    # credential appartient à l'org (coût fixe), jamais métré sur un quota
    # plateforme. Override perso (ci-dessus) prime toujours.
    if provider in ORG_SHAREABLE_PROVIDERS:
        active_org = db.get_active_org(sub)
        if active_org is not None:
            org_key = db.get_org_secret(active_org, provider)
            if org_key:
                return org_key, False

    grant = db.get_active_grant(sub, provider)
    if not grant:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Aucune clé `{provider}` configurée pour toi. Soit pose "
                f"ta propre clé sur {_ACCOUNT_URL} (section {provider.capitalize()}), "
                f"soit demande à un admin de te grant un accès à une clé plateforme."
            ),
        ))

    used = db.get_usage_today(sub, provider)
    limit = grant.get("daily_quota") or quota_for(provider)
    if limit and used >= limit:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Quota plateforme {provider} dépassé aujourd'hui ({used}/{limit}) "
                f"pour la clé `{grant['label']}`. Pose ta propre clé sur {_ACCOUNT_URL} "
                f"pour continuer sans limite."
            ),
        ))

    return grant["api_key"], True


def record_platform_usage(provider: str) -> None:
    """À appeler APRÈS un appel réussi avec la platform key. No-op si pas authentifié."""
    sub = current_user_sub_from_token()
    if not sub:
        return
    db.increment_usage(sub, provider)


def status_for(sub: str) -> dict:
    """Snapshot pour `/api/me` — rôle + statut par provider :

    - `mode` : `user` (clé perso) | `platform` (grant + quota OK)
              | `over_quota` (grant mais quota épuisé)
              | `forbidden` (ni user key ni grant)
    """
    role = get_user_role(sub)
    # Org active résolue une fois (perf : sinon 1 lookup/provider). None pour
    # tout user sans org → la branche org_secret ci-dessous est inerte et
    # status_for reste identique à avant.
    active_org = db.get_active_org(sub)
    out: dict = {"role": role, "active_org": active_org, "providers": {}}
    for provider in db.KEY_PROVIDERS:
        user_key = db.get_user_api_key(sub, provider)
        org_key = (
            db.get_org_secret(active_org, provider)
            if active_org is not None and provider in ORG_SHAREABLE_PROVIDERS
            else None
        )
        grant = db.get_active_grant(sub, provider)
        used = db.get_usage_today(sub, provider)
        limit = (grant.get("daily_quota") if grant else None) or quota_for(provider)

        # Miroir EXACT de la cascade de resolve_api_key : user_key > org_secret
        # > grant plateforme. Toute divergence = /api/me ment sur le mode réel.
        if user_key:
            mode = "user"
        elif org_key:
            mode = "org"
        elif not grant:
            mode = "forbidden"
        elif limit and used >= limit:
            mode = "over_quota"
        else:
            mode = "platform"

        out["providers"][provider] = {
            "mode": mode,
            "user_key_configured": bool(user_key),
            "org_secret_configured": bool(org_key),
            "platform_key_label": grant["label"] if grant else None,
            "quota_used_today": used,
            "quota_daily": limit if grant else None,
        }
    return out


def bootstrap_env_keys(env_keys: dict[str, str]) -> None:
    """Au démarrage : importe les env vars `<PROVIDER>_API_KEY` en
    `platform_keys` (label `env`). Idempotent — appelable à chaque boot.

    Les clés sont importées mais PAS auto-grantées. Un admin doit
    explicitement accorder l'accès via `/api/admin/users/{sub}/grants/{key_id}`
    avec un `daily_quota` par user.

    `env_keys` = {provider: api_key} extrait par le caller via
    `oto.config.get_secret` ; on ne touche pas l'env nous-mêmes pour rester
    découplé du runtime de secrets.
    """
    for provider, api_key in env_keys.items():
        if not api_key:
            continue
        if provider not in db.KEY_PROVIDERS:
            continue
        try:
            db.upsert_platform_key(provider, "env", api_key)
        except Exception:
            continue
