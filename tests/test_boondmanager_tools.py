"""Tools `boondmanager_*` et sonde du connecteur BoondManager.

Ce que ce fichier verrouille — les tripwires génériques couvrent déjà registre,
éditeur, logo, dérivation des modules, prose servie et jointure au client :
- la SURFACE : 4 tools, aucun verbe de modification ou de suppression ;
- le credential à trois champs passé tel quel au client ;
- `boondmanager_create` en dry-run PAR DÉFAUT : validé, rendu, jamais envoyé ;
- le dictionnaire : les clés sans `path`, une branche avec, une clé absente
  refusée en nommant celles qui existent ;
- la traduction des refus amont et des refus locaux du client ;
- la sonde : `current_user()`, 401/403 → `NonAutorise`, autre code → non classé.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.mcp_errors import McpError

_CREDS = {"client_token": "ct", "client_key": "ck", "user_token": "ut"}


@pytest.fixture
def client(monkeypatch):
    """Le faux client, et les kwargs de chaque construction."""
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.boondmanager.BoondManagerClient", fabrique)
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: dict(_CREDS))
    inst.built = built
    return inst


def _mcp():
    from fastmcp import FastMCP
    from oto_mcp.tools import boondmanager as B

    m = FastMCP("t")
    B.register(m)
    return m


def _tool(name: str):
    return asyncio.run(_mcp().get_tool(name)).fn


def _upstream(status, body=None):
    from oto.tools.common.errors import UpstreamHTTPError
    return UpstreamHTTPError(status, body or {"errors": ["x"]}, service="boondmanager")


# --- surface ------------------------------------------------------------------

def test_surface_quatre_tools_sans_modification():
    noms = {t.name for t in asyncio.run(_mcp().list_tools())}
    assert noms == {"boondmanager_search", "boondmanager_get",
                    "boondmanager_dictionary", "boondmanager_create"}
    assert not any(v in n for n in noms for v in ("update", "delete", "merge"))


def test_chaque_tool_a_une_description():
    for t in asyncio.run(_mcp().list_tools()):
        assert t.description and len(t.description) > 40, t.name


# --- recherche et lecture -------------------------------------------------------

def test_search_route_et_credential(client):
    client.search.return_value = {"data": [], "meta": {"totals": {"rows": 0}}}
    out = _tool("boondmanager_search")(entity="contacts", keywords="CSOC3",
                                       max_results=100)
    assert out["data"] == [] and out["meta"] == {"totals": {"rows": 0}}
    assert client.built == [_CREDS]
    args, kw = client.search.call_args
    assert args == ("contacts",)
    assert kw["keywords"] == "CSOC3" and kw["max_results"] == 100


# Conforme au schéma officiel de la référence (`contacts/search.json`).
_PAGE = {"meta": {"version": "9.1", "isLogged": True, "language": "fr",
                  "totals": {"rows": 1}},
         "data": [{"id": "5", "type": "contact",
                   "attributes": {"lastName": "Lovelace"},
                   "relationships": {
                       "company": {"data": {"id": "12", "type": "company"}},
                       "mainManager": {"data": {"id": "3", "type": "resource"}}}}],
         "included": [{"id": "12", "type": "company", "attributes": {"name": "X"}}]}


def test_search_vue_resserree_par_defaut(client):
    client.search.return_value = _PAGE
    out = _tool("boondmanager_search")(entity="contacts")
    assert out["data"] == [{"id": "5", "type": "contact",
                            "attributes": {"lastName": "Lovelace"},
                            "relationships": {"company": "12", "mainManager": "3"}}]
    assert out["meta"] == _PAGE["meta"] and "included" not in out
    assert "included" in out["projection"]["omitted"]


def test_search_full_rend_le_brut(client):
    client.search.return_value = _PAGE
    assert _tool("boondmanager_search")(entity="contacts", full=True) == _PAGE


def test_get_route(client):
    _tool("boondmanager_get")(entity="companies", record_id=3)
    client.get.assert_called_once_with("companies", 3)


def test_refus_local_du_client_devient_invalid_params(client):
    client.search.side_effect = ValueError("`keywords`: 'AO7' uses prefix 'AO'")
    with pytest.raises(McpError, match="AO7"):
        _tool("boondmanager_search")(entity="companies", keywords="AO7")


@pytest.mark.parametrize("status,attendu", [
    (401, "API access"), (403, "API access"), (404, "not found"),
    (422, "rejected the data"), (429, "per month"), (503, "temporarily"),
])
def test_refus_amont_traduits(client, status, attendu):
    client.get.side_effect = _upstream(status)
    with pytest.raises(McpError, match=attendu):
        _tool("boondmanager_get")(entity="contacts", record_id=1)


# Forme réelle (relevée sur l'API) d'un jeton refusé : un 422, pas un 401.
_JETON_REFUSE = {"meta": {"isLogged": False}, "errors": [{
    "status": "422", "code": "422", "detail": "422 - unable to load agency key",
    "source": {"parameter": "xJwtClient"}}]}


def test_jeton_refuse_en_422_dit_l_acces(client):
    client.get.side_effect = _upstream(422, _JETON_REFUSE)
    with pytest.raises(McpError, match="API access"):
        _tool("boondmanager_get")(entity="contacts", record_id=1)


def test_422_de_donnees_reste_un_refus_de_donnees(client):
    client.create.side_effect = _upstream(422, {"errors": [{
        "detail": "lastName required", "source": {"pointer": "/data/attributes"}}]})
    with pytest.raises(McpError, match="rejected the data"):
        _tool("boondmanager_create")(entity="companies", attributes={"name": "A"},
                                     dry_run=False)


# --- dictionnaire ---------------------------------------------------------------

_DICT = {"data": {"setting": {"state": {"contact": [{"id": 1, "value": "Client"}],
                                        "company": []},
                              "action": {"contact": []}},
                  "language": "fr"}}


def test_dictionnaire_sans_path_rend_les_cles(client):
    client.dictionary.return_value = _DICT
    out = _tool("boondmanager_dictionary")()
    assert out["keys"] == {"setting": ["action", "state"], "language": "<str>"}


def test_dictionnaire_branche(client):
    client.dictionary.return_value = _DICT
    out = _tool("boondmanager_dictionary")(path="setting.state.contact")
    assert out == {"path": "setting.state.contact",
                   "value": [{"id": 1, "value": "Client"}]}


def test_dictionnaire_cle_absente_nomme_les_existantes(client):
    client.dictionary.return_value = _DICT
    with pytest.raises(McpError, match="Keys there: action, state"):
        _tool("boondmanager_dictionary")(path="setting.typeOf")


# --- création -------------------------------------------------------------------

def test_create_dry_run_par_defaut_n_appelle_rien(client):
    out = _tool("boondmanager_create")(
        entity="contacts", attributes={"firstName": "Ada", "lastName": "Lovelace"},
        relationships={"company": {"type": "company", "id": 12}})
    assert out["dry_run"] is True and out["would_post"] == "/contacts"
    assert out["body"]["data"]["relationships"]["company"]["data"] == {
        "id": "12", "type": "company"}
    assert client.create.call_count == 0 and client.built == []


def test_create_dry_run_valide_les_champs_requis(client):
    with pytest.raises(McpError, match="company"):
        _tool("boondmanager_create")(
            entity="contacts", attributes={"firstName": "A", "lastName": "B"})


def test_create_reel_seulement_avec_dry_run_false(client):
    client.create.return_value = {"data": {"id": "99", "type": "company"}}
    out = _tool("boondmanager_create")(entity="companies",
                                       attributes={"name": "Acme"}, dry_run=False)
    assert out["data"]["id"] == "99"
    client.create.assert_called_once_with("companies", {"name": "Acme"}, None)


# --- sonde ----------------------------------------------------------------------

def _sonde(monkeypatch, effet=None):
    inst = MagicMock()
    if effet is not None:
        inst.current_user.side_effect = effet
    monkeypatch.setattr("oto.tools.boondmanager.BoondManagerClient",
                        lambda **kw: inst)
    from oto_mcp.tools import boondmanager as B
    return lambda: B._verify(dict(_CREDS)), inst


def test_sonde_ok(monkeypatch):
    run, inst = _sonde(monkeypatch)
    run()
    inst.current_user.assert_called_once()


@pytest.mark.parametrize("status,body", [(401, None), (403, None),
                                         (422, _JETON_REFUSE)])
def test_sonde_refus_d_acces(monkeypatch, status, body):
    run, _ = _sonde(monkeypatch, _upstream(status, body))
    with pytest.raises(cv.NonAutorise):
        run()


def test_sonde_autre_erreur_non_classee(monkeypatch):
    run, _ = _sonde(monkeypatch, _upstream(500))
    with pytest.raises(RuntimeError):
        run()
