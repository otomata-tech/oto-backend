"""Connector registry — SINGLE SOURCE of truth (the AGGREGATOR).

This module DESCRIBES no connector: it ASSEMBLES them. Each connector
declares its entry in `providers/<name>.py` (`CONNECTOR = _c(…)`, plus its
curated constants `CATEGORY` / `PUBLISHER` / `DESCRIPTION` / `LOGO_DOMAIN`),
and `_DECLARATIONS` below fixes the ORDER in which they enter the registry.
Everything else — `REGISTRY`, `KEY_PROVIDERS`, `DEFAULT_ACTIVE_CONNECTORS`, the
public catalog — is DERIVED from it: the registry is a **computed projection, never
stored**.

Adding a connector = a `providers/<name>.py` file + a line in
`_DECLARATIONS`. The module is named like the connector, and the aggregation
checks it at import (`tests/test_providers_registry_snapshot.py` locks both
directions: no orphan file, no phantom line).

PURE module (no `oto_mcp` import at module level, like tool_visibility.py).
That is what forbids housing the declarations in `tools/<name>.py`: those
modules import `..access` (which imports this registry) and the oto-core
clients, and `register_all` loads them in a try/except — a missing optional
dep would then remove a connector from the CATALOG, not just its tools.

Replaces the 4 hard-coded lists that used to drift (`db.KEY_PROVIDERS`,
`access.ORG_SHAREABLE_PROVIDERS`, `tool_visibility.ADMIN_GRANT_ONLY_NAMESPACES`,
the frontend's `PROVIDERS`) plus `_QUOTA_DEFAULTS`.

NB "Phase 1" rung: this registry encodes the CURRENT state (the derivations are
byte-identical to the old lists). Taxonomy changes (e.g. gocardless
→ BYO self_serve keyed, a grant-only → platform injection) are later, explicit
changes to this registry, which will drive their migrations.
"""
from __future__ import annotations

import importlib

from ._model import (  # noqa: F401  — surface publique historique du module
    BROWSER_PROVIDERS,
    Connector,
    CredentialField,
    _c,
)

# --- the registry ORDER, explicit --------------------------------------------
# The declaration order governs NO computation: neither `KEY_PROVIDERS` nor the
# registry is indexed by position, and `status_for` (access/status.py) ITERATES them
# to fill `out["providers"][name]`, a dict keyed by NAME. It survives only as the
# SERIALIZATION order — hence the display order (catalog, namespace primer,
# `status_for`). It is still an order we WANT stable: it is written here,
# by hand, and never derived from the filesystem (a `glob` would make the
# output depend on directory order).
#
# ⚠️ Three connector comments long claimed the opposite
# ("the order is loaded, `status_for` depends on it, I am the last") — fixed
# on 21/08/2026: that sentence had cost three false tests (ahrefs, fireflies,
# granola), two of which claimed to be last, and a red on main the day
# two connectors were added the same morning. Check an invariant before
# asking that it be kept.
#
# Composition notes (ABSENT connectors, and why):
# - `bridge` (ADR 0034) REMOVED on 2026-07-16 (ADR 0037 / oto-backend#108):
#   subsumed by the generic `http` connector — a bridge is just an HTTP API
#   that the back-office re-exposes, joined via http_get/http_post. The pilot bridge
#   migrated bridge→http. The "remote data-driven" concept (base_url on a
#   provider outside the registry) survives in `org_secret_meta`, with no
#   catalog entry; the client identity lives in the org CONFIG, never hard-coded.
# - `justicelibre` (no-auth mount) REMOVED on 2026-08-21, then `atlassian` and
#   `folkmcp` on 2026-09-09 along with the MECHANISM itself: MCP federation
#   (`kind="mount"`) is removed from the platform (ADR 0069). A remote service
#   is now joined through the generic `http` connector, or written as a
#   native connector — which is what `planity` became.
# - `linkedin` dropped on 2026-08-10 (#231): absorbed by `aiark` — same vendor,
#   same client, the distinction was only an auth mode, hence an INSTANCE.
_DECLARATIONS: tuple[str, ...] = (
    # --- keyed (resolved via resolve_api_key, per-user api key) --------------
    "serper",
    "hunter",
    "reddit",
    "sirene",
    "droit",
    "attio",
    # Neighbour of `attio` (CRM) — wired 2026-10-02.
    "affinity",
    "lemlist",
    # Neighbour of `lemlist`: the domains and mailboxes it sends from — wired 2026-10-05.
    "mailpool",
    "kaspr",
    "pennylane",
    # The firm side of the same publisher (one token, every company of the firm):
    # next to the company side, so the two cards read as a pair.
    "pennylane_firm",
    # Neighbour of `pennylane`: same Finance category, and the order governs
    # the catalog display.
    "finkare",
    "slack",
    "fullenrich",
    # Neighbour of `fullenrich` by trade (enrichment); also carries the Clay
    # table webhooks (write), see `providers/clay.py`.
    "clay",
    "dropcontact",
    "folk",
    "aiark",
    "unipile",
    # The CONNECTIONS of the unipile account above (split 2026-08-28): each
    # is a connector in its own right (activation, ACL, selection, visibility,
    # its own hosted connection) that DELEGATES its credential to `unipile`. They
    # are declared right after it: the order governs the display, and a channel
    # card floating far from its account would read as an unrelated
    # connector.
    "linkedin_unipile",
    "whatsapp",
    "telegram",
    "instagram",
    "topograph",
    "resend",
    "routine",
    "scaleway",
    "lusha",
    # --- byo_user with multi-field credential (outside resolve_api_key) ------
    "silae",
    "forager",
    "lucca",
    "inqom",
    "threecx",
    # --- gocardless: keyed BYO self-serve ------------------------------------
    "gocardless",
    # --- yousign: BYO self-serve (key + environment), really writes (sends invitations)
    "yousign",
    # `planity` stays HERE, in the spot it held when it was federated: this
    # order only governs the DISPLAY, and moving it would reorder the catalog
    # without fixing anything. Its place is judged by the neighbours shown, not by `kind`.
    "planity",
    # Neighbour of `planity` by SITUATION, not by mechanism: a business
    # connector that the operator configures once (application credentials at the
    # platform tier) before anyone can connect to it.
    "instagram_meta",
    "meta_ads",
    "cognism",
    "lighton",
    # --- the Microsoft 365 ACCOUNT (delegated OAuth, per person) and its SERVICES ----
    # The account carries the vault; each service has its card, its activation, its
    # selection, ITS consent — `providers/microsoft.service`.
    "microsoft",
    "sharepoint",
    "outlook",
    "outlook_calendar",
    "teams",
    "promptwatch",
    # --- per-user sessions (outside resolve_api_key, dedicated storage) ------
    "crunchbase",
    "brevoauto",
    "pennylaneged",
    "browser",
    "google",
    # --- the six Google SERVICES, on the `google` account (split 2026-09-26) ------
    # Each has its card, its activation, its selection, ITS consent (its scopes
    # only); the account, for its part, carries the vault — `providers/google.service`.
    "gmail",
    "drive",
    "sheets",
    "calendar",
    "tasks",
    "chat",
    # seventh service (2026-10-02) — same shape, `bigquery` consent only.
    "bigquery",
    # eighth service (2026-10-08) — same shape, `adwords` consent only, read-only tools.
    "google_ads",
    # --- open-data / no credential ------------------------------------------
    # Unrelated public sources → distinct connectors (formerly `fr_open`, which
    # merged them: an incoherent "open data" bag, activating one activated the other).
    # namespace = real prefix: culture_spectacle_* → `culture` (namespace_of =
    # 1st token). Declare "culture", NOT "culture_spectacle" (never matched →
    # gate fail-open, #24).
    "web",
    "culture",
    "gr",
    "foncier",
    "urba",
    "sante",
    "osm",
    "frenchtech",
    "infosec",
    # Open data too, but from the United States: wages and employment by occupation (OEWS).
    "bls",
    # French open data again: the BODACC as a whole, searched by period, family,
    # department — not by SIREN, which `sirene` (`fr_events`) already serves.
    "bodacc",
    # --- third-party API connectors (oto-core clients already written, wired 2026-06-19) ---
    "hubspot",
    "brevo",
    # Neighbour of `brevo` by trade (emailing & customer data) — wired 2026-10-08.
    "klaviyo",
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
    # Neighbour of `sellsy` by trade: the CRM for IT services firms and consultancies.
    "boondmanager",
    # --- ATS / talent sourcing (HR) — wired 2026-06-20 -----------------------
    "greenhouse",
    "lever",
    "ashby",
    "teamtailor",
    "recruitee",
    "spott",
    # Neighbour of `spott`: the Welcome to the Jungle ATS, recruiter side.
    "wttj",
    "serpapi",
    "searchapi",
    "brightdata",
    "cloro",
    "firecrawl",
    "tavily",
    "apify",
    # Neighbour of `apify`: same regime (paid gateway, platform key on grant).
    "monid",
    # --- hiring signals + outbound campaigns — wired 2026-08-17 --------------
    "theirstack",
    "origami",
    # --- agent-native CRM — wired 2026-08-19 --------------------------------
    "lightfield",
    # --- workflow automation (no-code) — wired 2026-06-21 --------------------
    "n8n",
    "make",
    "zapier",
    "fireflies",
    # --- generic http connector (secret IN the oto vault) ---------------------
    "http",
    "webflow",
    # Neighbour of `webflow` by trade (CMS): the two cards read together.
    "wordpress",
    "ahrefs",
    # Neighbour of `ahrefs` by trade (SEO), but signed in like `sharepoint`: each
    # person connects their own Ubersuggest account (OAuth, public client).
    "ubersuggest",
    "granola",
    "grain",
    "linear",
    "stripe",
    "posthog",
    # Neighbour of `posthog` by trade (audience measurement): the two cards
    # read together.
    "google_analytics",
    # Same family (product analytics) as `posthog`, read only — wired 2026-10-02.
    "amplitude",
    "snitcher",
    "waalaxy",
    "airtable",
    "tally",
    # Neighbour of `tally` by trade (online forms); read-only.
    "typeform",
    # --- electronic signature — wired 2026-09-16 -----------------------------
    "signwell",
    # --- phone prospecting — wired 2026-08-31 --------------------------------
    "minari",
    # --- software forge — wired 2026-09-02 -----------------------------------
    "github",
    # Neighbour of `fireflies`/`grain`/`granola` by trade (conversation
    # intelligence), and the order governs the catalog display.
    "leexi",
    # Neighbour of `leexi` by trade: a company's calls, their
    # recordings and what the vendor's AI drew from them.
    "aircall",
    # Same family (meeting recordings, transcripts) — wired 2026-09-29.
    "claap",
    # Neighbour of `linear`: Productlane's roadmap is BACKED by Linear
    # (projects and issues are born there, then mirrored). The two cards
    # read together.
    "productlane",
    # --- administration of a business marketplace — wired 2026-09-11 ---------
    # PERSONAL token of a marketplace administrator (byo_user only); the
    # only connector in the catalog that writes into a product we operate.
    "hellostock",
    # --- aesthetic clinic management — wired 2026-09-17 ----------------------
    # Administrative side only (calendar, catalog, sales, leads, settings),
    # allowlisted: medical content is not served, see `tools/nextmotion.py`.
    "nextmotion",
    # --- payroll and HR, read-only — wired 2026-09-17 -------------------------
    # Neighbour of `nextmotion` by SITUATION: software that carries heavy personal
    # data (NIR, IBAN, medical absence reasons), served through an allowlist,
    # see `tools/payfit.py`.
    "payfit",
    # --- a project audio becomes a project page — ADR 0074, #674 ------------
    "transcription",
    # --- US occupations reference — wired 2026-09-22 --------------------------
    # The keyed counterpart of `bls`: O*NET names the occupation, BLS gives its wages.
    "onet",
    # --- typed decision (System One) — wired 2026-09-28 ----------------------
    # The model that settles a closed question instead of a model turn. Neighbour
    # of `transcription` by SITUATION: an inference service whose key is
    # never an org's — here the tenant's, never ours.
    "jev",
    # --- KEY carriers, no tools (kind="credential") --------------------------
    # The model key an org deposits for its scheduled agents. They serve
    # no tool: the worker consumes it on the org's behalf.
    "anthropic",
    "mistral",
)

_MODULES: dict = {}
_REGISTRY_LIST: list[Connector] = []
for _nom in _DECLARATIONS:
    _mod = importlib.import_module(f".{_nom}", __name__)
    if _mod.CONNECTOR.name != _nom:
        raise RuntimeError(
            f"providers/{_nom}.py declares connector {_mod.CONNECTOR.name!r}: "
            "the module must be named like its connector (one home, one name).")
    _MODULES[_nom] = _mod
    _REGISTRY_LIST.append(_mod.CONNECTOR)


def _curee(constante: str) -> dict:
    """Index by connector a curated constant declared in its module."""
    return {nom: getattr(mod, constante) for nom, mod in _MODULES.items()
            if getattr(mod, constante, None) is not None}


# Curated data PER CONNECTOR — declared in `providers/<name>.py`, indexed
# here. They are not fields of `Connector`: the shape of the dataclass is
# a contract read as far as ANOTHER repo (oto-dashboard, via `public_catalog`),
# and adding a typed field is decided connector by connector (cf. #409 for
# auth cardinality, whose natural home is indeed the entry itself).
_CATEGORY_BY_CONNECTOR: dict = _curee("CATEGORY")
_PUBLISHER_BY_CONNECTOR: dict = _curee("PUBLISHER")
_DESCRIPTION_BY_CONNECTOR: dict = _curee("DESCRIPTION")
_LOGO_DOMAIN_BY_CONNECTOR: dict = _curee("LOGO_DOMAIN")
# Connectors WITHOUT a brand logo, on purpose: either generic (the connector
# is not a brand — `http`, `browser`), or in-house (`gr`), or made of
# heterogeneous public sources (`infosec`). The UI renders a monogram there. Declared
# `SANS_LOGO_DE_MARQUE = True` in the connector's module, with its reason.
# The opposite of a debt: makes the absence DELIBERATE and verifiable, instead of
# letting an oversight pass for a choice — the 20 connectors without a logo on
# 31/07 were all oversights, except these five. Ratchet: test_connector_logos.py.
_SANS_LOGO_DE_MARQUE: frozenset = frozenset(_curee("SANS_LOGO_DE_MARQUE"))


REGISTRY: dict[str, Connector] = {c.name: c for c in _REGISTRY_LIST}


# --- reverse index namespace -> connector -----------------------------------
_NS_INDEX: dict[str, Connector] = {}
for _c_obj in _REGISTRY_LIST:
    for _ns in _c_obj.namespaces:
        _NS_INDEX[_ns] = _c_obj


# --- derivations (replace the 4 hard-coded lists + quotas + env-names) -------

KEY_PROVIDERS: tuple = tuple(c.name for c in _REGISTRY_LIST if c.keyed)
# Providers that can HOLD a per-member credential in the vault — write guard
# `db._check_provider`. Broader than KEY_PROVIDERS (keyed only): includes **browser
# sessions** (secret_kind="cookie": brevo/crunchbase/pennylaneged, which persist the
# Browserbase Context) and **multi-field byo** connectors. Without it, persisting
# a session (ADR 0026/0033, `_persist`→`set_member_api_key`) raised "Unknown provider".
# ⚠️ A connector that DELEGATES its credential (`credential_of`, e.g. the six unipile
# channels) is EXCLUDED: its key exists only under the carrier. Without this exclusion,
# a key set under `whatsapp` would be accepted by the vault then never read back (the
# cascade normalizes to `unipile`) — a phantom credential, and two keys that
# contradict each other. See `credential_provider`.
CREDENTIAL_PROVIDERS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST
    if c.credential_of is None
    and (c.keyed or c.credential_fields or c.secret_kind != "none")
)
ORG_SHAREABLE_PROVIDERS: frozenset = frozenset(c.name for c in _REGISTRY_LIST if c.org_shareable)
QUOTA_DEFAULTS: dict = {c.name: c.default_quota for c in _REGISTRY_LIST if c.default_quota}
# Curated base (ADR 0050): the connectors installed by default (state='active') at
# the selection seed of a NEW (sub, org). The rest of the exposed set = library.
# ⚠️ Current policy (decision 16/07): EMPTY base — no connector is
# pre-installed; the agent guides the user from the spine tools (`oto_connector`
# op=list/select, `oto_call`) and the injected catalog. The mechanism remains: setting
# default_active=True on a connector would put it back at the start.
DEFAULT_ACTIVE_CONNECTORS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST if c.default_active
)

# Email-sending connectors → effective transport. A sender belongs to a
# connector (its config lives in orgs.email_settings keyed by connector); the
# transport is DERIVED from it. `email_send` (spine) routes sender→connector→transport.
EMAIL_CONNECTOR_TRANSPORT: dict = {"scaleway": "scaleway", "resend": "resend"}
REMOTE_CONNECTORS: tuple = tuple(c for c in _REGISTRY_LIST if c.kind == "remote")


# --- namespace catalog presented to the agent (_SERVER_INSTRUCTIONS) ---------
# DERIVED from the registry (gone is the hand-written list that drifted — reddit/culture
# mentioned, foncier/pennylane/apollo/sante… omitted). Improving a namespace's
# blurb = editing the connector's `help` (single source: catalog + card +
# this primer).
#
# The BASE (the capabilities oto carries itself, outside the connector registry) lives in
# `oto_mcp/spine_catalog.py` — same regime: a family declares its line, and the
# coverage is VERIFIED against the tools actually mounted, so that no
# capability can be passed over in silence. It used to be: four hand-written
# entries that nothing made grow, hence the absence of `oto_resource`/`oto_doc`/`oto_kb`
# from the map that announces itself as "complete" (signal #813 of 08/09/2026, settled the same
# day). It is OUTSIDE this package on purpose: `providers/` is the registry of
# CONNECTORS, and any file sleeping there without a line in `_DECLARATIONS` is an
# unreachable connector (`test_providers_registry_snapshot`). It is imported in the
# body of `render_namespace_catalog` — this aggregator stays PURE.


def _availability_tag(c: "Connector") -> str:
    """Short availability annotation (so as not to suggest that a gated/hidden
    namespace is callable out of the box)."""
    bits: list[str] = []
    if c.hosted_auth:
        bits.append("account to connect")
    return f" ({'; '.join(bits)})" if bits else ""


def render_namespace_catalog(spine_tools=None) -> str:
    """The "namespaces" block of the server instructions — BOTH halves derived.

    Connectors: one line per connector (its namespaces grouped), over all of
    `_REGISTRY_LIST` → no omission. The pure-credential email transports
    (scaleway/resend, no tool of their own) are presented via the `email_send` family.

    Base: one line per declared family (`spine_catalog.SPINE_FAMILIES`), then one line
    per spine tool that nobody claims. `spine_tools=None` = default derivation
    (capability registry); the parameter exists so that a caller who
    knows the inventory actually mounted can pass it, and so that the test proves the
    mechanism without depending on what is mounted that day."""
    lines: list[str] = []
    for c in _REGISTRY_LIST:
        if c.name in EMAIL_CONNECTOR_TRANSPORT:   # credential-only → covered by email_send
            continue
        ns = " / ".join(f"{n}_*" for n in c.namespaces)
        desc = f"{c.label} : {c.help}" if c.help else c.label
        lines.append(f"• {ns} — {desc}{_availability_tag(c)}")
    lines.append("")
    lines.append("Platform (the base — what oto carries itself, always mounted):")
    from ..spine_catalog import render_spine   # pure module, late import (see above)
    lines += render_spine(spine_tools)
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
    """Replaces db._check_provider: raises if `name` is not a keyed provider."""
    if not is_keyed(name):
        raise ValueError(f"Unknown provider {name!r} (allowed: {KEY_PROVIDERS})")


def require_credential(entity_type: str, name: str) -> None:
    """Raises if the connector CANNOT carry a credential at this entity level.
    user → must accept `byo_user` (keyed API key OR session secret:
    linkedin/crunchbase/google/slack…); group → org-shareable OR byo_user (a
    team delegates the org, ADR 0012); org → must be org-shareable (byo_org,
    e.g. http, or an org-only remote). Used by credentials_store (single vault for all secrets)."""
    # Delegation (`credential_of`): the connector has no credential of its own, at
    # NO entity level. Refusal naming the carrier — "whatsapp does not accept a
    # key" without saying where to put it would leave the caller looking for a card that
    # does not exist.
    porteur = credential_provider(name)
    if porteur != name:
        raise ValueError(
            f"{name!r} does not carry a credential: its key is set on {porteur!r} "
            f"(a single provider account for all its connections).")
    if entity_type in ("org", "tenant"):
        # A TENANT (L-keys PR 1) asks the same question as the org: its key is
        # shared by its orgs, hence read at the walker's shared rungs (gate
        # `ORG_SHAREABLE_PROVIDERS`) — and at those alone.
        if not is_org_shareable(name):
            raise ValueError(f"{name!r} is not an org-shareable credential")
    elif entity_type == "platform":
        # ADR 0044 §F: the platform key is an instance of the vault, gated on the connector's
        # 'platform' auth mode (the same gate as the platform tier of the
        # resolution: a byo-only provider never carries a platform key).
        c = REGISTRY.get(name)
        if not (c and "platform" in c.auth_modes):
            raise ValueError(f"{name!r} does not accept a platform credential (auth_modes 'platform' required)")
    elif entity_type == "group":
        # A GROUP is a delegation of the org (ADR 0012): whatever is
        # org-shareable can be set at team level (EXACT mirror of the resolution's
        # group tier, gated `ORG_SHAREABLE_PROVIDERS`, not byo_user).
        # A pure byo_user (linkedin/google sessions) can be set at team level too.
        # ⚠️ DO NOT require byo_user here: an org-only connector (http "one per
        # department", #183) MUST be able to set its team secret without becoming
        # byo_user (which would wrongly reactivate the member tier — see access/cascade.py).
        if not (is_org_shareable(name) or is_byo_user(name)):
            raise ValueError(f"{name!r} does not accept a group credential")
    else:  # user
        if not is_byo_user(name):
            raise ValueError(
                f"{name!r} does not accept a per-user credential (byo_user required)")


def is_byo_user(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and "byo_user" in c.auth_modes)


def is_org_shareable(name: str) -> bool:
    c = REGISTRY.get(name)
    return bool(c and c.org_shareable)


def is_personal_cross_org(name: str) -> bool:
    """Does the connector carry a PERSONAL cross-org instance (issue #172)?
    True ⟹ a `sub`'s member key set in one org follows it across all its
    orgs (proximity resolution). Default False (ADR 0033: scope `(sub, org)`)."""
    c = REGISTRY.get(name)
    return bool(c and c.personal_cross_org)


PERSONAL_CROSS_ORG_PROVIDERS: frozenset = frozenset(
    c.name for c in _REGISTRY_LIST if c.personal_cross_org)


def credential_provider(name: str) -> str:
    """The connector that CARRIES `name`'s credential (itself by default).

    **THE seam of delegation** (`Connector.credential_of`): everything that touches the
    vault, the cascade, the quota, the platform key or the layer-3 option asks
    its question through it; everything that GATES (activation, ACL, selection,
    visibility, `_instance=` pin) keeps the BARE name. The two questions look alike
    and are not the same — exactly the confusion that produced, on
    2026-07-07, a green "org key" card next to a red "Blocked".

    One level only, on purpose: a carrier does not delegate in turn (a chain
    would make the vault addressable by a path no surface shows). Unknown
    name ⟹ returned as is (the gates' fail-open is unchanged)."""
    c = REGISTRY.get(name)
    return (c.credential_of or name) if c else name


def delegates_credential(name: str) -> bool:
    """Does `name` borrow another connector's credential?"""
    c = REGISTRY.get(name)
    return bool(c and c.credential_of)


def connector_for_hosted_channel(channel: str) -> Connector | None:
    """The connector that REPRESENTS a hosted channel (`LINKEDIN`, `WHATSAPP`…).

    Inverse of `Connector.hosted_channel`. This is how code that only
    knows the channel — operated-account resolution, the messaging tools,
    the identity picker — finds the connector to GATE, instead of falling back to
    the key's carrier and gating everyone the same."""
    if not channel:
        return None
    return _CHANNEL_INDEX.get(channel.upper())


_CHANNEL_INDEX: dict = {c.hosted_channel: c for c in _REGISTRY_LIST if c.hosted_channel}


def org_secret_meta(provider: str, base_url: str | None) -> tuple[dict | None, str | None]:
    """Validates the write of an org shared secret and computes its satellite `meta`.

    A **remote** connector (ADR 0003/0011) is defined by DATA: supplying a
    `base_url` (bridge endpoint) ⇒ it is a remote, whether or not it has an entry
    in the registry (zero hard-coded client names). Otherwise, the provider must be an
    org-shareable connector of the registry (shared key: attio, pennylane…) and REFUSES a
    `base_url`. Pure (registry only) → testable without a DB.

    Returns `(meta, error_code)`. `error_code` None = OK; `meta` = `{base_url}` for
    a remote, otherwise None. Codes: `provider_not_shareable`, `base_url_required`,
    `base_url_not_allowed`.
    """
    c = connector_for_provider(provider)
    # remote = registry entry kind="remote" (legacy) OR a base_url on a provider
    # outside the registry (data-driven: the credential defines the bridge).
    is_remote = (c is not None and c.kind == "remote") or (c is None and bool(base_url))
    if is_remote:
        if not base_url:
            return None, "base_url_required"
        return {"base_url": base_url.rstrip("/")}, None
    # NB: a connector that DELEGATES its credential is excluded by construction
    # (`Connector.org_shareable`) — its key is set on the carrier, not on it.
    if provider not in ORG_SHAREABLE_PROVIDERS:
        return None, "provider_not_shareable"
    # An OAuth connector is shared by CONSENT, never by a pasted secret:
    # `google` accepts the org tier (shared account set by an admin, 2026-09-27),
    # but only through its flow (`auth/google.build_auth_url(scope='org')`). A
    # generic set would write a row without a refresh token that no tool would read.
    if c is not None and c.secret_kind == "oauth":
        return None, "provider_not_shareable"
    if base_url:
        return None, "base_url_not_allowed"
    return None, None


def public_catalog() -> list[dict]:
    """Public view (GET /api/connectors) — no secret, for the frontend."""
    # Lazy: the identity backends registry fills at import of the tools/*
    # modules (register_all at boot) — we read it on demand, never at import.
    from ..connectors import flow as connector_flow
    from ..connectors import identities as connector_identities
    from ..connectors import verify as connector_verify
    return [
        {
            "name": c.name,
            "label": c.label,
            "help": c.help,
            # Curated 2-3 sentence description (catalog card) — "" if not written,
            # the front falls back to `help`.
            "description": c.description,
            # User-facing "how-to" doc (prerequisites/setup/usage), markdown per section.
            "doc_sections": [
                {"kind": s.kind, "title": s.title, "body_md": s.body_md}
                for s in c.doc_sections
            ],
            "href": c.href,
            "publisher": c.publisher_name,   # publisher (curated) — catalog
            "logo_url": c.logo_url_for(),     # publisher logo (oto-media), None if absent
            "availability": c.availability,
            "auth_modes": sorted(c.auth_modes),
            "personal_session": c.personal_session,
            "secret_kind": c.secret_kind,
            # Unified auth descriptor (ADR 0024) — method/cardinality/fields.
            # Source of the card's credential widget; `secret_kind` stays exposed
            # during the transition (derivable from one another).
            "auth": c.auth,
            "namespaces": list(c.namespaces),
            "family": c.family,        # builder axis (derived) — ADR 0011
            "category": c.category,    # user axis (curated) — ADR 0011
            # Credential input schema (generic multi-field model) — the
            # dashboard renders the form by looping over it. Never a value,
            # just the shape (name/label/secret/when/choices).
            # DERIVED from `auth["fields"]`, not copied: the two lists described the
            # same thing in two places, and a field added to one was silently missing from
            # the other (noticed when adding `when`/`choices`, #449).
            "credential_fields": c.auth["fields"],
            # Free-tier (ADR 0031): platform key open without grant, free quota
            # per user/day. The dashboard shows a "free: N/d" badge on the USER side.
            "free_tier": {"daily_quota": c.default_quota} if c.platform_key_open else None,
            # Identity selector (ADR 0024): the connector lets you choose a default
            # identity/target (pennylaneged: the company = ITS GED). The
            # USER card derives its picker from it (google/unipile have their own widget).
            "identities": connector_identities.supports(c.name),
            # Credential probe ("test the connection" framework): the connector has
            # registered a side-effect-free `verify` (zoho…). The card then shows
            # a "test the connection" button next to the "key set" state.
            "verifiable": connector_verify.supports(c.name),
            # SHAPE of the "connect" gesture (label + expected parameters), or None
            # for the ~56 connectors without a flow. Never a URL or a
            # capability name: /api/connectors is served WITHOUT auth, and the path is
            # fixed client-side. See `connector_flow`.
            "connect": connector_flow.describe(c.name),
        }
        for c in _REGISTRY_LIST
    ]

