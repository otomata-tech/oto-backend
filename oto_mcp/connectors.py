"""Registre des connecteurs — SOURCE UNIQUE de vérité.

Module pur (aucun import oto_mcp, comme tool_visibility.py). Remplace les 4
listes en dur qui dérivaient (`db.KEY_PROVIDERS`, `access.ORG_SHAREABLE_PROVIDERS`,
`tool_visibility.ADMIN_GRANT_ONLY_NAMESPACES`, le `PROVIDERS` du frontend) plus
`_QUOTA_DEFAULTS` et l'override `env_secret_names` du bootstrap. Tout en dérive.

Chaque connecteur porte les 3 axes du modèle plateforme :
- **A. Disponibilité** : `availability` (self_serve | platform_granted). platform_granted
  = grant-only (la plateforme accorde explicitement, ex. `mm` réservé à Movinmotion).
- **B. Visibilité** : `in_default_bundle` (accordé d'office à une nouvelle entité) /
  `in_default_preset` (affiché+activé par le preset de base). Policy, tunable.
- **C. Credential** : `auth_modes` ⊆ {byo_user, byo_org, platform} ; `keyed` (résolu via
  `resolve_api_key` avec une clé api) ; `secret_kind` ; `personal_session` (session
  physiologiquement per-user : linkedin/google/slack/whatsapp/crunchbase, jamais org).

NB barreau « Phase 1 » : ce registre encode l'état ACTUEL (les dérivations sont
byte-identiques aux anciennes listes). Les évolutions de taxonomie (ex. gocardless
→ BYO self_serve keyed, mm → injection platform) sont des changements ultérieurs
explicites de ce registre, qui piloteront leurs migrations.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Connector:
    name: str                          # identité = clé de credential
    namespaces: tuple[str, ...]        # préfixes de tools possédés
    availability: str                  # "self_serve" | "platform_granted"
    auth_modes: frozenset              # ⊆ {"byo_user","byo_org","platform"}
    keyed: bool                        # résolu via resolve_api_key (→ KEY_PROVIDERS)
    personal_session: bool             # per-user only, jamais org
    secret_kind: str                   # api_key|refresh_token|oauth|cookie|none
    env_secret_name: str | None        # var SOPS de la clé plateforme (None = aucune)
    default_quota: int                 # 0 = illimité
    in_default_bundle: bool            # axe A : accordé d'office (bundle par défaut)
    in_default_preset: bool            # axe B : affiché+activé par le preset de base
    label: str = ""
    help: str = ""
    href: str | None = None
    # "tools" = module in-process (tools/<name>.py) ; "remote" = bridge distant
    # (ADR 0003) servi par le module générique tools/remote.py — le credential
    # d'org est alors {secret=token M2M, meta.base_url=endpoint du bridge}.
    kind: str = "tools"

    @property
    def org_shareable(self) -> bool:
        return "byo_org" in self.auth_modes

    @property
    def grant_only(self) -> bool:
        return self.availability == "platform_granted"


def _c(name, namespaces, *, availability="self_serve", auth_modes=(), keyed=False,
       personal_session=False, secret_kind="none", env_secret_name=None,
       default_quota=0, in_default_bundle=True, in_default_preset=False,
       label="", help="", href=None, kind="tools") -> Connector:
    return Connector(
        name=name, namespaces=tuple(namespaces), availability=availability,
        auth_modes=frozenset(auth_modes), keyed=keyed, personal_session=personal_session,
        secret_kind=secret_kind, env_secret_name=env_secret_name, default_quota=default_quota,
        in_default_bundle=in_default_bundle, in_default_preset=in_default_preset,
        label=label or name.capitalize(), help=help, href=href, kind=kind,
    )


# Ordre des 9 connecteurs `keyed` = ordre EXACT de l'ancien KEY_PROVIDERS
# (status_for itère dessus, l'affichage en dépend). Ne pas réordonner.
_REGISTRY_LIST = [
    # --- keyed (résolus via resolve_api_key, clé api per-user) ---------------
    _c("serper", ["serper"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="SERPER_API_KEY", default_quota=50,
       in_default_preset=True, label="Serper", help="recherche web", href="https://serper.dev"),
    _c("hunter", ["hunter"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="HUNTER_API_KEY", default_quota=10,
       in_default_preset=True, label="Hunter.io", help="emails", href="https://hunter.io"),
    _c("sirene", ["fr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="SIRENE_API_KEY", default_quota=200,
       in_default_preset=True, label="INSEE SIRENE", help="données entreprise FR",
       href="https://api.insee.fr"),
    _c("attio", ["attio"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="ATTIO_API_KEY", default_quota=200,
       in_default_preset=True, label="Attio", help="CRM", href="https://app.attio.com"),
    _c("lemlist", ["lemlist"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="LEMLIST_API_KEY",
       label="Lemlist", help="cold outreach", href="https://app.lemlist.com"),
    _c("kaspr", ["kaspr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="KASPR_API_KEY", default_quota=5,
       label="Kaspr", help="enrichissement", href="https://app.kaspr.io"),
    _c("pennylane", ["pennylane"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="PENNYLANE_API_KEY",
       label="Pennylane", help="compta", href="https://app.pennylane.com"),
    _c("slack", ["slack"], auth_modes={"byo_user"}, keyed=True, personal_session=True,
       secret_kind="refresh_token", env_secret_name="SLACK_USER_TOKEN",
       label="Slack", help="messagerie (user token)"),
    _c("fullenrich", ["fullenrich"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="FULLENRICH_API_KEY",
       label="FullEnrich", help="enrichissement waterfall", href="https://app.fullenrich.com"),

    # --- platform_granted (grant-only, deny-by-default) ----------------------
    # gocardless : aujourd'hui grant-only non-keyed (resolve_api_key('gocardless')
    # lève faute de provider). Reclassé BYO self_serve dans une phase ultérieure.
    _c("gocardless", ["gocardless"], availability="platform_granted", secret_kind="api_key",
       env_secret_name="GOCARDLESS_API_KEY", in_default_bundle=False,
       label="GoCardless", help="prélèvements SEPA (lecture)"),
    # mm : connecteur REMOTE (bridge, ADR 0003). Le credential Movinmotion vit
    # dans le service distant movinmotion-backoffice-bridge — JAMAIS ici. Le
    # credential d'org (Movinmotion, org-partageable) = token M2M du bridge
    # (`secret`) + endpoint (`meta.base_url`). availability platform_granted
    # (réservé à l'org entitled). Tools servis par le générique tools/remote.py.
    _c("mm", ["mm"], kind="remote", availability="platform_granted", auth_modes={"byo_org"},
       secret_kind="api_key", in_default_bundle=False,
       label="Movinmotion BO", help="back-office Movinmotion (lecture, via bridge)"),

    # --- sessions per-user (hors resolve_api_key, stockage dédié) ------------
    _c("linkedin", ["linkedin"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_preset=True, label="LinkedIn"),
    _c("crunchbase", ["crunchbase"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_bundle=False, label="Crunchbase"),
    _c("google", ["gmail", "data", "datastore"], auth_modes={"byo_user"},
       personal_session=True, secret_kind="oauth", in_default_preset=True,
       label="Google", help="Gmail + Sheets/Drive (OAuth)"),
    _c("whatsapp", ["whatsapp"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_bundle=False, label="WhatsApp"),

    # --- open-data / sans credential ----------------------------------------
    _c("fr_open", ["culture_spectacle", "dvf", "reddit"], secret_kind="none",
       in_default_preset=True, label="Open data", help="culture / DVF / reddit"),
    _c("sirene_stock", ["sirene_stock"], secret_kind="none", in_default_preset=True,
       label="SIRENE stock", help="établissements INSEE (DuckDB)"),
]

REGISTRY: dict[str, Connector] = {c.name: c for c in _REGISTRY_LIST}


# --- index inverse namespace -> connecteur ----------------------------------
_NS_INDEX: dict[str, Connector] = {}
for _c_obj in _REGISTRY_LIST:
    for _ns in _c_obj.namespaces:
        _NS_INDEX[_ns] = _c_obj


# --- dérivations (remplacent les 4 listes en dur + quotas + env-names) -------

KEY_PROVIDERS: tuple = tuple(c.name for c in _REGISTRY_LIST if c.keyed)
ORG_SHAREABLE_PROVIDERS: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.org_shareable)
ADMIN_GRANT_ONLY_NAMESPACES: frozenset = frozenset(
    ns for c in _REGISTRY_LIST if c.grant_only for ns in c.namespaces
)
QUOTA_DEFAULTS: dict = {c.name: c.default_quota for c in _REGISTRY_LIST if c.default_quota}
ENV_SECRET_NAMES: dict = {c.name: c.env_secret_name for c in _REGISTRY_LIST if c.env_secret_name}
DEFAULT_BUNDLE: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.in_default_bundle)
DEFAULT_PRESET: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.in_default_preset)
REMOTE_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "remote")


# --- helpers ----------------------------------------------------------------

def connector_for_provider(name: str) -> Connector | None:
    return REGISTRY.get(name)


def connector_for_namespace(namespace: str) -> Connector | None:
    return _NS_INDEX.get(namespace)


def is_keyed(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and c.keyed)


def require_keyed(name: str) -> None:
    """Remplace db._check_provider : lève si `name` n'est pas un provider keyed."""
    if not is_keyed(name):
        raise ValueError(f"Unknown provider {name!r} (allowed: {KEY_PROVIDERS})")


def require_credential(entity_type: str, name: str) -> None:
    """Lève si le connecteur ne peut PAS porter un credential à ce niveau d'entité.
    user → doit accepter `byo_user` (clé API keyed OU secret de session :
    linkedin/crunchbase/google/slack…) ; org → doit être org-partageable (byo_org,
    ex. mm org-only). Utilisé par credentials_store (coffre unique tous secrets)."""
    if entity_type == "org":
        if not is_org_shareable(name):
            raise ValueError(f"{name!r} n'est pas un credential org-partageable")
    else:
        if not is_byo_user(name):
            raise ValueError(
                f"{name!r} n'accepte pas de credential per-user (byo_user requis)")


def is_byo_user(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and "byo_user" in c.auth_modes)


def is_org_shareable(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and c.org_shareable)


def public_catalog() -> list[dict]:
    """Vue publique (GET /api/connectors) — sans secret, pour le frontend."""
    return [
        {
            "name": c.name,
            "label": c.label,
            "help": c.help,
            "href": c.href,
            "availability": c.availability,
            "auth_modes": sorted(c.auth_modes),
            "personal_session": c.personal_session,
            "secret_kind": c.secret_kind,
            "namespaces": list(c.namespaces),
        }
        for c in _REGISTRY_LIST
    ]
