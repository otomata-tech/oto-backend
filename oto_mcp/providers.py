"""Registre des connecteurs — SOURCE UNIQUE de vérité.

Module pur (aucun import oto_mcp, comme tool_visibility.py). Remplace les 4
listes en dur qui dérivaient (`db.KEY_PROVIDERS`, `access.ORG_SHAREABLE_PROVIDERS`,
`tool_visibility.ADMIN_GRANT_ONLY_NAMESPACES`, le `PROVIDERS` du frontend) plus
`_QUOTA_DEFAULTS`. Tout en dérive.

Chaque connecteur porte les 3 axes du modèle plateforme :
- **A. Disponibilité** : `availability` (self_serve | platform_granted). platform_granted
  = grant-only (la plateforme accorde explicitement, ex. `mm` réservé à un client).
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
    # False = champ facultatif (connecteur « ET/OU » type slack : au moins un
    # champ non vide exigé à la pose, mais aucun champ individuellement requis).
    required: bool = True
    # False (défaut) = les whitespace n'ont aucun sens dans la valeur (clés, tokens,
    # ids) → nettoyés à la pose (parasites d'un copier-coller). True = l'espace est
    # significatif (mot de passe) → strip des bords seul. Cf.
    # credentials_store.clean_field_value.
    whitespace_significant: bool = False


@dataclass(frozen=True)
class Connector:
    name: str                          # identité = clé de credential
    namespaces: tuple[str, ...]        # préfixes de tools possédés
    availability: str                  # "self_serve" | "platform_granted"
    auth_modes: frozenset              # ⊆ {"byo_user","byo_org","platform"}
    keyed: bool                        # résolu via resolve_api_key (→ KEY_PROVIDERS)
    personal_session: bool             # catégorie « session navigateur » (Live View
                                       # Browserbase) côté UI — ORTHOGONAL au partage :
                                       # le niveau (user/équipe/org) suit `auth_modes`
                                       # (`byo_org` ⇒ session partageable, ex. pennylaneged)
    secret_kind: str                   # api_key|refresh_token|oauth|cookie|none
    default_quota: int                 # 0 = illimité
    in_default_bundle: bool            # axe A : accordé d'office (bundle par défaut)
    in_default_preset: bool            # axe B : affiché+activé par le preset de base
    default_hidden: bool = False       # axe B : namespaces masqués par défaut mais
                                       # self-activables (oto_enable_tool) — découvrabilité,
                                       # pas sécurité (≠ platform_granted)
    platform_key_open: bool = False    # free-tier : clé plateforme utilisable SANS grant
                                       # (quota gratuit = default_quota par user/jour, ADR 0031)
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
    # Préfixe à retirer du NOM des tools distants avant le préfixe de namespace
    # (kind="mount"). Évite la redondance quand le MCP distant préfixe déjà ses
    # tools d'un mot proche du namespace oto — ex. folkmcp : distant `folk_*`
    # monté `folkmcp_*` (strip="folk_") au lieu de `folkmcp_folk_*`. Le forward
    # vers le distant garde le nom d'origine (ProxyTool). None = pas de strip.
    mount_strip_prefix: str | None = None
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
    # Auth « hébergée » (ADR 0024) : le credential est une clé (resolve_api_key,
    # cascade inchangée), MAIS la connexion user-facing passe par un flux hébergé
    # tiers (ex. unipile : l'org pose l'abonnement, chaque membre lie son compte
    # LinkedIn/WhatsApp par hosted-auth) — pas un formulaire de clé. Posé ici, le
    # descripteur `auth.method` vaut "hosted" → la carte rend le widget dédié sans
    # cas par nom côté front.
    hosted_auth: bool = False

    @property
    def org_shareable(self) -> bool:
        return "byo_org" in self.auth_modes

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
    def doc_sections(self) -> tuple:
        """Sections de doc « how-to » (CURÉ, contenu dans `connector_docs.py`) —
        dérivé par nom. Lazy import : garde ce module pur au niveau module."""
        from .connector_docs import DOC_SECTIONS
        return DOC_SECTIONS.get(self.name, ())

    @property
    def description(self) -> str:
        """Description user-facing 2-3 phrases (CURÉE, `_DESCRIPTION_BY_CONNECTOR`).
        Vide si non rédigée — le front retombe alors sur `help`."""
        return _DESCRIPTION_BY_CONNECTOR.get(self.name, "")

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
    def auth_method(self) -> str:
        """Mécanisme d'obtention du credential (ADR 0024) — DÉRIVÉ. Pilote le
        widget rendu par la `ConnectorCard` (un flux, une carte). Priorité :
        `hosted` (flux hébergé tiers, ex. unipile) > `remote` (bridge ADR 0003,
        posé par grant d'org) > `oauth`/`cookie`/`none` (flux dédiés / pas de
        credential) > `secret` (champ(s) à coller : api_key, basic_auth, fields,
        refresh_token). NB : un MCP fédéré (kind=mount) hérite de son `secret_kind`
        (planity=basic_auth→secret, memento=oauth→oauth)."""
        if self.hosted_auth:
            return "hosted"
        if self.kind == "remote" and not self.credential_fields:
            # Bridge legacy (ADR 0003) : credential posé par grant d'org, pas de
            # formulaire. Un bridge NOUVEAU modèle (ADR 0034) déclare ses
            # credential_fields → formulaire self-serve standard (method=secret).
            return "remote"
        if self.secret_kind in ("oauth", "cookie", "none"):
            return self.secret_kind
        return "secret"

    @property
    def auth_multi_account(self) -> bool:
        """Le credential est-il multi-compte — N grants pour une même entité
        (ADR 0024) ? Aujourd'hui seul Google (N comptes OAuth liés)."""
        return self.name in MULTI_ACCOUNT_PROVIDERS

    @property
    def auth(self) -> dict:
        """Descripteur d'auth unifié (ADR 0024) — source unique du rendu de la
        face credential, quel que soit le mécanisme. `fields` = schéma de saisie
        (vide hors `method=secret`, où les flux sont dédiés)."""
        return {
            "method": self.auth_method,
            "cardinality": "multi_account" if self.auth_multi_account else "single",
            "fields": [
                {"name": f.name, "label": f.label, "secret": f.secret,
                 "required": f.required, "help": f.help}
                for f in self.secret_fields
            ],
        }

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
                    CredentialField("password", "Mot de passe", secret=True,
                                    whitespace_significant=True))
        return ()

    @property
    def config_fields(self) -> tuple[CredentialField, ...]:
        """Champs NON-secrets du credential (endpoint/host/region : `base_url`
        n8n/make, `data_center` zoho, `org_id` zohodesk…). Dérivés de `secret_fields`
        (flag `secret=False`) — la config voyage avec la clé via `resolve_credential`
        (le `meta` non-secret, ex. `dsn` unipile, s'y ajoute à la résolution)."""
        return tuple(f for f in self.secret_fields if not f.secret)


# Connecteurs passant par un browser IN-PROCESS (o-browser local) — non dérivable
# du seul secret_kind. Vide depuis la migration de crunchbase sur le substrat
# HÉBERGÉ Browserbase (ADR 0026) : crunchbase appelle désormais l'API privée
# `/v4/data` via une session navigateur distante (family dérivée → "api", comme
# brevo). LinkedIn était déjà parti vers Unipile. Mécanisme conservé pour un
# éventuel futur connecteur browser local.
BROWSER_PROVIDERS = frozenset()

# Connecteurs dont le credential est MULTI-COMPTE — N grants liés à une même
# entité (ADR 0024). Aujourd'hui seul Google (N comptes OAuth) ; les autres
# sessions/oauth (crunchbase, memento…) restent mono-compte par entité.
MULTI_ACCOUNT_PROVIDERS = frozenset({"google", "zoho"})

# Catégorie d'usage (domaine) par connecteur — CURÉE (pas dérivable), tunable.
_CATEGORY_BY_CONNECTOR = {
    "serper": "Prospection", "hunter": "Prospection", "kaspr": "Prospection",
    "fullenrich": "Prospection", "lemlist": "Prospection", "attio": "Prospection",
    "folk": "Prospection", "crunchbase": "Prospection",
    "unipile": "Prospection", "topograph": "Prospection",
    "sirene": "Data FR", "culture": "Data FR", "droit": "Data FR",
    "foncier": "Data FR", "sante": "Data FR", "frenchtech": "Data FR", "gr": "Data GR",
    "reddit": "Web",
    "infosec": "Infosec",
    "pennylane": "Finance", "pennylaneged": "Finance", "gocardless": "Finance", "silae": "Finance",
    "slack": "Comms", "google": "Comms", "zohodesk": "Comms",
    "memento": "Knowledge", "notion": "Knowledge", "zohoanalytics": "Knowledge",
    "planity": "Métier",
    "atlassian": "Métier",
    "hubspot": "Prospection", "apollo": "Prospection", "zerobounce": "Prospection",
    "hithorizons": "Prospection", "phantombuster": "Prospection", "zoho": "Prospection",
    "brevo": "Prospection",
    "figma": "Design", "supabase": "Dev",
    # recherche web / scraping
    "aiark": "Prospection",
    "serpapi": "Prospection", "searchapi": "Prospection", "brightdata": "Prospection", "cloro": "Prospection",
    # ATS / talent sourcing (RH)
    "greenhouse": "Recrutement", "lever": "Recrutement", "ashby": "Recrutement",
    "recruitee": "Recrutement", "teamtailor": "Recrutement",
    # automatisation no-code (workflows)
    "n8n": "Automatisation", "make": "Automatisation", "zapier": "Automatisation",
    "brevoauto": "Automatisation",
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
    "memento": "Memento", "planity": "Planity", "atlassian": "Atlassian",
    "hubspot": "HubSpot", "apollo": "Apollo", "zerobounce": "ZeroBounce",
    "hithorizons": "HitHorizons", "phantombuster": "Phantombuster",
    "notion": "Notion", "figma": "Figma", "supabase": "Supabase",
    "zoho": "Zoho", "zohodesk": "Zoho", "zohoanalytics": "Zoho",
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "aiark": "AI Ark",
    "recruitee": "Recruitee", "teamtailor": "Teamtailor", "serpapi": "SerpApi",
    "searchapi": "SearchApi", "brightdata": "Bright Data", "cloro": "Cloro",
    "n8n": "n8n", "make": "Make", "zapier": "Zapier",
    # open-data FR → éditeur = la source publique
    "sirene": "INSEE", "culture": "Ministère de la Culture",
    "droit": "Légifrance / DILA",
    "reddit": "Reddit",
    "foncier": "État (open data)", "sante": "HAS / FINESS",
    "frenchtech": "La French Tech (open data)",
    "infosec": "Otomata (OSINT)",
    # open-data GR → éditeur = la source publique
    "gr": "GEMI / VIES",
}

# Description user-facing (2-3 phrases) par connecteur — CURÉE, affichée sur la
# carte catalogue du dashboard (le front retombe sur `help` si absente). Décrire
# ce que le connecteur couvre concrètement, pas de superlatifs. Premier lot =
# les données France (le différenciateur du catalogue) ; compléter au fil de l'eau.
_DESCRIPTION_BY_CONNECTOR = {
    "sirene": (
        "Les données d'entreprise françaises unifiées : recherche multicritère, "
        "fiche agrégée (identité + bilans INPI + événements BODACC), dirigeants, "
        "marchés publics BOAMP, accords d'entreprise. Inclut le stock SIRENE "
        "complet (~43 M d'établissements) pour le batch : sièges, établissements, "
        "recherche NAF/commune."
    ),
    "droit": (
        "L'information légale française : jurisprudence (Cour de cassation, "
        "Conseil d'État, Conseil constitutionnel, CEDH/CJUE), codes consolidés "
        "versionnés (texte en vigueur à une date) et conventions collectives de "
        "branche (KALI). Sources DILA/Légifrance."
    ),
    "culture": (
        "Les entreprises du spectacle vivant, en open data du Ministère de la "
        "Culture : recherche multicritère, fiches détaillées, statistiques "
        "sectorielles et export."
    ),
    "foncier": (
        "Le foncier et l'immobilier français en open data : géocodage BAN, "
        "parcelles cadastrales, bâti, transactions DVF (prix au m², comparables "
        "par adresse), risques et ICPE, DPE, consommation électrique et "
        "productible solaire."
    ),
    "urba": (
        "L'urbanisme réglementaire en open data : zonage PLU/GPU et règlements, "
        "risques naturels, argiles, QPV et proximité, EPFIF, socio-démographie "
        "communale."
    ),
    "sante": (
        "Les établissements de santé et médico-sociaux français : répertoire "
        "FINESS complet et évaluations ESSMS de la HAS, avec recherche "
        "multicritère."
    ),
    "frenchtech": (
        "L'écosystème d'une capitale French Tech (défaut Aix-Marseille) : "
        "annuaire des startups, structures et prestataires, événements, appels "
        "à projets, financements et French Tech Central."
    ),
    "infosec": (
        "L'empreinte numérique d'un domaine, en reconnaissance passive : "
        "WHOIS/RDAP, DNS, posture e-mail (SPF/DMARC), sous-domaines via "
        "Certificate Transparency, TLS et headers de sécurité."
    ),
}

# Domaine de marque curé par connecteur → le CDN logo.dev en dérive l'URL du logo
# (cf. `logo_url_for`). Domaine RACINE (pas les `app.*` ni sous-domaines MCP). Les
# connecteurs absents n'ont pas de marque produit → pas de logo → monogramme côté
# UI. Les sources d'État (`culture`/`foncier`/`urba`/`frenchtech`) résolvent vers
# la Marianne via leur domaine .gouv.fr — identité visuelle commune « Data FR ».
_LOGO_DOMAIN_BY_CONNECTOR = {
    "serper": "serper.dev", "hunter": "hunter.io", "kaspr": "kaspr.io",
    "fullenrich": "fullenrich.com", "lemlist": "lemlist.com", "folk": "folk.app",
    "unipile": "unipile.com", "pennylane": "pennylane.com", "pennylaneged": "pennylane.com", "gocardless": "gocardless.com",
    "silae": "silae.fr", "attio": "attio.com", "crunchbase": "crunchbase.com",
    "brevo": "brevo.com", "brevoauto": "brevo.com",
    "slack": "slack.com", "whatsapp": "whatsapp.com", "google": "google.com",
    "memento": "mento.cc", "planity": "planity.com", "topograph": "topograph.co",
    "atlassian": "atlassian.com",
    "sirene": "insee.fr", "droit": "legifrance.gouv.fr",
    "culture": "culture.gouv.fr", "foncier": "data.gouv.fr",
    "urba": "geoportail-urbanisme.gouv.fr", "sante": "has-sante.fr",
    "frenchtech": "lafrenchtech.com",
    "greenhouse": "greenhouse.io", "lever": "lever.co", "ashby": "ashbyhq.com",
    "recruitee": "recruitee.com", "teamtailor": "teamtailor.com",
    "serpapi": "serpapi.com", "searchapi": "searchapi.io", "brightdata": "brightdata.com", "cloro": "cloro.dev",
    "aiark": "ai-ark.com",
    "n8n": "n8n.io", "make": "make.com", "zapier": "zapier.com",
    "reddit": "reddit.com",
}


def _c(name, namespaces, *, availability="self_serve", auth_modes=(), keyed=False,
       personal_session=False, secret_kind="none",
       default_quota=0, in_default_bundle=True, in_default_preset=False,
       default_hidden=False, platform_key_open=False, label="", help="", href=None,
       publisher="", logo_url=None, kind="tools", mount_url=None,
       mount_strip_prefix=None,
       credential_fields=(), modules=(), hosted_auth=False) -> Connector:
    return Connector(
        name=name, namespaces=tuple(namespaces), availability=availability,
        auth_modes=frozenset(auth_modes), keyed=keyed, personal_session=personal_session,
        secret_kind=secret_kind, default_quota=default_quota,
        in_default_bundle=in_default_bundle, in_default_preset=in_default_preset,
        default_hidden=default_hidden, platform_key_open=platform_key_open,
        label=label or name.capitalize(), help=help, href=href,
        publisher=publisher, logo_url=logo_url, kind=kind,
        mount_url=mount_url, mount_strip_prefix=mount_strip_prefix,
        credential_fields=tuple(credential_fields),
        modules=tuple(modules), hosted_auth=hosted_auth,
    )


# Ordre des connecteurs `keyed` = ordre EXACT de l'ancien KEY_PROVIDERS
# (status_for itère dessus, l'affichage en dépend). Ne pas réordonner.
# (slack est sorti du modèle keyed le 2026-06-30 → fields multi-champs, #25.)
_REGISTRY_LIST = [
    # --- keyed (résolus via resolve_api_key, clé api per-user) ---------------
    _c("serper", ["serper"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=200, platform_key_open=True,
       in_default_preset=True, label="Serper", help="recherche web", href="https://serper.dev"),
    _c("hunter", ["hunter"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=5, platform_key_open=True,
       in_default_preset=True, label="Hunter.io", help="emails", href="https://hunter.io"),
    # `fr` (APIs live SIRENE/Recherche Entreprises/INPI/BODACC/BOAMP) + `fr_stock`
    # (stock SIRENE parquet, ex-connecteur `sirene_stock`, fusionné 2026-06-22 :
    # même domaine entreprises FR, namespace fr_stock_* → namespace_of="fr").
    # default_quota=0 (illimité) : données entreprise FR ouvertes à tous, sans
    # crédits. La plupart des fr_* sont open-data/parquet (aucune clé) ; seuls
    # fr_siret/fr_avis_sirene/fr_headquarters touchent la clé INSEE partagée —
    # non métrée. Le seul plafond restant = le rate limit INSEE (30 req/min) sur
    # la clé partagée, remonté tel quel (429) sans throttle oto.
    _c("sirene", ["fr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=0, platform_key_open=True,
       in_default_preset=True, label="INSEE SIRENE", help="données entreprise FR",
       href="https://api.insee.fr", modules=("fr", "fr_stock")),
    # droit : jurisprudence (juris_*) + codes consolidés (loi_*) + conventions
    # collectives (ccn_*), servis par le service FOD (fod_juris/loi/ccn). Extrait
    # de `sirene`/`fr` (n'était pas de l'INSEE : DILA/Justice/Légifrance). Open
    # data, sans clé. 3 namespaces → 1 carte « Info légale FR ».
    _c("droit", ["juris", "loi", "ccn"], secret_kind="none", in_default_preset=True,
       label="Info légale FR",
       help="jurisprudence, codes consolidés, conventions collectives (open data DILA/Légifrance)",
       href="https://www.legifrance.gouv.fr", modules=("droit",)),
    # attio : masqué par défaut (2026-06-11) — le MCP Attio officiel est meilleur
    # pour l'instant. Code conservé (tools/attio.py) pour d'éventuelles implems
    # custom ; self-activable via oto_enable_tool.
    _c("attio", ["attio"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", default_quota=200,
       default_hidden=True, label="Attio", help="CRM", href="https://app.attio.com"),
    _c("lemlist", ["lemlist"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key",
       label="Lemlist", help="cold outreach", href="https://app.lemlist.com"),
    _c("kaspr", ["kaspr"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=5, platform_key_open=True,
       label="Kaspr", help="enrichissement", href="https://app.kaspr.io"),
    _c("pennylane", ["pennylane"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key",
       label="Pennylane", help="compta", href="https://app.pennylane.com"),
    # slack : messagerie. BYO 100% configurable par org/user (#25) — credential
    # MULTI-CHAMPS (bot token xoxb- ET/OU user token xoxp-, au moins un requis),
    # résolu via resolve_credential_fields (modèle silae/zoho, PAS keyed). byo_user
    # OU byo_org (un workspace partagé par l'org = son bot token). Le workspace est
    # implicite = celui des tokens posés. Fallback de lecture du credential legacy
    # (token unique pré-multichamps) dans tools/slack.py.
    _c("slack", ["slack"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
       personal_session=False, label="Slack",
       help="messagerie Slack (bot token xoxb- et/ou user token xoxp-)",
       href="https://slack.com", credential_fields=(
           CredentialField("bot_token", "Bot token (xoxb-)", secret=True,
                           required=False),
           CredentialField("user_token", "User token (xoxp-)", secret=True,
                           required=False),
       )),
    _c("fullenrich", ["fullenrich"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=5, platform_key_open=True,
       label="FullEnrich", help="enrichissement waterfall", href="https://app.fullenrich.com"),
    # folk : né APRÈS le coffre — pas de colonne legacy users.folk_api_key,
    # le coffre connector_credentials est canonique. byo-only (pas de clé
    # plateforme) ; compte partagé équipe = credential de l'org Otomata.
    _c("folk", ["folk"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key",
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
       secret_kind="api_key", hosted_auth=True,
       in_default_bundle=False, label="Messagerie hébergée (Unipile)",
       help="LinkedIn + WhatsApp + Telegram + Instagram + Messenger + X/Twitter hébergés (recherche/scrape/messagerie)",
       href="https://www.unipile.com",
       modules=("unipile", "whatsapp", "telegram", "instagram", "messenger", "twitter")),
    # topograph : KYB — données + documents normalisés de 100+ registres publics
    # européens via une seule API REST. byo-only (pay-per-request, chacun connecte
    # son compte ; clé d'org partageable), keyed api_key (en-tête x-api-key résolu
    # côté client). Pas de clé plateforme. Hors bundle par défaut : opt-in.
    _c("topograph", ["topograph"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key",
       in_default_bundle=False, label="Topograph",
       help="KYB — données & documents entreprise (registres européens)",
       href="https://www.topograph.co"),
    # resend : credential-only (PAS de tools propres). La clé Resend de l'org est
    # consommée par `email_send` (transport=resend) via resolve_api_key, cascade
    # user > org. Domaine d'envoi vérifié côté Resend par l'org ; l'adresse `from`
    # vit dans orgs.email_settings, pas dans le credential. default_hidden + hors
    # bundle (pas un tool à exposer). tools/resend.py = register() no-op pour
    # satisfaire l'invariant « un fichier tools/ par provider kind=tools ».
    # resend : email transactionnel BYOK (clé Resend de l'ORG). byo_org uniquement
    # (l'email est org-level) ; self_serve = dispo à la demande pour toute org. La
    # propriété du domaine est garantie par Resend (la clé ne peut envoyer que depuis
    # les domaines vérifiés dans le compte Resend de l'org) → zéro logique domaine côté oto.
    _c("resend", ["resend"], auth_modes={"byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, default_hidden=True,
       label="Resend", help="envoi d'email transactionnel (clé de l'org)",
       publisher="Resend", href="https://resend.com"),
    # scaleway : email transactionnel via le compte Scaleway TEM DE L'ORG (BYO, comme resend).
    # L'org amène sa clé (secret_key + project_id) ; l'API TEM n'envoie que depuis les domaines
    # VÉRIFIÉS dans le compte Scaleway de l'org → propriété du domaine garantie par Scaleway,
    # zéro logique domaine côté oto, plus d'override/activation (connecteur normal self-serve).
    # Config (expéditeurs + fenêtre calme) dans le panneau email de la carte connecteur ORG ;
    # email_send (spine) route sender→connecteur→transport.
    _c("scaleway", ["scaleway"], auth_modes={"byo_org"}, secret_kind="fields",
       in_default_bundle=False, default_hidden=True,
       label="Scaleway TEM (email)",
       help="envoi d'email transactionnel via ton compte Scaleway TEM (domaine vérifié chez Scaleway)",
       publisher="Scaleway", href="https://www.scaleway.com/en/transactional-email-tem/",
       credential_fields=(
           CredentialField("secret_key", "Clé secrète Scaleway (X-Auth-Token)", secret=True, reveal=True),
           CredentialField("project_id", "Project ID Scaleway", secret=False, reveal=True),
           CredentialField("region", "Région TEM (déf. fr-par)", secret=False, reveal=True),
       )),

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
    # y pose le token de son compte de service pour le POC avoirs (doctrine d'une org client).
    _c("gocardless", ["gocardless"], availability="self_serve",
       auth_modes={"byo_user", "byo_org"}, keyed=True, secret_kind="api_key", in_default_bundle=False,
       label="GoCardless", help="prélèvements SEPA (lecture)"),
    # (Ponts vers un service distant : voir l'entrée `bridge` universelle en fin
    # de registre — ADR 0034. L'ex-modèle remote data-driven per-namespace,
    # découvert de `meta.base_url` sans entrée au registre, a été retiré en B4 ;
    # l'identité client vit dans la CONFIG d'org du bridge, jamais en dur.)
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
    # atlassian : MCP fédéré (kind=mount, #40). Le Rovo Remote MCP d'Atlassian
    # (mcp.atlassian.com/v1/mcp, Jira+Confluence) a son propre AS OAuth 2.1 + DCR +
    # PKCE ; client PUBLIC (token_endpoint_auth_method=none, pas de secret), flow web
    # per-user dans atlassian_oauth.py. Le cloudid/site est résolu par l'AS Atlassian.
    # Inerte tant que `atlassian` n'est pas dans OTO_MCP_MOUNTS_ENABLED (défaut =
    # memento seul) ET que ATLASSIAN_OAUTH_CLIENT_ID n'est pas posé.
    _c("atlassian", ["atlassian"], kind="mount",
       mount_url="https://mcp.atlassian.com/v1/mcp",
       auth_modes={"byo_user"}, secret_kind="oauth",
       in_default_bundle=False, label="Atlassian",
       help="Jira / Confluence (MCP fédéré)", href="https://atlassian.com"),
    # folkmcp : MCP OFFICIEL de Folk (kind=mount, #85), COEXISTANT avec le
    # connecteur natif `folk` (clé API REST). Namespace distinct `folkmcp` ; le MCP
    # distant préfixe déjà ses tools `folk_*` → `mount_strip_prefix="folk_"` évite
    # le double `folkmcp_folk_*` : les tools montés sont `folkmcp_*` (le forward
    # garde le nom d'origine). Pas de collision avec le natif `folk_*`.
    # AS = Stytch (app.folk.app/oauth/authorize + api.stytch.folk.app), client
    # PUBLIC + DCR + PKCE, flow web per-user dans folk_oauth.py. Le MCP Folk s'auth
    # UNIQUEMENT par OAuth (pas de clé). Inerte tant que `folkmcp` n'est pas dans
    # OTO_MCP_MOUNTS_ENABLED (défaut = memento seul). Coexistence gérée par la
    # visibilité per-user (ADR 0011/0031) : un user voit soit `folk`, soit `folkmcp`.
    _c("folkmcp", ["folkmcp"], kind="mount",
       mount_url="https://mcp.folk.app/mcp", mount_strip_prefix="folk_",
       auth_modes={"byo_user"}, secret_kind="oauth",
       in_default_bundle=False, label="Folk (MCP)",
       help="CRM Folk via son MCP officiel (fédéré, OAuth per-user)",
       href="https://folk.app"),
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
    # justicelibre : MCP fédéré (kind=mount) SANS auth — endpoint hébergé PUBLIC
    # (justicelibre.org/mcp, Streamable HTTP, aucune clé ni compte ; MIT + Licence
    # Ouverte Etalab 2.0). Droit français & européen : législation (LEGI/JORF/KALI)
    # + jurisprudence (Cass/Judilibre, Conseil d'État, Conseil constitutionnel,
    # CEDH, CJUE, CNIL). `auth_modes` VIDE = **mount no-auth** : tools/mount.py fetch
    # le catalogue et forward SANS token per-user (chemin dédié, pas de
    # resolve_mount_token). Opt-in par org : master OFF au registre d'activation, une
    # org l'active via l'écran connector activation (le mount suit —
    # `_db_activated_mounts`). Hors bundle par défaut.
    _c("justicelibre", ["justicelibre"], kind="mount",
       mount_url="https://justicelibre.org/mcp",
       auth_modes=frozenset(), secret_kind="none", in_default_bundle=False,
       label="JusticeLibre",
       help="droit français & européen — législation + jurisprudence "
            "(Légifrance/Judilibre/CE/CC/CEDH/CJUE), MCP fédéré, sources ouvertes",
       href="https://justicelibre.org"),
    # aiark : connecteur classique (kind="tools", ex-mount fédéré #152 → requalifié
    # #160). Client REST synchrone dans oto-core (`oto.tools.aiark`), tools curés
    # dans `tools/aiark.py` (contrat LLM), cascade de clé standard
    # (`resolve_api_key`) + `record_platform_usage` → mode plateforme possible.
    # v1 = endpoints synchrones (company/people search, single-person export+email,
    # reverse-lookup, mobile phone) ; les exports en lot d'AI Ark sont async
    # (webhook) → hors périmètre.
    _c("aiark", ["aiark"],
       auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key",
       in_default_bundle=False, label="AI Ark",
       help="people & company search via LinkedIn",
       href="https://ai-ark.com"),

    # --- sessions per-user (hors resolve_api_key, stockage dédié) ------------
    # LinkedIn n'est plus un connecteur browser ici : remplacé par le connecteur
    # `unipile` (LinkedIn hébergé). Le browser LinkedIn local reste dans oto-cli.
    # crunchbase : fiches société/personne via l'API PRIVÉE du frontend
    # (`www.crunchbase.com/v4/data`, schéma v4 sans user_key). Exécution =
    # **Browserbase** (Chrome distant hébergé, ADR 0026) : l'user se logue 1× via
    # Live View (`crunchbase_connect_start`), sa session persiste dans un Context =
    # le credential per-user (coffre `crunchbase`). Plus de scraping DOM in-process.
    _c("crunchbase", ["crunchbase"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_bundle=False, label="Crunchbase",
       help="fiches société/personne (session Browserbase)", publisher="Crunchbase",
       href="https://www.crunchbase.com/"),
    # brevoauto : automations (workflows marketing) via l'API PRIVÉE de l'éditeur
    # (`workflow-apis.brevo.com/v1`). Connecteur SÉPARÉ du `brevo` keyé (API publique
    # v3, plus bas) car le credential diffère — session navigateur ici, clé API là ;
    # même éditeur, deux surfaces disjointes (la clé v3 n'ouvre pas l'authoring
    # d'automations). Même partition que pennylane / pennylaneged.
    # Exécution = **Browserbase** (Chrome distant hébergé) : l'user se logue 1× via
    # Live View (`brevoauto_connect_start`), sa session persiste dans un Context = le
    # credential per-user (coffre). Pas de browser sur la box, pas d'export de cookie.
    # personal_session (session physiologiquement per-user). Expérimental (API non
    # documentée) : hors bundle + masqué, self-activable.
    _c("brevoauto", ["brevoauto"], auth_modes={"byo_user"}, personal_session=True,
       secret_kind="cookie", in_default_bundle=False, default_hidden=True,
       label="Brevo (automation)", help="automations marketing (session Browserbase)",
       publisher="Brevo", href="https://app.brevo.com/automation/automations"),
    # pennylaneged : GED (bac documentaire) Pennylane via l'API PRIVÉE de la SPA
    # (`app.pennylane.com/companies/{cid}/dms`, cookie + CSRF tournant). DISTINCT du
    # connecteur keyé `pennylane` (API publique) : credential = session navigateur,
    # pas une clé API → l'API publique ne porte aucun scope DMS. Exécution =
    # **Browserbase** : l'user se logue 1× via Live View (`pennylaneged_connect_start`),
    # sa session persiste dans un Context = le credential (coffre). Upload =
    # control plane ici (URL S3 présignée) + PUT des octets EN LOCAL (RGPD, issue #31).
    # Expérimental (API interne RE) : hors bundle + masqué, self-activable.
    # **byo_org** : la session peut être configurée au niveau USER, ÉQUIPE ou ORG
    # (cas cabinet : une seule connexion Pennylane partagée par la team pour pousser
    # dans les GED clients — cascade user > groupe > org). `personal_session=True`
    # reste = catégorie « session navigateur » côté UI (orthogonal au partage).
    _c("pennylaneged", ["pennylaneged"], auth_modes={"byo_user", "byo_org"},
       personal_session=True, secret_kind="cookie", in_default_bundle=False,
       default_hidden=True, label="Pennylane GED",
       help="bac documentaire Pennylane (session Browserbase)",
       publisher="Pennylane", href="https://app.pennylane.com"),
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
    # Deux sources publiques SANS RAPPORT → deux connecteurs distincts (ex-`fr_open`
    # qui les fusionnait : un sac « open data » incohérent, activer l'un activait
    # l'autre). namespace = préfixe réel : culture_spectacle_* → `culture`
    # (namespace_of = 1er token), reddit_* → `reddit`. Déclarer "culture", PAS
    # "culture_spectacle" (jamais matché → fail-open du gate, #24).
    _c("culture", ["culture"], secret_kind="none", in_default_preset=True,
       label="Culture (open data)",
       help="entreprises du spectacle vivant — open data Ministère de la Culture"),
    _c("reddit", ["reddit"], secret_kind="none", in_default_preset=True,
       label="Reddit", help="recherche & lecture de posts/subreddits (API publique)"),
    # Grèce : lookup entité via registre GEMI (autocomplete) + VIES. Open data,
    # sans clé. Inerte tant que non activé en DB (deny-by-default), comme foncier/sante.
    _c("gr", ["gr"], secret_kind="none", in_default_bundle=False,
       label="Data GR", help="entreprises Grèce — registre GEMI + VIES (open data)"),
    # foncier / sante : connecteurs open-data déclarés (ADR 0010). Inertes tant
    # que non activés en DB (connector_activation) — register_all gate dessus,
    # donc absents du seed initial → OFF par défaut (deny-by-default).
    _c("foncier", ["foncier"], secret_kind="none", in_default_bundle=False,
       label="Foncier", help="géocodage, cadastre, bâti, risques/ICPE, solaire, immobilier (open data)"),
    _c("urba", ["urba"], secret_kind="none", in_default_bundle=False,
       label="Urbanisme", help="zonage PLU/GPU, risques, QPV, EPFIF, socio-démo commune (open data)"),
    _c("sante", ["sante"], secret_kind="none", in_default_bundle=False,
       label="Santé", help="établissements FINESS + évaluations ESSMS HAS (open data)"),
    _c("osm", ["osm"], secret_kind="none", in_default_bundle=False,
       label="OpenStreetMap", help="points d'intérêt OSM par tag sur une zone (parkings, équipements, commerces) — recensement exhaustif via Overpass (open data)"),
    _c("frenchtech", ["frenchtech"], secret_kind="none", in_default_bundle=False,
       label="French Tech", help="annuaire écosystème d'une capitale French Tech (startups/structures/prestataires) + événements, appels à projet, financements + French Tech Central (open data, défaut Aix-Marseille)"),
    # infosec : recon PASSIF d'un domaine (RDAP/DNS/CT/TLS/headers, OSINT, sans clé).
    # Complète fr_* (identité légale) par l'empreinte numérique. Pas de scan intrusif.
    _c("infosec", ["infosec"], secret_kind="none", in_default_bundle=False,
       label="Infosec", help="empreinte numérique d'un domaine : whois/RDAP, DNS, posture e-mail (SPF/DMARC), sous-domaines (CT), TLS, headers de sécurité (recon passif)"),

    # --- connecteurs API tiers (clients oto-core déjà écrits, câblés 2026-06-19) ---
    # byo keyed api_key, hors bundle (opt-in, activables par org/admin), pas de
    # clé plateforme (chacun pose la sienne). Inertes tant que non activés en DB
    # (connector_activation, deny-by-default), comme foncier/sante.
    _c("hubspot", ["hubspot"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="HubSpot",
       help="CRM (contacts, companies, deals, tickets, notes)",
       href="https://app.hubspot.com"),
    # brevo : API PUBLIQUE v3 (`api.brevo.com/v3`, header `api-key`). Une clé porte
    # tout le compte (pas de scope) → byo. Ne PAS confondre avec `brevoauto`
    # (automations, session navigateur) : surfaces disjointes, credentials distincts.
    _c("brevo", ["brevo"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Brevo",
       help="emailing & CRM (contacts, listes, transactionnel, campagnes, deals)",
       publisher="Brevo", href="https://app.brevo.com",
       # 2 modules, 1 namespace : le CRM natif est un sous-domaine distinct, sorti
       # pour tenir la taille de fichier. `brevo_crm_*` → namespace_of = `brevo`.
       modules=("brevo", "brevo_crm")),
    _c("apollo", ["apollo"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=20, platform_key_open=True,
       in_default_bundle=False, label="Apollo.io",
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
    # comme silae), résolu via resolve_credential_fields. byo_user OU byo_org
    # (zoho : clé d'org/groupe partageable — équipe sales partage un self-client).
    # `data_center` (non-secret) sélectionne la région Zoho (com/eu/in…).
    _c("zoho", ["zoho"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
       in_default_bundle=False, label="Zoho CRM",
       help="CRM Zoho (CRUD modules, notes)", href="https://crm.zoho.com",
       credential_fields=(
           CredentialField("client_id", "Client ID", secret=True,
                           help="1000.XXXXXXXX… (self-client)"),
           CredentialField("client_secret", "Client Secret", secret=True,
                           help="secret du self-client"),
           CredentialField("refresh_token", "Refresh Token", secret=True,
                           help="1000.xxxxx.yyyyy"),
           CredentialField("data_center", "Data center (com, eu, in, au, jp, ca)",
                           secret=False, reveal=True, help="eu"),
       )),
    _c("zohodesk", ["zohodesk"], auth_modes={"byo_user"}, secret_kind="fields",
       in_default_bundle=False, label="Zoho Desk",
       help="support Zoho Desk (tickets, threads, contacts)",
       href="https://desk.zoho.com", credential_fields=(
           CredentialField("client_id", "Client ID", secret=True,
                           help="1000.XXXXXXXX… (self-client)"),
           CredentialField("client_secret", "Client Secret", secret=True,
                           help="secret du self-client"),
           CredentialField("refresh_token", "Refresh Token", secret=True,
                           help="1000.xxxxx.yyyyy"),
           CredentialField("org_id", "Org ID", secret=False, help="ex. 800123456"),
       )),
    _c("zohoanalytics", ["zohoanalytics"], auth_modes={"byo_user", "byo_org"},
       secret_kind="fields", in_default_bundle=False, label="Zoho Analytics",
       help="Zoho Analytics (workspaces, vues, export, requêtes SQL)",
       href="https://analytics.zoho.com", credential_fields=(
           CredentialField("client_id", "Client ID", secret=True),
           CredentialField("client_secret", "Client Secret", secret=True),
           CredentialField("refresh_token", "Refresh Token", secret=True),
           CredentialField("org_id", "Org ID", secret=False),
           CredentialField("data_center", "Data center (com, eu, in, au, jp, ca, sa)",
                           secret=False, reveal=True),
       )),

    # --- ATS / talent sourcing (RH) — câblés 2026-06-20 ----------------------
    # Connecteurs de recrutement (Applicant Tracking Systems). byo keyed api_key
    # (chacun pose sa clé Harvest/API key, cascade user > org), hors bundle (opt-in,
    # activables par org/admin). Inertes tant que non activés en DB (deny-by-default,
    # comme hubspot/apollo). Recruitee = credential à 2 champs (token + company id)
    # → resolve_credential_fields, pas keyed.
    _c("greenhouse", ["greenhouse"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Greenhouse",
       help="ATS — candidats, jobs, candidatures, notes (Harvest API)",
       href="https://www.greenhouse.io"),
    _c("lever", ["lever"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Lever",
       help="ATS — opportunities (candidats), postings, stages, notes",
       href="https://www.lever.co"),
    _c("ashby", ["ashby"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Ashby",
       help="ATS — candidates, jobs, applications, notes",
       href="https://www.ashbyhq.com"),
    _c("teamtailor", ["teamtailor"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Teamtailor",
       help="ATS — candidats, jobs, candidatures (JSON:API)",
       href="https://www.teamtailor.com"),
    _c("recruitee", ["recruitee"], auth_modes={"byo_user"}, secret_kind="fields",
       in_default_bundle=False, label="Recruitee",
       help="ATS — candidats, offers (postes), notes",
       href="https://www.recruitee.com", credential_fields=(
           CredentialField("api_token", "API token", secret=True),
           CredentialField("company_id", "Company ID", secret=False),
       )),
    # serpapi : recherche multi-moteurs (scope complet — tous les verticaux Google
    # + Bing/YouTube/Walmart/Amazon/eBay/… + Google Jobs). keyed api_key, platform-
    # eligible (clé plateforme + quota daily, comme serper).
    _c("serpapi", ["serpapi"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=200, platform_key_open=True,
       in_default_bundle=False, label="SerpApi",
       help="recherche multi-moteurs (Google verticals, Bing, YouTube, Walmart, Amazon, jobs…)",
       href="https://serpapi.com"),
    # searchapi : recherche multi-moteurs via SearchApi.io (verticaux Google +
    # YouTube/Bing/Amazon/… + jobs/news/maps/scholar). keyed api_key, platform-
    # eligible (clé plateforme + quota daily, comme serper/serpapi). Client HTTP
    # auto-contenu (pas de dép oto-core).
    _c("searchapi", ["searchapi"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=200, platform_key_open=True,
       in_default_bundle=False, label="SearchApi",
       help="recherche multi-moteurs (Google verticals, YouTube, Bing, jobs, news, maps, scholar…)",
       href="https://www.searchapi.io"),
    # brightdata : scraping & SERP via réseau proxy Bright Data. COQUILLE VIDE —
    # connecteur câblé (clé platform + quota) mais produits (SERP/Unlocker/Datasets)
    # pas encore implémentés (tools/brightdata.py n'expose aucun tool pour l'instant).
    _c("brightdata", ["brightdata"], auth_modes={"byo_user", "byo_org", "platform"},
       keyed=True, secret_kind="api_key",
       default_quota=50, in_default_bundle=False, label="Bright Data",
       help="scraping & SERP via proxy (coquille vide — à implémenter)",
       href="https://brightdata.com"),
    # cloro : veille AI-search (ChatGPT/Gemini/Perplexity/Copilot/Grok/AI Mode) +
    # SERP Google en JSON. keyed api_key, platform-eligible (clé + quota daily).
    _c("cloro", ["cloro"], auth_modes={"byo_user", "byo_org", "platform"}, keyed=True,
       secret_kind="api_key", default_quota=50,
       in_default_bundle=False, label="Cloro",
       help="veille AI-search (ChatGPT, Gemini, Perplexity…) + SERP Google JSON",
       href="https://cloro.dev"),

    # --- automatisation de workflows (no-code) — câblés 2026-06-21 -----------
    # Connecteurs vers les plateformes d'automatisation tierces. byo, hors bundle
    # (opt-in, activables par org/admin), pas de clé plateforme (chacun pose la
    # sienne). Inertes tant que non activés en DB (deny-by-default, comme hubspot).
    # n8n / make : credential à 2 champs (clé + base URL de l'instance/zone —
    # self-hosting & régionalisation imposent une URL propre) → secret_kind="fields",
    # résolu via resolve_credential_fields. zapier : clé simple (AI Actions API),
    # keyed → resolve_api_key.
    _c("n8n", ["n8n"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
       in_default_bundle=False, label="n8n",
       help="automatisation de workflows — workflows + exécutions (API publique)",
       href="https://n8n.io", credential_fields=(
           CredentialField("api_key", "API key", secret=True),
           CredentialField("base_url", "Instance URL", secret=False,
                           help="ex. https://acme.app.n8n.cloud"),
       )),
    _c("make", ["make"], auth_modes={"byo_user", "byo_org"}, secret_kind="fields",
       in_default_bundle=False, label="Make",
       help="automatisation de workflows — scénarios, exécution, logs (API v2)",
       href="https://www.make.com", credential_fields=(
           CredentialField("api_token", "API token", secret=True),
           CredentialField("base_url", "Zone URL", secret=False,
                           help="ex. https://eu1.make.com ou https://us1.make.com"),
       )),
    _c("zapier", ["zapier"], auth_modes={"byo_user", "byo_org"}, keyed=True,
       secret_kind="api_key", in_default_bundle=False, label="Zapier",
       help="automatisation — actions exposées (AI Actions) + exécution",
       href="https://actions.zapier.com"),

    # --- connecteur http générique (secret DANS le coffre oto) ----------------
    # Client HTTP multi-auth : contrairement au bridge, oto DÉTIENT le secret de
    # l'API cible (coffre AES, byo_org) et tape l'API directement (pas de service
    # distant). `auth_mode` discrimine le mode (bearer/header/query/basic/oauth2/
    # none) ; les champs secrets requis dépendent du mode (validés au call-time par
    # oto_http.build_auth). Lecture seule (GET), garde-fou anti-SSRF sur l'hôte.
    # À DISTINGUER du bridge (credential hors plateforme) : ici la clé est confiée
    # à oto — pas de custody côté client.
    _c("http", ["http"], auth_modes={"byo_org"}, secret_kind="fields",
       in_default_bundle=False, default_hidden=True, label="HTTP",
       help="connecte n'importe quelle API HTTP à oto : renseigne l'URL de base, "
            "le mode d'auth (bearer / clé en header ou query / basic / oauth2) et "
            "le secret correspondant. oto stocke le secret (coffre chiffré) et tape "
            "l'API directement, en lecture seule (GET).",
       credential_fields=(
           CredentialField("base_url", "URL de base", secret=False, reveal=True,
                           help="racine HTTPS de l'API (ex. https://api.acme.com)"),
           CredentialField("auth_mode", "Mode d'auth", secret=False, reveal=True,
                           help="bearer | header | query | basic | oauth2 | none"),
           CredentialField("label", "Nom affiché", secret=False, reveal=True,
                           required=False, help="ex. « API Acme » — visible de ta seule org"),
           CredentialField("token", "Token / clé API", secret=True, required=False,
                           help="valeur du bearer, ou de la clé (modes header/query)"),
           CredentialField("header_name", "Nom du header", secret=False, reveal=True,
                           required=False, help="mode header (ex. x-api-key)"),
           CredentialField("query_param", "Nom du param", secret=False, reveal=True,
                           required=False, help="mode query (ex. api_key)"),
           CredentialField("username", "Utilisateur", secret=False, reveal=True,
                           required=False, help="mode basic"),
           CredentialField("password", "Mot de passe", secret=True, required=False,
                           whitespace_significant=True, help="mode basic"),
           CredentialField("token_url", "URL du token", secret=False, reveal=True,
                           required=False, help="mode oauth2 (endpoint client-credentials)"),
           CredentialField("client_id", "Client ID", secret=False, reveal=True,
                           required=False, help="mode oauth2"),
           CredentialField("client_secret", "Client secret", secret=True,
                           required=False, help="mode oauth2"),
           CredentialField("scope", "Scope", secret=False, reveal=True,
                           required=False, help="mode oauth2 (optionnel)"),
       )),

    # --- bridge universel (ADR 0034, amende 0003/0011) ------------------------
    # UN connecteur générique pour tout pont vers un middleware distant qui détient
    # le credential métier (bridge ADR 0003). L'identité du service ponté vit dans
    # la CONFIG d'org (base_url + label, privés) — jamais dans le namespace, donc
    # montrable au catalogue sans nom client. oto ne stocke que l'endpoint + le
    # token M2M. Tools bridge_describe/bridge_call (namespace fixe, barreau B2).
    _c("bridge", ["bridge"], kind="remote", auth_modes={"byo_org"},
       secret_kind="fields", in_default_bundle=False, default_hidden=True,
       label="Bridge",
       help="pont universel vers ton propre service distant (middleware) : le "
            "service détient tes credentials métier, oto ne stocke que son URL "
            "et un token d'accès",
       credential_fields=(
           CredentialField("base_url", "URL du bridge", secret=False, reveal=True,
                           help="endpoint HTTPS de ton service (ex. https://bridge.acme.com)"),
           CredentialField("token", "Token M2M", secret=True),
           CredentialField("label", "Nom affiché", secret=False, reveal=True,
                           help="ex. « Back-office Acme » — visible de ta seule org"),
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
# Providers pouvant DÉTENIR un credential per-membre dans le coffre — garde-fou d'écriture
# `db._check_provider`. Plus large que KEY_PROVIDERS (keyed seul) : inclut les **sessions
# navigateur** (secret_kind="cookie" : brevo/crunchbase/pennylaneged, qui persistent le
# Context Browserbase) et les connecteurs **byo multi-champs**. Sans ça, la persistance
# d'une session (ADR 0026/0033, `_persist`→`set_member_api_key`) levait « Unknown provider ».
CREDENTIAL_PROVIDERS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST
    if c.keyed or c.credential_fields or c.secret_kind != "none"
)
ORG_SHAREABLE_PROVIDERS: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.org_shareable)
QUOTA_DEFAULTS: dict = {c.name: c.default_quota for c in _REGISTRY_LIST if c.default_quota}
DEFAULT_BUNDLE: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.in_default_bundle)
DEFAULT_PRESET: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.in_default_preset)

# Connecteurs d'envoi d'email → transport effectif. Un expéditeur appartient à un
# connecteur (sa config vit dans orgs.email_settings keyé par connecteur) ; le
# transport en DÉRIVE. `email_send` (spine) route sender→connecteur→transport.
EMAIL_CONNECTOR_TRANSPORT: dict = {"scaleway": "scaleway", "resend": "resend"}
DEFAULT_HIDDEN_NAMESPACES: frozenset = frozenset(
    ns for c in _REGISTRY_LIST if c.default_hidden for ns in c.namespaces
)
REMOTE_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "remote")
MOUNT_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "mount")


# --- catalogue de namespaces présenté à l'agent (_SERVER_INSTRUCTIONS) -------
# DÉRIVÉ du registre (fini la liste écrite à la main qui dérivait — reddit/culture
# mentionnés, foncier/pennylane/apollo/sante… omis). Améliorer le blurb d'un
# namespace = éditer le `help` du connecteur (source unique : catalogue + carte +
# ce primer). Les concepts SPINE (hors registre connecteurs, chargés explicitement
# dans register_all, non gatés) sont déclarés ici car ils ne portent pas de
# `Connector` — datastore/facts/email/méta/boucle d'usage.
SPINE_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("data_*", "datastore tabulaire per-user (PG natif, schéma libre) — data_write/data_rows/data_share"),
    ("email_send", "envoi d'email per-org (transports scaleway/resend), différé + quiet-hours"),
    ("oto_*", "méta : visibilité des outils (enable/disable), doctrine d'org, orgs & équipes"),
    ("run_* / feedback", "boucle d'usage : run_start/run_finish encadrent un déroulé ; feedback(gap|tool_feedback) remonte les signaux"),
)


def _availability_tag(c: "Connector") -> str:
    """Annotation courte de disponibilité (pour ne pas faire croire qu'un namespace
    gaté/masqué est appelable d'office)."""
    bits: list[str] = []
    if c.hosted_auth:
        bits.append("compte à connecter")
    if c.default_hidden:
        bits.append("masqué — oto_enable_tool")
    elif not c.in_default_bundle and c.kind == "tools":
        bits.append("à activer selon ton org")
    return f" ({'; '.join(bits)})" if bits else ""


def render_namespace_catalog() -> str:
    """Le bloc « namespaces » des instructions serveur, dérivé du registre + spine.
    Une ligne par connecteur (ses namespaces groupés) + le bloc spine. Couvre TOUT
    le registre → pas d'omission. Les transports email pur-credential (scaleway/
    resend, aucun tool propre) sont présentés via le concept spine `email_send`."""
    lines: list[str] = []
    for c in _REGISTRY_LIST:
        if c.name in EMAIL_CONNECTOR_TRANSPORT:   # credential-only → couvert par email_send
            continue
        ns = " / ".join(f"{n}_*" for n in c.namespaces)
        desc = f"{c.label} : {c.help}" if c.help else c.label
        lines.append(f"• {ns} — {desc}{_availability_tag(c)}")
    lines.append("")
    lines.append("Plateforme (spine — toujours dispo, non gaté) :")
    for ns, desc in SPINE_CONCEPTS:
        lines.append(f"• {ns} — {desc}")
    return "\n".join(lines)


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
    elif entity_type == "platform":
        # ADR 0044 §F : la clé plateforme est une instance du coffre, gatée sur le mode
        # d'auth 'platform' du connecteur (le même gate que le palier plateforme de la
        # résolution : un provider byo-only ne porte jamais de clé plateforme).
        c = REGISTRY.get(name)
        if not (c and "platform" in c.auth_modes):
            raise ValueError(f"{name!r} n'accepte pas de credential plateforme (auth_modes 'platform' requis)")
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
    # Lazy : le registre des backends d'identités se remplit à l'import des modules
    # tools/* (register_all au boot) — on le lit à la demande, jamais à l'import.
    from . import connector_identities, connector_verify
    return [
        {
            "name": c.name,
            "label": c.label,
            "help": c.help,
            # Description curée 2-3 phrases (carte catalogue) — "" si non rédigée,
            # le front retombe sur `help`.
            "description": c.description,
            # Doc « how-to » user-facing (prérequis/setup/usage), markdown par section.
            "doc_sections": [
                {"kind": s.kind, "title": s.title, "body_md": s.body_md}
                for s in c.doc_sections
            ],
            "href": c.href,
            "publisher": c.publisher_name,   # éditeur (curé) — catalogue
            "logo_url": c.logo_url_for(),     # logo éditeur (oto-media), None si absent
            "availability": c.availability,
            "auth_modes": sorted(c.auth_modes),
            "personal_session": c.personal_session,
            "secret_kind": c.secret_kind,
            # Descripteur d'auth unifié (ADR 0024) — method/cardinality/fields.
            # Source du widget credential de la carte ; `secret_kind` reste exposé
            # le temps de la transition (dérivable l'un de l'autre).
            "auth": c.auth,
            "namespaces": list(c.namespaces),
            "family": c.family,        # axe builder (dérivé) — ADR 0011
            "category": c.category,    # axe utilisateur (curé) — ADR 0011
            # Schéma de saisie du credential (modèle générique multi-champs) — le
            # dashboard rend le formulaire en bouclant dessus. Jamais de valeur,
            # juste la forme (name/label/secret).
            "credential_fields": [
                {"name": f.name, "label": f.label, "secret": f.secret,
                 "required": f.required, "help": f.help}
                for f in c.secret_fields
            ],
            # Free-tier (ADR 0031) : clé plateforme ouverte sans grant, quota gratuit
            # par user/jour. Le dashboard affiche un badge « gratuit : N/j » côté USER.
            "free_tier": {"daily_quota": c.default_quota} if c.platform_key_open else None,
            # Sélecteur d'identité (ADR 0024) : le connecteur permet de choisir une
            # identité/cible par défaut (pennylaneged : la société = SA GED). La
            # carte USER en dérive son picker (google/unipile ont leur widget dédié).
            "identities": connector_identities.supports(c.name),
            # Sonde de credential (framework « tester la connexion ») : le connecteur a
            # enregistré un `verify` sans effet de bord (zoho…). La carte affiche alors
            # un bouton « tester la connexion » à côté de l'état « clé posée ».
            "verifiable": connector_verify.supports(c.name),
        }
        for c in _REGISTRY_LIST
    ]

