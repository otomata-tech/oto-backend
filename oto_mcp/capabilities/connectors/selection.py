"""Capabilities "connector selection" — marketplace (ADR 0019).

Per-member, scoped to the active org (`SUB_ONLY` injects `ctx.org_id`). Three distinct
facts (cf. `connector_selection`): exposure (`connector_activation`, the ceiling),
proposal (`orgs.default_connectors`), selection (`user_selected_connectors`).

- `connectors.me` (read) = the catalog exposed for the active org, merged with
  the per-member state (`not_selected` | `active` | `paused`) + `recommended` (org baseline).
  Single source consumed by the dashboard (library + "my connectors").
- `connectors.select` / `.pause` / `.unselect` (mutation) = installs / pauses /
  removes a connector. Guard: refuse a connector not exposed for the active org
  (the `connector_activation` exposure ceiling is never relaxed).

SYNC handlers (the adapters don't await). NOMINAL regime (ADR 0050):
"not selected = hidden" — seeding a new (sub, org) installs the curated
`default_active` base; selecting/pausing takes visibility effect at the next
session (`session_visibility`).
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

from ... import access, db, org_store, providers, session_org, tool_registry
from ...connectors import activation as connector_activation
from ...connectors import cardinality as connector_cardinality
from ...connectors import credential_presence
from ...connectors.credential_presence import CredentialPresence
from ...connectors import readiness as connector_readiness
from ...connectors import selection as connector_selection
from ...connectors import usable as connector_usable
from .._authz import ORG_ADMIN_OF, SUB_ONLY
from .._types import (AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding)
from ..registry import CAPABILITIES
from . import custom_api
from .kit import (BulkSelectInput, BulkSelectResult,  # noqa: F401 — re-exported
                  OrgRecommendedConnectors, RecommendInput, UnsetDefaultInput,
                  UnsetDefaultResult, _bulk_select, _recommend, _unset_default)
from .catalog_card import (AuthDescriptor, ConnectFlow, CredentialField,
                           DocSection, FreeTier)

logger = logging.getLogger(__name__)

# Route placeholder {id} → `org_id` Input field mapping (real routes use {id}).
_ID = {"id": "org_id"}


class MyConnectorsInput(BaseModel):
    """Filter/projection of `connectors.me`. Default = **compact** (identity + state):
    the full view (doc_sections/auth/credential_fields/…) bloats the payload to ~90 KB
    for 55 connectors and exceeds the MCP token ceiling (oto-backend#109).

    `name` = state read of ONE connector. It was declared on `oto_connector`
    (for select/pause/…) but **silently ignored** on op=list → the agent that
    passed it received the whole catalog (~30k tokens in verbose) without the slightest
    warning (feedback #326). A `name` that matches nothing raises, never an empty
    list: a mute filter is what cost the context."""
    verbose: bool = False                # True = full payload (dashboard / credential setup)
    state: Optional[str] = None          # filter: not_selected | active | paused
    # filter: ONE connector (targeted state read). Exact name, or label / namespace /
    # word of the name (#1112: "linkedin" finds `linkedin_unipile` AND `aiark`, whose
    # tools are `linkedin_aiark_*`) — several candidates are ALL returned.
    name: Optional[str] = None
    # Card blocks added to the COMPACT row, for a LIST that renders the detail of every
    # connector in ONE call (#1245). `card` = the whole catalog card EXCEPT
    # `doc_sections`; `docs` = `doc_sections`. `card` + `docs` = the verbose row. Why
    # split: on the served catalog (145 connectors, 13/10/2026) the verbose list weighs
    # ~710 KB, 72 % of it the markdown how-to that a front only shows in the panel of an
    # unconfigured connector — and reading card by card instead cost one call per
    # connector, each paying a readiness diagnosis the list does not need. An URL
    # carries no list: `?include=card,docs` and `?include=card&include=docs` both work
    # (#367). An unknown value is REFUSED, never ignored.
    include: Optional[list[str] | str] = None

    @field_validator("include", mode="after")
    @classmethod
    def _include_en_liste(cls, v):
        """Same pattern as `projects.include` (#367): normalised ONCE, here."""
        if v is None:
            return None
        # Each entry split in turn: `?include=card,docs&include=…` combines both forms.
        brut = [p for x in (v if isinstance(v, list) else [v]) for p in str(x).split(",")]
        valeurs = [m for m in (x.strip() for x in brut) if m]
        inconnues = sorted(set(valeurs) - set(_INCLUDE_BLOCKS))
        if inconnues:
            raise ValueError(f"include: unknown value(s) {inconnues} — accepted: "
                             f"{list(_INCLUDE_BLOCKS)}")
        return valeurs

    @model_validator(mode="after")
    def _verbose_ou_include(self):
        # `verbose=true` already IS the whole card: combining it with `include` would let
        # one of the two be silently ignored.
        if self.verbose and self.include:
            raise ValueError("pass either verbose=true (the whole card) or include=… "
                             "(card blocks on the compact row), not both")
        return self


class ConnectorActionInput(BaseModel):
    name: str                            # connector name (providers/ registry)


class ReachableInstance(BaseModel):
    """A key of the connector that exists within the member's reach WITHOUT being theirs
    (a team they belong to, another org) — discoverability, not a right: usage goes
    through a pin (`_group=`/`_org=`/`_instance=`) and is re-guarded at call time."""
    kind: Literal["group", "org"]
    id: int
    name: str


class MyConnectorRow(BaseModel):
    """A catalog connector as seen by the member: the card + THEIR state.

    **Two projections come out of the same field**, depending on `verbose` (the envelope echoes it):

    - `verbose=false` (default) — identity + sort axes + state: the eight keys of
      `_COMPACT_KEYS`, plus the per-member state. This is what a LIST reads.
    - `verbose=true` — the same row PLUS the whole public catalog card,
      "card" section below. This is what a CARD reads, and what the credential form
      depends on.
    - `include=card` (#1245) — the compact row PLUS the card WITHOUT `doc_sections`:
      the detail of every connector in ONE list call, at a third of the verbose weight.
      `include=card,docs` is the verbose row.

    A card field is therefore `Optional` because it is absent in compact, not
    because it would sometimes be null in verbose: with `verbose=true` all thirteen are
    always present (`base = c`, the whole row — cf. `_me`). `null` keeps its own
    meaning there, stated field by field.

    ⚠️ The docstring said until 2026-09-01 that "the wide form is the dashboard's, it is
    not frozen here" — and the openness to additional fields followed from it. Lifted
    (#667): as soon as an integrator outside the repo depends on it, "not frozen"
    means "breakable without notice, and without knowing who breaks". The thirteen
    top-level keys that verbose mode served without declaring them are now
    named (cf. `catalog_card.py` for the objects).

    `extra="allow"` is removed (oto-backend#742): the ratchet
    `tests/connectors/test_carte_connecteur_declaree.py`, put in place by #738, has served —
    it is now the mechanical safeguard against the next undeclared field.
    A forgotten field would disappear from the payload rather than stay silently tolerated."""

    name: str
    label: Optional[str] = None
    help: Optional[str] = None
    family: Optional[str] = None            # builder axis (derived)
    category: Optional[str] = None          # user axis (curated)
    availability: Optional[str] = None
    # None = DECLARED absence of a brand logo (generic/in-house) → monogram
    # on the UI side, not a failed load.
    logo_url: Optional[str] = None
    # NATURE of the expected credential — api_key|basic_auth|fields|oauth|cookie|none
    # — not its state: `none` says "this connector works without bringing anything
    # at all", and says nothing about the key that is set (that is `providers[name]`
    # of `/api/me`). The only auth field of compact mode; cf. `_COMPACT_KEYS`.
    secret_kind: Optional[str] = None
    state: Literal["not_selected", "active", "paused"]
    # WHO set the installation (ADR 0050 §E7) — present only when `state` ≠
    # `not_selected`. `kit` = installed by your organization (removal from the kit
    # removes it); `membre` = by you; `admin` = pushed to you by an admin; `socle` =
    # by default by the platform; `inconnue` = set before the platform
    # tracked it — the latter is never removed by an org gesture; `cle` = a key
    # usable for you made it active (active = usable, `connectors/usable.py`).
    origin: Optional[Literal["socle", "kit", "admin", "membre", "inconnue", "cle"]] = None
    # Date ("YYYY-MM-DD HH:MM:SS", UTC) at which YOU removed this connector — present only when
    # `state` = `not_selected` and the removal came from you. No org gesture (kit,
    # push) reinstalls it while it is set; reinstalling it yourself clears it.
    removed_at: Optional[str] = None
    # Baseline proposed by the ORG (ADR 0019), never the member's state: a
    # `recommended` connector may very well be `not_selected`.
    recommended: bool
    # Number of org procedures that cite a namespace of the connector — derived
    # from the guide bodies, best-effort (0 on a read incident, no error).
    guide_ref_count: int
    doctrine_ref_count: int                # deprecated ALIAS (removal 29/10/2026, #519)
    paid_option: Optional[str] = None       # required paid option (layer 3), None = none
    # `true` = the option is lifted OR none is required. Says NOTHING about the credential:
    # an `option_ok` connector remains unusable without a key set.
    option_ok: bool
    # Present ONLY if a key is within reach without being installed (the row
    # stands out instead of adding an empty field on 40 rows). Its ABSENCE does not
    # prove there is none: the batch does not cover "my member key
    # in another org" (accepted limit of `access.reachable_instances_map`).
    reachable_instances: Optional[list[ReachableInstance]] = None
    # ── EFFECTIVE readiness (#476) — present ONLY on a targeted read
    # (`name=`), cf. `readiness` on the envelope. `state` answers "did I install it?",
    # `ready` answers "does it work?": ORTHOGONAL, and the signal was born from
    # confusing them. Layer details and cost: `connectors/readiness.py`.
    ready: Optional[bool] = None
    # The FIRST layer that is missing: paid_option_off | no_credential | over_quota |
    # credential_rejected | pending_step. Absent when `ready` is true.
    not_ready: Optional[str] = None
    next_step: Optional[str] = None         # the gesture, rendered as is (never reworded)
    # ── AVAILABLE credential (#1112) — on the WHOLE catalog, not only on a
    # targeted read. Present when a key or an account exists for you at a level of the
    # cascade, EVEN if `state` is `not_selected`: this is the line that was missing
    # when two agents read `not_selected` as "not connected". Third axis,
    # "verified alive", never computed here: `credential.next_step` names the tool.
    credential: Optional[CredentialPresence] = None

    # ── The CARD (`verbose=true` only) ───────────────────────────────────────────
    # The thirteen keys that `providers.public_catalog()` puts on the whole row.
    # Served all along, declared since 2026-09-01 (#667): without them in the
    # contract, a third-party front end could not render a credential form, even
    # though the data arrived. The order follows the producer's, so the two
    # lists can be read side by side.
    #
    # Curated 2-3 sentence description (catalog card). `""` if not written — the front end
    # then falls back on `help`, which `null` would not say.
    description: Optional[str] = None
    doc_sections: Optional[list[DocSection]] = None
    href: Optional[str] = None              # connector's website; `null` = none
    publisher: Optional[str] = None         # publisher (curated)
    auth_modes: Optional[list[str]] = None  # ⊆ {byo_user, byo_org, platform}
    # "Browser session" category (the credential is a session, not a key).
    personal_session: Optional[bool] = None
    # The unified auth descriptor (ADR 0024) — it is what drives the credential
    # widget, `secret_kind` being only the sort scalar kept in compact.
    auth: Optional[AuthDescriptor] = None
    namespaces: Optional[list[str]] = None  # prefixes of the owned tools
    # DERIVED from `auth.fields`, not copied — hence always identical. Both keys
    # are served, both are declared: deprecate whoever wants, but not silently
    # (the shape of a served-name deprecation is #519, and it is dated).
    credential_fields: Optional[list[CredentialField]] = None
    free_tier: Optional[FreeTier] = None
    # Does the connector offer to pick a default identity/target (ADR 0024)?
    identities: Optional[bool] = None
    # Has the connector registered a side-effect-free credential probe? If
    # so, the card shows "test the connection" next to the "key set" state.
    verifiable: Optional[bool] = None
    connect: Optional[ConnectFlow] = None


class ToolboxScope(BaseModel):
    """The gap between the org the SESSION was mounted for and the one the call
    PINS (#577). Present only when the two differ — cf. `_toolbox_scope`."""
    mounted_for_org: Optional[int] = None
    listing_for_org: Optional[int] = None
    note: str


class NameMatch(BaseModel):
    """`name` was not an exact name: what it found, and how (#1112). Absent
    on an exact name. Several candidates = all returned as rows, none is chosen
    in place of the caller."""
    query: str
    candidates: list[str]
    note: str


class MyConnectors(BaseModel):
    """The catalog exposed to the active org, merged with the per-member state.
    UNIQUE source of the dashboard's library AND "my connectors"."""
    connectors: list[MyConnectorRow]
    # Echo of the requested projection — tells the client whether the rows carry the
    # full card or the compact view (both shapes come out of the SAME field).
    verbose: bool
    # Echo of `include` (#1245): the card blocks added to the compact rows — `card`
    # (the card without `doc_sections`), `docs` (`doc_sections`). Empty = none.
    include: list[str] = []
    # `computed` (targeted `name=` read) | `not_computed` (catalog: too expensive,
    # cf. `connectors/readiness.py`) | `unavailable` (reading the layers failed).
    # ALWAYS says something: a mute absence of `ready` read as "nothing to
    # report", and that shortcut cost five days (#476).
    readiness: str = "not_computed"
    readiness_hint: Optional[str] = None    # the gesture to get it, when we don't have it
    # `computed` | `unavailable` (#1112) — the computation of `credential` on the rows.
    # `unavailable` = the snapshot could not be read: a row WITHOUT `credential` then says
    # NOTHING (it does not mean "nothing is connected").
    credentials: str = "computed"
    name_match: Optional[NameMatch] = None
    toolbox_scope: Optional[ToolboxScope] = None
    # `name=` designates one of the org's custom connectors (`custom_api.py`): that one,
    # and `connectors` is empty.
    custom_connector: Optional[dict] = None


class ConnectorSelectionState(BaseModel):
    """Selection state of ONE connector for the member, after a mutation. Common
    shape for `select` / `pause` / `unselect` — same object (the connector's
    membership in the toolbox), the extras differ by verb."""
    connector: str
    state: Literal["active", "paused", "not_selected"]
    # `select` only: the connector's tools, read from the BOOT registry. Empty if
    # the registry is not warmed up (script outside the server) — not a connector
    # without tools.
    tools: Optional[list[str]] = None
    # `select` only: the instructions of the moment — the tools are NOT mounted in the
    # current conversation (registry frozen at open), you must go
    # through `oto_call` or reopen a conversation.
    hint: Optional[str] = None
    # `unselect` only, always `True` on success (oto#42/oto-backend#868):
    # a removal that found nothing now REFUSES (`connector_not_selected`, 404)
    # instead of returning `removed: false` on a 200 — a success that did nothing is
    # worse than a refusal, same pattern as the project unlink (`d3c5de40`).
    removed: Optional[bool] = None


def _visible_catalog(ctx: ResolvedCtx) -> list[dict]:
    """Catalog exposed for the caller's active org — mirror of the filtering in
    `api_routes_public.connectors_catalog`: activation (ceiling).

    ⚠️ **The served row leaves here with its EFFECTIVE cardinality**, not the code's
    (oto-backend#732). `providers.public_catalog()` sets `auth.cardinality` from the
    registry, which is pure and therefore cannot read an org override — yet that is the
    key the dashboard's connection panel reads to decide whether to offer a second
    account. The seam is HERE and not at the call site: it is the single passage point
    from the card to `connectors.me` AND `oto_search`, hence the only place where one
    cannot be forgotten."""
    exposed = connector_activation.exposed_connectors(ctx.org_id)
    out = [c for c in providers.public_catalog() if c["name"] in exposed]
    return connector_cardinality.overlay_for_org(out, ctx.org_id)


def _guide_refs_by_ns(org_id: int | None) -> dict[str, set]:
    """namespace → set of the org's guides that reference it (`<tool:slug>`).
    Empty if no org. Pure derivation from the guide bodies ("guide-only"
    posture, ADR 0024) — best-effort, never makes the read fail."""
    if not org_id:
        return {}
    try:
        refs: dict[str, set[str]] = {}
        for d in org_store.list_instruction_bodies("org", org_id):
            slug = d.get("slug") or ""
            for ns in tool_registry.namespaces_in(d.get("body_md") or ""):
                refs.setdefault(ns, set()).add(slug)
        return refs
    # noqa: SILENT — unreadable guide refs ⇒ empty overlay, catalog served
    except Exception:
        return {}


# Fields kept in COMPACT mode (MCP default): identity + sort axes + state.
# The big fields (doc_sections/auth/credential_fields/description/namespaces) only
# come back with `verbose=True` (dashboard, credential setup). Cf. #109.
# `secret_kind` is in the COMPACT, and not only in the verbose card:
# it is the only scalar that distinguishes "this connector asks for no key"
# (open data — `none`) from "it asks for one you don't have yet". Without it
# at load time, a table cannot say so, and the absence of an entry in
# `providers` is NOT enough to decide: `scaleway` and `http` are absent from it too
# while requiring a credential. The front end
# therefore showed "Not connected" on OpenStreetMap until the card was opened,
# where the verbose said "Included" (observed in prod on 2026-08-29).
# Already-computed scalar, no cost: compact avoids `auth`/`credential_fields`
# for their lists, not for a single word.
_COMPACT_KEYS = ("name", "label", "help", "family", "category", "availability",
                 "logo_url", "secret_kind")

# `include` blocks (#1245): `docs` is the how-to, `card` everything else on the card.
_INCLUDE_BLOCKS = ("card", "docs")


def _projection(c: dict, inp: "MyConnectorsInput") -> dict:
    """The catalog row as the request asks for it: compact, verbose, or compact plus
    `include` blocks. `card` + `docs` gives exactly the verbose row."""
    if inp.verbose:
        return c
    blocs = set(inp.include or ())
    if not blocs:
        return {k: c.get(k) for k in _COMPACT_KEYS}
    return {k: v for k, v in c.items()
            if k in _COMPACT_KEYS
            or (k == "doc_sections" and "docs" in blocs)
            or (k != "doc_sections" and "card" in blocs)}


def _toolbox_scope(sub: str) -> Optional[dict]:
    """The gap between the org the SESSION was mounted for and the one the call
    pins — or None if there is none (signal #577).

    Proven by differential on prod on 28/08/2026. An MCP session's toolbox is
    computed AT THE HANDSHAKE (`session_visibility.compute_hidden_tools`, called at
    `on_initialize`): at that moment no `_org=` token exists, so `current_org`
    falls back on the HOME org. A scheduled session then pins `_org=` on EACH
    call — but the tool registry is frozen for the home. The sub running
    the #577 procedure has org 42 as home (`folk`, `grain` selected)
    and works on org 196 (thirteen connectors, including `granola`, `slack`, `linear`).
    Hence "no connector tool comes up" — whereas the seven cited all
    answered first time via `oto_call`.

    This is a VISIBILITY defect, never an access or credential one. Nowhere did the card
    say so: three mornings (20-22/08) of false "Linear is down" reports.

    Said ONLY on a real gap: an always-present field becomes noise that people
    stop reading. No call token (the dashboard's REST face) ⟹ no MCP session
    whose box could diverge ⟹ nothing to announce.

    ⚠️ TWO consumers since 03/09: this card, and `oto_list_my_tools`
    (`tools/meta.py`). Put here alone, the admission was only read by whoever already called
    `oto_connector` — yet an agent looking for a tool calls `oto_list_my_tools`, and
    read an empty list there without a word. Takes only a `sub` for that reason: the
    fact is a property of the SESSION, not of the connectors."""
    call_org = session_org.current_call_org()
    if call_org is None:
        return None
    home = org_store.get_active_org(sub)
    if home == call_org:
        return None
    return {
        "mounted_for_org": home,
        "listing_for_org": call_org,
        "note": (
            f"This session's toolbox was mounted for org {home} (your "
            f"home org at handshake time), not for org {call_org} that this "
            f"call pins: the tools of the connectors active here may NOT "
            f"be listed. They remain callable through "
            f"`oto_call(name=..., arguments={{...}})` — a tool missing from the list "
            f"is NOT a broken connector."),
    }


# ── Finding a connector by what the caller KNOWS of it (#1112) ─────────────────
# The agent doesn't know `linkedin_unipile`: it knows "LinkedIn", the card's label.
# `name="linkedin"` answered "unknown or unavailable" — a curt refusal, which
# the agent read as "no LinkedIn" and handed back to the user. Same for
# `linkedin_aiark`, which is the NAMESPACE of the `aiark` connector's tools.
#
# The `providers/` registry is the only source (the catalog is not a table,
# #905): no persisted alias, we read what each connector already declares — its name,
# its label, its namespaces.

# Beyond this, the targeted read stops being one: the readiness verdict (~244 ms
# each, `connectors/readiness.py`) is not computed, and we say so.
_CANDIDATS_DIAGNOSTIQUES = 5


def _normalise(texte: str) -> str:
    """Case, accents and separators neutralized: "LinkedIn", `linkedin`,
    `linked-in` don't differ for whoever searches."""
    plat = unicodedata.normalize("NFKD", texte or "")
    plat = "".join(ch for ch in plat if not unicodedata.combining(ch)).lower()
    return " ".join(re.split(r"[^a-z0-9]+", plat)).strip()


def _formes(c: dict) -> set[str]:
    """The names under which this connector can be REFERRED TO: name, label,
    namespaces — normalized."""
    return {f for f in (_normalise(c.get("name") or ""), _normalise(c.get("label") or ""),
                        *(_normalise(ns) for ns in c.get("namespaces") or [])) if f}


def _resoudre_nom(catalog: list[dict], demande: str) -> list[dict]:
    """The rows that `demande` designates: the exact name first (alone), otherwise every
    row one of whose forms (name, label, namespace) EQUALS the request or contains
    each of its words. "linkedin" → `linkedin_unipile` (label) and `aiark` (namespace
    `linkedin_aiark`). Empty = nothing matches; it is up to the caller to refuse."""
    exact = [c for c in catalog if c["name"] == demande]
    if exact:
        return exact
    q = _normalise(demande)
    if not q:
        return []
    mots = set(q.split())
    egal, contient = [], []
    for c in catalog:
        formes = _formes(c)
        if q in formes:
            egal.append(c)
        elif any(mots <= set(f.split()) for f in formes):
            contient.append(c)
    return egal + contient


def _suggestions(catalog: list[dict], demande: str, n: int = 5) -> list[str]:
    """The names CLOSE to `demande` (spelling, prefix) — what a refusal offers
    instead of a curt "unknown". The connector's exact name, never its label: it is
    what the next call must carry."""
    q = _normalise(demande)
    if not q:
        return []
    par_forme: dict[str, str] = {}
    for c in catalog:
        for f in _formes(c):
            par_forme.setdefault(f, c["name"])
    proches = [par_forme[f] for f in difflib.get_close_matches(q, par_forme, n=n * 2,
                                                               cutoff=0.6)]
    proches += [nom for f, nom in par_forme.items() if q in f or f in q]
    return list(dict.fromkeys(proches))[:n]


def _refus_nom_inconnu(catalog: list[dict], demande: str,
                       ctx: Optional[ResolvedCtx] = None) -> AuthzDenied:
    """The refusal of a name that designates nothing — NAMED and offering suggestions
    (#1112), and the Custom API way out when the org can reach it."""
    proches = _suggestions(catalog, demande)
    if proches:
        suite = (f" Close names: {', '.join(f'`{n}`' for n in proches)} — retry with "
                 f"one of them (`oto_connector(op='list', name='{proches[0]}')`).")
    else:
        suite = (" No close name: `oto_connector(op='list')` without `name` returns the "
                 "compact catalog with the exact names.")
    repli, extra = custom_api.repli(catalog, demande, ctx)
    return AuthzDenied(
        404, "unknown_connector",
        f"No connector available to your active org is named `{demande}` or carries "
        f"this label (or it is not open to your org).{suite}{repli} This refusal says "
        f"NOTHING about your connections: it concerns the name.",
        details={"query": demande, "suggestions": proches, **extra})


def _with_readiness(ctx: ResolvedCtx, row: dict) -> dict:
    """Sets `ready` / `not_ready` / `next_step` on THE requested row, and returns what
    the envelope must say about the computation.

    Fail-VISIBLE and not fail-open: if the layers cannot be read, we return
    `readiness:"unavailable"` instead of silently omitting `ready`. Omitting would
    reproduce the very defect of #476 — an absence the caller reads as "nothing to
    report"."""
    try:
        diag = connector_readiness.diagnose(
            ctx.sub, row["name"], org=ctx.org_id,
            # Explicit: `credential_mode_for` would otherwise re-derive it (73 % of the time
            # of a measured card), and above all the context must be the SUBJECT's.
            group=access.current_group(ctx.sub))
    except Exception:
        logger.warning("readiness unavailable for %s (fail-visible)", row["name"],
                       exc_info=True)
        return {"readiness": "unavailable",
                "readiness_hint": ("The real state could not be read (key/option "
                                   "layers unavailable) — `state` above says "
                                   "ONLY your selection, not whether the connector works.")}
    if diag is None:
        row["ready"] = True
    else:
        row["ready"] = False
        row["not_ready"] = diag.reason
        row["next_step"] = diag.next_step
    return {"readiness": "computed"}


def _me(ctx: ResolvedCtx, inp: MyConnectorsInput) -> dict:
    """`GET /api/me/connectors`: ONE pool connection for the whole read
    (oto-backend#1148). The path does ~160 unit reads (selection, vault,
    cascade, options, OAuth apps); each one borrowed its own connection and paid its
    `BEGIN`/`COMMIT` — 3 round trips per read, and as many waits on the pool under
    load: median 3.0 s, p95 15.7 s in production on 03-04/10/2026. Same envelope
    as `access.status_for`, and same condition: the path ONLY reads (a test bench
    records each request, `tests/test_connecteurs_me_une_connexion_1148.py`)."""
    with db.reuse_connection():
        return _me_projection(ctx, inp)


def _me_projection(ctx: ResolvedCtx, inp: MyConnectorsInput) -> dict:
    org_id = ctx.org_id or 0
    detail = connector_selection.list_selection_detail(ctx.sub, org_id)
    selection = {name: d["state"] for name, d in detail.items()}
    removed = connector_selection.list_removed(ctx.sub, org_id)
    recommended = set(org_store.get_org_default_connectors(ctx.org_id) or []) if ctx.org_id else set()
    doc_refs = _guide_refs_by_ns(ctx.org_id)
    # Discoverability: a key may exist within reach (a team I belong to,
    # another org) without the cascade reading it — the connector then looks "empty"
    # in the library even though it is usable by pinning. Until now the info
    # only existed AFTER a failed call ("nothing resolves" error), hence never if
    # the connector is not installed (the call dies at dispatch). One
    # BATCHED pass, outside the loop, so as not to pay N×M queries.
    reach = access.reachable_instances_map(ctx.sub, ctx.org_id)
    catalog = _visible_catalog(ctx)
    name_match: Optional[dict] = None
    if inp.name:
        trouves = _resoudre_nom(catalog, inp.name)
        if not trouves:
            # Not exposed for the active org, restricted by the org RBAC, or unknown name
            # — indistinguishable on the member side (same verdict as `_require_exposed`). But
            # the refusal OFFERS suggestions: a curt "unknown" was read as "not connected" (#1112).
            perso = custom_api.projection_for_name(ctx, catalog, inp.name,
                                                   verbose=inp.verbose, include=inp.include)
            if perso is not None:
                return perso
            raise _refus_nom_inconnu(catalog, inp.name, ctx)
        if [c["name"] for c in trouves] != [inp.name]:
            name_match = {
                "query": inp.name,
                "candidates": [c["name"] for c in trouves],
                "note": (f"`{inp.name}` is not an exact name: "
                         + (f"it designates `{trouves[0]['name']}`." if len(trouves) == 1 else
                            f"{len(trouves)} connectors match it, all returned — "
                            f"none is chosen for you.")
                         + " The gestures (select/pause/unselect) take the exact name."),
            }
        catalog = trouves
    # AVAILABLE credential (#1112), on all rows — the same function as
    # `oto_list_my_tools`. Fail-VISIBLE: an unreadable snapshot SAYS SO in the
    # envelope, otherwise rows without `credential` would be read again as "nothing is
    # connected", the very conclusion being repaired.
    presence, credentials = credential_presence.lire(ctx.sub, org=ctx.org_id)
    # The layer-3 option is judged by (option, credential holder): the channels of a
    # hosted account share the option AND the key of their holder, hence the same verdict.
    # One cascade walk per pair, not one per row (#1148).
    options_ouvertes: dict[tuple, bool] = {}

    def _option_ok(nom: str) -> bool:
        cle = (access.paid_option_for(nom), providers.credential_provider(nom))
        if cle not in options_ouvertes:
            options_ouvertes[cle] = access.option_open(ctx.sub, nom, org=ctx.org_id)
        return options_ouvertes[cle]

    connectors = []
    for c in catalog:
        state = selection.get(c["name"], "not_selected")
        if inp.state and state != inp.state:
            continue
        refset: set = set()
        for ns in c.get("namespaces") or []:
            refset |= doc_refs.get(ns, set())
        base = _projection(c, inp)
        # Consent return URL — on the AUTHENTICATED projection only.
        # `providers.public_catalog()` also feeds `/api/connectors`, served without
        # auth: the public descriptor stays without a URL (cf. `connector_flow.describe`).
        # It is DERIVED from the environment, never written: it is the value the
        # client must register with its provider, and hard-coded prose lies
        # as soon as it is read from preprod (experienced: an incomprehensible
        # `redirect_uri_mismatch` on the client side).
        # `app_ready` follows the same rule (answer specific to the REQUESTER, hence never to the
        # public catalog): it tells the front end whether an app remains to be set before
        # being able to consent. Without it, the connection screen promised the worst case to
        # everyone — "first set the application's credentials", including to whoever has nothing left to set
        # since oto publishes its own. `fixed`, same rule: what the requester's app
        # already binds (the Zoho region of their team's app) is shown, not asked.
        if base.get("connect"):
            from ...connectors import flow as connector_flow
            base = {**base, "connect": {
                **base["connect"],
                "callback_url": connector_flow.callback_url(c["name"]),
                "app_ready": connector_flow.app_ready(c["name"], ctx.sub),
                "fixed": connector_flow.fixed(c["name"], ctx.sub),
                "warnings": connector_flow.warnings(c["name"], ctx.sub)}}
        # Layer 3 (paid option) on the USER surface — without it the front end cannot
        # say WHICH of the 3 conditions is missing (the "State for you" banner, ADR 0044):
        # `mode==forbidden` conflates option/activation/RBAC. `option_ok=True` if no
        # option is required. Verbose only (compact = catalog).
        opt = access.paid_option_for(c["name"])
        # `option_ok` = SINGLE SOURCE `access.option_open` (shared with status_for):
        # no option ⟹ ok; otherwise BYO (own key) OR has_option. One single place
        # decides "usable" → no more divergence "org key" card + "Blocked".
        row = {
            **base,
            "state": state,
            "recommended": c["name"] in recommended,
            "guide_ref_count": len(refset),
            "doctrine_ref_count": len(refset),   # deprecated ALIAS (removal 29/10/2026)
            "paid_option": opt,
            "option_ok": _option_ok(c["name"]),
        }
        # Present ONLY if an instance is within reach → the row stands out instead of
        # adding an empty field on 40 rows. Deliberately distinct from
        # `recommended` (= the org's baseline): overloading the latter would make
        # the org setting lie. This is VISIBILITY: access is judged at call time.
        if reach.get(c["name"]):
            row["reachable_instances"] = reach[c["name"]]
        if c["name"] in presence:
            row["credential"] = presence[c["name"]]
        # Provenance and removal (ADR 0050 §E7): set only when they say
        # something, for the same reason as `reachable_instances`.
        if c["name"] in detail:
            row["origin"] = detail[c["name"]]["origin"]
        elif c["name"] in removed:
            # Already a string: `db`'s row factory renders timestamps as
            # "YYYY-MM-DD HH:MM:SS" (UTC, no timezone) — served as is.
            row["removed_at"] = str(removed[c["name"]])
        connectors.append(row)
    out: dict = {"connectors": connectors, "verbose": inp.verbose,
                 "include": inp.include or [],
                 "credentials": credentials}
    if name_match is not None:
        out["name_match"] = name_match
    # Readiness verdict (#476) — on a TARGETED read only. Measured on prod
    # on 28/08/2026: rendering it on the whole catalog costs 1,993 ms for 90
    # connectors, on a SINGLE-LOOP server. So we don't compute it — but we
    # SAY so, otherwise the absence of `ready` is read as "nothing to report", which is
    # precisely the shortcut being repaired. A search by label may return
    # a few candidates (#1112): each gets its verdict, within a bound.
    if inp.name and connectors and len(connectors) <= _CANDIDATS_DIAGNOSTIQUES:
        verdicts = [_with_readiness(ctx, row) for row in connectors]
        out.update(next((v for v in verdicts if v["readiness"] == "unavailable"),
                        verdicts[0]))
    else:
        out["readiness"] = "not_computed"
        out["readiness_hint"] = (
            "Readiness not computed on a catalog (too expensive for a "
            "single-loop server). THREE axes, not to be confused: `state` = your SELECTION in "
            "the toolbox (`not_selected` does NOT mean not connected); `credential` "
            "= a key or an account exists for you (absent = none resolves); "
            "alive = never verified here, `credential.next_step` names the tool that "
            "verifies it. For a connector's readiness, ask for it alone — "
            "`oto_connector(op='list', name='<connector>')` returns `ready` and, if it "
            "isn't, the missing step.")
    tb = _toolbox_scope(ctx.sub)
    if tb is not None:
        out["toolbox_scope"] = tb
    return out


def _require_exposed(ctx: ResolvedCtx, name: str) -> None:
    """Ceiling: a connector not exposed for the active org can be neither
    selected nor paused (deny-by-default never relaxed)."""
    if name not in connector_activation.exposed_connectors(ctx.org_id):
        # A gesture is NOT resolved by label (it writes): it requires the exact name, but
        # its refusal offers the close names, like the read (#1112).
        raise _refus_nom_inconnu(_visible_catalog(ctx), name, ctx)


# Post-activation guidance (oto-backend#111). An MCP session's tool registry is
# FROZEN when the conversation opens: a connector activated mid-session does not mount
# its tools there (the `tools/list_changed` hot-reload is not applied by
# claude.ai). The reliable bridge = `oto_call` (universal dispatch, ADR 0036); otherwise, a new
# conversation. We TELL the agent at the moment it installs, so it carries on without
# concluding "the capability doesn't exist".
def _connector_tools(name: str) -> list[str]:
    """Names of the connector's tools, from the BOOT registry (immune to
    session visibility) — the "discovery" half of #186: the hint ordered
    "call via oto_call" without giving ONE SINGLE name, and session
    introspection did not see the tools of a freshly activated connector."""
    from ... import providers, tool_registry
    from ...tool_visibility import namespace_of
    con = providers.REGISTRY.get(name)
    ns = set(con.namespaces) if con else {name}
    return [t for t in tool_registry.boot_tool_names() if namespace_of(t) in ns]


def _activation_hint(name: str, tools: list[str]) -> str:
    listing = (f" Its tools: {', '.join(tools[:12])}." if tools
               else " (names unavailable — oto_tool_schema describes them on demand).")
    return (f"`{name}` is active. Its tools are not yet mounted in THIS conversation "
            f"(the tool registry is frozen at open) —{listing} Call them RIGHT "
            f"NOW via `oto_call(name=…, arguments={{…}})` (a tool's schema: "
            f"`oto_tool_schema`), or open a NEW conversation to see them listed.")


def _org_du_geste(ctx: ResolvedCtx) -> int:
    """The org under which a member's gesture files their selection — a REAL org,
    never the old `0` sentinel (#959). That `ctx.org_id or 0` wrote rows
    that no read returns anymore since the end of "personal without org" (ADR 0030 §8):
    with no active org, the gesture is REFUSED by name rather than filed out of sight."""
    if not ctx.org_id:
        raise AuthzDenied(400, "no_active_org",
                          "No active org: a connector selection is filed under "
                          "an org — pick one with oto_use_org.")
    return ctx.org_id


_REFUS_SANS_ORG = DeclaredError(400, "no_active_org",
                                "no active org: a selection is filed under a real "
                                "org, never out of sight")


def _select(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    org_id = _org_du_geste(ctx)
    _require_exposed(ctx, inp.name)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.ACTIVE, org_id)
    tools = _connector_tools(inp.name)
    return {"connector": inp.name, "state": "active", "tools": tools,
            "hint": _activation_hint(inp.name, tools)}


_REFUS_UTILISABLE = DeclaredError(
    409, "connector_usable",
    "a key is usable for you on this connector, so it is active (active = usable): "
    "it can't be paused or removed — the key's owner removes the key instead")


def _refuse_if_usable(ctx: ResolvedCtx, name: str, org_id: int) -> None:
    """Active = usable (`connectors/usable.py`): pausing or removing a connector a key
    answers for would be undone at the next session — the gesture would lie. Refused
    by name instead. A snapshot that can't be read lets the gesture through (the
    refusal is a courtesy, not a barrier: the next reconciliation says the rule)."""
    try:
        usable = connector_usable.is_usable(ctx.sub, org_id, access.current_group(ctx.sub), name)
    except Exception as e:
        logger.warning("usable check skipped for %s/%s (gesture allowed): %s", ctx.sub, name, e)
        return
    if usable:
        raise AuthzDenied(409, "connector_usable",
                          f"`{name}` has a key usable for you here, so it is active and stays "
                          "active (active = usable). To stop it, the key's owner removes the "
                          "key; nothing to pause or remove on your side.")


def _pause(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    org_id = _org_du_geste(ctx)
    _require_exposed(ctx, inp.name)
    _refuse_if_usable(ctx, inp.name, org_id)
    connector_selection.set_state(ctx.sub, inp.name, connector_selection.PAUSED, org_id)
    return {"connector": inp.name, "state": "paused"}


def _unselect(ctx: ResolvedCtx, inp: ConnectorActionInput) -> dict:
    # oto#42/oto-backend#868 — a removal that removes nothing no longer answers `ok`
    # (`removed: false` on top of a 200 read as an idempotent success;
    # `setExposure` on the dashboard side doesn't read that field anyway and sets the
    # local state as soon as the call didn't raise). REFUSES by name, on the same pattern
    # as the project unlink (`d3c5de40`): a success that did nothing is worse
    # than a refusal.
    org_id = _org_du_geste(ctx)
    _refuse_if_usable(ctx, inp.name, org_id)
    if not connector_selection.unselect(ctx.sub, inp.name, org_id):
        raise AuthzDenied(404, "connector_not_selected",
                          f"`{inp.name}` is not in your active selection for this org — "
                          "nothing was removed (already uninstalled, or never installed here). "
                          "`connectors.me` tells you what is.")
    return {"connector": inp.name, "state": "not_selected", "removed": True}


CAPABILITIES += [
    Capability(
        key="connectors.me", handler=_me, Input=MyConnectorsInput, authz=SUB_ONLY,
        Output=MyConnectors,
        description="List every connector available to you (the marketplace catalog) with your "
                    "per-workspace state: not_selected (in the library) / active / paused, plus "
                    "`recommended` when your org proposes it. Source for both the connector "
                    "library and your installed connectors. Returns a COMPACT row per connector "
                    "by default (name/label/family/category/state/…) — pass verbose=true for the "
                    "full card (doc, auth descriptor, credential fields). Filter with state="
                    "active|paused|not_selected, or with name=<connector> to read the state of "
                    "a SINGLE connector (pair it with verbose=true instead of pulling the whole "
                    "catalog). To render the detail of EVERY connector in one call, pass "
                    "include=card (the card without its markdown how-to `doc_sections`; "
                    "include=card,docs adds it) instead of reading card by card. "
                    "`name` also accepts a label or a tool namespace (\"linkedin\" "
                    "finds every LinkedIn connector, all returned); an unmatched name is refused "
                    "with the closest names. ⚠️ `state` is only YOUR SELECTION in the toolbox — "
                    "`not_selected` does NOT mean not connected. `credential` (on every row, even "
                    "not_selected) says a key or an account exists for you, at which level, and "
                    "its `next_step` names the tool that checks it is ALIVE (e.g. "
                    "linkedin_unipile_account op=status) — call it before telling anyone a "
                    "connector is not connected. A name=<connector> lookup also returns `ready` "
                    "(key resolves, paid option open, no step left) plus `not_ready`/`next_step` "
                    "when it doesn't; the whole catalog returns readiness:not_computed.",
        errors=(DeclaredError(404, "unknown_connector",
                              "name unknown to the registry, connector not exposed "
                              "for the active org, or restricted by a rule — "
                              "the three are indistinguishable on the member side, and "
                              "all are resolved by the same request to an admin"),),
        rest=RestBinding("GET", "/api/me/connectors"),
    ),
    Capability(
        key="connectors.select", handler=_select, Input=ConnectorActionInput, authz=SUB_ONLY,
        Output=ConnectorSelectionState,
        description="Install a connector into your active workspace (state=active). name = "
                    "connector name from the catalog. Its tools do NOT mount in the current "
                    "conversation (the tool registry is frozen at open) — the response `hint` "
                    "tells you to reach them right away via oto_call, or open a new conversation.",
        errors=(DeclaredError(404, "unknown_connector",
                              "name unknown to the registry, connector not exposed "
                              "for the active org, or restricted by a rule — "
                              "the three are indistinguishable on the member side, and "
                              "all are resolved by the same request to an admin"),
                _REFUS_SANS_ORG),
        rest=RestBinding("POST", "/api/me/connectors/{name}/select"),
    ),
    Capability(
        key="connectors.pause", handler=_pause, Input=ConnectorActionInput, authz=SUB_ONLY,
        Output=ConnectorSelectionState,
        description="Pause an installed connector (state=paused): kept installed but its tools "
                    "are hidden. Resume by selecting it again. REFUSES (connector_usable, 409) "
                    "when a key is usable for you on it: active = usable, it would come back "
                    "at your next session.",
        errors=(DeclaredError(404, "unknown_connector",
                              "name unknown to the registry, connector not exposed "
                              "for the active org, or restricted by a rule — "
                              "the three are indistinguishable on the member side, and "
                              "all are resolved by the same request to an admin"),
                _REFUS_SANS_ORG, _REFUS_UTILISABLE),
        rest=RestBinding("POST", "/api/me/connectors/{name}/pause"),
    ),
    Capability(
        key="connectors.unselect", handler=_unselect, Input=ConnectorActionInput, authz=SUB_ONLY,
        Output=ConnectorSelectionState,
        description="Remove a connector from your workspace (back to the library). Does not touch "
                    "credentials, only your selection. REFUSES (connector_not_selected, 404) if "
                    "it wasn't in your active selection for this org — it never answers ok on a "
                    "removal that found nothing. REFUSES (connector_usable, 409) when a key is "
                    "usable for you on it: active = usable.",
        errors=(DeclaredError(404, "connector_not_selected",
                              "the connector is not in your active selection for "
                              "this org: already removed, never installed here, or "
                              "installed under another active org"),
                _REFUS_SANS_ORG, _REFUS_UTILISABLE),
        rest=RestBinding("DELETE", "/api/me/connectors/{name}"),
    ),
    Capability(
        key="connectors.recommend", handler=_recommend, Input=RecommendInput,
        Output=OrgRecommendedConnectors,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Set your org's KIT as a whole list — the connectors your org "
                    "installs into its members' toolboxes. Only the DIFFERENCE with the current "
                    "kit is applied, to current members AND to members who join later: each "
                    "added connector is installed for every member who doesn't have it (never "
                    "over a member's own choice — a connector they paused or removed themselves "
                    "stays that way); a connector taken out of the kit is uninstalled where the "
                    "kit installed it, and nowhere else. Connectors already in the kit are not "
                    "replayed. Returns, per changed "
                    "connector, installed / already_active / paused / removed_by_member. "
                    "Members' agents see it at their NEXT conversation. "
                    "ADDING a connector unknown to the catalog or not available for your org "
                    "is REFUSED, naming why — nothing is written. A connector already in the "
                    "kit that your org has since cut stays in it: installed, hidden for "
                    "everyone, back on its own when reopened (listed in `cut`). "
                    "connectors = connector names ([] empties the kit).",
        errors=(DeclaredError(404, "unknown_org", "unknown org"),
                DeclaredError(404, "unknown_connector",
                              "a connector ADDED to the kit is unknown to the registry — nothing "
                              "is written"),
                DeclaredError(409, "org_disabled",
                              "a connector ADDED to the kit is not available to the "
                              "org's members (the org cut it) — nothing is written"),
                DeclaredError(409, "platform_disabled",
                              "a connector ADDED to the kit is cut by the platform — "
                              "nothing is written"),),
        rest=RestBinding("PUT", "/api/orgs/{id}/default-connectors", _ID),
    ),
    Capability(
        key="connectors.bulk_select", handler=_bulk_select, Input=BulkSelectInput,
        Output=BulkSelectResult,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Add a connector to your org's KIT: it is installed right away "
                    "for every current member who doesn't have it, and for every member who "
                    "joins later. Never over a member's own choice — a member who paused it or "
                    "removed it themselves keeps it that way. If it is ALREADY in the kit, "
                    "nothing is replayed: the response says why and how to apply it anyway. "
                    "Requires the org to expose the connector. Returns activated (installed "
                    "now), skipped, and the per-population detail in `changes`. Members' "
                    "agents see it at their NEXT conversation.",
        errors=(DeclaredError(404, "unknown_connector",
                              "name unknown to the registry"),
                DeclaredError(409, "org_disabled",
                              "the org has disabled this connector: enabling it for "
                              "everyone would contradict its own governance"),
                DeclaredError(409, "platform_disabled",
                              "the platform has cut this connector: the org cannot "
                              "install it"),),
        rest=RestBinding("POST", "/api/orgs/{id}/connectors/{name}/bulk-select", _ID),
    ),
    Capability(
        key="connectors.unset_default", handler=_unset_default, Input=UnsetDefaultInput,
        Output=UnsetDefaultResult,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Take a connector out of your org's KIT: it is uninstalled "
                    "from every member the KIT installed it for (active or paused), and kept "
                    "where the member installed or resumed it themselves, where an admin pushed "
                    "it to them, or where it predates tracking — `uninstalled` and `kept` (by "
                    "provenance) say so. Members who join later no longer get it. It never "
                    "hides the connector from search/the library: that is the availability "
                    "switch, a different lever.",
        rest=RestBinding("DELETE", "/api/orgs/{id}/connectors/{name}/bulk-select", _ID),
    ),
]
