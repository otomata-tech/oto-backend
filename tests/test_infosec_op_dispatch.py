"""L'enveloppe MCP `infosec_domain` (ADR 0047 §Amendement : 6 tools → 1).

Les vérifications elles-mêmes (RDAP, DNS, SPF/DMARC/DKIM, listes de blocage, score de
délivrabilité, CT, TLS, en-têtes) vivent dans oto-core depuis v1.233.0
(`oto.tools.infosec.InfosecClient`, oto-now/connectors#244) et s'y testent. Ici, la part
de l'hôte seulement : chaque op et ses arguments arrivent tels quels au client, le
User-Agent est celui de l'instance (#968), un refus local (`ValueError`) devient
`INVALID_PARAMS` AVANT tout appel réseau, un refus amont remonte tel quel.

Aucun test ne sort sur le réseau.
"""
import asyncio

import pytest
import requests
from mcp.types import INVALID_PARAMS
from oto.tools.common.errors import UpstreamHTTPError

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import infosec as I

_OPS = ("whois", "dns", "email_security", "subdomains", "tls", "headers",
        "blocklist", "deliverability")


def _tool():
    from fastmcp import FastMCP

    m = FastMCP("t")
    I.register(m)
    return asyncio.run(m.get_tool("infosec_domain")).fn


@pytest.fixture
def client(monkeypatch):
    """Remplace le client de la lib par un mouchard : on teste l'ENVELOPPE, pas la lib."""
    seen: dict = {}

    class _Client:
        def __init__(self, user_agent):
            seen["ua"] = user_agent

        def check(self, op, **kw):
            seen["check"] = (op, kw)
            return {"op": op}

    monkeypatch.setattr(I, "InfosecClient", _Client)
    return seen


@pytest.fixture
def no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("appel réseau inattendu")
    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests.Session, "request", _boom)


# --- transmission ----------------------------------------------------------------

@pytest.mark.parametrize("op", _OPS)
def test_each_op_reaches_the_client_with_its_arguments(client, op):
    out = _tool()(op=op, domain="https://Example.com/x", ip="1.2.3.4",
                  dkim_selector="s1", limit=7, port=8443)
    assert out == {"op": op}
    assert client["check"] == (op, {"domain": "https://Example.com/x", "ip": "1.2.3.4",
                                    "dkim_selector": "s1", "limit": 7, "port": 8443})


def test_optional_args_keep_their_historical_defaults(client):
    """Défauts d'avant la fusion : `limit=100` (subdomains), `port=443` (tls)."""
    _tool()(op="subdomains", domain="example.com")
    _, kw = client["check"]
    assert (kw["limit"], kw["port"], kw["ip"], kw["dkim_selector"]) == (100, 443, None, None)


def test_the_probes_present_this_instance(client):
    """Le User-Agent porte l'adresse de CETTE instance (#968), résolue à l'appel."""
    _tool()(op="dns", domain="example.com")
    assert client["ua"] == I._ua()
    assert I._ua().startswith("oto-infosec/1.0 (+")


def test_the_user_agent_reaches_the_wire(monkeypatch):
    """Bout en bout à travers la vraie lib (transport stubbé) : l'UA de l'hôte part
    bien chez RDAP, et un domaine non enregistré reste une réponse, pas une erreur."""
    seen = {}

    class _Resp:
        status_code = 404

    def _get(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers")
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)
    out = _tool()(op="whois", domain="contact@example.com")
    assert out["found"] is False and out["domain"] == "example.com"
    assert seen["headers"] == {"user-agent": I._ua()}


# --- refus -------------------------------------------------------------------------

def test_op_is_mandatory():
    """Pas de défaut : demander « l'infosec d'un domaine » sans dire QUOI est une
    erreur d'appel, pas une invitation à choisir à la place de l'appelant."""
    with pytest.raises(TypeError):
        _tool()(domain="example.com")


def test_unknown_op_is_refused_with_the_allowed_list(no_network):
    with pytest.raises(McpError, match="op must be") as e:
        _tool()(op="portscan", domain="example.com")
    assert e.value.error.code == INVALID_PARAMS
    for op in _OPS:
        assert op in str(e.value)


@pytest.mark.parametrize("op", _OPS)
@pytest.mark.parametrize("bad", ["", "   ", "@", "://"])
def test_invalid_domain_is_refused_before_any_network_call(no_network, op, bad):
    with pytest.raises(McpError, match="invalid domain") as e:
        _tool()(op=op, domain=bad)
    assert e.value.error.code == INVALID_PARAMS


@pytest.mark.parametrize("ip,msg", [("2001:db8::1", "IPv4"), ("pas-une-ip", "invalid ip")])
def test_a_bad_ip_is_refused_before_any_network_call(no_network, ip, msg):
    with pytest.raises(McpError, match=msg) as e:
        _tool()(op="blocklist", ip=ip)
    assert e.value.error.code == INVALID_PARAMS


def test_an_upstream_refusal_is_left_to_the_error_envelope(monkeypatch):
    """Un refus amont n'est ni avalé ni requalifié en erreur d'appel : il remonte tel
    quel, et l'enveloppe d'erreur le classe par son statut."""
    class _Client:
        def __init__(self, user_agent):
            pass

        def check(self, op, **kw):
            raise UpstreamHTTPError(503, "down", service="rdap")

    monkeypatch.setattr(I, "InfosecClient", _Client)
    with pytest.raises(UpstreamHTTPError):
        _tool()(op="whois", domain="example.com")


# --- surface -----------------------------------------------------------------------

def test_the_tool_declares_exactly_the_ops_of_the_lib():
    """Le `Literal` de l'outil et `OPS` de la lib ne peuvent pas dériver l'un de l'autre :
    une op ajoutée d'un côté seulement serait refusée ou invisible."""
    from typing import get_args, get_type_hints

    from oto.tools.infosec import OPS

    assert get_args(get_type_hints(_tool())["op"]) == OPS == _OPS
