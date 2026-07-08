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

_ACCOUNT_URL = "https://manage.oto.cx/account"


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

    Résout `jeton d'appel ?? consultation ?? maison` (ADR 0038, amende 0023) :
    - **jeton d'appel** (MCP) — `org=`/`project=`/`group=` posés déjà gardés par
      les axes/adaptateurs (contextvar per-requête) ; AUCUN état de session ;
    - **org de consultation** (REST) — view-as du dashboard, contextvar per-requête
      posé APRÈS validation d'appartenance par l'adaptateur REST ;
    - sinon → repli sur la **maison** persistante (`org_store.get_active_org`).

    Jeton et consultation ne coexistent jamais (jeton = MCP only, consultation =
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
    # Jeton explicite de l'appel (`org=`, modèle sans état de session) : posé par
    # l'adaptateur capacité APRÈS validation d'appartenance → rendu tel quel. Prime
    # sur l'override de session (qui, lui, ne survit pas au stateless claude.ai).
    call = session_org.current_call_org()
    if call is not None:
        return call
    # Le BRACELET de session (`oto_use_org`, dict keyé Mcp-Session-Id) n'est PLUS lu
    # (ADR 0038 B3) : claude.ai renouvelle le session_id à chaque appel (jamais relu)
    # et un session_id recyclé cross-compte faisait fuiter le scope (#108). Le scope
    # est porté par l'appel (`org=`/`project=`/`group=`, ci-dessus) ou retombe maison.
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


# Add-on payant requis par un connecteur (couche 3, ADR 0043). None = aucun. HOME
# canonique de ce mapping (les surfaces org ET user en dérivent — derive don't duplicate).
_PAID_OPTION_BY_CONNECTOR = {"unipile": "unipile"}


def paid_option_for(connector: str) -> Optional[str]:
    """Option payante requise par un connecteur (ou None)."""
    return _PAID_OPTION_BY_CONNECTOR.get(connector)


def has_option(sub: str, option: str, *, org: "int | None | object" = _UNSET) -> bool:
    """Couche 3 du modèle de connecteur (cf. docs/connector-model.md) : l'option de
    connecteur `option` (ex. `unipile`) est-elle débloquée pour `sub` ? **Seam unique** —
    deux sources (ADR 0043) : un **comp admin** sur l'USER ou l'ORG active, OU
    l'**abonnement actif de l'org** dont le plan inclut l'option (mapping
    `billing.plan_options`, miroir `org_subscriptions` — `past_due` reste ouvert
    tant que la grace court ; la fermeture est un acte du billing_runner).
    Ne JAMAIS lire les sources en direct ailleurs (un nouveau chemin passe par ici).
    `org` explicite (≠ _UNSET) = calcul pour un tiers contre une org donnée (fiche admin),
    sans current_org (anti-fuite de contexte)."""
    if db.has_option_comp("user", sub, option):
        return True
    org = current_org(sub) if org is _UNSET else org
    if org is None:
        return False
    if db.has_option_comp("org", str(org), option):
        return True
    plan = db.subscription_plan_for_org(int(org))
    if plan is not None:
        from . import billing  # import tardif (billing tire stancer/httpx)

        return option in billing.plan_options(plan)
    return False


def option_open(sub: str, connector: str, *, org: "int | None | object" = _UNSET,
                group: "int | None | object" = _UNSET) -> bool:
    """SOURCE UNIQUE de « l'option (couche 3) du connecteur est-elle levée pour `sub` ? ».
    Le statut carte (`connectors_selection.option_ok`) ET le gate « connecter » d'unipile
    (`status_for.subscribed`) l'appellent → ils ne peuvent plus DIVERGER (le BYO ouvrait
    l'option ici mais pas là → carte « clé d'org » + « Bloqué » incohérente, corrigé
    2026-07-07). Règle : pas d'option requise ⟹ ouvert ; sinon **BYO** (clé propre
    user/groupe/org — l'user gère sa propre instance) OU **has_option** (comp admin /
    abonnement). `org`/`group` explicites = calcul pour un tiers (fiche admin)."""
    opt = paid_option_for(connector)
    if opt is None:
        return True
    if credential_mode_for(sub, connector, org=org, group=group) in BYO_MODES:
        return True
    return has_option(sub, opt, org=org)


def current_group(sub: str | None) -> Optional[int]:
    """Équipe (groupe) EFFECTIVE — mirror de `current_org` pour l'axe groupe
    (ADR 0038). Résout `jeton d'appel ?? consultation ?? maison` en TENANT
    l'invariant « groupe ⊂ org » : un jeton/consultation d'ORG **sans** groupe
    explicite ⇒ niveau org (None), jamais le home_group d'une autre org."""
    if sub is None:
        return None
    # Sous lock d'org par sous-domaine : le groupe n'est rendu QUE s'il ⊂ l'org
    # épinglée (sinon None = niveau org) — hard-lock cohérent avec current_org.
    cand = session_org.current_subdomain_candidate()
    if cand is not None:
        from . import roles
        if not roles.is_org_member(sub, cand):
            return None
        ag = group_store.get_active_group(sub)
        if ag is not None and (group_store.get_group(ag) or {}).get("org_id") == cand:
            return ag
        return None
    # Jeton d'appel `group=` : déjà gardé à la pose (can_read_group + org co-posée
    # par l'axe, invariant par construction) → rendu tel quel. Le BRACELET de session
    # (`oto_use_group`) n'est plus lu (ADR 0038 B3, même raison que current_org).
    call_g = session_org.current_call_group()
    if call_g is not None:
        return call_g
    vg = session_org.current_view_group()
    if vg is not None:
        return None if vg == 0 else vg
    if session_org.current_view_org() is not None:
        return None  # consultation d'org sans groupe → niveau org
    ag = group_store.get_active_group(sub)  # maison
    if ag is None:
        return None
    # Jeton d'org (`org=`/`project=`) SANS groupe : le home_group n'est rendu que
    # s'il appartient à l'org épinglée (invariant groupe ⊂ org — jamais le
    # home_group d'une AUTRE org sous une org de jeton).
    call_org = session_org.current_call_org()
    if call_org is not None:
        g = group_store.get_group(ag)
        if not g or g.get("org_id") != call_org:
            return None
    return ag


def current_project() -> Optional[int]:
    """Projet de l'APPEL courant (ADR 0038) = jeton `project=` — posé déjà gardé
    (`can_access` + org dérivée co-posée) par l'axe d'appel. Le BRACELET de session
    (`oto_use_project`) n'est plus lu (B3b — même raison que org/groupe : claude.ai
    renouvelle le session_id à chaque appel, et un session_id recyclé cross-compte
    faisait hériter le contexte, #108). Pas de projet « maison » : pas de jeton ⇒
    None (hors projet). Sert la surcharge connecteur PRÉFAITE du projet, les slots
    (ADR 0035) et le gel `runs.project_id`."""
    return session_org.current_call_project()


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


# Préfixe d'adressage par slot (ADR 0035 B3) : `slot:<name>` dans un argument
# `namespace` des tools data_* = « le tableau bindé sous ce nom par le projet actif ».
SLOT_PREFIX = "slot:"


def resolve_slot_tableau(name: str) -> str:
    """Résout un slot `tableau` contre les bindings du projet ACTIF (ADR 0035 B3) →
    le NOM réel du namespace. **Enforcement serveur, jamais de fallback** : pas de
    projet actif, slot non bindé, ou binding pendouillant (namespace disparu) ⇒
    `McpError` ACTIONNABLE — on n'interprète jamais `slot:x` comme un nom littéral
    et on ne « prend jamais le premier tableau venu »."""
    from . import slots as slots_mod
    try:
        name = slots_mod.normalize_name(name)
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"slot invalide : {e}"))
    pid = current_project()
    if pid is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"`slot:{name}` exige un PROJET (le binding nom→instance vit "
                     "dans le projet, ADR 0035). Passe `project=<id>` sur CET appel "
                     "(liste : `oto_project op=list`) — ou crée un projet et binde le slot "
                     f"(`oto_project op=link target_type=tableau … slot='{name}'`), ou "
                     "passe un `namespace` explicite.")))
    links = db.list_project_links(int(pid))
    match = [l for l in links
             if l.get("target_type") == "tableau" and l.get("slot") == name]
    if not match:
        bound = sorted(l["slot"] for l in links
                       if l.get("target_type") == "tableau" and l.get("slot"))
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"le projet actif (#{pid}) ne binde aucun slot tableau `{name}`. "
                     + (f"Slots bindés : {', '.join(bound)}. " if bound else
                        "Aucun slot tableau bindé dans ce projet. ")
                     + f"Binde-le : `oto_project op=link project_id={pid} "
                       f"target_type=tableau target_ref=<id> slot='{name}'`.")))
    ns = match[0].get("namespace")
    if not ns:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"le slot `{name}` du projet #{pid} pointe un tableau qui ne résout "
                     f"plus (ref `{match[0].get('target_ref')}`) — re-binde-le sur un "
                     "namespace existant (`oto_project op=link`).")))
    return ns


def rbac_denied_connectors(sub: str, org: Optional[int]) -> set:
    """Connecteurs REFUSÉS à `sub` dans `org` par le RBAC interne (ADR 0025) — seam
    UNIQUE des 4 surfaces (call-time `require_connector_access`, visibilité session,
    listing d'instances, marketplace). Escalade descendante alignée sur `roles.py` :
    super_admin ET **org_admin de l'org** transcendent la restriction — l'admin
    gouverne l'ACL (`org_connector_access`), lui en interdire l'USAGE était une
    incohérence (un connecteur réservé à une équipe restait inaccessible — et même
    invisible — à l'admin de l'org). LÈVE sur hoquet DB : chaque surface garde sa
    propre doctrine fail-open (le call-time logue, les listings best-effort)."""
    if org is None:
        return set()
    if is_super_admin(sub):
        return set()
    from . import roles
    if roles.is_org_admin(sub, org):
        return set()
    restricted = db.org_restricted_connectors(org)
    if not restricted:
        return set()
    return set(restricted) - set(db.member_allowed_connectors(sub, org))


def require_connector_access(provider: str, sub: Optional[str] = None) -> None:
    """Backstop call-time du RBAC connecteur interne à l'org (ADR 0025) : si
    `provider` est RESTREINT dans l'org active du `sub` et que `sub` n'y est pas
    autorisé (département/user), lève. **DUR** — appelé dans `resolve_credential`
    (couvre keyed + fields + BYO : pas de clé perso qui contourne). super_admin
    et org_admin de l'org bypassent (escalade `rbac_denied_connectors`) ; pas
    d'org active → restriction non applicable ; stdio local (sub=None) = accès
    complet."""
    sub = sub or current_user_sub_from_token()
    if sub is None:
        return
    try:
        allowed = provider not in rbac_denied_connectors(sub, current_org(sub))
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


def _org_unmetered(org: int) -> bool:
    """L'org a-t-elle un plan actif qui lève les quotas plateforme ? (ADR 0043)"""
    plan = db.subscription_plan_for_org(int(org))
    if plan is None:
        return False
    from . import billing  # import tardif (billing tire stancer/httpx)

    return billing.plan_is_unmetered(plan)


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
                       account: Optional[str] = None,
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
        return _resolve_credential_impl(provider, want, sub, account=account)
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


def _is_multi_account(provider: str) -> bool:
    """Le connecteur porte-t-il plusieurs comptes dans le coffre (segment `account`,
    registre `providers.MULTI_ACCOUNT_PROVIDERS`) ? Gate le chemin de sélection de
    compte ; un connecteur mono-compte garde la résolution historique (account='')."""
    con = connectors.connector_for_provider(provider)
    return con is not None and con.auth_multi_account


def _platform_grantee_scope(sub, active_org, scopes) -> "str | None":
    """Le scope de `scopes` qui vise `sub` sur une instance PLATEFORME, ou None (ADR 0044
    §F). `user:<sub>` prime (le plus spécifique) ; `org:<id>` gaté sur l'org **ACTIVE**
    (mirroir EXACT de l'ancien `get_active_org_grant(active_org)` — un grant d'org est métré
    per-contexte-d'org, pas per-appartenance : un membre de l'org X actif dans Y n'en profite
    pas). Sert l'accès (closed) ET le quota (rate_limit_by)."""
    if not scopes:
        return None
    if f"user:{sub}" in scopes:
        return f"user:{sub}"
    if active_org is not None and f"org:{active_org}" in scopes:
        return f"org:{active_org}"
    return None


def _platform_instance_usable(sub, active_org, inst: dict) -> bool:
    """Instance plateforme utilisable par `sub` ? (ADR 0044 §F, mode-aware). Un prêt
    `share_side` autorise (membership, comme un prêt BYO). Sinon selon `share_mode` :
    'open' = `share_down` vide (free-tier, ouvert à tous) OU `sub` grantee ; 'closed' =
    `sub` grantee (défaut fermé)."""
    down, side = inst.get("share_down") or [], inst.get("share_side") or []
    if _sub_matches_scopes(sub, side):
        return True
    granted = _platform_grantee_scope(sub, active_org, down) is not None
    if inst.get("share_mode") == "closed":
        return bool(down) and granted
    return (not down) or granted


def _platform_quota(sub, active_org, meta: dict) -> "int | None":
    """Quota/jour du bénéficiaire sur une instance plateforme : `rate_limit_by[scope de sub]`
    (user prime > org active), sinon le défaut `rate_limit` de l'instance."""
    rlb = (meta or {}).get("rate_limit_by") or {}
    scope = _platform_grantee_scope(sub, active_org, list(rlb.keys()))
    if scope is not None and scope in rlb:
        return rlb[scope]
    return (meta or {}).get("rate_limit")


def _platform_grant_meta(sub, provider, active_org) -> "dict | None":
    """Palier plateforme (ADR 0044 §F R3) SANS secret : {label, daily_quota} de l'instance
    PLATEFORM utilisable par `sub` la plus récente, ou None. Base des miroirs `status_for`/
    `credential_mode_for` (présence + quota, jamais de déchiffrement)."""
    for inst in credentials_store.list_platform_instances(provider):
        if _platform_instance_usable(sub, active_org, inst):
            return {"label": inst["label"],
                    "daily_quota": _platform_quota(sub, active_org, inst.get("meta"))}
    return None


def _resolve_platform_grant(sub, provider, active_org) -> "dict | None":
    """Palier plateforme AVEC secret : {label, secret, daily_quota} ou None. Remplace les 3
    lectures legacy (get_active_grant/get_active_org_grant/get_platform_api_key). Le secret
    n'est déchiffré QUE pour l'instance gagnante (chemin chaud)."""
    g = _platform_grant_meta(sub, provider, active_org)
    if not g:
        return None
    secret = credentials_store.get_credential(credentials_store.PLATFORM, g["label"], provider)
    if secret is None:
        return None
    return {**g, "secret": secret}


def _resolve_credential_impl(provider: str, want: str, sub: str,
                             account: Optional[str] = None) -> ResolvedCredential:
    """Résolveur substrat unique (ADR 0024) : marche la cascade EXACTE
    user > groupe actif > org active [> grant plateforme] **une fois** et renvoie
    le credential gagnant (clé + origine + config). `want="byo"` court-circuite le
    palier plateforme (sémantique byo-only de `resolve_credential_fields`) ;
    `want="auto"` inclut le grant plateforme + quota (sémantique `resolve_api_key`).
    `sub` explicite = utilisable HORS contexte MCP (routes REST) ; None = sub courant.
    `account` sélectionne le compte au palier MEMBRE en multi-compte (« 2 Zoho ») —
    None ⇒ épinglage projet, sinon compte unique auto, sinon McpError (voir plus bas).
    Lève une McpError actionnable si rien ne résout."""
    sub = sub or current_user_sub_or_raise()
    # RBAC connecteur interne à l'org (ADR 0025) — backstop DUR : un connecteur
    # restreint dans l'org du sub n'est résolu que pour les principals autorisés
    # (département/user). Avant toute résolution → couvre keyed/fields/BYO.
    require_connector_access(provider, sub)

    # Instance EXPLICITE de l'appel (`instance=`, ADR 0038 §C/B6) : si le ref épinglé
    # vise CE provider, on résout EXACTEMENT cette ligne du coffre — jamais de
    # fallback (une instance demandée qui ne résout pas = erreur actionnable, pas
    # une autre identité). Un ref d'un AUTRE provider est ignoré ici (il ne visait
    # pas cette résolution — ex. résolution auxiliaire d'un tool composite).
    pinned = session_org.current_call_instance()
    if pinned is not None and getattr(pinned, "connector", None) == provider:
        return _resolve_pinned_instance(provider, sub, pinned)

    # Binding de PROJET (ADR 0038 B5) : le projet de l'appel (`project=`) binde une
    # instance pour ce provider → résolution EN DUR, RE-GARDÉE pour l'APPELANT (le
    # binding a été gardé pour celui qui l'a posé ; l'appelant d'un projet partagé
    # peut être un autre membre). `instance=` explicite (ci-dessus) prime — le jeton
    # le plus spécifique de l'appel.
    bound = project_pinned_instance(provider)
    if bound is not None:
        guard_instance_access(sub, bound)
        return _resolve_pinned_instance(provider, sub, bound)

    # Scope MEMBRE (ADR 0033) : « ma clé » n'existe QUE dans l'org de contexte —
    # posée dans l'org A, elle ne résout pas depuis l'org B. L'org est résolue via
    # le seam `current_org` (session MCP ?? consultation ?? maison, ADR 0023) AVANT
    # le premier palier : plus aucun credential per-user org-agnostique.
    active_org = current_org(sub)
    member_key = None
    eff = ""  # compte effectif du palier membre (multi-compte)
    # Palier membre SEULEMENT pour un provider byo_user : un byo-org-only (http,
    # ex-mm…) n'a pas de credential membre par construction — et la LECTURE du
    # coffre le valide (`require_credential` lève ValueError) : sans ce gate, toute
    # résolution d'un provider org-only explosait AVANT les paliers groupe/org
    # (trou exposé par le cas « un http par département », 2026-07-05).
    if active_org is not None and connectors.is_byo_user(provider):
        if _is_multi_account(provider):
            # Multi-compte (coffre à N comptes, ex. « 2 Zoho ») : sélection du compte =
            # account explicite (param) > axe d'appel `account=` (#108) > épinglage
            # projet > compte unique auto > McpError (jamais de repli muet vers un autre
            # compte/l'org/la plateforme — anti-usurpation). '' = mono-compte legacy.
            eff = (account if account is not None
                   else session_org.current_call_account() or project_pinned_identity(provider))
            if eff is None:
                accts = credentials_store.list_accounts(
                    credentials_store.MEMBER, credentials_store.member_id(active_org, sub), provider)
                if len(accts) == 1:
                    eff = accts[0]["account"]
                elif len(accts) == 0:
                    eff = ""
                else:
                    raise McpError(ErrorData(
                        code=INVALID_PARAMS,
                        message=(
                            f"Plusieurs comptes `{provider}` configurés dans cette org — "
                            f"précise lequel (oto_connector_identities pour les lister)."
                        )))
            member_key = db.get_member_api_key(sub, active_org, provider, eff)
            if eff and not member_key:
                # Compte explicite/épinglé introuvable → on NE retombe PAS sur l'org/la
                # plateforme (ce serait agir sous une autre identité que celle demandée).
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=(
                        f"Compte `{eff}` introuvable pour `{provider}` — vérifie avec "
                        f"oto_connector_identities, ou pose-le sur {_ACCOUNT_URL}."
                    )))
        else:
            # Mono-compte (chemin historique inchangé) : account='' implicite.
            member_key = db.get_member_api_key(sub, active_org, provider)
    if member_key:
        return ResolvedCredential(provider, member_key, False, "user",
                                  credentials_store.MEMBER,
                                  credentials_store.member_id(active_org, sub), account=eff)

    # Paliers partagés (ADR 0012) : secret du GROUPE actif (le plus spécifique),
    # puis de l'ORG active. Sautés tant que l'user n'a ni groupe ni org actifs
    # (-> None) → strictement identique à avant pour tout user flat.
    # Cascade : clé membre > group_secret > org_secret > platform_grant.
    if provider in ORG_SHAREABLE_PROVIDERS:
        active_group = current_group(sub)
        if active_group is not None:
            grp_key = group_store.get_group_secret(active_group, provider)
            # Une instance BYO est utilisable par tout le sous-arbre de son owner —
            # restreindre = poser l'instance au bon niveau (équipe), pas une
            # allowlist (le cran `share_down` BYO a été retiré, jamais exposé à
            # l'écriture ; sur les instances PLATFORM il reste la liste des
            # grantees, cf. `_platform_instance_usable`).
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
    # ADR 0044 §F R3 : le palier plateforme lit les instances scope PLATFORM du coffre unifié
    # (share_mode/share_down = accès ; meta.rate_limit* = quota) au lieu des tables legacy
    # platform_keys/user_grants/org_grants. Free-tier = instance 'open' (share_down vide,
    # ouverte à tous) ; grant = instance 'closed' (sub ∈ share_down : user-grant OU org-grant
    # sur l'org active). Le secret n'est déchiffré que pour l'instance gagnante.
    grant = _resolve_platform_grant(sub, provider, active_org) if platform_eligible else None
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
    # ADR 0043 : une org abonnée à un plan `unmetered` n'a PLUS de quota sur les
    # clés plateforme — fin du micro-management des « credits d'appel ». Le plan
    # est le seul cran ; hors abonnement, les quotas d'essai tiennent.
    if limit and active_org is not None and _org_unmetered(active_org):
        limit = 0
    if limit and used >= limit:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Quota plateforme {provider} dépassé aujourd'hui ({used}/{limit}) "
                f"pour la clé `{grant['label']}`. Pose ta propre clé sur {_ACCOUNT_URL} "
                f"pour continuer sans limite."
            ),
        ))

    return ResolvedCredential(provider, grant["secret"], True, "platform",
                              credentials_store.PLATFORM, grant["label"])


def _sub_matches_scopes(sub: str, scopes) -> bool:
    """Vrai si `sub` appartient à l'un des scopes listés — vocabulaire COMMUN aux
    allowlists `share_down` et aux prêts `share_side` (ADR 0044), aligné sur
    `org_connector_access` : `user:<sub>` | `group:<gid>` | `org:<id>` (appartenance
    réelle) | `org` (tout le monde du sous-arbre). `org:<id>` (ADR 0044 §F) porte
    l'ancien grant org-level d'une clé plateforme. Fail-closed par entrée (une ref
    malformée est ignorée, jamais d'exception qui casserait la résolution)."""
    from . import group_store, roles
    for s in scopes or []:
        if s == "org":
            return True
        kind, _, ident = str(s).partition(":")
        if kind == "user" and ident == sub:
            return True
        if kind == "group":
            try:
                if group_store.is_group_member(sub, int(ident)):
                    return True
            except (ValueError, TypeError):
                continue
        if kind == "org":
            try:
                if roles.is_org_member(sub, int(ident)):
                    return True
            except (ValueError, TypeError):
                continue
    return False


def _instance_side_shares_safe(entity_type: str, entity_id: str, provider: str,
                               account: str = "") -> list:
    """`share_side` (prêts nominatifs) d'une instance, RÉSILIENT : sur hoquet DB →
    `[]` + warning = **fail-CLOSED** (aucun prêt accordé sans preuve). En prod ce
    chemin n'est atteint qu'après une lecture de clé réussie (même DB) — le fail-safe
    ne mord donc qu'aux tests unitaires sans DB. (Le cran `share_down` BYO a été
    retiré : une instance BYO est utilisable par tout le sous-arbre de son owner,
    restreindre = la poser au bon niveau. `share_down` ne vit plus que sur les
    instances PLATFORM, comme liste de grantees — `_platform_instance_usable`.)"""
    try:
        _, side = credentials_store.get_instance_sharing(entity_type, entity_id, provider, account)
        return side
    except Exception as e:
        logger.warning("instance_sharing fail-safe %s:%s/%s: %s", entity_type, entity_id, provider, e)
        return []


def guard_instance_access(sub: str, ref) -> Optional[int]:
    """Garde d'accès à une instance de connecteur par NIVEAU (ADR 0038 B6) — même
    sémantique que la projection B4 : member = MA ligne dans une org où je suis
    membre ; group = groupe dont je suis lecteur ; org = org dont je suis membre ;
    platform = refusé (le grant se résout déjà en dernier palier). Renvoie l'org de
    l'instance (à co-poser). McpError actionnable sinon. Chemin DB sync — appelants
    inbound chauds : threadpool. Partagée par l'axe `instance=` (pose) et la
    résolution d'un binding de projet (re-garde pour l'APPELANT, qui n'est pas
    forcément celui qui a bindé)."""
    from . import group_store, roles
    if ref.level == "member":
        if ref.sub == sub:                       # owner : ma propre instance
            if not roles.is_org_member(sub, ref.org_id):
                raise McpError(ErrorData(
                    code=INVALID_PARAMS,
                    message=f"Instance refusée : tu n'es plus membre de l'org #{ref.org_id}."))
            return ref.org_id
        # Prêt à un pair (share_side, ADR 0044) : instance d'un AUTRE membre, autorisée
        # ssi `sub` est nommé dans son share_side. On EMPRUNTE la clé mais on garde le
        # contexte de l'APPELANT → co-pose SON org (pas celle de l'owner ; cross-org OK,
        # le prêt nominatif EST le consentement). Pin explicite → refus DUR si non prêté.
        side = _instance_side_shares_safe(
            credentials_store.MEMBER, credentials_store.member_id(ref.org_id, ref.sub),
            ref.connector, ref.account)
        if _sub_matches_scopes(sub, side):
            return current_org(sub)
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=("Instance refusée : elle appartient à un autre membre et ne t'est "
                     "pas prêtée (share_side).")))
    if ref.level == "group":
        # Lecteur du groupe = membre OU admin de l'org (escalade `can_read_group`,
        # roles.py) — c'est le chemin par lequel un org_admin utilise l'instance
        # d'une équipe de son org (pin `instance=` / binding projet).
        if not roles.can_read_group(sub, ref.group_id):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Instance refusée : tu n'es pas membre du groupe #{ref.group_id}."))
        g = group_store.get_group(ref.group_id)
        return g.get("org_id") if g else None
    if ref.level == "org":
        if not roles.is_org_member(sub, ref.org_id):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"Instance refusée : tu n'es pas membre de l'org #{ref.org_id}."))
        return ref.org_id
    raise McpError(ErrorData(
        code=INVALID_PARAMS,
        message="Les refs `platform:` ne s'épinglent pas (le grant plateforme se "
                "résout déjà tout seul en dernier palier)."))


def project_pinned_instance(provider: str, project_id: Optional[int] = None):
    """Instance de connecteur BINDÉE par le projet de l'appel pour `provider`
    (`project_links.config.instance_ref`, ADR 0038 B5), ou None ⇒ cascade normale.
    **Un seul** binding à instance ⇒ son ref (parsé) ; **plusieurs** ⇒ McpError
    actionnable (identité d'action en jeu — jamais de choix silencieux : l'agent
    précise `instance=`). Lecture des liens fail-soft (DB en hoquet ⇒ None, comme
    `project_pinned_identity`) ; un ref STOCKÉ inparsable lève (validé au link —
    corruption = erreur, pas un repli muet)."""
    pid = current_project() if project_id is None else project_id
    if pid is None:
        return None
    try:
        refs = [(link.get("config") or {}).get("instance_ref")
                for link in db.list_project_links(int(pid))
                if link.get("target_type") == "connecteur"
                and link.get("target_ref") == provider
                and (link.get("config") or {}).get("instance_ref")]
    except Exception as e:
        logger.warning("project_pinned_instance fail-soft %s/%s: %s", pid, provider, e)
        return None
    if not refs:
        return None
    if len(refs) > 1:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"Le projet #{pid} binde PLUSIEURS instances `{provider}` — "
                     f"précise laquelle avec `instance=` ({', '.join(refs)}).")))
    from . import instance_refs
    return instance_refs.parse_ref(refs[0])


def _resolve_pinned_instance(provider: str, sub: str, ref) -> ResolvedCredential:
    """Résolution EN DUR d'une instance explicite (`instance=` OU binding de projet,
    ADR 0038 B6/B5) : lit exactement la ligne du coffre que le ref désigne. L'ACCÈS
    a été gardé par `guard_instance_access` (à la pose pour l'axe ; re-gardé pour
    l'APPELANT sur le chemin binding) ; le RBAC connecteur (ADR 0025) a été rejoué
    par l'appelant. Ligne absente = McpError actionnable, JAMAIS de fallback vers
    un autre palier (§C : agir sous une autre identité que celle demandée est
    interdit)."""
    from . import instance_refs
    if ref.level == "member":
        etype, eid = credentials_store.MEMBER, credentials_store.member_id(ref.org_id, sub)
        mode = "user"
    elif ref.level == "group":
        etype, eid, mode = "group", str(ref.group_id), "group"
    elif ref.level == "org":
        etype, eid, mode = "org", str(ref.org_id), "org"
    else:  # platform — refusé dès la pose par l'axe ; défense en profondeur ici.
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="Ref d'instance `platform:` non résoluble en `instance=` (B6)."))
    secret = credentials_store.get_credential(etype, eid, provider, ref.account)
    if not secret:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'instance `{instance_refs.format_ref(ref)}` ne résout plus "
                     "(credential retiré ou compte renommé ?). Reliste avec "
                     "oto_connector_instances — pas de repli vers une autre identité.")))
    return ResolvedCredential(provider, secret, False, mode, etype, eid,
                              account=ref.account)


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
    # ADR 0044 §F R3 : anon (pas de sub) → instance 'open' (free-tier, ouverte à tous) OU
    # 'closed' dont le share_down vise l'org du projet (`org:<org_id>`, gaté sur org_id ici).
    grant = _resolve_platform_grant(None, provider, org_id) if platform_eligible else None
    if not grant:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(f"L'endpoint anonyme ne peut pas résoudre `{provider}` : configure "
                     f"une clé d'org, ou grant une clé plateforme à l'org du projet.")))
    return ResolvedCredential(provider, grant["secret"], True, "platform",
                              credentials_store.PLATFORM, grant["label"])


def resolve_api_key(provider: str, account: Optional[str] = None) -> tuple[str, bool]:
    """Renvoie `(api_key, is_platform)` ou lève McpError actionnable. Vue mince
    sur `resolve_credential` (contrat inchangé pour les ~15 tools keyed ; `account`
    optionnel sélectionne le compte en multi-compte)."""
    rc = resolve_credential(provider, want="auto", account=account)
    return rc.key, rc.is_platform


def resolve_credential_fields(provider: str, account: Optional[str] = None) -> dict:
    """Résout un credential **multi-champs** byo_user (modèle générique, ADR 0011)
    du sub courant → dict des champs déclarés (`Connector.secret_fields`).

    Pour les connecteurs in-process dont le client s'instancie avec plusieurs
    secrets (ex. Silae : client_id / client_secret / subscription_key, OAuth2
    client-credentials). **byo-only** : pas de clé plateforme ni de quota — le
    credential EST le grant, comme un mount. Vue mince sur `resolve_credential`
    (cascade user > groupe > org, sans palier plateforme ; `account` sélectionne
    le compte en multi-compte)."""
    return resolve_credential(provider, want="byo", account=account).fields


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
    # Mode plateforme (ADR 0044 §F R3) : instance PLATFORM utilisable par sub. Gate sur
    # l'éligibilité `platform` du registre (défense en profondeur, comme resolve_api_key).
    con = connectors.connector_for_provider("unipile")
    if con and "platform" in con.auth_modes:
        grant = _resolve_platform_grant(sub, "unipile", active_org)
        if grant:
            return grant["secret"]
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
    grant = _platform_grant_meta(sub, provider, o)  # ADR 0044 §F R3 : instances PLATFORM
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
    # ADR 0044 §F R3 : clé plateforme utilisable par l'org (instance 'open' free-tier, ou
    # 'closed' dont le share_down vise `org:<org_id>`). Anon = pas de sub → org_id seul.
    if "platform" in con.auth_modes and _platform_grant_meta(None, provider, org_id):
        return True
    return False


BYO_MODES = ("user", "group", "org")


# (resolve_remote_credential retiré — ADR 0034 B4 : le connecteur `bridge`
# universel se résout par les champs standard, cf. resolve_credential_fields.)


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
            f"Connecte-le depuis ton dashboard (manage.oto.cx)."
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
        grant = _platform_grant_meta(sub, provider, active_org)  # ADR 0044 §F R3 : PLATFORM
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
        shareable = c.name in ORG_SHAREABLE_PROVIDERS
        st = (credentials_store.credential_status(
                  credentials_store.MEMBER,
                  credentials_store.member_id(active_org, sub), c.name)
              if active_org is not None else None)
        # Sessions partagées (connecteur org-partageable) : équipe active puis org.
        # Miroir de la cascade de résolution (membre > groupe > org).
        grp_st = (credentials_store.credential_status("group", str(active_group), c.name)
                  if shareable and active_group is not None else None)
        org_st = (credentials_store.credential_status("org", str(active_org), c.name)
                  if shareable and active_org is not None else None)
        meta = (st or {}).get("meta") or {}
        # `mode` = niveau gagnant de la cascade (membre > groupe > org), pour que la
        # carte dise sous quelle session on résout — comme les connecteurs keyés.
        if st:
            mode = "user"
        elif grp_st:
            mode = "group"
        elif org_st:
            mode = "org"
        else:
            mode = "forbidden"
        out["providers"][c.name] = {
            "mode": mode,
            "user_key_configured": st is not None,
            "session_set_at": st["set_at"] if st else None,
            # Identité/cible par défaut du sélecteur ADR 0024 (pennylaneged : la
            # société cliente = SA GED) — satellites PUBLICS du meta, la carte les
            # affiche sans lister (le listing = une session Browserbase louée).
            "identity_id": meta.get("default_identity_id"),
            "identity_label": meta.get("default_identity_label"),
            # Sessions partagées (une par scope) : présence + horodatage, pour que la
            # carte affiche/déconnecte chaque niveau. `session_set_at` reste le membre.
            "group_secret_configured": grp_st is not None,
            "group_session_set_at": grp_st["set_at"] if grp_st else None,
            "org_secret_configured": org_st is not None,
            "org_session_set_at": org_st["set_at"] if org_st else None,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }
    return out
