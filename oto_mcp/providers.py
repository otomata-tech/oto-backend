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

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CredentialField:
    """Un champ de saisie d'un credential (modèle générique multi-champs, ADR 0011).

    SOURCE UNIQUE du formulaire de saisie (dashboard), de l'endpoint REST et du
    packing au coffre. `secret` = masqué dans l'UI ; `reveal` = renvoyé tel quel
    en GET (l'`api_key` se relit pour copier, un mot de passe/secret jamais)."""
    name: str
    label: str
    secret: bool = True
    reveal: bool = False
    help: str = ""


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
    default_hidden: bool = False       # axe B : namespaces masqués par défaut mais
                                       # self-activables (oto_enable_tool) — découvrabilité,
                                       # pas sécurité (≠ platform_granted)
    label: str = ""
    help: str = ""
    href: str | None = None
    # Éditeur du connecteur (affiché au catalogue). Vide → dérivé de
    # `_PUBLISHER_BY_CONNECTOR` (cf. `publisher_name`), défaut "Otomata".
    publisher: str = ""
    # URL publique du logo de l'éditeur. None → dérivée du CDN logo.dev à partir
    # du domaine de marque curé `_LOGO_DOMAIN_BY_CONNECTOR` (cf. `logo_url_for`).
    # Le champ explicite reste un override (logo custom hébergé ailleurs).
    logo_url: str | None = None
    # "tools" = module in-process (tools/<name>.py) ; "remote" = bridge distant
    # (ADR 0003) servi par le module générique tools/remote.py — le credential
    # d'org est alors {secret=token M2M, meta.base_url=endpoint du bridge} ;
    # "mount" = MCP distant fédéré (otomata#16) monté via FastMCP proxy par le
    # module générique tools/mount.py — credential per-user (token OAuth) injecté
    # par requête, endpoint = `mount_url`.
    kind: str = "tools"
    # Endpoint MCP du serveur distant à monter (kind="mount" uniquement).
    mount_url: str | None = None
    # Schéma de saisie EXPLICITE du credential (modèle générique multi-champs).
    # Vide → dérivé du secret_kind (cf. `secret_fields`). Renseigné pour les
    # credentials à >1 champ qui ne sont ni api_key ni basic_auth (ex. Silae :
    # client_id + client_secret + subscription_key).
    credential_fields: tuple[CredentialField, ...] = ()
    # Modules `tools/<m>.py` à importer pour ce connecteur (kind="tools" seulement).
    # Vide ⇒ `(name,)`. Renseigné quand le module ≠ nom du provider (sirene→fr) ou
    # qu'un provider porte plusieurs modules (google→gmail/datastore/tasks).
    # `register_all` DÉRIVE le chargement de ce champ (fin de la liste hardcodée, #24).
    modules: tuple[str, ...] = ()

    @property
    def org_shareable(self) -> bool:
        return "byo_org" in self.auth_modes

    @property
    def grant_only(self) -> bool:
        return self.availability == "platform_granted"

    @property
    def family(self) -> str:
        """Nature de l'intégration (axe *builder*, ADR 0011) — DÉRIVÉE du credential
        + runtime : open-data | api | browser | google | federated | bridge."""
        if self.kind == "remote":
            return "bridge"
        if self.kind == "mount":
            return "federated"
        if self.name in BROWSER_PROVIDERS:
            return "browser"
        if self.name == "google":
            return "google"
        if self.secret_kind == "none":
            return "open-data"
        return "api"

    @property
    def category(self) -> str:
        """Domaine d'usage (axe *utilisateur*, ADR 0011) — CURÉ, pour grouper l'UI."""
        return _CATEGORY_BY_CONNECTOR.get(self.name, "Autres")

    @property
    def publisher_name(self) -> str:
        """Éditeur affiché au catalogue — override champ si renseigné, sinon la
        map curée `_PUBLISHER_BY_CONNECTOR`, sinon "Otomata" (connecteur maison)."""
        if self.publisher:
            return self.publisher
        return _PUBLISHER_BY_CONNECTOR.get(self.name, "Otomata")

    def logo_url_for(self) -> str | None:
        """URL publique du logo de l'éditeur. Override `logo_url` si présent,
        sinon dérivée du CDN **logo.dev** : domaine de marque curé
        (`_LOGO_DOMAIN_BY_CONNECTOR`) + token publishable `LOGODEV_TOKEN` (env).
        None si pas de domaine connu (open-data/maison → monogramme côté UI) ou
        token absent. Le token est *publishable* (conçu pour vivre dans l'URL)."""
        if self.logo_url:
            return self.logo_url
        domain = _LOGO_DOMAIN_BY_CONNECTOR.get(self.name)
        token = os.environ.get("LOGODEV_TOKEN")
        if not domain or not token:
            return None
        return (f"https://img.logo.dev/{domain}"
                f"?token={token}&size=256&format=png&retina=true")

    @property
    def secret_fields(self) -> tuple[CredentialField, ...]:
        """Schéma de saisie du credential — SOURCE UNIQUE pour l'UI, l'endpoint REST,
        `status_for` et le packing. Déclaré explicitement (`credential_fields`),
        sinon dérivé des formes simples. Vide = pas de saisie générique : `cookie`
        (linkedin/crunchbase), `oauth` (google/memento) et `none` (open-data) ont
        des flux dédiés, pas un formulaire de champs."""
        if self.credential_fields:
            return self.credential_fields
        if self.secret_kind == "api_key":
            return (CredentialField("key", "API key", secret=True, reveal=True),)
        if self.secret_kind == "basic_auth":
            return (CredentialField("email", "Email", secret=False),
                    CredentialField("password", "Mot de passe", secret=True))
        return ()


# Connecteurs passant par l'automation navigateur (o-browser) — non dérivable du
# seul secret_kind (slack est aussi personal_session, mais c'est une API).
BROWSER_PROVIDERS = frozenset({"crunchbase"})

# Catégorie d'usage (domaine) par connecteur — CURÉE (pas dérivable), tunable.
_CATEGORY_BY_CONNECTOR = {
    "serper": "Prospection", "hunter": "Prospection", "kaspr": "Prospection",
    "fullenrich": "Prospection", "lemlist": "Prospection", "attio": "Prospection",
    "folk": "Prospection", "crunchbase": "Prospection",
    "unipile": "Prospection", "topograph": "Prospection",
    "sirene": "Data FR", "fr_open": "Data FR", "sirene_stock": "Data FR",
    "foncier": "Data FR", "sante": "Data FR",
    "pennylane": "Finance", "gocardless": "Finance", "silae": "Finance",
    "slack": "Comms", "google": "Comms", "zohodesk": "Comms",
    "memento": "Knowledge", "notion": "Knowledge", "planity": "Métier",
    "hubspot": "Prospection", "apollo": "Prospection", "zerobounce": "Prospection",
    "hithorizons": "Prospection", "phantombuster": "Prospection", "zoho": "Prospection",
    "figma": "Design", "supabase": "Dev",
}

# Éditeur (publisher) par connecteur — CURÉ. Défaut "Otomata" (connecteurs maison /
# open-data agrégés par nous) ; sinon l'éditeur tiers de l'API sous-jacente. Keyé
# sur `Connector.name`, comme `_CATEGORY_BY_CONNECTOR`.
_PUBLISHER_BY_CONNECTOR = {
    "serper": "Serper", "hunter": "Hunter.io", "kaspr": "Kaspr",
    "fullenrich": "FullEnrich", "lemlist": "lemlist", "folk": "Folk",
    "unipile": "Unipile", "pennylane": "Pennylane", "gocardless": "GoCardless",
    "silae": "Silae", "attio": "Attio", "crunchbase": "Crunchbase",
    "slack": "Slack", "whatsapp": "WhatsApp", "google": "Google",
    "memento": "Memento", "planity": "Planity",
    "hubspot": "HubSpot", "apollo": "Apollo", "zerobounce": "ZeroBounce",
    "hithorizons": "HitHorizons", "phantombuster": "Phantombuster",
    "notion": "Notion", "figma": "Figma", "supabase": "Supabase",
    "zoho": "Zoho", "zohodesk": "Zoho",
    # open-data FR → éditeur = la source publique
    "sirene": "INSEE", "sirene_stock": "INSEE", "fr_open": "Open data FR",
    "foncier": "État (open data)", "sante": "HAS / FINESS",
}

# Domaine de marque curé par connecteur → le CDN logo.dev en dérive l'URL du logo
# (cf. `logo_url_for`). Domaine RACINE (pas les `app.*` ni sous-domaines MCP). Les
# connecteurs absents (open-data/État `fr_open`/`foncier`/`sante`) n'ont pas de
# marque produit → pas de logo → monogramme côté UI.
_LOGO_DOMAIN_BY_CONNECTOR = {
    "serper": "serper.dev", "hunter": "hunter.io", "kaspr": "kaspr.io",
    "fullenrich": "fullenrich.com", "lemlist": "lemlist.com", "folk": "folk.app",
    "unipile": "unipile.com", "pennylane": "pennylane.com", "gocardless": "gocardless.com",
    "silae": "silae.fr", "attio": "attio.com", "crunchbase": "crunchbase.com",
    "slack": "slack.com", "whatsapp": "whatsapp.com", "google": "google.com",
    "memento": "mento.cc", "planity": "planity.com", "topograph": "topograph.co",
    "sirene": "insee.fr", "sirene_stock": "insee.fr",
}


def _c(name, namespaces, *, availability="self_serve", auth_modes=(), keyed=False,
       personal_session=False, secret_kind="none", env_secret_name=None,
       default_quota=0, in_default_bundle=True, in_default_preset=False,
       default_hidden=False, label="", help="", href=None, publisher="",
       logo_url=None, kind="tools", mount_url=None, credential_fields=(),
       modules=()) -> Connector:
    return Connector(
        name=name, namespaces=tuple(namespaces), availability=availability,
        auth_modes=frozenset(auth_modes), keyed=keyed, personal_session=personal_session,
        secret_kind=secret_kind, env_secret_name=env_secret_name, default_quota=default_quota,
        in_default_bundle=in_default_bundle, in_default_preset=in_default_preset,
        default_hidden=default_hidden,
        label=label or name.capitalize(), help=help, href=href,
        publisher=publisher, logo_url=logo_url, kind=kind,
        mount_url=mount_url, credential_fields=tuple(credential_fields),
        modules=tuple(modules),
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
       href="https://api.insee.fr", modules=("fr",)),
    # attio : masqué par défaut (2026-06-11) — le MCP Attio officiel est meilleur
    # pour l'instant. Code conservé (tools/attio.py) pour d'éventuelles implems
    # custom ; self-activable via oto_enable_tool.
    _c("attio", ["attio"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="ATTIO_API_KEY", default_quota=200,
       default_hidden=True, label="Attio", help="CRM", href="https://app.attio.com"),
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
    _c("fullenrich", ["fullenrich"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="FULLENRICH_API_KEY", default_quota=25,
       label="FullEnrich", help="enrichissement waterfall", href="https://app.fullenrich.com"),
    # folk : né APRÈS le coffre — pas de colonne legacy users.folk_api_key,
    # le coffre connector_credentials est canonique. byo-only (pas de clé
    # plateforme) ; compte partagé équipe = credential de l'org Otomata.
    _c("folk", ["folk"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="FOLK_API_KEY",
       label="Folk", help="CRM", href="https://app.folk.app"),
    # unipile : LinkedIn hébergé (recherche/scrape/messagerie) via l'API Unipile.
    # La session LinkedIn vit chez Unipile (vrai Chrome + proxy résidentiel) →
    # contourne empreinte TLS + isolation de session du browser local (#5). Keyed
    # api_key (résolu via resolve_api_key, cascade user > org). byo_user (BYO) OU
    # byo_org (l'org pose l'abonnement Otomata, ses membres connectent leur LinkedIn
    # par hosted-auth). Hors bundle par défaut : SaaS payant, opt-in. Le **dsn**
    # (sous-domaine dédié `api<NN>.unipile.com:port`) est résolu côté client (env
    # `UNIPILE_DSN`, défaut api25 = celui d'Otomata) — PAS un champ de credential
    # tant qu'un BYO sur un autre sous-domaine n'existe pas (déféré ; single-field
    # = compatible avec le stockage org-secret existant, mono-valeur). 2e provider
    # du domaine LinkedIn — convergence en capabilities provider-agnostiques (0010/0011) plus tard.
    _c("unipile", ["unipile", "whatsapp", "telegram", "instagram", "messenger", "twitter"],
       auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", env_secret_name="UNIPILE_API_KEY",
       in_default_bundle=False, label="Messagerie hébergée (Unipile)",
       help="LinkedIn + WhatsApp + Telegram + Instagram + Messenger + X/Twitter hébergés (recherche/scrape/messagerie)",
       href="https://www.unipile.com",
       modules=("unipile", "whatsapp", "telegram", "instagram", "messenger", "twitter")),
    # topograph : KYB — données + documents normalisés de 100+ registres publics
    # européens via une seule API REST. byo-only (pay-per-request, chacun connecte
    # son compte ; clé d'org partageable), keyed api_key (en-tête x-api-key résolu
    # côté client). Pas de clé plateforme. Hors bundle par défaut : opt-in.
    _c("topograph", ["topograph"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", env_secret_name="TOPOGRAPH_API_KEY",
       in_default_bundle=False, label="Topograph",
       help="KYB — données & documents entreprise (registres européens)",
       href="https://www.topograph.co"),

    # --- byo_user à credential multi-champs (hors resolve_api_key) -----------
    # silae : paie FR. Auth OAuth2 client-credentials (Azure AD B2C) = 3 secrets
    # → modèle générique multi-champs (ADR 0011). PAS keyed (résolu via
    # access.resolve_credential_fields, pas de clé plateforme ni quota : byo-only,
    # le credential EST le grant). in_default_bundle=False → activable à la demande
    # (cran d'activation par org). IBAN/BIC masqués avant l'agent (tools/silae.py).
    _c("silae", ["silae"], auth_modes={"byo_user"}, secret_kind="fields",
       in_default_bundle=False, label="Silae", help="paie FR (lecture) — API Silae Paie v1",
       href="https://www.silae.fr", credential_fields=(
           CredentialField("client_id", "Client ID", secret=True),
           CredentialField("client_secret", "Client Secret", secret=True),
           CredentialField("subscription_key", "Subscription Key", secret=True),
       )),

    # --- gocardless : keyed BYO self-serve -----------------------------------
    # keyed BYO (user OU org), résolu via resolve_api_key comme pennylane/attio.
    # self_serve : chacun connecte SON propre compte GoCardless (sandbox ou prod) —
    # PAS de clé plateforme partagée, donc rien de sensible à gater par grant. Reste
    # hors bundle par défaut (in_default_bundle=False) → opt-in, pas imposé. L'org MM
    # y pose le token de son compte de service pour le POC avoirs (doctrine org 35).
    _c("gocardless", ["gocardless"], availability="self_serve",
       auth_modes={"byo_user", "byo_org"}, keyed=True, secret_kind="api_key",
       env_secret_name="GOCARDLESS_API_KEY", in_default_bundle=False,
       label="GoCardless", help="prélèvements SEPA (lecture)"),
    # (Aucune entrée remote au registre : un connecteur REMOTE (ADR 0003/0011) est
    # défini par la DONNÉE — un credential d'org avec `meta.base_url` (l'endpoint du
    # bridge). Zéro nom client en dur. Découvert au boot par tools/remote.py via
    # credentials_store.list_remote_namespaces ; le credential d'org EST le grant.)
    # memento : MCP fédéré (otomata#16, kind=mount). MCP autonome distant
    # (mcp.mento.cc) monté via proxy FastMCP (tools/mount.py) ; credential
    # per-user = token OAuth Supabase (flow memento_oauth.py), injecté par
    # requête. **Fédération systématique** : `self_serve` (PAS platform_granted)
    # → visible dans le catalogue de TOUS les users (la carte « federated mcp » du
    # dashboard les invite à connecter leur compte memento — auto-prompt), et ses
    # outils sont de droit. Un appel sans compte connecté lève une McpError
    # actionnable (resolve_mount_token) qui pointe vers le dashboard. Le compte
    # memento est lui-même provisionné d'office à la création du compte oto
    # (memento_federation.py). byo_user (chacun connecte SON compte).
    _c("memento", ["memento"], kind="mount", mount_url="https://mcp.mento.cc/mcp",
       auth_modes={"byo_user"}, secret_kind="oauth",
       in_default_bundle=False, label="Memento",
       help="base de connaissance structurée (MCP fédéré)", href="https://mento.cc"),
    # planity : MCP fédéré (kind=mount). Serveur autonome stateless distant
    # (planity-mcp.oto.zone) monté via proxy FastMCP ; credential per-user =
    # base64("email:password") du compte Planity de l'user, injecté par requête
    # dans le bearer (planity-mcp le décode et rejoue la chaîne d'auth Planity).
    _c("planity", ["planity"], kind="mount",
       mount_url="https://planity-mcp.oto.zone/mcp",
       auth_modes={"byo_user"}, secret_kind="basic_auth", in_default_bundle=False,
       label="Planity",
       help="agenda + caisse Planity (RDV, clients, CA, stats) — MCP fédéré",
       href="https://planity-mcp.oto.zone"),

    # --- sessions per-user (hors resolve_api_key, stockage dédié) ------------
    # LinkedIn n'est plus un connecteur browser ici : remplacé par le connecteur
    # `unipile` (LinkedIn hébergé). Le browser LinkedIn local reste dans oto-cli.
    _c("crunchbase", ["crunchbase"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_bundle=False, label="Crunchbase"),
    # namespaces = préfixes RÉELS des tools (namespace_of = 1er token avant `_`) :
    # gmail_* / tasks_*. PAS "data" : datastore est un SPINE plateforme (ADR 0016),
    # pas un connecteur Google — chargé explicitement dans register_all, non gaté
    # par l'activation (cf. middleware.py « tools plateforme … data … jamais gatés »).
    _c("google", ["gmail", "tasks", "calendar", "sheets", "drive", "chat"],
       auth_modes={"byo_user"},
       personal_session=True, secret_kind="oauth", in_default_preset=True,
       label="Google", help="Gmail + Tasks + Calendar + Sheets + Drive + Chat (OAuth)",
       modules=("gmail", "tasks", "calendar", "sheets", "drive", "chat")),

    # --- open-data / sans credential ----------------------------------------
    # namespace = préfixe réel : culture_spectacle_* → `culture` (namespace_of =
    # 1er token), reddit_* → `reddit`. Déclarer "culture", pas "culture_spectacle"
    # (jamais matché → fail-open du gate, #24).
    _c("fr_open", ["culture", "reddit"], secret_kind="none",
       in_default_preset=True, label="Open data", help="culture / reddit",
       modules=("culture", "reddit")),
    _c("sirene_stock", ["sirene_stock"], secret_kind="none", in_default_preset=True,
       label="SIRENE stock", help="établissements INSEE (DuckDB)"),
    # foncier / sante : connecteurs open-data déclarés (ADR 0010). Inertes tant
    # que non activés en DB (connector_activation) — register_all gate dessus,
    # donc absents du seed initial → OFF par défaut (deny-by-default).
    _c("foncier", ["foncier"], secret_kind="none", in_default_bundle=False,
       label="Foncier", help="géocodage, cadastre, bâti, risques/ICPE, solaire, immobilier (open data)"),
    _c("sante", ["sante"], secret_kind="none", in_default_bundle=False,
       label="Santé", help="établissements FINESS + évaluations ESSMS HAS (open data)"),

    # --- connecteurs API tiers (clients oto-core déjà écrits, câblés 2026-06-19) ---
    # byo keyed api_key, hors bundle (opt-in, activables par org/admin), pas de
    # clé plateforme (chacun pose la sienne). Inertes tant que non activés en DB
    # (connector_activation, deny-by-default), comme foncier/sante.
    _c("hubspot", ["hubspot"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="HubSpot",
       help="CRM (contacts, companies, deals, tickets, notes)",
       href="https://app.hubspot.com"),
    _c("apollo", ["apollo"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Apollo.io",
       help="prospection B2B (organizations, people, job postings)",
       href="https://app.apollo.io"),
    _c("zerobounce", ["zerobounce"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="ZeroBounce",
       help="vérification de délivrabilité email", href="https://www.zerobounce.net"),
    _c("hithorizons", ["hithorizons"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="HitHorizons",
       help="données entreprise européennes (recherche + détails)",
       href="https://www.hithorizons.com"),
    _c("phantombuster", ["phantombuster"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Phantombuster",
       help="agents d'automatisation (launch + résultats)",
       href="https://phantombuster.com"),
    _c("notion", ["notion"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Notion",
       help="pages, bases de données, blocs (lecture + écriture)",
       href="https://www.notion.so"),
    _c("figma", ["figma"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Figma",
       help="fichiers, export d'images, commentaires, FigJam",
       href="https://www.figma.com"),
    _c("supabase", ["supabase"], auth_modes={"byo_user"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Supabase",
       help="Management API (projets, config auth, logs)",
       href="https://supabase.com"),
    # zoho / zohodesk : OAuth2 self-client → credential multi-champs (ADR 0011,
    # comme silae), résolu via resolve_credential_fields. byo_user (pas de quota).
    _c("zoho", ["zoho"], auth_modes={"byo_user"}, secret_kind="fields",
       in_default_bundle=False, label="Zoho CRM",
       help="CRM Zoho (CRUD modules, notes)", href="https://crm.zoho.com",
       credential_fields=(
           CredentialField("client_id", "Client ID", secret=True),
           CredentialField("client_secret", "Client Secret", secret=True),
           CredentialField("refresh_token", "Refresh Token", secret=True),
       )),
    _c("zohodesk", ["zohodesk"], auth_modes={"byo_user"}, secret_kind="fields",
       in_default_bundle=False, label="Zoho Desk",
       help="support Zoho Desk (tickets, threads, contacts)",
       href="https://desk.zoho.com", credential_fields=(
           CredentialField("client_id", "Client ID", secret=True),
           CredentialField("client_secret", "Client Secret", secret=True),
           CredentialField("refresh_token", "Refresh Token", secret=True),
           CredentialField("org_id", "Org ID", secret=False),
       )),
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
DEFAULT_HIDDEN_NAMESPACES: frozenset = frozenset(
    ns for c in _REGISTRY_LIST if c.default_hidden for ns in c.namespaces
)
REMOTE_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "remote")
MOUNT_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "mount")


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


def org_secret_meta(provider: str, base_url: str | None) -> tuple[dict | None, str | None]:
    """Valide l'écriture d'un secret partagé d'org et calcule son `meta` satellite.

    Un connecteur **remote** (ADR 0003/0011) est défini par la DONNÉE : fournir un
    `base_url` (endpoint du bridge) ⇒ c'est un remote, qu'il ait ou non une entrée
    au registre (zéro nom client en dur). Sinon, le provider doit être un connecteur
    org-partageable du registre (clé partagée : attio, pennylane…) et REFUSE un
    `base_url`. Pure (registre seul) → testable hors DB.

    Renvoie `(meta, error_code)`. `error_code` None = OK ; `meta` = `{base_url}` pour
    un remote, sinon None. Codes : `provider_not_shareable`, `base_url_required`,
    `base_url_not_allowed`.
    """
    c = connector_for_provider(provider)
    # remote = entrée registre kind="remote" (legacy) OU un base_url sur un provider
    # hors registre (data-driven : le credential définit le bridge).
    is_remote = (c is not None and c.kind == "remote") or (c is None and bool(base_url))
    if is_remote:
        if not base_url:
            return None, "base_url_required"
        return {"base_url": base_url.rstrip("/")}, None
    if provider not in ORG_SHAREABLE_PROVIDERS:
        return None, "provider_not_shareable"
    if base_url:
        return None, "base_url_not_allowed"
    return None, None


def public_catalog() -> list[dict]:
    """Vue publique (GET /api/connectors) — sans secret, pour le frontend."""
    return [
        {
            "name": c.name,
            "label": c.label,
            "help": c.help,
            "href": c.href,
            "publisher": c.publisher_name,   # éditeur (curé) — catalogue
            "logo_url": c.logo_url_for(),     # logo éditeur (oto-media), None si absent
            "availability": c.availability,
            "auth_modes": sorted(c.auth_modes),
            "personal_session": c.personal_session,
            "secret_kind": c.secret_kind,
            "namespaces": list(c.namespaces),
            "family": c.family,        # axe builder (dérivé) — ADR 0011
            "category": c.category,    # axe utilisateur (curé) — ADR 0011
            # Schéma de saisie du credential (modèle générique multi-champs) — le
            # dashboard rend le formulaire en bouclant dessus. Jamais de valeur,
            # juste la forme (name/label/secret).
            "credential_fields": [
                {"name": f.name, "label": f.label, "secret": f.secret}
                for f in c.secret_fields
            ],
        }
        for c in _REGISTRY_LIST
    ]
