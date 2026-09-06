"""Registre des connecteurs — SOURCE UNIQUE de vérité (l'AGRÉGATEUR).

Ce module ne DÉCRIT aucun connecteur : il les ASSEMBLE. Chaque connecteur
déclare son entrée dans `providers/<nom>.py` (`CONNECTOR = _c(…)`, plus ses
constantes curées `CATEGORY` / `PUBLISHER` / `DESCRIPTION` / `LOGO_DOMAIN`),
et `_DECLARATIONS` ci-dessous fixe l'ORDRE dans lequel ils entrent au registre.
Tout le reste — `REGISTRY`, `KEY_PROVIDERS`, `DEFAULT_ACTIVE_CONNECTORS`, le
catalogue public — en DÉRIVE : le registre est une **projection calculée, jamais
stockée**.

Ajouter un connecteur = un fichier `providers/<nom>.py` + une ligne dans
`_DECLARATIONS`. Le module s'appelle comme le connecteur, et l'agrégation le
vérifie à l'import (`tests/test_providers_registry_snapshot.py` verrouille les
deux sens : pas de fichier orphelin, pas de ligne fantôme).

Module PUR (aucun import `oto_mcp` au niveau module, comme tool_visibility.py).
C'est ce qui interdit de loger les déclarations dans `tools/<nom>.py` : ces
modules-là importent `..access` (qui importe ce registre) et les clients
oto-core, et `register_all` les charge en try/except — une dép optionnelle
manquante retirerait alors un connecteur du CATALOGUE, pas seulement ses tools.

Remplace les 4 listes en dur qui dérivaient (`db.KEY_PROVIDERS`,
`access.ORG_SHAREABLE_PROVIDERS`, `tool_visibility.ADMIN_GRANT_ONLY_NAMESPACES`,
le `PROVIDERS` du frontend) plus `_QUOTA_DEFAULTS`.

NB barreau « Phase 1 » : ce registre encode l'état ACTUEL (les dérivations sont
byte-identiques aux anciennes listes). Les évolutions de taxonomie (ex. gocardless
→ BYO self_serve keyed, un grant-only → injection platform) sont des changements ultérieurs
explicites de ce registre, qui piloteront leurs migrations.
"""
from __future__ import annotations

import importlib

from ._model import (  # noqa: F401  — surface publique historique du module
    BROWSER_PROVIDERS,
    Connector,
    CredentialField,
    _c,
)

# --- l'ORDRE du registre, explicite ------------------------------------------
# L'ordre de déclaration ne gouverne AUCUN calcul : ni `KEY_PROVIDERS` ni le
# registre ne sont indexés par position, et `status_for` (access/status.py) les ITÈRE
# pour remplir `out["providers"][nom]`, un dict par NOM. Il ne survit que comme
# ordre de SÉRIALISATION — donc d'affichage (catalogue, primer de namespaces,
# `status_for`). C'est quand même un ordre qu'on VEUT stable : il est écrit ici,
# à la main, et jamais dérivé du système de fichiers (un `glob` rendrait la
# sortie dépendante de l'ordre du répertoire).
#
# ⚠️ Trois commentaires de connecteur ont longtemps affirmé le contraire
# (« l'ordre est chargé, `status_for` en dépend, je suis le dernier ») — corrigé
# le 21/08/2026 : la phrase avait coûté trois tests faux (ahrefs, fireflies,
# granola), dont deux affirmaient être le dernier, et un rouge sur main le jour
# où deux connecteurs ont été ajoutés le même matin. Vérifier un invariant avant
# de demander qu'on le garde.
#
# Notes de composition (des connecteurs ABSENTS, et pourquoi) :
# - `bridge` (ADR 0034) RETIRÉ le 2026-07-16 (ADR 0037 / oto-backend#108) :
#   subsumé par le connecteur `http` générique — un bridge n'est qu'une API HTTP
#   que le back-office re-expose, jointe via http_get/http_post. Le bridge pilote a
#   migré bridge→http. Le concept « remote data-driven » (base_url sur un
#   provider hors registre) subsiste dans `org_secret_meta`, sans entrée de
#   catalogue ; l'identité client vit dans la CONFIG d'org, jamais en dur.
# - `justicelibre` (mount no-auth vers justicelibre.org/mcp) RETIRÉ le
#   2026-08-21 : la fédération MCP est en sommeil, son master était OFF en prod
#   et le connecteur n'était plus qu'un reste. La branche « mount no-auth » de
#   tools/mount.py (auth_modes VIDE) reste, générique et sans consommateur
#   vivant — cf. docs/federation.md.
# - `linkedin` déposé le 2026-08-10 (#231) : absorbé par `aiark` — même vendeur,
#   même client, la distinction n'était qu'un mode d'auth, donc une INSTANCE.
_DECLARATIONS: tuple[str, ...] = (
    # --- keyed (résolus via resolve_api_key, clé api per-user) ---------------
    "serper",
    "hunter",
    "reddit",
    "sirene",
    "droit",
    "attio",
    "lemlist",
    "kaspr",
    "pennylane",
    # Voisin de `pennylane` : même catégorie Finance, et l'ordre gouverne
    # l'affichage du catalogue.
    "finkare",
    "slack",
    "fullenrich",
    "dropcontact",
    "folk",
    "aiark",
    "unipile",
    # Les six CONNEXIONS du compte unipile ci-dessus (split 2026-08-28) : chacune
    # est un connecteur à part entière (activation, ACL, sélection, visibilité,
    # connexion hébergée en propre) qui DÉLÈGUE son credential à `unipile`. Elles
    # se déclarent juste après lui : l'ordre gouverne l'affichage, et une carte de
    # canal qui flotterait loin de son compte se lirait comme un connecteur sans
    # rapport.
    "linkedin_unipile",
    "whatsapp",
    "telegram",
    "instagram",
    "messenger",
    "twitter",
    "topograph",
    "resend",
    "routine",
    "scaleway",
    "lusha",
    # --- byo_user à credential multi-champs (hors resolve_api_key) -----------
    "silae",
    "forager",
    # --- gocardless : keyed BYO self-serve -----------------------------------
    "gocardless",
    "atlassian",
    "folkmcp",
    "planity",
    "cognism",
    "lighton",
    "promptwatch",
    # --- sessions per-user (hors resolve_api_key, stockage dédié) ------------
    "crunchbase",
    "brevoauto",
    "pennylaneged",
    "browser",
    "google",
    # --- open-data / sans credential ----------------------------------------
    # Sources publiques sans rapport → connecteurs distincts (ex-`fr_open` qui les
    # fusionnait : un sac « open data » incohérent, activer l'un activait l'autre).
    # namespace = préfixe réel : culture_spectacle_* → `culture` (namespace_of =
    # 1er token). Déclarer "culture", PAS "culture_spectacle" (jamais matché →
    # fail-open du gate, #24).
    "web",
    "culture",
    "gr",
    "foncier",
    "urba",
    "sante",
    "osm",
    "frenchtech",
    "infosec",
    # --- connecteurs API tiers (clients oto-core déjà écrits, câblés 2026-06-19) ---
    "hubspot",
    "brevo",
    "apollo",
    "zerobounce",
    "hithorizons",
    "phantombuster",
    "notion",
    "figma",
    "supabase",
    "zoho",
    "zohodesk",
    "zohoanalytics",
    "salesforce",
    "pipedrive",
    "sellsy",
    # --- ATS / talent sourcing (RH) — câblés 2026-06-20 ----------------------
    "greenhouse",
    "lever",
    "ashby",
    "teamtailor",
    "recruitee",
    "spott",
    "serpapi",
    "searchapi",
    "brightdata",
    "cloro",
    "firecrawl",
    "tavily",
    "apify",
    # --- signaux de recrutement + campagnes sortantes — câblés 2026-08-17 ----
    "theirstack",
    "origami",
    # --- CRM agent-native — câblé 2026-08-19 --------------------------------
    "lightfield",
    # --- automatisation de workflows (no-code) — câblés 2026-06-21 -----------
    "n8n",
    "make",
    "zapier",
    "fireflies",
    # --- connecteur http générique (secret DANS le coffre oto) ----------------
    "http",
    "webflow",
    "ahrefs",
    "granola",
    "grain",
    "linear",
    "stripe",
    "posthog",
    "snitcher",
    "waalaxy",
    "airtable",
    "tally",
    # --- prospection téléphonique — câblé 2026-08-31 -------------------------
    "minari",
    # --- forge logicielle — câblé 2026-09-02 ---------------------------------
    "github",
    # Voisin de `fireflies`/`grain`/`granola` par le métier (intelligence
    # conversationnelle), et l'ordre gouverne l'affichage du catalogue.
    "leexi",
    # Voisin de `linear` : la roadmap de Productlane est ADOSSÉE à Linear
    # (projets et issues y naissent, puis sont reflétés). Les deux cartes se
    # lisent ensemble.
    "productlane",
    # --- porteurs de CLÉ, aucun outil (kind="credential") --------------------
    # La clé de modèle qu'une org dépose pour ses agents programmés. Ils ne
    # servent aucun tool : le worker la consomme pour le compte de l'org.
    "anthropic",
    "mistral",
)

_MODULES: dict = {}
_REGISTRY_LIST: list[Connector] = []
for _nom in _DECLARATIONS:
    _mod = importlib.import_module(f".{_nom}", __name__)
    if _mod.CONNECTOR.name != _nom:
        raise RuntimeError(
            f"providers/{_nom}.py déclare le connecteur {_mod.CONNECTOR.name!r} : "
            "le module doit s'appeler comme son connecteur (un domicile, un nom).")
    _MODULES[_nom] = _mod
    _REGISTRY_LIST.append(_mod.CONNECTOR)


def _curee(constante: str) -> dict:
    """Indexe par connecteur une constante curée déclarée dans son module."""
    return {nom: getattr(mod, constante) for nom, mod in _MODULES.items()
            if getattr(mod, constante, None) is not None}


# Données curées PAR CONNECTEUR — déclarées dans `providers/<nom>.py`, indexées
# ici. Elles ne sont pas des champs de `Connector` : la forme de la dataclass est
# un contrat lu jusque dans un AUTRE repo (oto-dashboard, via `public_catalog`),
# et l'ajout d'un champ typé se décide connecteur par connecteur (cf. #409 pour
# la cardinalité d'auth, dont le domicile naturel est bien l'entrée elle-même).
_CATEGORY_BY_CONNECTOR: dict = _curee("CATEGORY")
_PUBLISHER_BY_CONNECTOR: dict = _curee("PUBLISHER")
_DESCRIPTION_BY_CONNECTOR: dict = _curee("DESCRIPTION")
_LOGO_DOMAIN_BY_CONNECTOR: dict = _curee("LOGO_DOMAIN")
# Connecteurs SANS logo de marque, et c'est voulu : soit génériques (le connecteur
# n'est pas une marque — `http`, `browser`), soit maison (`gr`), soit composés de
# sources publiques hétérogènes (`infosec`). L'UI y rend un monogramme. Déclaré
# `SANS_LOGO_DE_MARQUE = True` dans le module du connecteur, avec son motif.
# L'inverse d'une dette : rend l'absence DÉLIBÉRÉE et vérifiable, au lieu de
# laisser un oubli se confondre avec un choix — les 20 connecteurs sans logo du
# 31/07 étaient tous des oublis, sauf ces cinq. Ratchet : test_connector_logos.py.
_SANS_LOGO_DE_MARQUE: frozenset = frozenset(_curee("SANS_LOGO_DE_MARQUE"))


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
# ⚠️ Un connecteur qui DÉLÈGUE son credential (`credential_of`, ex. les six canaux
# unipile) en est EXCLU : sa clé n'existe que sous le porteur. Sans cette exclusion,
# une clé posée sous `whatsapp` serait acceptée au coffre puis jamais relue (la
# cascade normalise vers `unipile`) — un credential fantôme, et deux clés qui se
# contredisent. Cf. `credential_provider`.
CREDENTIAL_PROVIDERS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST
    if c.credential_of is None
    and (c.keyed or c.credential_fields or c.secret_kind != "none")
)
ORG_SHAREABLE_PROVIDERS: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.org_shareable)
QUOTA_DEFAULTS: dict = {c.name: c.default_quota for c in _REGISTRY_LIST if c.default_quota}
# Socle curé (ADR 0050) : les connecteurs installés d'office (state='active') au
# seed de la sélection d'un NOUVEAU (sub, org). Le reste de l'exposé = library.
# ⚠️ Politique actuelle (décision 16/07) : socle VIDE — aucun connecteur n'est
# pré-installé ; l'agent guide l'utilisateur depuis les tools spine (`oto_connector`
# op=list/select, `oto_call`) et le catalogue injecté. Le mécanisme reste : poser
# default_active=True sur un connecteur le remettrait au départ.
DEFAULT_ACTIVE_CONNECTORS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST if c.default_active
)

# Connecteurs d'envoi d'email → transport effectif. Un expéditeur appartient à un
# connecteur (sa config vit dans orgs.email_settings keyé par connecteur) ; le
# transport en DÉRIVE. `email_send` (spine) route sender→connecteur→transport.
EMAIL_CONNECTOR_TRANSPORT: dict = {"scaleway": "scaleway", "resend": "resend"}
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
    ("oto_*", "méta : visibilité des outils (enable/disable), guide d'org, orgs & équipes"),
    ("run_* / feedback", "boucle d'usage : run_start/run_finish encadrent un déroulé ; feedback(gap|tool_feedback) remonte les signaux"),
)


def _availability_tag(c: "Connector") -> str:
    """Annotation courte de disponibilité (pour ne pas faire croire qu'un namespace
    gaté/masqué est appelable d'office)."""
    bits: list[str] = []
    if c.hosted_auth:
        bits.append("compte à connecter")
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
    linkedin/crunchbase/google/slack…) ; group → org-partageable OU byo_user (une
    équipe délègue l'org, ADR 0012) ; org → doit être org-partageable (byo_org,
    ex. http, ou un remote org-only). Utilisé par credentials_store (coffre unique tous secrets)."""
    # Délégation (`credential_of`) : le connecteur n'a pas de credential à lui, à
    # AUCUN niveau d'entité. Refus nommant le porteur — « whatsapp n'accepte pas de
    # clé » sans dire où la poser laisserait l'appelant chercher une carte qui
    # n'existe pas.
    porteur = credential_provider(name)
    if porteur != name:
        raise ValueError(
            f"{name!r} ne porte pas de credential : sa clé se pose sur {porteur!r} "
            f"(un seul compte fournisseur pour toutes ses connexions).")
    if entity_type in ("org", "tenant"):
        # Un TENANT (L-clés PR 1) pose la même question que l'org : sa clé est
        # partagée par ses orgs, donc lue aux barreaux partagés du walker (gate
        # `ORG_SHAREABLE_PROVIDERS`) — et à eux seuls.
        if not is_org_shareable(name):
            raise ValueError(f"{name!r} n'est pas un credential org-partageable")
    elif entity_type == "platform":
        # ADR 0044 §F : la clé plateforme est une instance du coffre, gatée sur le mode
        # d'auth 'platform' du connecteur (le même gate que le palier plateforme de la
        # résolution : un provider byo-only ne porte jamais de clé plateforme).
        c = REGISTRY.get(name)
        if not (c and "platform" in c.auth_modes):
            raise ValueError(f"{name!r} n'accepte pas de credential plateforme (auth_modes 'platform' requis)")
    elif entity_type == "group":
        # Un GROUPE est une délégation de l'org (ADR 0012) : ce qui est
        # org-partageable est posable au niveau équipe (miroir EXACT du palier
        # groupe de la résolution, gaté `ORG_SHAREABLE_PROVIDERS`, pas byo_user).
        # Un byo_user pur (sessions linkedin/google) reste posable en équipe aussi.
        # ⚠️ NE PAS exiger byo_user ici : un connecteur org-only (http « un par
        # département », #183) DOIT pouvoir poser son secret d'équipe sans devenir
        # byo_user (ce qui réactiverait à tort le palier membre — cf. access/cascade.py).
        if not (is_org_shareable(name) or is_byo_user(name)):
            raise ValueError(f"{name!r} n'accepte pas de credential de groupe")
    else:  # user
        if not is_byo_user(name):
            raise ValueError(
                f"{name!r} n'accepte pas de credential per-user (byo_user requis)")


def is_byo_user(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and "byo_user" in c.auth_modes)


def is_org_shareable(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and c.org_shareable)


def is_personal_cross_org(name: str) -> bool:
    """Le connecteur porte-t-il une instance PERSONNELLE cross-org (issue #172) ?
    Vrai ⟹ la clé membre d'un `sub` posée dans une org le suit dans toutes ses
    orgs (résolution de proximité). Défaut False (ADR 0033 : scope `(sub, org)`)."""
    c = REGISTRY.get(name)
    return bool(c and c.personal_cross_org)


PERSONAL_CROSS_ORG_PROVIDERS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST if c.personal_cross_org)


def credential_provider(name: str) -> str:
    """Le connecteur qui PORTE le credential de `name` (lui-même par défaut).

    **LE seam de la délégation** (`Connector.credential_of`) : tout ce qui touche au
    coffre, à la cascade, au quota, à la clé plateforme ou à l'option couche-3 pose
    sa question à travers lui ; tout ce qui GATE (activation, ACL, sélection,
    visibilité, pin `_instance=`) garde le nom NU. Les deux questions se ressemblent
    et ne sont pas la même — c'est exactement la confusion qui a produit, le
    2026-07-07, une carte « clé d'org » verte à côté d'un « Bloqué » rouge.

    Un seul niveau, volontairement : un porteur ne délègue pas à son tour (une chaîne
    rendrait le coffre adressable par un chemin qu'aucune surface ne montre). Nom
    inconnu ⟹ rendu tel quel (le fail-open des gates est inchangé)."""
    c = REGISTRY.get(name)
    return (c.credential_of or name) if c else name


def delegates_credential(name: str) -> bool:
    """`name` emprunte-t-il le credential d'un autre connecteur ?"""
    c = REGISTRY.get(name)
    return bool(c and c.credential_of)


def connector_for_hosted_channel(channel: str) -> Connector | None:
    """Le connecteur qui REPRÉSENTE un canal hébergé (`LINKEDIN`, `WHATSAPP`…).

    Réciproque de `Connector.hosted_channel`. C'est par là que le code qui ne
    connaît que le canal — la résolution du compte opéré, les tools de messagerie,
    le picker d'identités — retrouve le connecteur à GATER, au lieu de retomber sur
    le porteur de la clé et de gater tout le monde pareil."""
    if not channel:
        return None
    return _CHANNEL_INDEX.get(channel.upper())


_CHANNEL_INDEX: dict = {c.hosted_channel: c for c in _REGISTRY_LIST if c.hosted_channel}


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
    # NB : un connecteur qui DÉLÈGUE son credential en est exclu par construction
    # (`Connector.org_shareable`) — sa clé se pose sur le porteur, pas sur lui.
    if provider not in ORG_SHAREABLE_PROVIDERS:
        return None, "provider_not_shareable"
    if base_url:
        return None, "base_url_not_allowed"
    return None, None


def public_catalog() -> list[dict]:
    """Vue publique (GET /api/connectors) — sans secret, pour le frontend."""
    # Lazy : le registre des backends d'identités se remplit à l'import des modules
    # tools/* (register_all au boot) — on le lit à la demande, jamais à l'import.
    from ..connectors import flow as connector_flow
    from ..connectors import identities as connector_identities
    from ..connectors import verify as connector_verify
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
            # juste la forme (name/label/secret/when/choices).
            # DÉRIVÉ de `auth["fields"]`, pas recopié : les deux listes décrivaient la
            # même chose à deux endroits, et un champ ajouté à l'une manquait à
            # l'autre en silence (constaté en ajoutant `when`/`choices`, #449).
            "credential_fields": c.auth["fields"],
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
            # FORME du geste « connecter » (label + paramètres attendus), ou None
            # pour les ~56 connecteurs sans flux. Jamais d'URL ni de nom de
            # capacité : /api/connectors est servie SANS auth, et le chemin est
            # fixe côté client. Cf. `connector_flow`.
            "connect": connector_flow.describe(c.name),
        }
        for c in _REGISTRY_LIST
    ]

