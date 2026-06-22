"""Rôles + résolution de clé API + quotas par tool.

Le rôle `users.role` décide de l'accès à l'admin UI, sur **3 paliers** (du plus
faible au plus fort) :

- **member** : rôle par défaut (non-admin), sans effet sur l'accès aux
  tools. L'accès se décide via les `user_grants` (cf. ci-dessous).
- **admin** (palier OPÉRATIONNEL intermédiaire) : supervision plateforme —
  liste des users, fiche user, monitoring des appels, activation des
  connecteurs, maintenance (refresh des mounts), lecture/admin opérationnelle
  des orgs. **PAS** d'escalade en masse vers les orgs tierces.
- **super_admin** (le tout-puissant) : tout l'opérationnel + escalade
  `org_admin` de TOUTES les orgs et `group_admin` de TOUS les groupes,
  gestion des rôles plateforme, platform keys, émission de tokens, écriture
  sur les orgs tierces (entitlements, doctrine d'une autre org), création d'org.
  Bootstrap : env `OTO_MCP_ADMIN_SUB` force ce sub en **super_admin** quoi
  qu'il y ait en DB.

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

Les clés plateforme vivent en DB (coffre `platform_keys`) — posées/rotées via la
surface admin (REST `/api/admin/platform-keys`, meta-tools `oto_admin_*`), plus
aucun import SOPS/env au boot (oto-mcp#12). Importer ≠ auto-granter : une clé
n'est accessible qu'avec un grant admin explicite.
"""
from __future__ import annotations

import os
from typing import Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from . import connectors, credentials_store, db, group_store, org_store, session_org
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES

# Rôles plateforme, du plus faible au plus fort : `member` (défaut non-admin) <
# `admin` (opérateur : supervision sans escalade en masse) < `super_admin`
# (tout-puissant : escalade org/groupe, rôles, keys, tokens, orgs tierces).
# `guest` retiré (2026-06-15) — c'était un alias sans effet, migré en `member`.
MEMBER = "member"
ADMIN = "admin"
SUPER_ADMIN = "super_admin"
ROLES = (MEMBER, ADMIN, SUPER_ADMIN)

# DÉRIVÉS du registre source unique (connectors.py) :
# - _QUOTA_DEFAULTS : quota daily par provider (fallback si pas d'env/grant).
# - ORG_SHAREABLE_PROVIDERS : providers dont le secret peut être POSSÉDÉ par une
#   org et partagé (auth_mode byo_org) — exclut slack (xoxp = identité perso) et
#   les sessions per-user (linkedin/google/whatsapp/crunchbase).
_QUOTA_DEFAULTS = connectors.QUOTA_DEFAULTS
ORG_SHAREABLE_PROVIDERS = connectors.ORG_SHAREABLE_PROVIDERS

_ACCOUNT_URL = "https://oto.ninja/account"


def get_user_role(sub: str) -> str:
    """Rôle effectif du user — env override > DB > défaut member.

    Le bootstrap `OTO_MCP_ADMIN_SUB` force le **super_admin** (le tout-puissant)
    — c'est le sub propriétaire de la plateforme."""
    admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
    if admin_sub and sub == admin_sub:
        return SUPER_ADMIN
    user = db.get_user(sub)
    role = (user or {}).get("role") or MEMBER
    return role if role in ROLES else MEMBER


def is_super_admin(sub: str) -> bool:
    """Tout-puissant : escalade org/groupe, rôles plateforme, keys, tokens,
    écriture sur orgs tierces."""
    return get_user_role(sub) == SUPER_ADMIN


def is_platform_operator(sub: str) -> bool:
    """Opérateur plateforme = `admin` (supervision) OU `super_admin`. Cran de
    visibilité/supervision, SANS l'escalade en masse réservée au super_admin."""
    return get_user_role(sub) in (ADMIN, SUPER_ADMIN)


def current_org(sub: str | None) -> Optional[int]:
    """Org sous laquelle Claude AGIT pour le `sub` courant — **seam unique** de
    résolution d'org (ADR 0023, amende 0015).

    Point de passage de TOUT ce qui scope une action sur l'org (credentials,
    visibilité, entitlements, redaction, billing). Aujourd'hui (barreau R0) =
    l'org persistée (`org_store.get_active_org`, qui devient l'« org maison »).

    Résout `org_de_session ?? org_de_consultation ?? org_maison` (ADR 0023) :
    - **org de session** (MCP) — override éphémère posé par `oto_use_org`, keyé par
      la session MCP courante ;
    - **org de consultation** (REST) — view-as du dashboard, contextvar per-requête
      posé APRÈS validation d'appartenance par l'adaptateur REST ;
    - sinon → repli sur la **maison** persistante (`org_store.get_active_org`).

    Les deux premières ne coexistent jamais (session = MCP only, consultation =
    REST only). Garder ce seam étroit : candidat broker de credentials (ADR 0004)."""
    if sub is None:
        return None
    present, org = session_org.current_override()
    if present:
        return org
    view = session_org.current_view_org()
    if view is not None:
        return None if view == 0 else view
    return org_store.get_active_org(sub)


def granted_namespaces_for(sub: str) -> frozenset:
    """Namespaces auxquels le `sub` a droit = grants per-user UNION entitlements
    de son org active.

    SOURCE UNIQUE de la visibilité des namespaces gouvernés (grant-only),
    consommée par le middleware, les meta-tools MCP ET les gardes REST — pour
    qu'aucune surface ne diverge (sinon un namespace refusé côté MCP serait
    contournable côté /account). Cf. project_oto_mcp_org_tier.
    """
    ns = set(db.list_user_granted_namespaces(sub))
    active_org = current_org(sub)
    if active_org is not None:
        ns |= set(org_store.list_org_entitled_namespaces(active_org))
        # Remote data-driven (ADR 0003/0011) : posséder le credential d'org (avec
        # base_url) EST le grant du namespace remote — pas d'entitlement séparé.
        ns |= credentials_store.org_remote_namespaces(active_org)
    return frozenset(ns)


def require_namespace(namespace: str) -> None:
    """Backstop d'autorisation AU CALL-TIME pour un namespace gouverné
    (grant-only) à credential SERVEUR — lève si le sub courant n'y a pas droit.

    Indépendant de la visibilité : l'autorisation ne doit JAMAIS reposer sur le
    seul masquage (qui peut fail-open si `list_tools` échoue au handshake →
    denylist incomplète). `gocardless` a déjà ce backstop via `resolve_api_key`
    (clé per-user) ; `mm` utilise un secret serveur (MM_REFRESH_TOKEN) sans clé
    per-user, d'où ce garde explicite à appeler dans son `_client()`.

    stdio local (sub=None) = accès complet, cohérent avec le middleware.
    """
    if namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
        return
    sub = current_user_sub_from_token()
    if sub is None:
        return
    if is_super_admin(sub):
        return
    if namespace not in granted_namespaces_for(sub):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Accès au namespace `{namespace}` non accordé. Demande à un "
                f"admin de te l'accorder (per-user ou via l'entitlement de ton org)."
            ),
        ))


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


def resolve_api_key(provider: str) -> tuple[str, bool]:
    """Renvoie `(api_key, is_platform)` ou lève McpError actionnable."""
    sub = current_user_sub_or_raise()

    user_key = db.get_user_api_key(sub, provider)
    if user_key:
        return user_key, False

    # Paliers partagés (ADR 0012) : secret du GROUPE actif (le plus spécifique),
    # puis de l'ORG active. Sautés tant que l'user n'a ni groupe ni org actifs
    # (-> None) → comportement strictement identique à avant pour tout user flat.
    # is_platform=False : credential possédé par le groupe/org (coût fixe), jamais
    # métré sur un quota plateforme. Override perso (ci-dessus) prime toujours.
    # Cascade : user_key > group_secret > org_secret > platform_grant.
    if provider in ORG_SHAREABLE_PROVIDERS:
        active_group = group_store.get_active_group(sub)
        if active_group is not None:
            grp_key = group_store.get_group_secret(active_group, provider)
            if grp_key:
                return grp_key, False
        active_org = current_org(sub)
        if active_org is not None:
            org_key = org_store.get_org_secret(active_org, provider)
            if org_key:
                return org_key, False

    # Défense en profondeur : le chemin platform-grant n'est valide que si le
    # registre AUTORISE `platform` pour ce provider. Un provider byo-only
    # (attio, lemlist, pennylane, fullenrich, slack…) ne doit JAMAIS être résolu
    # via une clé plateforme — même si une clé résiduelle existait en base
    # (seed SOPS historique). Sans ce gate, un grant suffisait à utiliser un
    # compte privé, l'inverse du modèle (audité 2026-06-11).
    con = connectors.connector_for_provider(provider)
    platform_eligible = con is not None and "platform" in con.auth_modes
    grant = db.get_active_grant(sub, provider) if platform_eligible else None
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


def resolve_credential_fields(provider: str) -> dict:
    """Résout un credential **multi-champs** byo_user (modèle générique, ADR 0011)
    du sub courant → dict des champs déclarés (`Connector.secret_fields`).

    Pour les connecteurs in-process dont le client s'instancie avec plusieurs
    secrets (ex. Silae : client_id / client_secret / subscription_key, OAuth2
    client-credentials). **byo-only** : pas de clé plateforme ni de quota — le
    credential EST le grant, comme un mount. Lève une McpError actionnable si le
    user n'a rien posé. Fait partie du seam de résolution `access` (avec
    resolve_api_key / resolve_mount_token), candidat broker (ADR 0004)."""
    sub = current_user_sub_or_raise()
    secret = credentials_store.get_credential("user", sub, provider)
    if not secret:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Aucun credential `{provider}` configuré pour toi. Renseigne-le "
                f"sur {_ACCOUNT_URL} (section {provider.capitalize()})."
            ),
        ))
    return credentials_store.unpack_secret(provider, secret)


def resolve_field_filter(service: str):
    """Construit le `FieldFilter` à appliquer aux réponses d'un connecteur pour
    le sub courant, selon la politique de redaction de son **org active**.

    Cascade (décision « contrôle total org ») :
      1. l'org active a une politique pour ce service → elle est **autoritaire**
         (peut lever le masquage baseline, ou ne rien masquer) ;
      2. sinon → repli sur le **défaut serveur** (`field_filter_defaults`, plancher
         PII explicite, ex. IBAN Silae) ;
      3. sinon → filtre vide (no-op, aucune redaction).

    Best-effort : sans org active ou sur erreur DB, on retombe sur le défaut
    serveur (jamais moins protecteur que l'état pré-UI)."""
    from oto.tools.common import FieldFilter

    from . import field_filter_defaults

    block: Optional[dict] = None
    sub = current_user_sub_from_token()
    if sub:
        active_org = current_org(sub)
        if active_org is not None:
            configured = org_store.get_org_field_filters(active_org)
            if service in configured:
                block = configured[service]
    if block is None:
        block = field_filter_defaults.SERVER_DEFAULTS.get(service)
    if not block:
        return FieldFilter()
    return FieldFilter(rules=block.get("rules", []), salt=block.get("salt"))


def unipile_api_key_for(sub: str) -> Optional[str]:
    """Clé API Unipile pour `sub`, en cascade (sans lever) : clé de l'user (BYO),
    secret de son org active (abonnement Otomata), puis **clé plateforme** si l'user
    a un grant (mode revente — partage de la clé sans la copier dans chaque org).
    None si aucune.

    Pris pour `sub` EXPLICITE → utilisable hors contexte MCP (route REST connect).
    Les tools MCP, eux, passent par `resolve_api_key("unipile")` (idiome keyed)."""
    key = db.get_user_api_key(sub, "unipile")
    if key:
        return key
    active_org = current_org(sub)
    if active_org is not None:
        org_key = org_store.get_org_secret(active_org, "unipile")
        if org_key:
            return org_key
    # Mode plateforme : grant explicite sur la clé plateforme unipile. Gate sur
    # l'éligibilité `platform` du registre (défense en profondeur, comme resolve_api_key).
    con = connectors.connector_for_provider("unipile")
    if con and "platform" in con.auth_modes:
        grant = db.get_active_grant(sub, "unipile")
        if grant:
            return grant["api_key"]
    return None


def resolve_remote_credential(provider: str) -> tuple[str, str]:
    """Résout `(base_url, token_m2m)` du **bridge** d'un connecteur remote
    (ADR 0003) depuis le credential de l'org active du sub courant.

    Le credential d'org d'un remote n'est PAS le secret du système client (il
    vit dans le bridge, ex. movinmotion-backoffice-bridge) : c'est le moyen
    d'appeler le bridge — `secret` = token M2M, `meta.base_url` = endpoint.
    Lève une McpError actionnable si l'org active n'a pas ce credential —
    **pas** de fallback SOPS côté serveur (cf. Phase 6). Remplace
    `resolve_org_credential` (l'injection in-process de l'ex-tools/mm.py).
    """
    sub = current_user_sub_or_raise()
    active_org = current_org(sub)
    if active_org is not None:
        cred = credentials_store.get_credential_with_meta("org", str(active_org), provider)
        if cred and cred["secret"]:
            base_url = (cred["meta"] or {}).get("base_url")
            if not base_url:
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(
                        f"Credential `{provider}` posé sans `base_url` dans meta — "
                        f"re-poser via `oto_admin_set_org_secret` avec l'endpoint du bridge."
                    ),
                ))
            return base_url.rstrip("/"), cred["secret"]
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Aucun credential `{provider}` sur ton org active. Un admin doit le "
            f"poser sur l'org propriétaire (`oto_admin_set_org_secret`) et t'y "
            f"rattacher."
        ),
    ))


def resolve_mount_token(provider: str) -> str:
    """Résout le **token OAuth per-user** d'un connecteur fédéré `kind="mount"`
    (otomata#16) depuis le coffre — entité `user` = sub courant.

    Contrairement à un remote (credential d'ORG = token M2M du bridge), un mount
    fédère un MCP distant déjà authentifié par user (ex. memento, OAuth Supabase) :
    chaque user porte SON token, résolu par requête et injecté en bearer dans le
    proxy (cf. tools/mount.py). Lève une McpError actionnable si le user n'a pas
    connecté ce service — le proxy traduit ça en « tools non visibles » (le
    ProxyProvider warn+skip), pas en crash de session.
    """
    sub = current_user_sub_or_raise()
    # OAuth fédéré : token avec refresh transparent (mémento = pilote otomata#16).
    # Le résolveur connector-spécifique vit hors d'access (refresh = flow OAuth).
    if provider == "memento":
        from . import memento_oauth
        token = memento_oauth.access_token_for(sub)
    elif provider == "atlassian":
        from . import atlassian_oauth
        token = atlassian_oauth.access_token_for(sub)
    else:
        token = credentials_store.get_credential("user", sub, provider)
    if token:
        return token
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Connecteur `{provider}` non connecté pour ton compte. "
            f"Connecte-le depuis ton dashboard (oto.ninja)."
        ),
    ))


def resolve_crunchbase_session() -> dict:
    """Résout la session Crunchbase per-user du sub courant pour **injection**
    dans le client (cookies + user_agent), ou lève une McpError actionnable.

    Fait partie du seam de résolution `access` (avec `resolve_api_key` /
    `resolve_remote_credential`) — source unique vers laquelle convergent les
    adaptateurs/runtime, pour qu'elle puisse devenir un broker en service
    (ADR 0004) sans réécriture. Les tools ne tapent plus `db` directement.
    """
    sub = current_user_sub_or_raise()
    sess = db.get_crunchbase_session(sub)
    if not sess:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                "Aucune session Crunchbase configurée pour cet utilisateur. "
                "Va sur https://app.oto.ninja/account (section Crunchbase) "
                "pour coller tes cookies de session (export JSON depuis "
                "DevTools ou une extension Cookie Editor)."
            ),
        ))
    return sess


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
    # Org effective résolue une fois (perf : sinon 1 lookup/provider). None pour
    # tout user sans org → la branche org_secret ci-dessous est inerte. Via le seam
    # `current_org` → reflète l'override de session (MCP) ou la consultation (REST
    # view-as) le cas échéant, sinon la maison (ADR 0023).
    active_org = current_org(sub)
    active_group = group_store.get_active_group(sub)
    out: dict = {"role": role, "active_org": active_org,
                 "active_group": active_group, "providers": {}}
    for provider in db.KEY_PROVIDERS:
        shareable = provider in ORG_SHAREABLE_PROVIDERS
        # PRÉSENCE seulement (pas de déchiffrement sur le chemin /api/me).
        user_has = db.has_user_api_key(sub, provider)
        group_has = (
            group_store.has_group_secret(active_group, provider)
            if active_group is not None and shareable else False
        )
        org_has = (
            org_store.has_org_secret(active_org, provider)
            if active_org is not None and shareable else False
        )
        grant = db.get_active_grant(sub, provider)
        used = db.get_usage_today(sub, provider)
        limit = (grant.get("daily_quota") if grant else None) or quota_for(provider)

        # Miroir EXACT de la cascade de resolve_api_key : user_key > group_secret
        # > org_secret > grant plateforme. Toute divergence = /api/me ment sur le
        # mode réel.
        if user_has:
            mode = "user"
        elif group_has:
            mode = "group"
        elif org_has:
            mode = "org"
        elif not grant:
            mode = "forbidden"
        elif limit and used >= limit:
            mode = "over_quota"
        else:
            mode = "platform"

        out["providers"][provider] = {
            "mode": mode,
            "user_key_configured": user_has,
            "group_secret_configured": group_has,
            "org_secret_configured": org_has,
            "platform_key_label": grant["label"] if grant else None,
            "quota_used_today": used,
            "quota_daily": limit if grant else None,
        }

    # Credentials byo_user à champs déclarés, hors KEY_PROVIDERS (modèle générique
    # multi-champs, ADR 0011) : mounts basic_auth (planity) ET clients in-process
    # multi-secrets (silae). Pas de quota ni de grant — le credential EST le grant
    # (cf. resolve_mount_token / resolve_credential_fields). `user` si posé, sinon
    # `forbidden`. Permet au dashboard d'afficher « configuré / remove » comme une clé.
    for c in connectors.REGISTRY.values():
        if (c.name in out["providers"] or not c.secret_fields
                or "byo_user" not in c.auth_modes):
            continue
        has = db.has_user_api_key(sub, c.name)
        out["providers"][c.name] = {
            "mode": "user" if has else "forbidden",
            "user_key_configured": has,
            "org_secret_configured": False,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }
    return out
