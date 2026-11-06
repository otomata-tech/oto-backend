"""The MODEL of a connector — the shape, never the content.

`CredentialField`, `Connector` and the `_c` factory live here; the ~90 entries
that instantiate them each live in `providers/<name>.py`, and
`providers/__init__.py` AGGREGATES them. Keeping the two apart avoids the import
cycle that a model defined in the aggregator and imported by the declarations
would create.

PURE module: no `oto_mcp` import at module level (the properties that need
curated data — `doc_sections`, `category`, `description`,
`publisher_name`, `logo_url_for` — import it LAZILY from the aggregator,
never at import time).

Each connector carries the 3 axes of the platform model:
- **A. Availability**: `availability` (self_serve | platform_granted). platform_granted
  = grant-only (the platform grants explicitly, e.g. a connector reserved for one customer).
- **B. Visibility**: `default_active` (curated BASE SET, ADR 0050 — installed by default
  in the selection of a new (sub, org); the rest of the catalog = installable
  library). Policy, tunable.
- **C. Credential**: `auth_modes` ⊆ {byo_user, byo_org, platform}; `keyed` (resolved via
  `resolve_api_key` with an api key); `secret_kind`; `personal_session` (session
  that is physiologically per-user: linkedin/google/slack/whatsapp/crunchbase, never org).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialField:
    """An input field of a credential (generic multi-field model, ADR 0011).

    SINGLE SOURCE for the input form (dashboard), the REST endpoint and the
    vault packing.

    `secret` alone carries the output rule, and there is **no exception anymore**:
    `secret=False` (a base URL, a region, an email) is rendered as-is on
    read; `secret=True` is **NEVER** rendered — not in clear, not truncated, not blanked:
    its key is absent from the body, and a non-reversible fingerprint names it alongside.

    ⚠️ **A `reveal` notch existed until 2026-08-31** (oto-backend#671) and it was
    the DEFAULT: every `secret_kind="api_key"` connector without explicit
    `credential_fields` inherited a revealable `key`, i.e. 55 connectors of which 49 had
    never decided it. It is **removed**, not neutralized: passing it to a `CredentialField`
    now raises a `TypeError` at import, rather than silently opening a secret
    output. A declaration that needed to render it does not have it — that is what
    the decision settled."""
    name: str
    label: str
    secret: bool = True
    help: str = ""
    # False = optional field (an "AND/OR" connector like slack: at least one
    # non-empty field required at setup, but no individually required field).
    required: bool = True
    # False (default) = whitespace is meaningless in the value (keys, tokens,
    # ids) → cleaned at setup (copy-paste debris). True = whitespace is
    # significant (password) → strip the edges only. See
    # credentials_store.clean_field_value.
    whitespace_significant: bool = False
    # RELEVANT values of the connector's discriminator field (`field_discriminator`)
    # for this field. Empty (default) = the field applies whatever the discriminator —
    # so NOTHING CHANGES for the ~90 connectors that declare none.
    # When set, it says two things at once: the field is only DISPLAYED for these
    # values, and its `required` only APPLIES there. E.g. `http`: `header_name` is
    # required, but only with `auth_mode=header`. See `Connector.fields_for`.
    when: tuple[str, ...] = ()
    # CLOSED set of accepted values (a `select`, not a free field). Empty = free.
    # Reserve for fields that discriminate or whose typo silently
    # breaks things: a value outside the list is refused at WRITE time, with the expected
    # set in the message.
    choices: tuple[str, ...] = ()
    # What the select SHOWS for a value of `choices`, as `(value, label)` pairs: `com` says
    # nothing to someone who only knows their Zoho address. A value without a label shows
    # as itself.
    choice_labels: tuple[tuple[str, str], ...] = ()
    # The key under which a screen holds this field's texts in another language (`zoho.client_id`).
    # The served `label` and `help` stay the English source, for agents and the English UI;
    # the screen translates by this key, never by a connector name. Empty = shown as served.
    text_key: str = ""
    # ACCEPTED PREFIXES of the value (a key whose shape tells what it is). Empty = free.
    # A value that starts with none of them is refused at WRITE time, with the expected
    # prefixes in the message — never the value: the field may be secret. E.g. the
    # Anthropic key, where a session token or an admin key would be accepted, then
    # refused by every worker.
    prefixes: tuple[str, ...] = ()
    # True = the value lives in `connector_credentials.meta`, IN CLEAR, and not in the
    # encrypted blob (14/09/2026). For a NON-secret field added to a connector whose
    # credentials are already set: the shape of the encrypted blob depends only on the fields that
    # live in it (`Connector.vault_fields`), so a key stored raw stays raw — no
    # row to repackage, and the old code and the new code read the same row
    # of the prod/preprod shared database. Founding case: the workspace of an Anthropic
    # organization key.
    in_meta: bool = False

    def __post_init__(self):
        # ⚠️ `meta` goes out in clear to the status and listing screens (`public_meta`):
        # a secret field living there would go out with it. Refused at import, not at use.
        if self.in_meta and self.secret:
            raise TypeError(
                f"CredentialField `{self.name}`: `in_meta` requires `secret=False` — "
                "`meta` is rendered in clear on the status and listing screens.")


@dataclass(frozen=True)
class Connector:
    name: str                          # identity = credential key
    namespaces: tuple[str, ...]        # owned tool prefixes
    availability: str                  # "self_serve" | "platform_granted"
    auth_modes: frozenset              # ⊆ {"byo_user","byo_org","platform"}
    keyed: bool                        # resolved via resolve_api_key (→ KEY_PROVIDERS)
    personal_session: bool             # "browser session" category (Live View
                                       # Browserbase) on the UI side — ORTHOGONAL to sharing:
                                       # the level (user/team/org) follows `auth_modes`
                                       # (`byo_org` ⇒ shareable session, e.g. pennylaneged)
    secret_kind: str                   # api_key|refresh_token|oauth|cookie|fields|none|settings
                                       # `settings` (19/10/2026): the card holds ONLY optional,
                                       # non-secret SETTINGS — it works with nothing set (the
                                       # waterfalls). Same form as `fields` (auth method
                                       # `secret`); a front reads it to say "included, nothing
                                       # to connect" instead of "not connected".
    default_quota: int                 # 0 = unlimited
    default_active: bool               # axis B: curated base set (ADR 0050) — installed
                                       # by default when seeding a new (sub, org);
                                       # the rest stays installable from the library
    platform_key_open: bool = False    # free-tier: platform key usable WITHOUT a grant
                                       # (free quota = default_quota per user/day, ADR 0031)
    label: str = ""
    help: str = ""
    href: str | None = None
    # Publisher of the connector (shown in the catalog). Empty → derived from the
    # `PUBLISHER` constant of the declaration module (see `publisher_name`). Empty on BOTH
    # sides ⟹ the sheet shows NO publisher: there is no default anymore (2026-09-02).
    # The field is used when the publisher is a property SHARED by several entries
    # built by the same factory (the six hosted channels, see `unipile.channel`);
    # otherwise, the normal home is the module constant.
    publisher: str = ""
    # Public URL of the publisher's logo. None → derived from the logo.dev CDN from
    # the curated brand domain `LOGO_DOMAIN` of the declaration module
    # (see `logo_url_for`).
    # The explicit field remains an override (custom logo hosted elsewhere).
    logo_url: str | None = None
    # "tools" = in-process module (tools/<name>.py); "remote" = remote bridge
    # (ADR 0003) served by the generic module tools/remote.py — the org
    # credential is then {secret=M2M token, meta.base_url=bridge endpoint};
    # "credential" = provides NO tool: the object only serves to carry a key
    # that the platform uses on behalf of the org (e.g. the model key
    # that a scheduled agent consumes). Decided by Alexis on 03/09: a
    # DISTINCT type, not a connector with empty namespaces.
    #
    # ⚠️ The reason is not technical — a connector without a namespace builds
    # just fine. It is that it **would present itself as a connector without having
    # its effects**: it would appear in the list, "connect", and its
    # activation would bring no capability. An object that looks like it does
    # something and does nothing is the family of defects we have been documenting
    # since 03/09 — a field read with no producer, a produced field that nothing
    # reads, an alert that never fires. Here it is the version that the
    # USER sees.
    #
    # A distinct type lets the screen present it for what it is — "provider
    # key", not "connector" — instead of lying by omission.
    #
    # ⚠️ The `"mount"` value (federation of a third-party MCP) was REMOVED on
    # 2026-09-09 along with the mechanism (ADR 0069): a remote service is joined through
    # the generic `http` connector, or written as a native connector.
    kind: str = "tools"
    # EXPLICIT input schema of the credential (generic multi-field model).
    # Empty → derived from secret_kind (see `secret_fields`). Set for
    # credentials with >1 field that are neither api_key nor basic_auth (e.g. Silae:
    # client_id + client_secret + subscription_key).
    credential_fields: tuple[CredentialField, ...] = ()
    # `tools/<m>.py` modules to import for this connector (kind="tools" only).
    # Empty ⇒ `(name,)`. Set when the module ≠ provider name (sirene→fr) or
    # when a provider carries several modules (google→gmail/datastore/tasks).
    # `register_all` DERIVES the loading from this field (end of the hardcoded list, #24).
    modules: tuple[str, ...] = ()
    # "Hosted" auth (ADR 0024): the credential is a key (resolve_api_key,
    # cascade unchanged), BUT the user-facing connection goes through a third-party
    # hosted flow (e.g. unipile: the org sets the subscription, each member links their
    # LinkedIn/WhatsApp account through hosted-auth) — not a key form. Set here, the
    # `auth.method` descriptor is "hosted" → the card renders the dedicated widget without
    # a per-name case on the front side.
    hosted_auth: bool = False
    # Cross-org PERSONAL instance (issue #172, ADR 0033 amended): the credential
    # is intrinsically PER-PERSON — a hosted messaging account (unipile:
    # the LinkedIn/WhatsApp login IS the human, not the membership). Its member key
    # set in ONE org then follows the `sub` into ALL their orgs (proximity
    # resolution, not just the `_instance=` pin): "same email = instance available in
    # every org". Without this flag, a member credential stays strictly `(sub, org)`
    # (ADR 0033) — the default value changes nothing for the ~other connectors.
    personal_cross_org: bool = False
    # AUTH CARDINALITY, declared — `"mono"` | `"multi"` | `""` (derived from the auth
    # descriptor, the normal case). Decided by Alexis on 2026-08-27: "what
    # bothers me is the LIST ITSELF" — cardinality is derived by default and
    # declared **per connector, in its entry**, never in a cross-cutting list
    # indexed by name (that is what took down `MULTI_ACCOUNT_PROVIDERS`).
    #
    # Only set for a PROVIDER reason, motivated next to the declaration
    # — never for the shape of the credential: the number of fields says nothing about
    # cardinality (a Slack token is issued per installation, two fields or not), and
    # that was precisely the rule gap of oto-backend#409.
    #
    # ⚠️ **The CODE's default, not the last word**: a `connector_settings` row
    # can override it per org (the pattern of the instruction block — constant =
    # default, DB row = override), so that a widening does not require
    # a deployment. The seam that decides between the two is `connectors.cardinality`, not
    # this property.
    cardinality: str = ""
    # Does the connector ANNOUNCE the `_account=` axis in the schema of its tools, without
    # waiting for the caller to hold two accounts? Nothing to do with cardinality,
    # which is why it is no longer the same field: each axis property is
    # copied into the schema of EVERY tool at each handshake (test_call_axes_budget),
    # so the static announcement is CURATED — reserved for connectors whose
    # caller, in practice, always has several accounts. Elsewhere the axis stays
    # announced DYNAMICALLY (from 2 held accounts) and ACCEPTED wherever it makes sense.
    account_axis_static: bool = False
    # Business word for an account at THIS provider, when "account" sounds wrong: a
    # Slack account in the vault is a workspace, a Zoho account an organization.
    # Empty = "compte". Published in the `auth` descriptor and displayed as-is.
    # French on purpose: the dashboard displays and inflects it.
    account_noun: str = ""
    # Name of the field whose VALUE selects the others (`auth_mode` on `http`).
    # Empty (default) = flat-schema credential: all fields always apply.
    # When set, it enables `fields_for`: a form that only shows what
    # is useful, and a write-time validation that refuses an inconsistent mode instead of
    # accepting it and failing on the first real call (oto-backend#449).
    field_discriminator: str = ""
    # CREDENTIAL DELEGATION (unipile split, oto-backend — 2026-08-28). This
    # connector has NO credential of its own: its key, quota, platform key
    # and option (layer 3) live under the connector NAMED here. The use case
    # is a provider whose ONE key opens N *connections* that we want to govern
    # separately — unipile: one subscription key, six channels, each with its card, its
    # activation and its selection.
    #
    # ⚠️ Two questions then coexist, and EVERY site must choose which one it asks:
    #   · "which connector is CALLED?"  → the gates: activation, selection,
    #     session visibility, `_instance=` pin. That is the BARE name.
    #   · "which connector CARRIES the key?" → the vault, the cascade, the quota, the
    #     platform key, the option. That is `providers.credential_provider(name)`.
    # Mixing the two reproduces exactly the 2026-07-07 divergence (green "org
    # key" card + red "Blocked"): the status and the resolution were answering
    # two different questions believing they answered the same one.
    #
    # A delegating connector never HOLDS a credential: it is outside
    # `CREDENTIAL_PROVIDERS`, so setting a key under its name is refused and
    # points to the carrier (otherwise two keys would contradict each other).
    credential_of: str | None = None
    # Unipile channel connected by this connector (`LINKEDIN`/`WHATSAPP`/…) when the
    # card represents ONE hosted connection. None = not a channel connector.
    # Makes the channel DERIVABLE from the connector: the hosted flow is then declared
    # without a parameter (`connectors/flow.py`), and the front no longer has a channel
    # selector to render — the card IS the channel.
    hosted_channel: str | None = None
    # The connectors whose key this one SPENDS on its own, without the caller choosing
    # it — an escalation tier that falls back on a paid reader (the platform key
    # included, quota and billing). Such a connector asks for no key of its own
    # (`secret_kind="none"`) but is not free: it is never installed by default as open
    # data (`connectors/usable.py`), the member installs it.
    spends_keys_of: tuple[str, ...] = ()
    # The UNIT of the platform key's daily quota, when every GRANT of that key must carry
    # one. Set ⟹ a grant without a positive `daily_quota` is refused
    # (`credentials_store.exiger_quota_d_octroi`), whatever the path that grants: the
    # default (`default_quota=0`) is UNLIMITED, which a key billed per use cannot afford.
    # Empty (default) = the quota stays optional. The unit is what the tool notes as
    # `units` (e.g. ElevenLabs credits), and the refusal names it.
    grant_quota_unit: str = ""
    # The library client of a connector served by the GENERIC READER of its description
    # (`tools/decrits.py`, decided 05/11/2026): `"module:Class"`. Set by `_decrit` only —
    # empty for every hand-written connector, which keeps its `tools/<name>.py`.
    client: str = ""

    @property
    def org_shareable(self) -> bool:
        """Can a secret of this connector be set at ORG (or team) level?

        False for a connector that DELEGATES its credential, even if it inherits
        `byo_org` from its carrier: what is shared is the key of the ACCOUNT, on the
        account's card. Without this false, the readers that query the BARE name —
        `org_secret_meta`, the "a team has the key" hints (`access/rbac.py`), the
        card's sharing lever — would offer to set or reach an org
        secret under `whatsapp`, which the cascade (normalized to `unipile`)
        would never go read."""
        return "byo_org" in self.auth_modes and not self.credential_of

    @property
    def family(self) -> str:
        """Nature of the integration (*builder* axis, ADR 0011) — DERIVED from the credential
        + runtime: open-data | api | browser | google | bridge."""
        if self.kind == "remote":
            return "bridge"
        if self.name in BROWSER_PROVIDERS:
            return "browser"
        if self.name == "google":
            return "google"
        if self.secret_kind == "none":
            return "open-data"
        return "api"

    @property
    def category(self) -> str:
        """Usage domain (*user* axis, ADR 0011) — CURATED (`CATEGORY` constant
        of the declaration module), for grouping in the UI."""
        from . import _CATEGORY_BY_CONNECTOR
        return _CATEGORY_BY_CONNECTOR.get(self.name, "Other")

    @property
    def doc_sections(self) -> tuple:
        """"How-to" doc sections (CURATED) — one markdown per connector,
        `connectors/docs/<name>.md`, joined by NAME. `connectors/docs_reader.py` only carries
        the parser and the marker resolution, no prose anymore. Lazy
        import: keeps this module pure at module level.

        ⚠️ This line used to say "contained in `connector_docs.py`" (the module of the time) until
        27/08/2026: true when written, false since the 02/08 migration (the
        prose moved from the Python dict to markdown), and copied verbatim
        when the registry was split on 27/08. An audit on 27/08 concluded that
        153 lines of curated prose remained to be distributed into `providers/<name>.py` —
        the move had been done for three weeks. A stale map costs
        more than no map: it is read with confidence."""
        from ..connectors.docs_reader import DOC_SECTIONS, multi_account_section
        sections = tuple(DOC_SECTIONS.get(self.name, ()))
        if self.auth_multi_account:
            sections += (multi_account_section(self.name, self.account_noun or "compte",
                                               par_connexion=self.secret_kind == "oauth"),)
        return sections

    @property
    def description(self) -> str:
        """User-facing description, 2-3 sentences (CURATED, `DESCRIPTION` constant
        of the declaration module).
        Empty if not written — the front then falls back to `help`."""
        from . import _DESCRIPTION_BY_CONNECTOR
        return _DESCRIPTION_BY_CONNECTOR.get(self.name, "")

    @property
    def publisher_name(self) -> str:
        """Publisher shown in the catalog — field override if set, otherwise the curated
        `PUBLISHER` constant of the declaration module. **Otherwise NOTHING.**

        ⚠️ This fallback used to be "Otomata" until 2026-09-02: an omitted
        declaration was then indistinguishable from the "in-house connector" choice, and the
        default attributed a third party's product to us — Folk's *official* MCP was
        served under our name for a month, and six messaging channels
        sent messages through a third-party gateway under our name.

        **An absence is seen and fixed; a false attribution is believed.**

        The empty value served is the empty string, **not `None`**. Measured on 2026-09-02 by
        rendering the dashboard components: the five surfaces that display
        the publisher treat `""` and `null` IDENTICALLY — they hide
        (`v-if`, `filter(Boolean)`) or coerce (`(c.publisher || "")`,
        `[…].join(" ")`), none renders "undefined" or breaks. The tie-breaker
        is therefore the CONTRACT, not the rendering: oto-dashboard declares
        `publisher: string` (`types/api.ts`) and the `MyConnectors` capability serves the
        key on all its verbose rows; `None` would make the first one lie without
        gaining anything on the second.

        The `tests/test_connector_publisher.py` ratchet ensures this empty value MUST
        never be reached from the registry: it requires one declaration per
        connector, the whole registry, without a family filter. The empty value remains the
        behavior of an entry built ELSEWHERE — that is where it protects."""
        if self.publisher:
            return self.publisher
        from . import _PUBLISHER_BY_CONNECTOR
        return _PUBLISHER_BY_CONNECTOR.get(self.name, "")

    def logo_url_for(self) -> str | None:
        """Public URL of the publisher's logo. `logo_url` override if present,
        otherwise derived from the **logo.dev** CDN: curated brand domain
        (`LOGO_DOMAIN` of the declaration module) + publishable token
        `LOGODEV_TOKEN` (env).
        None if no known domain (open-data/in-house → monogram on the UI side) or
        token missing. The token is *publishable* (designed to live in the URL)."""
        if self.logo_url:
            return self.logo_url
        from . import _LOGO_DOMAIN_BY_CONNECTOR
        domain = _LOGO_DOMAIN_BY_CONNECTOR.get(self.name)
        token = os.environ.get("LOGODEV_TOKEN")
        if not domain or not token:
            return None
        return (f"https://img.logo.dev/{domain}"
                f"?token={token}&size=256&format=png&retina=true")

    @property
    def auth_method(self) -> str:
        """Mechanism for obtaining the credential (ADR 0024) — DERIVED. Drives the
        widget rendered by the `ConnectorCard` (one flow, one card). Priority:
        `hosted` (third-party hosted flow, e.g. unipile) > `remote` (bridge ADR 0003,
        set by org grant) > `oauth`/`cookie`/`none` (dedicated flows / no
        credential) > `secret` (field(s) to paste: api_key, basic_auth, fields).
        ⚠️ This set of values is CLOSED *and consumed by a `switch` in ANOTHER
        repo* (oto-dashboard, `ConnectorConnectionPanel.connKind`): adding a
        value there is a cross-repo contract break that fails SILENTLY (`default`
        branch → empty connection panel). Lived through with `secret_then_oauth`,
        removed on 29/07: "one step remains" is expressed through `status_hints`
        (pending_action), not through a new auth method."""
        if self.hosted_auth:
            return "hosted"
        if self.kind == "remote" and not self.credential_fields:
            # Legacy bridge (ADR 0003): credential set by org grant, no
            # form. A NEW-model bridge (ADR 0034) declares its
            # credential_fields → standard self-serve form (method=secret).
            return "remote"
        if self.secret_kind in ("oauth", "cookie", "none"):
            return self.secret_kind
        return "secret"

    @property
    def auth_multi_account(self) -> bool:
        """Is the credential multi-account — N grants for the same entity
        (ADR 0024)?

        ⚠️ **This is the CODE's default, not the served answer.** A
        `connector_settings` row can override it per org: a caller that must DECIDE
        goes through `connectors.cardinality.is_multi_account(connector, org)`, never through
        this property. It stays here because the registry is PURE (no `oto_mcp`
        import, no database) — and that is what makes it readable from a test.

        By DEFAULT for every connector whose credential is SET (`method=secret`
        — simple `api_key`/`basic_auth` key **or** multi-field `fields`): the vault
        is already segmented by `account` on each row, the member resolution
        (access/resolve.py `_member_fetch`) treats a single account — the legacy row
        `account=''` included — exactly as before. A key set yesterday therefore stays
        today's key; what changes is that a second, named one can be set.

        **A `cardinality` declaration TAKES PRECEDENCE over the derivation** — the only two
        carriers are those whose auth descriptor is wrong: `google` (OAuth, so
        derived mono, but N consents = N accounts) and `browser` (cookie session,
        so derived mono, but one account = one SITE). `zoho` and `folk`, long in the
        cross-cutting list, have nothing to declare: the derivation makes them multi all
        by itself. It is the measurement that showed it, not an intuition.

        ⚠️ **The number of credential fields says NOTHING about cardinality.**
        Until 2026-08-27, `fields` was outside the rule: Slack (`bot_token` +
        `user_token`) fell into single-account although a Slack token is issued per
        INSTALLATION in a workspace (N installations = N independent tokens) —
        a rule gap, not a provider reason. Setting a 2nd account there
        wrote a row that the resolution would never read (oto-backend#409).

        Out of scope, deliberately: OAuth/cookie/none (N accounts = N
        consents, a different problem), hosted/remote (no key to set), and
        `personal_cross_org` connectors (unipile), whose cross-org rung
        of the cascade is single-account by construction. A case that
        falls into none of these families is declared `cardinality="mono"`, IN
        its registry entry and with its reason — never through a cross-cutting list."""
        if self.cardinality:
            return self.cardinality == "multi"
        return (self.auth_method == "secret"
                and self.secret_kind in ("api_key", "basic_auth", "fields")
                and not self.personal_cross_org)

    @property
    def auth(self) -> dict:
        """Unified auth descriptor (ADR 0024) — single source for rendering the
        credential face, whatever the mechanism. `fields` = input schema
        (empty outside `method=secret`, where the flows are dedicated)."""
        return {
            "method": self.auth_method,
            "cardinality": "multi_account" if self.auth_multi_account else "single",
            # The WORD the user uses for an account of this connector, when
            # "account" is wrong for it: a Slack account in the vault IS a workspace.
            # The front displays it as-is (oto-dashboard#121) — it is the registry that
            # knows the provider's vocabulary, not the screen.
            "account_noun": self.account_noun or "compte",
            # The field whose value selects the others, when there is one
            # (`auth_mode` on `http`): the front does not need to know the connector
            # to show only the useful fields — it reads `when` (#449).
            "field_discriminator": self.field_discriminator,
            # The hosted CHANNEL of this connector, when it carries one (unipile split
            # of 2026-08-28). Without it, a screen that renders the "connect an
            # account" face cannot know WHICH of the six channels its card represents —
            # and the only way out is a copy of the six in the front, which
            # displays identically on all six cards. That is exactly the observed
            # failure: the WhatsApp card offered to connect LinkedIn,
            # Telegram, Instagram… and displayed their connection state as its own.
            # Lowercase, like the flow parameter — the vault, for its part, uppercase.
            "hosted_channel": (self.hosted_channel or "").lower() or None,
            # The connector that HOLDS the credential, when it is not this one
            # (`credential_of`). A delegating connector has NO field to enter
            # (`secret_fields` is empty by construction, see below): a screen that
            # still offers "set your own key" offers a gesture with no
            # effect, the key it would set being read by nobody. The carrier's name
            # rather than a boolean: it is THAT one the screen must name to say where the
            # gesture is really done.
            "credential_of": self.credential_of,
            "fields": [
                {"name": f.name, "label": f.label, "secret": f.secret,
                 "required": f.required, "help": f.help,
                 "when": list(f.when), "choices": list(f.choices),
                 "choice_labels": dict(f.choice_labels),
                 "text_key": f.text_key}
                for f in self.secret_fields
            ],
        }

    def fields_for(self, values: dict) -> tuple[CredentialField, ...]:
        """The RELEVANT fields of an input, once the discriminator is known.

        Without `field_discriminator`, it is `secret_fields` — the case of the ~90 other
        connectors. With one, we remove the fields that no discriminator value
        makes useful: `header_name` has no business in a `bearer` form,
        and its `required` has nothing to refuse there.

        ⚠️ Discriminator ABSENT or empty = everything is relevant. This is deliberate: at
        this stage the input has not chosen yet, and hiding would be guessing. Refusing
        a value outside `choices` is the write's job, not this one's."""
        if not self.field_discriminator:
            return self.secret_fields
        picked = str(values.get(self.field_discriminator) or "").strip().lower()
        if not picked:
            return self.secret_fields
        return tuple(f for f in self.secret_fields if not f.when or picked in f.when)

    @property
    def secret_fields(self) -> tuple[CredentialField, ...]:
        """Input schema of the credential — SINGLE SOURCE for the UI, the REST endpoint,
        `status_for` and the packing. Declared explicitly (`credential_fields`),
        otherwise derived from the simple forms. Empty = no generic input: `cookie`
        (linkedin/crunchbase), `oauth` (google) and `none` (open-data) have
        dedicated flows, not a field form.

        Also empty for a connector that DELEGATES its credential (`credential_of`):
        it has no field of its own. Rendering its carrier's form on its card
        would invite setting a second key — which the vault refuses (outside
        `CREDENTIAL_PROVIDERS`) and which the cascade would never read. The
        carrier's card is the only place where the key is set."""
        if self.credential_of:
            return ()
        if self.credential_fields:
            return self.credential_fields
        if self.secret_kind == "api_key":
            return (CredentialField("key", "API key", secret=True),)
        if self.secret_kind == "basic_auth":
            return (CredentialField("email", "Email", secret=False),
                    CredentialField("password", "Password", secret=True,
                                    whitespace_significant=True))
        return ()

    @property
    def vault_fields(self) -> tuple[CredentialField, ...]:
        """The fields stored INSIDE the encrypted blob: `secret_fields` minus those that live in
        `meta` (`in_meta`). Their count, and that alone, decides the shape of the
        blob (`credentials_store.pack_secret`) — adding an `in_meta` field does not change
        it."""
        return tuple(f for f in self.secret_fields if not f.in_meta)

    @property
    def config_fields(self) -> tuple[CredentialField, ...]:
        """NON-secret fields of the credential (endpoint/host/region: `base_url`
        n8n/make, `data_center` zoho, `org_id` zohodesk…). Derived from `secret_fields`
        (flag `secret=False`) — the config travels with the key via `resolve_credential`
        (the non-secret `meta`, e.g. unipile `dsn`, is added to it at resolution)."""
        return tuple(f for f in self.secret_fields if not f.secret)


# Connectors going through an IN-PROCESS browser (local o-browser) — not derivable
# from secret_kind alone. Empty since crunchbase migrated to the HOSTED
# Browserbase substrate (ADR 0026): crunchbase now calls the private `/v4/data`
# API through a remote browser session (family derived → "api", like
# brevo). LinkedIn had already moved to Unipile. Mechanism kept for a
# possible future local browser connector.
BROWSER_PROVIDERS = frozenset()

# ⚠️ **`MULTI_ACCOUNT_PROVIDERS` WAS REMOVED on 2026-08-29** (batch L6 piece 2 c),
# and its disappearance IS the batch. It was the last cross-cutting list indexed by name,
# and Alexis settled it on 27/08: "what bothers me is the LIST itself". It
# carried two unrelated things, which now each live in the entry of the
# connector concerned, next to the declaration they qualify:
#
# · CARDINALITY → `cardinality="multi"` — and only where the derivation
#   is wrong: `google` (OAuth ⟹ derived mono, but N consents = N accounts) and
#   `browser` (cookie ⟹ derived mono, but one account = one SITE). `zoho` and `folk` have
#   NOTHING to declare: since the rule covers multi-field credentials, the
#   derivation makes them multi all by itself — they were only in the list
#   out of habit, and it is the measurement that showed it;
# · the STATIC announcement of the `_account=` axis → `account_axis_static=True`, on the
#   four. That role was never cardinality (it is about the tools' SCHEMA, not
#   the vault) — mixing them in a single list is what made it unremovable.


def _c(name, namespaces, *, availability="self_serve", auth_modes=(), keyed=False,
       personal_session=False, secret_kind="none",
       default_quota=0, default_active=False,
       platform_key_open=False, label="", help="", href=None,
       publisher="", logo_url=None, kind="tools",
       credential_fields=(), modules=(), hosted_auth=False,
       personal_cross_org=False, cardinality="", account_axis_static=False,
       account_noun="",
       field_discriminator="", credential_of=None, hosted_channel=None,
       spends_keys_of=(), grant_quota_unit="", client="") -> Connector:
    """Factory for a registry entry — called by `providers/<name>.py`.

    Adding a FIELD per connector = adding it to `Connector` AND here, then
    setting it in the module of the connector concerned, next to the declaration
    it qualifies (never in a cross-cutting list indexed by name)."""
    return Connector(
        name=name, namespaces=tuple(namespaces), availability=availability,
        auth_modes=frozenset(auth_modes), keyed=keyed, personal_session=personal_session,
        secret_kind=secret_kind, default_quota=default_quota,
        default_active=default_active, platform_key_open=platform_key_open,
        label=label or name.capitalize(), help=help, href=href,
        publisher=publisher, logo_url=logo_url, kind=kind,
        credential_fields=tuple(credential_fields),
        modules=tuple(modules), hosted_auth=hosted_auth,
        personal_cross_org=personal_cross_org, cardinality=cardinality,
        account_axis_static=account_axis_static,
        account_noun=account_noun, field_discriminator=field_discriminator,
        credential_of=credential_of, hosted_channel=hosted_channel,
        spends_keys_of=tuple(spends_keys_of), grant_quota_unit=grant_quota_unit,
        client=client,
    )


def _decrit(name, *, client, help, href, key_help) -> Connector:
    """Registry entry of a connector served by the GENERIC READER of its description
    (`tools/decrits.py`): its label, namespace, modes and key field are READ in the
    description (`oto.connectors.descriptions`), never written twice; `client` names
    its library client (`"module:Class"`); `help`, `href` and `key_help` are the
    catalog's prose. A description the reader cannot serve is refused here, at import
    (`connectors.descriptions.verifier`)."""
    from ..connectors import descriptions  # lazy: this module stays pure

    description = descriptions.charger(name)
    descriptions.verifier(description)
    connecteur = description["connector"]
    secret = descriptions.secret_unique(description)
    return _c(
        name, [connecteur["namespace"]], auth_modes=set(connecteur["modes"]), keyed=True,
        secret_kind="api_key", label=connecteur["label"], help=help, href=href,
        credential_fields=(CredentialField("key", secret["label"], secret=True, help=key_help),),
        client=client,
    )
