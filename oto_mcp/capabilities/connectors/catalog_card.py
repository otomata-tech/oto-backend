"""A connector's CARD, declared — the shape served with `verbose=true` (#667).

`GET /api/me/connectors?verbose=true` serves the entire catalog row
(`providers.public_catalog()`), i.e. thirteen more top-level keys than the
compact mode. None of them was declared: `MyConnectorRow` is `extra="allow"`, so
they passed through the model without leaving a trace in the schema, which only
announced `additionalProperties: true`. A third-party front that derives its
credential form from the contract could therefore get nothing out of it, even though the data arrived —
that is the blocker reported by the REST consumer.

**These models DESCRIBE, they do not validate** (same regime as `Capability.Output`,
see `capabilities/_types.py`): the handler keeps returning `dict`s built by
`providers.public_catalog()`. Declaring therefore cannot move a byte of the
payload — and that is the only reason this batch is safe to put on a
surface that is already consumed.

**Home.** Here and not in `selection.py`: the described shape is the one produced by
`providers/__init__.py::public_catalog`, not the one the capability composes. Both
faces use it — the authenticated projection (`connectors.me`) enriches it with
`connect.callback_url` / `connect.app_ready`, absent from the public catalog served without
auth (`connectors/flow.py`).

⚠️ What is declared here must remain the REFLECTION of what the producer returns. The
producer remains `providers/__init__.py::public_catalog` and `_model.Connector.auth`:
a field added there and forgotten here becomes a served-but-undeclared field again, which
is exactly the defect being fixed. `tests/test_carte_connecteur_declaree.py` holds
the ratchet: it compares the SERVED keys to the DECLARED keys, connector by
connector.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DocSection(BaseModel):
    """A section of a connector's "how-to" doc, in markdown.

    Curated per connector (`connectors/docs/<name>.md`), read by
    `connectors/docs_reader.py`. Observed `kind`: `prerequisite` | `setup` | `usage` |
    `note` — an open set by construction (it comes from the markdown headings), hence
    declared `str` and not an enum: freezing a set that the prose can widen would make
    the contract lie at the first doc file added."""
    kind: str
    title: str
    body_md: str


class CredentialField(BaseModel):
    """A credential input field — the SHAPE, never a value.

    This is what the dashboard loops over to render its form. A deliberate namesake
    of `providers._model.CredentialField`, which is the dataclass that
    PRODUCES it: this one is its served projection, and so it does not carry the
    internal fields (`whitespace_significant` is not on the wire).

    `secret=True` ⟹ the value is **never** returned on read (neither truncated nor
    emptied): its key is absent from the body. This model only describes the input."""
    name: str
    label: str
    secret: bool
    required: bool
    help: str
    when: list[str] = Field(default=[], description=(
        "Values of the discriminant field (`AuthDescriptor.field_discriminator`) for "
        "which this field is relevant. ⚠️ EMPTY list = relevant WHATEVER the "
        "discriminant — \"empty\" means \"always\", never \"never\" "
        "(this is the case for the ~90 connectors that declare none). When filled, it "
        "says two things at once: the field is only shown for these values, and "
        "its `required` only applies there."))
    choices: list[str] = Field(default=[], description=(
        "CLOSED set of accepted values — a select, not a free field. Empty = "
        "free. A value outside the list is refused on WRITE, with the expected set "
        "in the message."))


class AuthDescriptor(BaseModel):
    """Unified auth descriptor (ADR 0024) — the single source for rendering the
    credential face, whatever the mechanism.

    ⚠️ `method` is a CLOSED set, and it is consumed by a `switch` in another
    repo (oto-dashboard): adding a value there breaks silently (`default` branch →
    empty connection panel). It is nevertheless declared `str` and not `Literal` here, and
    that is deliberate: this model DESCRIBES a served response, and an enum in the contract
    would make a third party's client generation fail the day the server returns a
    sixth value — the contract must age better than the list. Values
    served as of 2026-09-01: `hosted` | `remote` | `oauth` | `cookie` | `none` |
    `secret` (see `providers/_model.py::auth_method`, which is its home)."""
    method: str
    cardinality: str = Field(description=(
        "`multi_account` | `single` — does this connector accept SEVERAL accounts "
        "for the same entity? This key decides whether to offer \"add "
        "an account\". Derived from the connector, not declared by it. ⚠️ **Served here "
        "with the org's setting applied**: an org may open multi-account "
        "on a connector that does not have it by default. The PUBLIC catalog, for its part, returns "
        "the registry default — so the two surfaces may differ for the same "
        "connector, and this one is the one that holds for the actor reading it."))
    # The WORD the user uses for an account of THIS connector, when
    # "account" is wrong for them (a vault Slack account IS a workspace). The
    # front displays it as is. Always filled — the default is the generic word for "account".
    account_noun: str
    field_discriminator: str = Field(default="", description=(
        "The field whose value selects the others, when there is one "
        "(`auth_mode` on `http`). **EMPTY string**, never `null`, when there is none. "
        "⚠️ As long as no value is chosen, ALL fields are "
        "relevant: the server hides nothing before the input has decided, "
        "because hiding would be guessing. A form that filtered as soon as it "
        "opened would hide fields that setting up requires."))
    hosted_channel: Optional[str] = Field(default=None, description=(
        "The hosted channel this card represents (`LINKEDIN`, `WHATSAPP`…), "
        "when it is one. `null` = this is not a channel card. ⚠️ The channel "
        "is thus DERIVABLE from the card: the connection flow is declared with no "
        "parameter and the screen has no channel selector to render — **the card IS "
        "the channel**."))
    credential_of: Optional[str] = Field(default=None, description=(
        "The connector that HOLDS the credential, when it is not this one. "
        "`null` = it holds its own. ⚠️ A connector that delegates has **no field "
        "to fill in**, and setting a key under its name is REFUSED — otherwise two keys "
        "would contradict each other. The carrier named here is the one "
        "to send to connect."))
    # Input schema. Empty outside `method=secret` (the other mechanisms have their
    # dedicated flow, see `connect`).
    fields: list[CredentialField] = []


class ConnectParamOption(BaseModel):
    """A value of a closed choice of a flow parameter (the front renders a select)."""
    value: str
    label: str


class ConnectParam(BaseModel):
    """A value the user must provide to start the connection flow."""
    name: str
    label: str
    required: bool
    default: str = ""
    help: str = ""
    # Non-empty ⟹ CLOSED list. Empty ⟹ free input. This is the single home of these
    # values (`connectors/flow.py::FlowParam`), never copied into a front.
    options: list[ConnectParamOption] = []


class ConnectFlow(BaseModel):
    """The SHAPE of the "connect" gesture — never an authorization URL nor a
    capability name (`/api/connectors` is served WITHOUT auth, and the call path is fixed
    on the client side). `null` on the ~85 connectors that have no flow: the front
    then renders its usual fields form.

    The last four fields exist ONLY on the authenticated projection
    (`GET /api/me/connectors?verbose=true`) and never in the public catalog: they
    answer to whoever asks, which an anonymous catalog cannot do."""
    label: str
    params: list[ConnectParam] = []
    # Can the gesture be undone generically — `DELETE /api/me/connectors/{name}/oauth`
    # (`me.connector_disconnect`)? `false` = no "Disconnect" button to show.
    disconnect: bool = False
    # The consent return URL to register with the provider, DERIVED from the
    # environment (never hard-coded: a URL in prose lies as soon as it is read
    # from preprod, and consent fails on an incomprehensible `redirect_uri_mismatch`).
    # `null` = flow with no declared return.
    callback_url: Optional[str] = None
    # Does this user already have an OAuth app available (their own, their
    # org's, or the vendor's)? `null` = question not declared by the connector —
    # the front must then stay SILENT rather than claim an app remains to be set up.
    app_ready: Optional[bool] = None
    # The levels (`member`, `org`, `group`) whose app is ready, for a connector whose
    # app depends on the level (HubSpot: user-level app for personal, account-level for
    # org/team). `null` = not declared — `app_ready` answers for every tab.
    app_ready_scopes: Optional[list[str]] = None
    # Parameters this user's applicable app already binds (`{name: value}`), e.g. the
    # Zoho region of the app set by their team: shown, never asked — the start refuses
    # any other value. Empty = every parameter is chosen at click time.
    fixed: dict[str, str] = {}
    # Named codes about this user's applicable app — `zoho_org_unverified`: the app names
    # no expected Zoho organization, so any Zoho account can consent to it. Empty = none.
    warnings: list[str] = []


class FreeTier(BaseModel):
    """Free-tier (ADR 0031): the platform key is open without a grant, with a free
    quota per user and per day. `null` on the card = no free-tier."""
    daily_quota: int
