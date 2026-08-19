"""Connecteur Lightfield — CRM agent-native (api.lightfield.app).

Verrouille : l'entrée de registre (keyed byo-only, hors socle), la surface MCP sous
le namespace `lightfield` (9 tools, chacun avec une description — régression du piège
« docstring qui n'en est pas un »), la jointure tool↔client oto-core, et les quatre
endroits où ce module ne fait pas que passer le plat :

- la **sonde**, qui lit les scopes réellement accordés et refuse une clé qui
  authentifie sans pouvoir lire le CRM (leçon Zoho) — dont l'inversion « liste vide
  = accès complet » ;
- la **validation des clés de champ** contre les définitions DU workspace, avant
  l'appel, en nommant les clés valides ;
- le **dry-run**, et le fait que l'envoi d'email y soit par DÉFAUT ;
- la **projection** du retour, qui aplatit les champs et NOMME ce qu'elle écarte.
"""
import asyncio
from unittest.mock import patch

import pytest
from mcp.shared.exceptions import McpError

from oto_mcp import connector_verify, providers
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "lightfield_accounts", "lightfield_contacts", "lightfield_opportunities",
    "lightfield_lists", "lightfield_notes", "lightfield_tasks",
    "lightfield_meetings", "lightfield_emails", "lightfield_objects",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(
        "oto_mcp.access.resolve_api_key", lambda provider, account=None: ("k", False))


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import lightfield

    m = FastMCP("t")
    lightfield.register(m)
    return asyncio.run(m.get_tool(name))


class _FakeClient:
    """Client oto-core simulé : enregistre les appels, rend des formes réalistes."""

    def __init__(self, definitions=None, record=None):
        self.calls = []
        self._defs = definitions if definitions is not None else {
            "objectType": "account",
            "fieldDefinitions": {"name": {"label": "Name"}, "domain": {"label": "Domain"}},
            "relationshipDefinitions": {},
        }
        self._record = record if record is not None else {
            "data": [{
                "id": "a1", "createdAt": "2026-08-19", "httpLink": "https://x/1",
                "fields": {"name": {"value": "Acme", "valueType": "TEXT"}},
                "relationships": {"contacts": {"values": ["c1"]}},
            }]
        }

    def __getattr__(self, name):
        def _call(*a, **kw):
            self.calls.append((name, a, kw))
            if name.endswith("definitions"):
                return self._defs
            return self._record
        return _call


def _with_client(fake):
    return patch("oto.tools.lightfield.client.LightfieldClient", return_value=fake)


# --- registre -----------------------------------------------------------------

def test_lightfield_is_a_keyed_byo_only_connector():
    c = providers.REGISTRY["lightfield"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    # Ce sont les données du CRM du client : une clé oto partagée n'a pas de sens.
    assert "platform" not in c.auth_modes
    assert c.default_active is False               # deny-by-default
    assert "lightfield" in providers.KEY_PROVIDERS
    assert c.category == "CRM"
    assert c.publisher_name == "Lightfield"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["lightfield"] == "lightfield.app"


def test_lightfield_has_an_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["lightfield"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_the_namespace_carries_exactly_the_expected_tools(all_tools):
    got = {n for n in all_tools if namespace_of(n) == "lightfield"}
    assert got == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_tool_has_a_non_empty_description(all_tools, name):
    """Un docstring formé par CONCATÉNATION (`\"\"\"…\"\"\" + CONST`) n'est pas un
    docstring : `__doc__` vaut None et l'outil part sans description. Vécu en
    écrivant ce module — d'où ce test, qui l'aurait attrapé."""
    assert (all_tools[name].description or "").strip()


def test_the_verify_probe_is_registered():
    assert connector_verify.supports("lightfield")


# --- la sonde -----------------------------------------------------------------

def test_a_key_that_authenticates_but_cannot_read_the_crm_is_REFUSED():
    """La leçon Zoho : tester l'auth seule rend un vert trompeur. Les scopes se
    cochent à la création de la clé — une clé sans lecture CRM répond 200 puis
    échoue sur chaque appel réel."""
    from oto_mcp.tools import lightfield
    fake = _FakeClient()
    fake.validate = lambda: {"active": True, "scopes": ["members:read"]}
    with _with_client(fake), pytest.raises(ValueError, match="scope"):
        lightfield._verify({"key": "sk_lf_x"})


def test_an_EMPTY_scope_list_is_FULL_access_and_passes():
    """L'inversion : `scopes: []` = accès complet. Une sonde naïve refuserait la clé
    la plus puissante."""
    from oto_mcp.tools import lightfield
    fake = _FakeClient()
    fake.validate = lambda: {"active": True, "scopes": []}
    with _with_client(fake):
        assert lightfield._verify({"key": "sk_lf_x"}) is None


def test_an_inactive_key_is_refused():
    from oto_mcp.tools import lightfield
    fake = _FakeClient()
    fake.validate = lambda: {"active": False, "scopes": []}
    with _with_client(fake), pytest.raises(ValueError, match="active"):
        lightfield._verify({"key": "sk_lf_x"})


def test_a_key_with_one_core_read_scope_passes():
    from oto_mcp.tools import lightfield
    fake = _FakeClient()
    fake.validate = lambda: {"active": True, "scopes": ["contacts:read"]}
    with _with_client(fake):
        assert lightfield._verify({"key": "sk_lf_x"}) is None


# --- clés de champ : validées AVANT l'appel -----------------------------------

def test_an_unknown_field_key_is_refused_and_the_valid_ones_are_named():
    """Le modèle de champs est propre au workspace. L'API rendrait un 400
    `unknown_field` — exact, mais muet sur ce qui aurait marché."""
    fake = _FakeClient()
    with _with_client(fake), pytest.raises(McpError) as e:
        _tool("lightfield_accounts").fn(op="upsert", fields={"nom": "Acme"})
    msg = str(e.value)
    assert "nom" in msg and "domain" in msg and "name" in msg
    assert not [c for c in fake.calls if c[0] in ("create_account", "update_account")]


def test_a_known_field_key_reaches_the_client():
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_accounts").fn(op="upsert", fields={"name": "Acme"})
    assert [c for c in fake.calls if c[0] == "create_account"]


def test_unreadable_definitions_do_not_block_the_write():
    """Fail-open assumé : si les définitions sont illisibles, on laisse l'API
    trancher plutôt que d'inventer un refus."""
    fake = _FakeClient(definitions={"unexpected": "shape"})
    with _with_client(fake):
        _tool("lightfield_accounts").fn(op="upsert", fields={"whatever": 1})
    assert [c for c in fake.calls if c[0] == "create_account"]


# --- dry-run ------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,forbidden", [
    ("lightfield_accounts", {"op": "upsert", "fields": {"name": "A"}}, "create_account"),
    ("lightfield_tasks", {"op": "upsert", "fields": {"name": "T"}}, "create_task"),
    ("lightfield_notes", {"op": "create", "fields": {"name": "N"}}, "create_note"),
    ("lightfield_lists", {"op": "upsert", "fields": {"name": "L"}}, "create_list"),
])
def test_a_dry_run_write_echoes_the_payload_and_calls_nothing(tool, kwargs, forbidden):
    fake = _FakeClient()
    with _with_client(fake):
        out = _tool(tool).fn(dry_run=True, **kwargs)
    assert out["dry_run"] is True and out["payload"]["fields"] == kwargs["fields"]
    assert not [c for c in fake.calls if c[0] == forbidden]


def test_sending_an_email_is_dry_run_BY_DEFAULT(capsys):
    """Le seul geste du connecteur qui atteint une personne réelle : il faut le
    DEMANDER, pas l'obtenir par défaut."""
    fake = _FakeClient()
    with _with_client(fake):
        out = _tool("lightfield_emails").fn(
            op="send", sender="me@corp.test", to=["you@corp.test"], subject="Hi")
    assert out["dry_run"] is True
    assert not [c for c in fake.calls if c[0] == "send_email"]


def test_sending_for_real_requires_dry_run_false(capsys):
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_emails").fn(
            op="send", sender="me@corp.test", to=["you@corp.test"], dry_run=False)
    sent = [c for c in fake.calls if c[0] == "send_email"]
    assert sent and sent[0][1][0]["from"] == "me@corp.test"


def test_an_email_without_a_connected_sender_is_refused_before_any_call():
    fake = _FakeClient()
    with _with_client(fake), pytest.raises(McpError, match="CONNECT"):
        _tool("lightfield_emails").fn(op="send", to=["you@corp.test"], dry_run=False)
    assert fake.calls == []


def test_a_send_with_no_recipient_is_refused():
    fake = _FakeClient()
    with _with_client(fake), pytest.raises(McpError):
        _tool("lightfield_emails").fn(op="send", sender="me@corp.test", dry_run=False)


def test_draft_and_send_are_separate_paths():
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_emails").fn(
            op="draft", sender="me@corp.test", subject="x", dry_run=False)
    assert [c[0] for c in fake.calls] == ["draft_email"]


# --- projection ---------------------------------------------------------------

def test_the_default_projection_flattens_fields_and_names_what_it_dropped():
    fake = _FakeClient()
    with _with_client(fake):
        out = _tool("lightfield_accounts").fn(op="search")
    rec = out["data"][0]
    assert rec["fields"] == {"name": "Acme"}          # aplati, plus de valueType
    assert "relationships" not in rec                  # écarté
    assert out["projection"]["dropped"] == ["relationships"]
    assert out["projection"]["how_to_get_everything"] == "full=True"


def test_full_true_returns_the_record_untouched():
    fake = _FakeClient()
    with _with_client(fake):
        out = _tool("lightfield_accounts").fn(op="search", full=True)
    rec = out["data"][0]
    assert rec["fields"]["name"] == {"value": "Acme", "valueType": "TEXT"}
    assert "relationships" in rec
    assert "projection" not in out


# --- dispatch -----------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_an_unknown_op_is_refused_by_every_tool(tool):
    fake = _FakeClient()
    with _with_client(fake), pytest.raises(McpError, match="op"):
        _tool(tool).fn(op="nonsense")


def test_get_requires_an_id():
    fake = _FakeClient()
    with _with_client(fake), pytest.raises(McpError, match="record_id"):
        _tool("lightfield_accounts").fn(op="get")


def test_members_of_a_list_routes_to_the_right_member_type():
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_lists").fn(op="members", list_id="l1", of="contacts")
    assert fake.calls[0][0] == "list_contacts_of_list"


def test_search_forwards_pagination_and_filters_to_the_client():
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_contacts").fn(op="search", limit=10, offset=25,
                                        filters={"primary-email": "a@b.test"})
    name, _a, kw = fake.calls[0]
    assert name == "list_contacts"
    assert kw == {"limit": 10, "offset": 25, "primary-email": "a@b.test"}


def test_a_client_side_refusal_becomes_an_actionable_tool_error():
    """Le plafond `limit` de l'API est refusé par le client (ValueError) : le tool
    doit le rendre lisible, pas le laisser remonter en erreur interne."""
    fake = _FakeClient()

    def _boom(**kw):
        raise ValueError("`limit` doit être entre 1 et 25 (plafond de l'API Lightfield)")

    fake.list_accounts = _boom
    with _with_client(fake), pytest.raises(McpError, match="25"):
        _tool("lightfield_accounts").fn(op="search", limit=500)


def test_a_filter_named_like_the_pagination_is_refused_with_its_reason():
    """`filters` est splaté à côté de `limit`/`offset` : une clé de filtre homonyme
    lèverait un TypeError que `_run` ne traduit pas — l'agent recevrait une erreur
    interne opaque. On refuse en nommant le paramètre dédié, avant tout appel."""
    fake = _FakeClient()
    for tool, kwargs in (("lightfield_accounts", {}),
                         ("lightfield_meetings", {}),
                         ("lightfield_emails", {})):
        with _with_client(fake), pytest.raises(McpError, match="limit"):
            _tool(tool).fn(op="search", filters={"limit": 10}, **kwargs)
    assert fake.calls == []


def test_reading_the_members_of_a_list_resolves_the_api_key_ONCE(monkeypatch):
    """Les trois voies membre étaient des méthodes LIÉES dans un dict : construire le
    dict construisait les trois clients — trois lectures du coffre + déchiffrements
    pour un seul appel servi."""
    calls = []
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: (calls.append(provider), ("k", False))[1])
    fake = _FakeClient()
    with _with_client(fake):
        _tool("lightfield_lists").fn(op="members", list_id="l1", of="accounts")
    assert calls == ["lightfield"]
    assert fake.calls[0][0] == "list_accounts_of_list"
