"""Rôles + résolution de clé API + quotas par tool.

Trois rôles user :
- **guest** (défaut sign-up) : ne consomme JAMAIS les clés serveur ; doit
  poser sa propre clé sur `oto.ninja/account` pour utiliser un tool.
- **member** : utilise la clé serveur (fallback) avec un quota daily ; sa
  user key, si posée, prend le pas et n'est pas comptée dans le quota.
- **admin** : pas de quota, accès aux endpoints d'admin.

Source de vérité du rôle = colonne `users.role` dans la SQLite locale. Logto
identifie l'utilisateur (sub), c'est tout. Bootstrap d'un admin : variable
d'env `OTO_MCP_ADMIN_SUB` (un sub Logto), force ce user en admin quoi qu'il
y ait en DB.

Quotas par tool depuis l'env (`OTO_MCP_QUOTA_<PROVIDER>_DAILY`) avec un
défaut conservateur — 0 = quota indéfiniment dépassé, autrement dit le mode
platform key est désactivé.
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

# Défauts de quota : Hunter coûte cher (1 crédit/call), SIRENE est gratuit
# mais l'API a son propre rate-limit, Serper est entre les deux.
_QUOTA_DEFAULTS = {
    "serper": 50,
    "hunter": 10,
    "sirene": 200,
}

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
    """Quota daily pour un provider, en lisant `OTO_MCP_QUOTA_<PROVIDER>_DAILY`."""
    raw = os.environ.get(f"OTO_MCP_QUOTA_{provider.upper()}_DAILY")
    if raw is not None:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _QUOTA_DEFAULTS.get(provider, 0)


def resolve_api_key(provider: str, env_secret_name: Optional[str] = None) -> tuple[str, bool]:
    """Choisit la clé à utiliser pour `provider`. Retourne `(key, is_platform)`.

    1. Si user key posée en DB → on la prend (is_platform=False), pas de quota.
    2. Sinon, si rôle guest → McpError pointant vers /account.
    3. Sinon (member/admin), quota check pour member, puis platform key
       résolue via `oto.config.get_secret(env_secret_name)`. Si quota dépassé,
       McpError pointant vers /account pour poser sa propre clé.

    Lève `McpError` (INVALID_PARAMS) avec un message actionnable dans tous
    les cas d'échec — l'utilisateur voit le message côté client MCP.
    """
    sub = current_user_sub_or_raise()
    user_key = db.get_user_api_key(sub, provider)
    if user_key:
        return user_key, False

    role = get_user_role(sub)

    if role == GUEST:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Le tool `{provider}` nécessite ta propre clé API tant que ton "
                f"compte est en mode invité. Pose ta clé sur {_ACCOUNT_URL} "
                f"(section {provider.capitalize()})."
            ),
        ))

    if role == MEMBER:
        used = db.get_usage_today(sub, provider)
        limit = quota_for(provider)
        if used >= limit:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"Quota plateforme {provider} dépassé pour aujourd'hui "
                    f"({used}/{limit}). Pose ta propre clé {provider.capitalize()} "
                    f"sur {_ACCOUNT_URL} pour continuer sans limite imposée par oto."
                ),
            ))

    # Platform key — résolue à la demande pour ne pas crasher au boot si elle
    # manque (le tool déclenchera une erreur claire).
    if not env_secret_name:
        env_secret_name = f"{provider.upper()}_API_KEY"
    try:
        from oto.config import get_secret
        platform_key = get_secret(env_secret_name)
    except Exception as e:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Clé plateforme {env_secret_name} indisponible : {e}",
        )) from e
    if not platform_key:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Clé plateforme {env_secret_name} non configurée côté serveur.",
        ))
    return platform_key, True


def record_platform_usage(provider: str) -> None:
    """À appeler APRÈS un appel réussi avec la platform key. No-op pour user keys."""
    sub = current_user_sub_from_token()
    if not sub:
        return
    db.increment_usage(sub, provider)


def status_for(sub: str) -> dict:
    """Snapshot des permissions du user pour `/api/me` — rôle + état des keys."""
    role = get_user_role(sub)
    out: dict = {"role": role, "providers": {}}
    for provider in db.KEY_PROVIDERS:
        user_key = db.get_user_api_key(sub, provider)
        used = db.get_usage_today(sub, provider) if role == MEMBER else 0
        limit = quota_for(provider) if role == MEMBER else None
        if user_key:
            mode = "user"
        elif role == GUEST:
            mode = "forbidden"
        elif role == MEMBER and used >= (limit or 0):
            mode = "over_quota"
        else:
            mode = "platform"
        out["providers"][provider] = {
            "mode": mode,
            "user_key_configured": bool(user_key),
            "quota_used_today": used,
            "quota_daily": limit,
        }
    return out
