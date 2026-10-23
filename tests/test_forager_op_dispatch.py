"""Dispatch `op=` des tools `forager_*` (6 tools, ADR 0047 : un objet métier, `op=` en verbe).

Trois familles de garanties, même patron que `test_silae_op_dispatch.py` :
  1. chaque op → la bonne méthode du client `ForagerClient`, avec les bons args ;
  2. les refus : op inconnue, argument obligatoire manquant nommant l'op, argument
     fourni mais non utilisé par l'op (silencieusement ignoré = résultat crédible à
     côté de la demande — la classe de bug que `_refuse_ignored` existe pour barrer) ;
  3. **la surface exacte** = 6 tools, et **aucun tool `api_keys_*`/`forager_api_key`**
     n'apparaît — la gestion de clé API reste dashboard-only par conception (secret
     brut jamais en argument MCP), pas un oubli à combler plus tard.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


class _Rc:
    """Stand-in for `ResolvedCredential`: the two attributes `_client` reads."""

    def __init__(self, is_platform: bool):
        self.is_platform = is_platform
        self.fields = {"api_key": "fg-test-key", "account_id": None}


@pytest.fixture
def key_mode():
    """Which key the cascade hands over: the customer's own (default) or ours."""
    return {"platform": False}


@pytest.fixture
def client(monkeypatch, key_mode):
    inst = MagicMock()
    monkeypatch.setattr("oto.tools.forager.ForagerClient", lambda **kw: inst)
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential",
        lambda provider, want="auto", **kw: _Rc(key_mode["platform"]),
    )
    return inst


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import forager as F

    m = FastMCP("t")
    F.register(m)
    return asyncio.run(m.get_tool(name)).fn


def test_the_surface_is_exactly_the_six_tools(client):
    from fastmcp import FastMCP
    from oto_mcp.tools import forager as F

    m = FastMCP("t")
    F.register(m)
    assert sorted(t.name for t in asyncio.run(m.list_tools())) == [
        "forager_account", "forager_autocomplete", "forager_feedback",
        "forager_job_post", "forager_organization", "forager_person",
    ]


# --- job posts ------------------------------------------------------------

def test_job_post_search_default(client):
    _tool("forager_job_post")(filters={"title": "engineer"})
    client.search_job_posts.assert_called_once_with(title="engineer")


def test_job_post_totals(client):
    _tool("forager_job_post")(op="totals", filters={"is_remote": True})
    client.search_job_posts_totals.assert_called_once_with(is_remote=True)


def test_job_post_no_filters_passes_empty_dict(client):
    _tool("forager_job_post")()
    client.search_job_posts.assert_called_once_with()


def test_job_post_bad_op(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("forager_job_post")(op="bogus")


# --- organizations (+ website folded in) -----------------------------------

def test_organization_search(client):
    _tool("forager_organization")(filters={"locations": [1, 2]})
    client.search_organizations.assert_called_once_with(locations=[1, 2])


def test_organization_totals(client):
    _tool("forager_organization")(op="totals")
    client.search_organizations_totals.assert_called_once_with()


def test_organization_website_by_domain(client):
    _tool("forager_organization")(op="website", domain="acme.com")
    client.lookup_website.assert_called_once_with(
        domain="acme.com", organization_id=None, organization_linkedin_public_identifier=None,
    )


def test_organization_website_requires_exactly_one_identifier(client):
    with pytest.raises(McpError, match="exactly one"):
        _tool("forager_organization")(op="website")
    with pytest.raises(McpError, match="exactly one"):
        _tool("forager_organization")(op="website", domain="acme.com", organization_id=5)
    client.lookup_website.assert_not_called()


def test_organization_search_refuses_website_params(client):
    with pytest.raises(McpError, match="op='search' does not use domain"):
        _tool("forager_organization")(domain="acme.com")
    client.search_organizations.assert_not_called()


def test_organization_website_refuses_filters(client):
    with pytest.raises(McpError, match="does not use filters"):
        _tool("forager_organization")(op="website", domain="acme.com", filters={"page": 2})
    client.lookup_website.assert_not_called()


# --- people ------------------------------------------------------------

@pytest.mark.parametrize("op,method", [
    ("detail", "lookup_person_detail"),
    ("work_emails", "lookup_person_work_emails"),
    ("personal_emails", "lookup_person_personal_emails"),
    ("phone_numbers", "lookup_person_phone_numbers"),
])
def test_person_lookup_ops_route_to_the_right_method(client, op, method):
    _tool("forager_person")(op=op, person_id=99)
    getattr(client, method).assert_called_once()


def test_person_lookup_requires_exactly_one_identifier(client):
    with pytest.raises(McpError, match="exactly one"):
        _tool("forager_person")(op="detail")
    with pytest.raises(McpError, match="exactly one"):
        _tool("forager_person")(op="detail", person_id=1, linkedin_public_identifier="janedoe")


def test_person_reverse_by_email(client):
    _tool("forager_person")(op="reverse_by_email", email="jane@acme.com")
    client.lookup_person_by_email.assert_called_once_with("jane@acme.com")


def test_person_reverse_by_email_requires_email(client):
    with pytest.raises(McpError, match="op='reverse_by_email' requires email"):
        _tool("forager_person")(op="reverse_by_email")
    client.lookup_person_by_email.assert_not_called()


def test_person_reverse_by_phone(client):
    _tool("forager_person")(op="reverse_by_phone", phone_number="+15551234567")
    client.lookup_person_by_phone_number.assert_called_once_with("+15551234567")


def test_person_do_contacts_enrichment_only_on_work_emails(client):
    with pytest.raises(McpError, match="do_contacts_enrichment only applies to op='work_emails'"):
        _tool("forager_person")(op="detail", person_id=1, do_contacts_enrichment=True)
    client.lookup_person_detail.assert_not_called()


def test_person_role_search(client):
    _tool("forager_person")(op="role_search", filters={"role_is_current": True})
    client.search_person_roles.assert_called_once_with(role_is_current=True)


def test_person_role_search_totals(client):
    _tool("forager_person")(op="role_search_totals")
    client.search_person_roles_totals.assert_called_once_with()


def test_person_role_search_refuses_identifier_params(client):
    """The refusal must name what was actually refused (`person_id`) and a
    hint the caller can act on — not a garbled reference to `filters`, which
    wasn't even passed here."""
    with pytest.raises(McpError, match=r"op='role_search' does not use person_id — selects by filters"):
        _tool("forager_person")(op="role_search", person_id=1)
    client.search_person_roles.assert_not_called()


def test_person_detail_do_contacts_enrichment_false_is_still_refused(client):
    """`False` is a real, meaningful value here (not "unset") — the refusal
    guard must not treat it as absent via a truthiness check."""
    with pytest.raises(McpError, match="do_contacts_enrichment only applies to op='work_emails'"):
        _tool("forager_person")(op="detail", person_id=1, do_contacts_enrichment=False)
    client.lookup_person_detail.assert_not_called()


def test_person_bad_op(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("forager_person")(op="bogus", person_id=1)


# --- feedback ------------------------------------------------------------

def test_feedback_personal_email(client):
    _tool("forager_feedback")(op="personal_email", contact_status="valid",
                               is_correct_person=True, email="jane@gmail.com")
    client.submit_personal_email_feedback.assert_called_once_with(
        "jane@gmail.com", "valid", True, name=None, person_id=None,
    )


def test_feedback_work_email(client):
    _tool("forager_feedback")(op="work_email", contact_status="invalid",
                               is_correct_person=False, email="jane@acme.com", person_id=5)
    client.submit_work_email_feedback.assert_called_once_with(
        "jane@acme.com", "invalid", False, name=None, person_id=5,
    )


def test_feedback_phone_number(client):
    _tool("forager_feedback")(op="phone_number", contact_status="connected",
                               is_correct_person=True, phone_number="+15551234567")
    client.submit_phone_number_feedback.assert_called_once_with(
        "+15551234567", "connected", True, name=None, person_id=None,
    )


def test_feedback_email_ops_require_email(client):
    with pytest.raises(McpError, match="requires email"):
        _tool("forager_feedback")(op="personal_email", contact_status="valid", is_correct_person=True)
    client.submit_personal_email_feedback.assert_not_called()


def test_feedback_phone_number_refuses_email_param(client):
    with pytest.raises(McpError, match="does not use email"):
        _tool("forager_feedback")(
            op="phone_number", contact_status="connected", is_correct_person=True,
            phone_number="+1", email="jane@acme.com",
        )
    client.submit_phone_number_feedback.assert_not_called()


def test_feedback_bad_op(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("forager_feedback")(op="bogus", contact_status="valid", is_correct_person=True)


# --- autocomplete ------------------------------------------------------------

@pytest.mark.parametrize("op", [
    "industries", "organizations", "organization_keywords",
    "locations", "person_skills", "web_technologies",
])
def test_autocomplete_ops_route_to_the_right_method(client, op):
    getattr(client, f"autocomplete_{op}").return_value = {"results": []}
    _tool("forager_autocomplete")(op=op, q="Paris")
    getattr(client, f"autocomplete_{op}").assert_called_once_with("Paris", page=None)


def test_autocomplete_passes_page(client):
    client.autocomplete_locations.return_value = {"results": []}
    _tool("forager_autocomplete")(op="locations", q="Paris", page=2)
    client.autocomplete_locations.assert_called_once_with("Paris", page=2)


# --- account ------------------------------------------------------------

def test_account_me(client):
    _tool("forager_account")()
    client.get_current_user.assert_called_once()


def test_account_me_refuses_date_range(client):
    with pytest.raises(McpError, match="op='me' has no date range"):
        _tool("forager_account")(date_created_start="2026-01-01")
    client.get_current_user.assert_not_called()


def test_account_balance_log(client):
    _tool("forager_account")(op="balance_log", date_created_start="2026-01-01", page=2)
    client.list_balance_change_logs.assert_called_once_with(
        date_created_start="2026-01-01", date_created_end=None, page=2,
    )


def test_account_balance_totals(client):
    _tool("forager_account")(op="balance_totals", date_created_start="2026-01-01")
    client.get_balance_change_totals.assert_called_once_with(
        date_created_start="2026-01-01", date_created_end=None,
    )


def test_account_balance_totals_refuses_page(client):
    with pytest.raises(McpError, match="page does not apply"):
        _tool("forager_account")(op="balance_totals", page=1)
    client.get_balance_change_totals.assert_not_called()


def test_account_bad_op(client):
    with pytest.raises(McpError, match="op must be"):
        _tool("forager_account")(op="bogus")


# --- multi-account refusal surfaces as a handled McpError, not a raw crash --

def test_multi_account_refusal_surfaces_as_invalid_params(monkeypatch):
    """`ForagerClient.resolve_account_id()` raises a plain `ValueError` when
    the key has access to several accounts and none was pinned. That's a
    caller-fixable input problem — it must reach the agent as `McpError`
    (`INVALID_PARAMS`), not propagate as an unhandled exception (which would
    land in Sentry as a backend defect, per this repo's own guide)."""
    inst = MagicMock()
    inst.search_job_posts.side_effect = ValueError(
        "forager: this API key has access to multiple accounts (1 (A), 2 (B)) — "
        "pass account_id explicitly, refusing to guess which one to bill"
    )
    monkeypatch.setattr("oto.tools.forager.ForagerClient", lambda **kw: inst)
    monkeypatch.setattr(
        "oto_mcp.access.resolve_credential",
        lambda provider, want="auto", **kw: _Rc(False),
    )
    with pytest.raises(McpError, match="multiple accounts"):
        _tool("forager_job_post")()


# --- API-key management is deliberately absent ------------------------------

def test_no_api_key_management_tool_exists(client):
    """`GET/POST /api/api_keys/` + `GET/DELETE /api/api_keys/{prefix}/` are real
    spec endpoints — deliberately unwrapped (dashboard-only, secret brut jamais
    en argument MCP). This is the tripwire: a future contributor adding a
    convenience wrapper for them should see this test and reconsider."""
    from fastmcp import FastMCP
    from oto_mcp.tools import forager as F

    m = FastMCP("t")
    F.register(m)
    names = {t.name for t in asyncio.run(m.list_tools())}
    assert not any("api_key" in n for n in names)


# --- the platform key: the ops the instance serves, metered -----------------

@pytest.fixture
def trace(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("oto_mcp.session_org.note_call_trace", lambda **kw: seen.update(kw))
    return seen


@pytest.fixture
def platform_usage(monkeypatch):
    calls: list = []
    from _releve import capter_units
    capter_units(monkeypatch, "forager", lambda provider, n: calls.append((provider, n)))
    return calls


@pytest.fixture
def setting(monkeypatch):
    """The `platform.ops` row of `connector_settings`: None = unset."""
    row = {"value": None}

    def _get(scope_type, scope_id, connector, key, *a, **kw):
        assert (scope_type, scope_id, connector, key) == (
            "platform", "platform", "forager", "platform.ops")
        return row["value"]
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting", _get)
    return row


_EVERYTHING_BUT_PHONES = [
    ("forager_job_post", {}),
    ("forager_organization", {}),
    ("forager_person", {"op": "detail", "person_id": 1}),
    ("forager_person", {"op": "work_emails", "person_id": 1}),
    ("forager_autocomplete", {"op": "locations", "q": "Paris"}),
    ("forager_account", {}),
    ("forager_feedback", {"op": "phone_number", "contact_status": "connected",
                          "is_correct_person": True, "phone_number": "+33100000000"}),
]


@pytest.mark.parametrize("tool, kwargs", _EVERYTHING_BUT_PHONES)
def test_unset_the_platform_key_refuses_everything_but_phone_numbers(
        client, key_mode, setting, tool, kwargs):
    key_mode["platform"] = True
    with pytest.raises(McpError, match="needs your own Forager key"):
        _tool(tool)(**kwargs)
    assert not client.method_calls, "a refused op must not reach Forager (no credit spent)"


def test_platform_key_runs_phone_numbers_and_meters_it(
        client, key_mode, setting, trace, platform_usage):
    key_mode["platform"] = True
    client.lookup_person_phone_numbers.return_value = [{"phone_number": "+33100000000"}]
    out = _tool("forager_person")(op="phone_numbers", person_id=7)
    assert out == [{"phone_number": "+33100000000"}]
    assert trace == {"found_work_emails": 0, "found_personal_emails": 0,
                     "found_phones": 1, "quantity": 1}
    assert platform_usage == [("forager", 1)]


def test_the_setting_opens_a_whole_tool_or_one_op(client, key_mode, setting, platform_usage):
    key_mode["platform"] = True
    setting["value"] = "forager_organization, forager_person:work_emails"
    _tool("forager_organization")(op="website", domain="example.com")
    _tool("forager_person")(op="work_emails", person_id=1)
    assert platform_usage == [("forager", 1), ("forager", 1)]
    with pytest.raises(McpError, match="needs your own Forager key"):
        _tool("forager_person")(op="phone_numbers", person_id=1)


def test_a_totals_count_debits_nothing(client, key_mode, setting, trace, platform_usage):
    key_mode["platform"] = True
    setting["value"] = "forager_job_post"
    _tool("forager_job_post")(op="totals")
    assert trace == {} and platform_usage == [], "units=0: served, nothing debited"


def test_free_tools_note_no_units(client, key_mode, setting, platform_usage):
    key_mode["platform"] = True
    setting["value"] = "forager_autocomplete"
    _tool("forager_autocomplete")(op="locations", q="Paris")
    assert platform_usage == []


def test_forager_account_is_never_served_on_the_platform_key(client, key_mode, setting):
    key_mode["platform"] = True
    setting["value"] = ",".join(F_OPS())
    with pytest.raises(McpError, match="needs your own Forager key"):
        _tool("forager_account")()
    assert not client.method_calls


def test_own_key_notes_no_units_and_ignores_the_setting(client, setting, platform_usage):
    setting["value"] = "forager_person:phone_numbers"
    _tool("forager_job_post")()
    assert platform_usage == []


def F_OPS():
    from oto_mcp.tools import forager as F
    return sorted(F.OPS)


@pytest.mark.parametrize("value, error", [
    ("forager_account", "not a Forager tool"),
    ("forager_person:nope", "has no op"),
    ("apollo_search_people", "not a Forager tool"),
])
def test_the_setting_refuses_what_the_tool_would_not_understand(value, error):
    from oto_mcp.tools import forager as F
    with pytest.raises(ValueError, match=error):
        F.parse_platform_ops(value)


def test_an_unreadable_setting_serves_the_default(monkeypatch):
    from oto_mcp.tools import forager as F

    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting", _boom)
    assert F.platform_ops() == {("forager_person", "phone_numbers")}


def test_a_miss_traces_zero_phones(client, trace, platform_usage):
    client.lookup_person_phone_numbers.return_value = []
    _tool("forager_person")(op="phone_numbers", linkedin_public_identifier="someone")
    assert trace == {"found_work_emails": 0, "found_personal_emails": 0,
                     "found_phones": 0, "quantity": 0}
    assert platform_usage == [], "own key: nothing debited on the platform counters"


def test_a_miss_on_the_platform_key_traces_zero_and_bills_nothing(
        client, key_mode, setting, trace, platform_usage):
    """A miss on OUR key: `quantity=0`, so the aggregated view (NULL reads 1) does not
    count it as a billed lookup."""
    key_mode["platform"] = True
    client.lookup_person_phone_numbers.return_value = []
    _tool("forager_person")(op="phone_numbers", person_id=7)
    assert trace["found_phones"] == 0 and trace["quantity"] == 0


def test_the_billing_lens_reads_the_trace_as_found_phones(client, trace, platform_usage):
    """The trace goes through the lens as the billing consumer reads it: `_found_from_row`
    is ALL OR NOTHING over the three `found_*` (a lone `found_phones` read `None`)."""
    from oto_mcp.db.usage import BILLABLE_FOUND_ARGS, _found_from_row

    client.lookup_person_phone_numbers.return_value = [{"phone_number": "+33100000000"}]
    _tool("forager_person")(op="phone_numbers", person_id=7)
    ligne = {arg: str(trace[arg]) for arg in BILLABLE_FOUND_ARGS.values()}
    assert _found_from_row(ligne) == {"work_emails": 0, "personal_emails": 0, "phones": 1}


def test_own_key_still_runs_every_op(client):
    _tool("forager_person")(op="detail", person_id=1)
    client.lookup_person_detail.assert_called_once()


# --- the admin console: `platform.ops` is refused unless the tool understands it ---

def _set(monkeypatch, value, org_id=None):
    from oto_mcp.capabilities import platform_connectors as PC
    from oto_mcp.capabilities._types import ResolvedCtx
    written = []
    monkeypatch.setattr("oto_mcp.db.connector_settings.set_connector_setting",
                        lambda *a, **k: written.append(a))
    PC._connector_setting(ResolvedCtx(sub="admin", org_id=1), PC.ConnectorSettingInput(
        op="set", connector="forager", key="platform.ops", value=value, org_id=org_id))
    return written


def test_the_console_stores_a_list_the_tool_understands(monkeypatch):
    written = _set(monkeypatch, "forager_person, forager_organization:website")
    assert written == [("platform", "platform", "forager", "platform.ops",
                        "forager_person, forager_organization:website")]


@pytest.mark.parametrize("value, org_id", [
    ("forager_account", None),
    ("forager_person:nope", None),
    ("forager_person", 42),
])
def test_the_console_refuses_a_list_the_tool_would_not_serve(monkeypatch, value, org_id):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _set(monkeypatch, value, org_id)
    assert e.value.code == "invalid_setting"
