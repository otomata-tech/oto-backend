"""Registry declaration of the `http` connector.

Sole home of its entry: `providers/__init__.py` AGGREGATES it (it does not
describe it). See `providers/_model.py` for the `Connector` contract.
"""
from __future__ import annotations

from ._model import CredentialField, _c

# Multi-auth HTTP client: unlike the bridge, oto HOLDS the target API's
# secret (AES vault, byo_org) and calls the API directly (no remote
# service). `auth_mode` discriminates the mode (bearer/header/query/basic/oauth2/
# none); the required secret fields depend on the mode (validated at call-time by
# oto_http.build_auth).
# TO BE DISTINGUISHED from the bridge (credential outside the platform): here the key
# is entrusted to oto — no custody on the client side.
#
# ⚠️ This comment announced "read-only (GET), anti-SSRF guard on
# the host" until 2026-08-27 — TWO false claims (oto-backend#449): the
# connector also carries `http_post`, and no SSRF guard existed. The second
# was then presented as intentional, on the grounds that the platform's egress
# filtering compensated; it only blocks one range (link-local), loopback
# and private ranges remained reachable. The guard now exists
# in the code — `oto_mcp/egress.py`, called by `tools/http.py:_client()` —
# and a legitimate internal destination is declared as a NAMED exception.
# CLOSED set of auth modes. COPIED from `oto.tools.http.AUTH_MODES` — not imported:
# the registry stays pure (no module-level runtime dependency, otherwise a missing
# dep would remove the connector from the catalog instead of degrading it). The copy is
# held by the `test_http_auth_modes.py` tripwire, which compares it to oto-core AND checks
# that the fields declared required per mode are EXACTLY the ones `build_auth` requires.
AUTH_MODES = ("bearer", "header", "query", "basic", "oauth2", "none")

CONNECTOR = _c(
    "http", ["http"], auth_modes={"byo_org"}, secret_kind="fields",
    # Presented as "Custom API": the connector a person looks for when the tool they
    # use (a payroll or ERP SaaS…) is not in the catalogue. The NAME stays `http` —
    # tool names, vault rows and instance refs all carry it.
    label="Custom API",
    # The pair (`tests/test_convention_act.py`): `tools/http_ops.py` serves `http`
    # (reads: custom connectors, definition, setup link), `tools/http_calls.py` serves
    # `http_act` (calls, OpenAPI import, saving the definition). `tools/http.py` keeps
    # the raw-path tools from before the pair (`http_get`/`http_post`/`http_doc`).
    modules=("http", "http_ops", "http_calls"),
    # `auth_mode` SELECTS the other fields (oto-backend#449): a `bearer` form
    # need not show the six oauth2 and basic fields, and the write
    # refuses an inconsistent mode instead of accepting it then failing on the 1st call.
    field_discriminator="auth_mode",
    help="connect any tool that has an API key but no connector in the catalogue "
         "(e.g. a payroll or ERP tool): fill in the API's base URL, the auth mode "
         "(bearer / key in header or query / basic / oauth2) and paste the key here — "
         "never in a chat. oto stores it encrypted and calls the API directly; the "
         "operations you describe (or import from its OpenAPI) become callable by name.",
    # `when=` = the modes that make the field RELEVANT; `required` then applies
    # WITHIN those modes. The three fields without `when` apply whatever the mode.
    credential_fields=(
        CredentialField("base_url", "Base URL", secret=False,
                        help="root of the API (e.g. https://api.acme.com). `http://` "
                             "is accepted. An INTERNAL address (loopback, "
                             "private network) is refused until it has been "
                             "declared as a named exception by the platform "
                             "operator — the refusal says how"),
        CredentialField("auth_mode", "Auth mode", secret=False,
                        choices=AUTH_MODES,
                        help="what the API expects to authenticate you — it decides the "
                             "fields to fill in next"),
        # `in_meta` (17/10/2026): the display name is what every member's agent and
        # the roster show for this custom connector — read in clear from `meta`,
        # without decrypting the card. A card set before keeps it in the blob until
        # its next save (`preparer_pose` merges, then moves it); meanwhile the
        # account name stands in.
        # A NAME, not a key: "Acme Payroll" must not come back as "AcmePayroll".
        CredentialField("label", "Display name", secret=False, in_meta=True,
                        required=False, whitespace_significant=True,
                        help="e.g. \"Acme API\" — visible to your org only"),
        CredentialField("doc_path", "Doc route", secret=False,
                        required=False,
                        help="path relative to base_url that returns the API's "
                             "documentation (e.g. /openapi.json) — serves the `http_doc` tool, "
                             "when set. Same auth as the rest, no separate "
                             "public route"),
        CredentialField("token", "Token / API key", secret=True,
                        when=("bearer", "header", "query"),
                        help="value of the bearer, or of the key (header/query modes)"),
        CredentialField("header_name", "Header name", secret=False,
                        when=("header",), help="e.g. x-api-key"),
        CredentialField("query_param", "Param name", secret=False,
                        when=("query",), help="e.g. api_key"),
        CredentialField("username", "Username", secret=False,
                        when=("basic",), help="identifier of the basic pair"),
        CredentialField("password", "Password", secret=True, when=("basic",),
                        whitespace_significant=True, help="password of the basic pair"),
        CredentialField("token_url", "Token URL", secret=False,
                        when=("oauth2",), help="client-credentials endpoint"),
        CredentialField("client_id", "Client ID", secret=False,
                        when=("oauth2",), help="identifier of the client application"),
        CredentialField("client_secret", "Client secret", secret=True,
                        when=("oauth2",), help="secret of the client application"),
        CredentialField("scope", "Scope", secret=False,
                        when=("oauth2",), required=False,
                        help="scopes requested from the token server"),
    ),
)

# Publisher: the connector is OURS, and there is no intermediary to name — oto
# holds the secret and calls DIRECTLY the API the org configured (no remote
# service). The host called is the `base_url` of each credential: it does not exist
# at the registry level, and nothing passes itself off as something else. DECLARED, not derived
# from a default: since 2026-09-02 there is none (`Connector.publisher_name`).
PUBLISHER = "Otomata"
# Its own shelf: what a person browses when their tool has no card of its own.
CATEGORY = "Custom & industry apps"
SANS_LOGO_DE_MARQUE = True

DESCRIPTION = (
    "Connect any tool that has an API key but no dedicated connector — a payroll, "
    "an ERP, an internal service: fill in the base URL, choose the authentication "
    "mode (bearer, header, query, basic, OAuth2 or none) and paste the key in the "
    "form, where the vault encrypts it. The agent calls the API by path, or by name "
    "through the operations you describe or import from its OpenAPI document."
)
