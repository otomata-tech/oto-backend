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

import logging
import os
from dataclasses import dataclass
from typing import Optional

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from . import connectors, credentials_store, db, group_store, org_store, session_org
from .auth_hooks import current_user_sub_from_token

logger = logging.getLogger(__name__)

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
    visibilité, entitlements, redaction). Aujourd'hui (barreau R0) =
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
        # Endpoint MCP ANONYME (`<slug>.mcp.oto.cx`, ADR 0032) : pas de sub, mais l'org
        # PROPRIÉTAIRE du projet est le contexte de résolution (credentials/redaction).
        from . import subdomain_project
        return subdomain_project.current_anon_org()
    # Endpoint scopé par sous-domaine (« 1 oto par org ») : épingle l'org de la
    # connexion AVANT tout. Garde d'appartenance ici (sub connu) → un non-membre
    # est ignoré (repli maison, zéro fuite). Précédence ⇒ hard-lock : `oto_use_org`
    # (override de session) ne peut pas sortir de l'org du sous-domaine.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from . import roles
        if roles.is_org_member(sub, cand):
            return cand
    present, org = session_org.current_override()
    if present:
        return org
    view = session_org.current_view_org()
    if view is not None:
        return None if view == 0 else view
    return org_store.get_active_org(sub)


# Sentinelle « param non fourni » — distingue « org=None » (perso, valeur légitime)
# de « pas d'org explicite → résous via current_org ». Sert à calculer l'état d'un
# TIERS (fiche admin) contre SON org persistée, sans laisser fuiter le contexte
# view-as/session du REQUÉRANT (bug 2026-06-24 : has_option(cible) lisait l'org du
# requérant). Le chemin self (/api/me) ne passe rien → comportement inchangé.
_UNSET: object = object()


def has_option(sub: str, option: str, *, org: "int | None | object" = _UNSET) -> bool:
    """Couche 3 du modèle de connecteur (cf. docs/connector-model.md) : l'option de
    connecteur `option` (ex. `unipile`) est-elle débloquée pour `sub` ? **Seam unique** —
    débloquée par un **comp admin** sur l'USER OU sur l'ORG active (plus de paiement :
    le modèle billing/Stripe a été retiré, la gouvernance de l'option est purement admin).
    Ne JAMAIS lire les sources en direct ailleurs (un nouveau chemin passe par ici).
    `org` explicite (≠ _UNSET) = calcul pour un tiers contre une org donnée (fiche admin),
    sans current_org (anti-fuite de contexte)."""
    if db.has_option_comp("user", sub, option):
        return True
    org = current_org(sub) if org is _UNSET else org
    if org is not None and db.has_option_comp("org", str(org), option):
        return True
    return False


def current_group(sub: str | None) -> Optional[int]:
    """Équipe (groupe) EFFECTIVE — mirror de `current_org` pour l'axe groupe
    (ADR 0023 étendu). Résout `session ?? consultation ?? maison` en TENANT
    l'invariant « groupe ⊂ org » : un override/consultation d'ORG **sans** groupe
    explicite ⇒ niveau org (None), jamais le home_group d'une autre org."""
    if sub is None:
        return None
    # Sous lock d'org par sous-domaine : le groupe n'est rendu QUE s'il ⊂ l'org
    # épinglée (sinon None = niveau org). Ignore tout override de session vers un
    # groupe d'une autre org → hard-lock cohérent avec current_org.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from . import roles
        if not roles.is_org_member(sub, cand):
            return None
        ag = group_store.get_active_group(sub)
        if ag is not None and (group_store.get_group(ag) or {}).get("org_id") == cand:
            return ag
        return None
    has_g, g = session_org.current_group_override()
    if has_g:
        return g
    if session_org.current_override()[0]:
        return None  # org de session sans groupe → niveau org
    vg = session_org.current_view_group()
    if vg is not None:
        return None if vg == 0 else vg
    if session_org.current_view_org() is not None:
        return None  # consultation d'org sans groupe → niveau org
    return group_store.get_active_group(sub)  # maison


def current_project() -> Optional[int]:
    """Projet ACTIF de la conversation (ADR 0032 §4, B2.2) — **bracelet de session**
    posé par `oto_use_project`, MCP-only, éphémère. Pas de projet « maison » : pas
    d'override ⇒ None. Sert à appliquer la surcharge connecteur PRÉFAITE du projet
    (le bracelet sélectionne un projet préfait ; il ne déclare jamais de config)."""
    return session_org.current_project_override()


def project_pinned_identity(connector: str, project_id: Optional[int] = None) -> Optional[str]:
    """Identité (account) ÉPINGLÉE par le projet actif pour `connector`, ou None ⇒ la
    résolution retombe sur le défaut user. Lit la clé de BINDING `project_links.identity_ref`
    (ADR 0032 §4 amendé, #57). Multiplicité : **un seul** binding avec identité ⇒ on l'épingle ;
    **plusieurs** ⇒ None (ambigu → l'agent doit préciser `account=` à l'appel). `project_id`
    omis ⇒ projet de session (`current_project`). **Fail-soft** : toute erreur ⇒ None
    (jamais de plantage de la résolution d'un tool sur ce chemin)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        pinned = [link.get("identity_ref")
                  for link in db.list_project_links(int(pid))
                  if link.get("target_type") == "connecteur"
                  and link.get("target_ref") == connector and link.get("identity_ref")]
        return pinned[0] if len(pinned) == 1 else None
    except Exception as e:
        logger.warning("project_pinned_identity fail-soft %s/%s: %s", pid, connector, e)
    return None


def require_connector_access(provider: str, sub: Optional[str] = None) -> None:
    """Backstop call-time du RBAC connecteur interne à l'org (ADR 0025) : si
    `provider` est RESTREINT dans l'org active du `sub` et que `sub` n'y est pas
    autorisé (département/user), lève. **DUR** — appelé dans `resolve_credential`
    (couvre keyed + fields + BYO : pas de clé perso qui contourne). super_admin
    bypasse ; pas d'org active → restriction non applicable ; stdio local (sub=None)
    = accès complet."""
    sub = sub or current_user_sub_from_token()
    if sub is None:
        return
    try:
        if is_super_admin(sub):
            return
        org = current_org(sub)
        if org is None or provider not in db.org_restricted_connectors(org):
            return  # pas d'org, ou connecteur ouvert dans l'org
        allowed = provider in db.member_allowed_connectors(sub, org)
    except Exception as e:
        # FAIL-OPEN sur erreur infra : ce gate tourne sur CHAQUE résolution de
        # credential → ne doit pas casser tous les connecteurs sur un hoquet DB.
        # Pas de bypass exploitable : la résolution qui suit retape la DB et échoue
        # pareil ; et la visibilité masque déjà le connecteur restreint. On LOGUE
        # (un silence avait masqué un bug `r[0]` 2026-06-25) → un fail-open persistant
        # = une régression visible, pas un trou muet.
        logger.warning("require_connector_access fail-open %s/%s: %s", sub, provider, e)
        return
    if not allowed:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Le connecteur `{provider}` est réservé à certaines équipes/personnes "
                f"de ton organisation. Demande l'accès à un admin de ton org."
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


@dataclass(frozen=True)
class ResolvedCredential:
    """Credential GAGNANT de la cascade (ADR 0024) — la clé, son origine, ET sa
    config non-secrète (endpoint/host) en un seul objet. Source unique : toute
    résolution (clé seule, multi-champs, ou endpoint) en dérive.

    - `secret` : la valeur stockée brute (la clé pour un keyed ; le pack JSON pour
      un multi-champs). `key` = alias (un keyed s'instancie avec).
    - `is_platform` / `mode` : origine (user|group|org|platform) — miroir de `status_for`.
    - `fields` (lazy) : champs unpackés (un client multi-secrets s'instancie avec).
    - `config` (lazy) : champs NON-secrets déclarés (data_center, base_url…) ∪ `meta`
      public du credential (ex. `dsn` unipile). La config voyage avec la clé.
    - `entity_type`/`entity_id` : niveau gagnant (None pour un grant plateforme — sa
      config est l'environnement, pas un credential du coffre)."""
    provider: str
    secret: str
    is_platform: bool
    mode: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    account: str = ""

    @property
    def key(self) -> str:
        return self.secret

    @property
    def fields(self) -> dict:
        return credentials_store.unpack_secret(self.provider, self.secret)

    @property
    def config(self) -> dict:
        """Config non-secrète appariée à la clé gagnante. Lazy : aucun coût pour
        les appelants qui ne lisent que `key` (chemin chaud resolve_api_key)."""
        _, cfg = credentials_store.split_secret_config(self.provider, self.fields)
        if self.entity_type is not None:
            try:
                row = credentials_store.get_credential_with_meta(
                    self.entity_type, self.entity_id, self.provider, self.account)
            except Exception:
                row = None
            if row:
                cfg = {**cfg, **credentials_store.public_meta(row.get("meta"))}
        return cfg


def resolve_credential(provider: str, want: str = "auto",
                       sub: Optional[str] = None, *,
                       emit_on_failure: bool = True) -> ResolvedCredential:
    """Vue publique de la résolution. Sur **échec** (McpError actionnable — credential
    absent / quota dépassé / accès RBAC refusé), émet un événement de monitoring
    `kind='connector'` dans le flux unifié (ADR 0017) AVANT de relever : c'est LE
    signal d'un connecteur qui ne résout pas pour un user/org, invisible jusqu'ici
    (un compte actif sans clé valide n'apparaissait nulle part). `emit_on_failure=False`
    pour les **sondes** qui avalent la McpError (ex. lookup de DSN), afin de ne pas
    fausser le signal. Cascade et sémantique : voir `_resolve_credential_impl`."""
    if sub is None:
        # Endpoint MCP ANONYME (ADR 0032) : pas de sub → résolution contre l'org
        # propriétaire du projet (org secret > grant org > clé plateforme ouverte),
        # sans quota per-sub (le rate-limit du sous-domaine borne l'abus).
        from . import subdomain_project
        anon = subdomain_project.current_anon_context()
        if anon is not None:
            return _resolve_credential_anon(provider, want, anon.org_id)
    sub = sub or current_user_sub_or_raise()
    try:
        return _resolve_credential_impl(provider, want, sub)
    except McpError:
        if emit_on_failure:
            _emit_connector_failure(provider, sub)
        raise


def _emit_connector_failure(provider: str, sub: str) -> None:
    """Best-effort : une ligne `tool_calls(kind='connector', ok=False)` = « la
    résolution de credential a échoué pour ce provider/sub ». Jamais bloquant, jamais
    d'exception qui masquerait la McpError d'origine (le monitoring ne casse pas le service)."""
    try:
        org = current_org(sub)
    except Exception:
        org = None
    try:
        db.insert_tool_call({
            "kind": "connector", "tool": provider, "sub": sub, "org_id": org,
            "ok": False, "error": "credential_resolution_failed",
        })
    except Exception:  # noqa: BLE001
        logger.debug("connector failure emit failed", exc_info=True)


def _resolve_credential_impl(provider: str, want: str, sub: str) -> ResolvedCredential:
    """Résolveur substrat unique (ADR 0024) : marche la cascade EXACTE
    user > groupe actif > org active [> grant plateforme] **une fois** et renvoie
    le credential gagnant (clé + origine + config). `want="byo"` court-circuite le
    palier plateforme (sémantique byo-only de `resolve_credential_fields`) ;
    `want="auto"` inclut le grant plateforme + quota (sémantique `resolve_api_key`).
    `sub` explicite = utilisable HORS contexte MCP (routes REST) ; None = sub courant.
    Lève une McpError actionnable si rien ne résout."""
    sub = sub or current_user_sub_or_raise()
    # RBAC connecteur interne à l'org (ADR 0025) — backstop DUR : un connecteur
    # restreint dans l'org du sub n'est résolu que pour les principals autorisés
    # (département/user). Avant toute résolution → couvre keyed/fields/BYO.
    require_connector_access(provider, sub)

    # Scope MEMBRE (ADR 0033) : « ma clé » n'existe QUE dans l'org de contexte —
    # posée dans l'org A, elle ne résout pas depuis l'org B. L'org est résolue via
    # le seam `current_org` (session MCP ?? consultation ?? maison, ADR 0023) AVANT
    # le premier palier : plus aucun credential per-user org-agnostique.
    active_org = current_org(sub)
    member_key = db.get_member_api_key(sub, active_org, provider)
    if member_key:
        return ResolvedCredential(provider, member_key, False, "user",
                                  credentials_store.MEMBER,
                                  credentials_store.member_id(active_org, sub))

    # Paliers partagés (ADR 0012) : secret du GROUPE actif (le plus spécifique),
    # puis de l'ORG active. Sautés tant que l'user n'a ni groupe ni org actifs
    # (-> None) → strictement identique à avant pour tout user flat.
    # Cascade : clé membre > group_secret > org_secret > platform_grant.
    if provider in ORG_SHAREABLE_PROVIDERS:
        active_group = current_group(sub)
        if active_group is not None:
            grp_key = group_store.get_group_secret(active_group, provider)
            if grp_key:
                return ResolvedCredential(provider, grp_key, False, "group",
                                          "group", str(active_group))
        if active_org is not None:
            org_key = org_store.get_org_secret(active_org, provider)
            if org_key:
                return ResolvedCredential(provider, org_key, False, "org",
                                          "org", str(active_org))

    # byo-only : pas de palier plateforme (mounts basic_auth, clients multi-secrets).
    if want == "byo":
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Aucun credential `{provider}` configuré pour toi. Renseigne-le "
                f"sur {_ACCOUNT_URL} (section {provider.capitalize()})."
            ),
        ))

    # Défense en profondeur : le chemin platform-grant n'est valide que si le
    # registre AUTORISE `platform` pour ce provider. Un provider byo-only
    # (attio, lemlist, pennylane, fullenrich, slack…) ne doit JAMAIS être résolu
    # via une clé plateforme — même si une clé résiduelle existait en base
    # (seed SOPS historique). Sans ce gate, un grant suffisait à utiliser un
    # compte privé, l'inverse du modèle (audité 2026-06-11).
    con = connectors.connector_for_provider(provider)
    platform_eligible = con is not None and "platform" in con.auth_modes
    grant = db.get_active_grant(sub, provider) if platform_eligible else None
    # Fallback : clé plateforme partagée à l'ORG active (couche 2, grant org-level).
    # Après le grant per-user (plus spécifique). Quota métré per-membre comme le user-grant.
    if not grant and platform_eligible and active_org is not None:
        grant = db.get_active_org_grant(active_org, provider)
    # Free-tier (ADR 0031) : clé plateforme OUVERTE sans grant pour les connecteurs
    # `platform_key_open`, avec quota gratuit par user (`default_quota`). N'est atteint
    # qu'en l'absence de toute clé BYO (cascade user>groupe>org>grant épuisée) — en BYO
    # on n'utilise JAMAIS la clé plateforme. Le quota ci-dessous est métré per-user.
    if not grant and platform_eligible and con.platform_key_open:
        pk = db.get_platform_api_key(provider)
        if pk:
            grant = {"api_key": pk["api_key"], "label": pk["label"],
                     "daily_quota": con.default_quota}
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

    return ResolvedCredential(provider, grant["api_key"], True, "platform")


def _resolve_credential_anon(provider: str, want: str, org_id: Optional[int]) -> ResolvedCredential:
    """Résolution pour un endpoint MCP ANONYME (ADR 0032) : aucun `sub`, aucune session
    per-user → cascade réduite `org_secret > grant plateforme d'org > clé plateforme
    ouverte`, scopée sur l'org PROPRIÉTAIRE du projet. Pas de user_key/group (inexistants
    sans identité), pas de quota per-sub (le rate-limit du sous-domaine borne l'abus).
    Miroir org-only des paliers de `_resolve_credential_impl` — ce qui n'est pas résoluble
    au niveau org (oauth/cookie per-user) lève une McpError actionnable, fail-closed."""
    con = connectors.connector_for_provider(provider)
    if con is None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Provider inconnu: {provider}"))
    if org_id is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme n'a pas d'org propriétaire pour résoudre "
                     f"`{provider}` (projet sans org).")))
    if provider in ORG_SHAREABLE_PROVIDERS:
        org_key = org_store.get_org_secret(org_id, provider)
        if org_key:
            return ResolvedCredential(provider, org_key, False, "org", "org", str(org_id))
    if want == "byo":
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Aucun credential `{provider}` configuré pour l'org de ce projet."))
    platform_eligible = "platform" in con.auth_modes
    grant = db.get_active_org_grant(org_id, provider) if platform_eligible else None
    if not grant and platform_eligible and con.platform_key_open:
        pk = db.get_platform_api_key(provider)
        if pk:
            grant = {"api_key": pk["api_key"], "label": pk["label"]}
    if not grant:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme ne peut pas résoudre `{provider}` : configure "
                     f"une clé d'org, ou grant une clé plateforme à l'org du projet.")))
    return ResolvedCredential(provider, grant["api_key"], True, "platform")


def resolve_api_key(provider: str) -> tuple[str, bool]:
    """Renvoie `(api_key, is_platform)` ou lève McpError actionnable. Vue mince
    sur `resolve_credential` (contrat inchangé pour les ~15 tools keyed)."""
    rc = resolve_credential(provider, want="auto")
    return rc.key, rc.is_platform


def resolve_credential_fields(provider: str) -> dict:
    """Résout un credential **multi-champs** byo_user (modèle générique, ADR 0011)
    du sub courant → dict des champs déclarés (`Connector.secret_fields`).

    Pour les connecteurs in-process dont le client s'instancie avec plusieurs
    secrets (ex. Silae : client_id / client_secret / subscription_key, OAuth2
    client-credentials). **byo-only** : pas de clé plateforme ni de quota — le
    credential EST le grant, comme un mount. Vue mince sur `resolve_credential`
    (cascade user > groupe > org, sans palier plateforme)."""
    return resolve_credential(provider, want="byo").fields


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
    active_org = current_org(sub)
    key = db.get_member_api_key(sub, active_org, "unipile")
    if key:
        return key
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


def credential_mode_for(sub: str, provider: str, *,
                        org: "int | None | object" = _UNSET,
                        group: "int | None | object" = _UNSET) -> str:
    """Origine de la clé `provider` pour `sub` (EXPLICITE, hors contexte MCP) :
    `user|group|org|platform|over_quota|forbidden`. PRÉSENCE seulement (pas de
    déchiffrement → sûr/léger pour un statut). **Miroir** de la cascade
    `resolve_credential` (incl. fallback grant org) — une divergence ferait mentir
    l'UI. « BYO » (clé propre, pas la plateforme) = mode ∈ {user, group, org}.
    `org`/`group` explicites (≠ _UNSET) = calcul pour un TIERS contre son propre
    contexte (fiche admin), sans current_org/current_group (anti-fuite du requérant)."""
    o = current_org(sub) if org is _UNSET else org
    g = current_group(sub) if group is _UNSET else group
    # Scope membre (ADR 0033) : la clé propre se cherche dans l'org de contexte —
    # pour un tiers (org explicite), dans SON org, jamais celle du requérant.
    if db.has_member_api_key(sub, o, provider):
        return "user"
    if provider in ORG_SHAREABLE_PROVIDERS:
        if g is not None and group_store.has_group_secret(g, provider):
            return "group"
        if o is not None and org_store.has_org_secret(o, provider):
            return "org"
    con = connectors.connector_for_provider(provider)
    if not (con and "platform" in con.auth_modes):
        return "forbidden"
    grant = db.get_active_grant(sub, provider)
    if not grant:
        grant = db.get_active_org_grant(o, provider) if o is not None else None
    if not grant:
        return "forbidden"
    used = db.get_usage_today(sub, provider)
    limit = grant.get("daily_quota") or quota_for(provider)
    return "over_quota" if (limit and used >= limit) else "platform"


def connector_resolvable_for_org(provider: str, org_id: int) -> bool:
    """Un connecteur peut-il être résolu pour une ORG **sans user identifié** ?
    Vrai si : credential-less (`secret_kind='none'`), OU secret d'org configuré, OU
    clé plateforme accordée à l'org. Sonde pour publier un endpoint MCP **anonyme**
    (ADR 0032) servi par la clé de l'org propriétaire du projet : un endpoint sans
    login n'a pas de `user_key`/session per-user → oauth/cookie sont exclus de fait
    (pas de secret d'org pour eux). Miroir org-only de la cascade `resolve_credential`."""
    con = connectors.connector_for_provider(provider)
    if con is None:
        return False
    if con.secret_kind == "none":
        return True
    if provider in ORG_SHAREABLE_PROVIDERS and org_store.has_org_secret(org_id, provider):
        return True
    if "platform" in con.auth_modes and db.get_active_org_grant(org_id, provider):
        return True
    return False


BYO_MODES = ("user", "group", "org")


def resolve_remote_credential(provider: str) -> tuple[str, str]:
    """Résout `(base_url, token_m2m)` du **bridge** d'un connecteur remote
    (ADR 0003) depuis le credential de l'org active du sub courant.

    Le credential d'org d'un remote n'est PAS le secret du système client (il
    vit dans le bridge, ex. un bridge back-office client) : c'est le moyen
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
    elif provider == "folkmcp":
        from . import folk_oauth
        token = folk_oauth.access_token_for(sub)
    else:
        # Mount non-oauth (basic_auth, ex. planity) : credential posé via la carte
        # api-keys → scope membre (ADR 0033), comme sa pose.
        org = current_org(sub)
        token = (credentials_store.get_credential(
                     credentials_store.MEMBER,
                     credentials_store.member_id(org, sub), provider)
                 if org is not None else None)
    if token:
        return token
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message=(
            f"Connecteur `{provider}` non connecté pour ton compte. "
            f"Connecte-le depuis ton dashboard (oto.ninja)."
        ),
    ))


def record_platform_usage(provider: str) -> None:
    """À appeler APRÈS un appel réussi avec la platform key. No-op si pas authentifié."""
    sub = current_user_sub_from_token()
    if not sub:
        return
    db.increment_usage(sub, provider)


def status_for(sub: str, *, org: "int | None | object" = _UNSET,
               group: "int | None | object" = _UNSET) -> dict:
    """Snapshot pour `/api/me` — rôle + statut par provider :

    - `mode` : `user` (clé perso) | `platform` (grant + quota OK)
              | `over_quota` (grant mais quota épuisé)
              | `forbidden` (ni user key ni grant)

    `org`/`group` explicites (≠ _UNSET) = snapshot d'un TIERS contre SON propre
    contexte (fiche admin), sans current_org/current_group du requérant (anti-fuite).
    """
    role = get_user_role(sub)
    # Org effective résolue une fois (perf : sinon 1 lookup/provider). None pour
    # tout user sans org → la branche org_secret ci-dessous est inerte. Via le seam
    # `current_org` → reflète l'override de session (MCP) ou la consultation (REST
    # view-as) le cas échéant, sinon la maison (ADR 0023).
    active_org = current_org(sub) if org is _UNSET else org
    active_group = current_group(sub) if group is _UNSET else group
    out: dict = {"role": role, "active_org": active_org,
                 "active_group": active_group, "providers": {}}
    for provider in db.KEY_PROVIDERS:
        shareable = provider in ORG_SHAREABLE_PROVIDERS
        # PRÉSENCE seulement (pas de déchiffrement sur le chemin /api/me).
        # Scope membre (ADR 0033) : la clé propre est celle posée dans CETTE org.
        user_has = db.has_member_api_key(sub, active_org, provider)
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
            # limit 0 = illimité (convention default_quota) → None pour que l'UI
            # affiche « ∞ », pas « /0 » (qui se lit comme un quota épuisé).
            "quota_daily": (limit or None) if grant else None,
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
        has = db.has_member_api_key(sub, active_org, c.name)
        out["providers"][c.name] = {
            "mode": "user" if has else "forbidden",
            "user_key_configured": has,
            "org_secret_configured": False,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }

    # Connecteurs à SESSION navigateur (`personal_session`, secret_kind="cookie" :
    # brevo/crunchbase) : pas de champ à saisir → connexion par Live View Browserbase
    # (MCP `<ns>_connect_start`), le credential = le Context persisté au coffre. On
    # expose juste « configuré + depuis quand » pour que la carte rende son widget
    # session (ADR 0026 prévoyait `providers` sans jamais l'alimenter → /api/me ne
    # disait plus rien sur ces sessions ; corrigé 2026-06-30).
    for c in connectors.REGISTRY.values():
        if c.name in out["providers"] or c.secret_kind != "cookie":
            continue
        st = (credentials_store.credential_status(
                  credentials_store.MEMBER,
                  credentials_store.member_id(active_org, sub), c.name)
              if active_org is not None else None)
        out["providers"][c.name] = {
            "mode": "user" if st else "forbidden",
            "user_key_configured": st is not None,
            "session_set_at": st["set_at"] if st else None,
            "org_secret_configured": False,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }
    return out
