"""The generic reader of a description (`tools/decrits.py`, `connectors/descriptions.py`), decided 05/11/2026, which
runs each call by the library's engine (`oto.connectors.engine`, 09/11/2026).

On a described connector of test (`_decrit_d_essai`), against a fake vendor (the engine's own `requests` session,
mounted on an adapter, no network): the pair and its schemas, the call with the caller's key, the named refusal,
`strip` and `projection`, the pages, the probe, the effects and batches derived (`effect: send` and `destroy`
included), recipes and the call log, the inscription that changes nothing else; then the real Search Console
description, its service account key and its probe.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import FastMCP

import _decrit_d_essai as essai
from oto_mcp import calllog, error_taxonomy, journal_secrets, providers
from oto_mcp.connectors import descriptions
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.recipes import effets as effets_recette
from oto_mcp.tools import decrits, effets
from oto_mcp.tools.lecture import ACTE, SIMULATION
from oto.connectors import engine
from oto.tools.common.errors import UpstreamHTTPError

pytestmark = pytest.mark.exige_pin_oto_core   # the library's served descriptions, `serve` and the engine


@pytest.fixture
def vendeur(monkeypatch):
    """The vendor the engine reaches: its own session replaced by one mounted on the fake, a 429 replayed at once."""
    v = essai.Vendeur()
    monkeypatch.setattr(engine.run, "_session", v.session)
    monkeypatch.setattr(engine.transport, "_sleep", lambda s: None)
    return v


@pytest.fixture
def monte(vendeur, monkeypatch):
    monkeypatch.setattr(decrits.access, "resolve_api_key", lambda name, *a, **k: (f"key-of-{name}", False))
    mcp = FastMCP("essai")
    outils = {t.name: t for t in decrits.monter(mcp, "essai", essai.description())}
    yield outils
    connector_verify._REGISTRY.pop("essai", None)
    connector_verify._COUVERTURE.pop("essai", None)


def _appel(outil, **arguments):
    return asyncio.run(outil.run(arguments)).structured_content


# --- the pair -----------------------------------------------------------------------------------------------------


def test_the_pair_splits_on_act(monte):
    assert set(monte) == {"essai", "essai_act"}
    lecteur, acteur = monte["essai"], monte["essai_act"]
    assert lecteur.parameters["properties"]["op"]["enum"] == ["get_account", "list_things", "get_thing", "save_draft"]
    assert acteur.parameters["properties"]["op"]["enum"] == ["update_thing", "add_people", "tag_things",
                                                             "delete_thing"]
    assert lecteur.annotations == SIMULATION and acteur.annotations == ACTE
    assert lecteur.parameters["required"] == ["op"] and lecteur.parameters["additionalProperties"] is False
    assert {"limit", "cursor", "all_pages", "max_pages", "thing_id", "text"} == set(lecteur.parameters["properties"]) - {"op"}


def test_a_side_without_a_write_is_lecture():
    d = essai.description()
    d["functions"] = [f for f in d["functions"] if f["class"] == "read"]
    assert not descriptions.lecteur_simule(descriptions.outils(d)["essai"])
    assert "essai" not in descriptions.effets(d)


def test_the_description_names_one_sentence_per_op(monte):
    texte = monte["essai_act"].description
    assert "- `add_people` — Add people." in texte and "More detail" not in texte
    assert "Reading is `essai`" in texte


def test_an_argument_declared_twice_differently_is_either():
    f1 = {"name": "a", "input": {"properties": {"x": {"type": "string"}}}}
    f2 = {"name": "b", "input": {"properties": {"x": {"type": "integer"}}}}
    f3 = {"name": "c", "input": {"properties": {"x": {"type": "string"}}}}
    assert decrits.schema_union([f1, f2, f3])["properties"]["x"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


# --- the call -----------------------------------------------------------------------------------------------------


def test_a_call_is_sent_by_the_engine_with_the_callers_key(monte, vendeur):
    vendeur.answers.append((200, {"id": 3, "owner": {"photo": "https://files.example.test/p.png", "name": "a"}}))
    assert _appel(monte["essai"], op="get_thing", thing_id=3) == {"id": 3, "owner": {"name": "a"}}
    envoi = essai.envoyee(vendeur.requests[0])
    assert (envoi["method"], envoi["url"], envoi["headers"]["x-key"]) == (
        "GET", "https://api.example.test/things/3", "key-of-essai")


def test_a_body_is_placed_as_the_call_says(monte, vendeur):
    vendeur.answers.append((200, None))
    assert _appel(monte["essai_act"], op="add_people", list_id=4, people=[{"email": "a@example.test"}]) == {}
    envoi = essai.envoyee(vendeur.requests[0])
    assert (envoi["method"], envoi["path"], envoi["body"]) == (
        "POST", "/lists/4/people", {"people": [{"email": "a@example.test"}]})


def test_a_list_is_shaped_and_pages_followed_on_all_pages(monte, vendeur):
    pages = {None: {"data": [{"id": 1, "secret": "s", "extra": 0}], "next": "c1"},
             "c1": {"data": [{"id": 2, "owner": {"photo": "p"}}], "next": "c2"},
             "c2": {"data": [{"id": 3}], "next": None}}

    def page(request):
        return 200, pages[dict(essai.envoyee(request)["query"]).get("cursor")]

    vendeur.answers.extend([page, page, page])
    one = _appel(monte["essai"], op="list_things", limit=10)
    assert one == {"items": [{"id": 1}], "next_cursor": "c1", "pages": 1}
    every = _appel(monte["essai"], op="list_things", limit=10, all_pages=True, max_pages=2)
    assert every == {"items": [{"id": 1}, {"id": 2, "owner": {}}], "next_cursor": "c2", "pages": 2}
    assert [essai.envoyee(r)["query"] for r in vendeur.requests[1:]] == [[("limit", "10")],
                                                                        [("limit", "10"), ("cursor", "c1")]]


@pytest.mark.parametrize("arguments, attendu", [
    ({"op": "get_thing"}, "'thing_id' is a required property"),
    ({"op": "get_thing", "thing_id": 0}, "0 is less than the minimum of 1 (at thing_id)"),
    ({"op": "get_thing", "thing_id": 1, "limit": 3}, "Additional properties are not allowed"),
    ({"op": "update_thing", "thing_id": 1}, "`op` must be one of get_account, list_things"),
    ({"thing_id": 1}, "`op` must be one of"),
])
def test_a_malformed_call_is_refused_with_the_op_s_detail(monte, vendeur, arguments, attendu):
    with pytest.raises(McpError) as refus:
        asyncio.run(monte["essai"].run(arguments))
    message = refus.value.error.message
    assert attendu in message and "Nothing was done." in message
    if "op" in arguments and arguments["op"] == "get_thing":
        assert "Get thing. More detail here." in message and "Input schema:" in message
        assert 'Example: {"op": "get_thing", "thing_id": 1}' in message
    assert vendeur.requests == []


def test_an_argument_the_engine_refuses_hides_the_personal_values(monte, monkeypatch):
    def refuse(description, function, arguments, **_):
        raise engine.InvalidArguments(f"{function}: `name` {arguments['name']!r} is refused.")
    monkeypatch.setattr(decrits.engine, "call", refuse)
    with pytest.raises(McpError) as refus:
        _appel(monte["essai_act"], op="update_thing", thing_id=1, name="bad")
    assert refus.value.error.message == "update_thing: `name` '[personal]' is refused. Nothing was done."


# --- the named refusal --------------------------------------------------------------------------------------------


def _refus(outil, vendeur, *reponses, **arguments):
    vendeur.answers.extend(reponses)
    try:
        _appel(outil, **arguments)
    except Exception as e:  # noqa: BLE001 — classified below
        return e
    pytest.fail("the call should have been refused")


def test_the_function_s_refusal_comes_before_the_table(monte, vendeur):
    e = _refus(monte["essai"], vendeur, (404, {"detail": "nope"}), op="get_thing", thing_id=9)
    assert isinstance(e, decrits.RefusNomme) and (e.code, e.status_code) == ("thing_not_found", 404)
    info = error_taxonomy.classify(e)
    assert (info.code, info.message) == ("not_found", "No thing with this id. (thing_not_found)")


def test_a_refusal_that_marks_the_key_is_read_by_the_taxonomy(monte, vendeur):
    e = _refus(monte["essai"], vendeur, (401, "Unauthorized"), op="get_thing", thing_id=9)
    assert e.marks_account == "credential_rejected" and error_taxonomy.credential_rejected_in_chain(e)
    assert error_taxonomy.classify(e).code == "not_authorized"


def test_a_condition_on_the_body_and_the_retry(monte, vendeur):
    e = _refus(monte["essai_act"], vendeur, (422, {"error": "Record LOCKED"}), op="update_thing", thing_id=1)
    assert (e.code, e.retryable, e.credential_rejected) == ("locked", False, False)
    e = _refus(monte["essai"], vendeur, (429, {}), (429, {}), (429, {}), op="get_account")
    assert error_taxonomy.classify(e).code == "rate_limited" and len(vendeur.requests) == 4


def test_a_refusal_under_a_success_status_is_served(vendeur, monkeypatch):
    """The engine reads a refusal under a 2xx (the client of old returned its JSON only): no longer refused at mount."""
    monkeypatch.setattr(decrits.access, "resolve_api_key", lambda name, *a, **k: ("k", False))
    d = essai.description()
    d["connector"]["errors"].insert(0, {"status": 200, "code": "soft_error", "message": "Refused under a 200.",
                                        "retryable": False, "body": {"path": "error", "equals": ["nope"]}})
    assert descriptions.refus_de_montage(d) == []
    servi = decrits.Servi("essai", d)
    vendeur.answers.append((200, {"error": "nope"}))
    with pytest.raises(decrits.RefusNomme, match=r"Refused under a 200. \(soft_error\)"):
        servi.appeler(servi.fonctions["get_account"], {})


def test_an_undeclared_status_passes_as_the_engine_raised_it(monte, vendeur):
    e = _refus(monte["essai"], vendeur, (418, {"x": "key-of-essai"}), op="get_account")
    assert isinstance(e, UpstreamHTTPError) and e.status_code == 418
    assert "key-of-essai" not in str(e)   # the engine masks the call's secrets in the body


# --- the probe ----------------------------------------------------------------------------------------------------


def test_the_probe_is_registered_with_its_balance(monte, vendeur):
    assert connector_verify.couverture("essai") == connector_verify.AUTH_QUOTA
    sonde = connector_verify.probe_for("essai")
    vendeur.answers.append((200, {"id": "a", "credits": 7}))
    assert sonde({"key": "k"}, {}) == {"quota": {"restant": 7, "unite": "credits", "seuil": 1}}
    assert essai.envoyee(vendeur.requests[-1])["headers"]["x-key"] == "k"
    vendeur.answers.append((200, {"id": "a", "credits": 0}))
    with pytest.raises(connector_verify.QuotaEpuise, match="under 1 credits"):
        sonde({"key": "k"}, {})
    vendeur.answers.append((200, {"id": "", "credits": 3}))
    with pytest.raises(RuntimeError, match="Essai answered without id."):
        sonde({"key": "k"}, {})
    vendeur.answers.append((401, {}))
    with pytest.raises(connector_verify.NonAutorise, match="key_refused"):
        sonde({"key": "k"}, {})
    vendeur.answers.append((402, {}))
    with pytest.raises(connector_verify.QuotaEpuise):
        sonde({"key": "k"}, {})
    vendeur.answers.append((404, {}))
    with pytest.raises(engine.Refused, match="not_found"):
        sonde({"key": "k"}, {})


# --- effects and batches ------------------------------------------------------------------------------------------


def test_effects_are_derived_from_class_act_and_effect():
    assert descriptions.effets(essai.description()) == {
        "essai": {"lire": "get_account list_things get_thing", "ecrire": "save_draft"},
        "essai_act": {"envoyer": "update_thing add_people tag_things", "detruire": "delete_thing"},
    }


def test_batches_are_every_list_of_an_op_that_writes_or_sends():
    """`update_thing` takes no list: it carries the tool's first batch, as `LOTS` asks of every op that writes on a
    tool with a list argument; `save_draft` is on a tool without one."""
    assert descriptions.lots(essai.description(), effets.LOT_MAX) == {
        "essai_act:update_thing": ("people", 20, ()),
        "essai_act:add_people": ("people", 20, ()),
        "essai_act:tag_things": ("ids", 50, ("tags",)),
    }
    assert descriptions.pas_un_lot(essai.description(), effets.LOT_MAX) == {}


def test_a_list_of_a_destruction_is_not_a_batch():
    d = essai.description()
    delete = next(f for f in d["functions"] if f["name"] == "delete_thing")
    delete["input"]["properties"]["also"] = {"type": "array", "items": {"type": "integer"}}
    assert descriptions.pas_un_lot(d, effets.LOT_MAX) == {"essai_act": "also"}


def test_a_sensitive_function_without_effect_is_refused_at_mount():
    d = essai.description()
    next(f for f in d["functions"] if f["name"] == "delete_thing").pop("effect")
    with pytest.raises(descriptions.DescriptionNonServie, match="delete_thing: sensitive without `effect`"):
        decrits.monter(FastMCP("x"), "essai", d)


@pytest.mark.parametrize("mutate, raison", [
    (lambda d: d["functions"][0].update({"async": {"collect": "get_thing"}}), "function get_account: async"),
    (lambda d: d["functions"][0].update({"checks": []}), "function get_account: checks"),
    (lambda d: d["functions"][0].update({"background": True}),
     "essai.get_account: a background function is not run by the Python engine"),
    (lambda d: d["functions"][0].update({"output": {"file": {"from": "body"}}}), "essai.get_account: a rendered file"),
    (lambda d: d["functions"][0]["call"].update({"files": {}}), r"essai.get_account: a multipart body \(call.files\)"),
    (lambda d: d["functions"][0].pop("call"), "essai.get_account: a function without call"),
    (lambda d: d["connector"].update(settings=[{"name": "region", "type": "choice", "choices": ["eu"]}]),
     "connector.settings"),
    (lambda d: d["connector"]["modes"].append("platform"), "mode platform"),
    (lambda d: d["connector"]["credential"].append({"name": "secret", "label": "S", "secret": True}),
     "the credential is not one secret of the account"),
    (lambda d: d["connector"].update(auth={"kind": "oauth2_user"}), "the credential is not one secret"),
    (lambda d: d["connector"].update(auth={"kind": "basic", "username": "api_key", "password": "api_key"}),
     "the credential is not one secret"),
])
def test_what_the_reader_cannot_serve_is_refused_at_mount(mutate, raison):
    d = essai.description()
    mutate(d)
    with pytest.raises(descriptions.DescriptionNonServie, match=raison):
        decrits.monter(FastMCP("x"), "essai", d)


def test_a_tool_declared_by_hand_and_by_its_description_raises():
    with pytest.raises(ValueError, match=r"\['amplemarket_act'\] declared twice"):
        effets._fusionner(dict(effets.EFFETS), {"amplemarket_act": {"lire": "x"}}, "EFFETS")


# --- recipes and the call log --------------------------------------------------------------------------------------


@pytest.fixture
def inscrit(monkeypatch):
    """`essai` inscribed as the registry would (`_decrit`), the caches of the index reset around."""
    entree = type("Entree", (), {"name": "essai", "decrit": True})()
    monkeypatch.setattr(descriptions, "inscrits", lambda: (entree,))
    monkeypatch.setattr(descriptions, "charger", lambda nom: essai.description())
    descriptions.classes_des_outils.cache_clear()
    descriptions.personnels_des_outils.cache_clear()
    monkeypatch.setattr(journal_secrets, "_ARG_NAMES", None)
    yield
    descriptions.classes_des_outils.cache_clear()
    descriptions.personnels_des_outils.cache_clear()


def test_a_recipe_classes_a_served_tool_by_its_description(inscrit):
    assert effets_recette.classer("essai_act", "add_people") == "envoyer"
    assert effets_recette.classer("essai_act", "delete_thing") == "detruire"
    assert effets_recette.classer("essai_act", "tag_things") == "envoyer"
    assert effets_recette.classer("essai", "save_draft") == "ecrire"
    assert effets_recette.classer("essai", "list_things") == "lire"
    with pytest.raises(ValueError, match="serves no op 'send_all'"):
        effets_recette.classer("essai_act", "send_all")
    assert effets_recette.classer("lemlist_act", "send_linkedin") == "envoyer"   # the verb, elsewhere


def test_the_call_log_masks_the_personal_arguments(inscrit):
    assert journal_secrets.secret_arg_names("essai_act") == {"name", "email"}
    ligne = calllog.truncated_args({"op": "add_people", "list_id": 1, "people": [{"email": "a@example.test"}]},
                                   tool="essai_act")
    assert "a@example.test" not in str(ligne) and ligne["list_id"] == 1


# --- the inscription ----------------------------------------------------------------------------------------------


def test_decrit_reads_a_key_in_the_description(monkeypatch):
    from oto_mcp.providers._model import _decrit
    monkeypatch.setattr(descriptions, "charger", lambda nom: essai.description())
    c = _decrit("essai", help="h", href="https://example.test", key_help="where to find it")
    assert (c.label, c.namespaces, c.auth_modes, c.keyed, c.secret_kind, c.decrit) == (
        "Essai", ("essai",), frozenset({"byo_user", "byo_org"}), True, "api_key", True)
    assert [(f.name, f.label, f.secret, f.help, f.whitespace_significant) for f in c.secret_fields] == [
        ("key", "Essai API key", True, "where to find it", False)]


def test_decrit_reads_a_service_account_key_in_the_description(monkeypatch):
    """The JSON key file of a service account, kept as `google_analytics` keeps its own: one field of
    `secret_kind="fields"`, under the description's name, its whitespace kept (the PEM private key holds spaces)."""
    from oto_mcp.providers._model import _decrit
    d = essai.description()
    d["connector"]["auth"] = {"kind": "oauth2_jwt_bearer", "token_url": "https://oauth2.example.test/token",
                              "scopes": ["read"], "key": "service_account_key"}
    d["connector"]["credential"] = [{"name": "service_account_key", "label": "Service account key", "secret": True}]
    monkeypatch.setattr(descriptions, "charger", lambda nom: d)
    c = _decrit("essai", help="h", href="https://example.test", key_help="the JSON file")
    assert (c.keyed, c.secret_kind, c.decrit) == (False, "fields", True)
    assert [(f.name, f.label, f.secret, f.help, f.whitespace_significant) for f in c.secret_fields] == [
        ("service_account_key", "Service account key", True, "the JSON file", True)]


def test_nothing_is_inscribed_but_what_decrit_builds():
    """Every inscribed entry is described; a hand-written connector is not, and keeps its tool file."""
    for c in providers.REGISTRY.values():
        assert (c in descriptions.inscrits()) == c.decrit, c.name
    inscrits = {c.name for c in descriptions.inscrits()}
    assert "google_search_console" in inscrits and "amplemarket" not in inscrits


def test_the_real_amplemarket_description_is_judged():
    """Amplemarket, described and served by hand today: the reader would still refuse it, saying why — three
    functions submit an asynchronous job, and its sensitive functions do not say their effect."""
    raisons = descriptions.refus_de_montage(descriptions.charger("amplemarket"))
    assert "function start_person_enrichment: async" in raisons
    assert all(r.startswith("function ") for r in raisons), raisons


# --- Google Search Console: a service account, served from its description alone --------------------------------


@pytest.fixture(scope="module")
def cle_de_compte_de_service():
    """A service account's JSON key file around a throwaway RSA key, generated for this test: never a real key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    privee = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = privee.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode("ascii")
    return json.dumps({"type": "service_account", "client_email": "reader@project-not-real.example.test",
                       "token_uri": "https://oauth2.googleapis.com/token", "private_key": pem})


@pytest.fixture
def search_console(vendeur, monkeypatch, cle_de_compte_de_service):
    """The real description mounted as the registry inscribes it, the caller's key resolved from the vault's field, a
    token cache of its own."""
    monkeypatch.setattr(engine.tokens, "TOKENS", engine.tokens.TokenCache())
    resolus = []

    def resoudre(nom, *a, **k):
        resolus.append(nom)
        return {"service_account_key": cle_de_compte_de_service}
    monkeypatch.setattr(decrits.access, "resolve_credential_fields", resoudre)
    outils = {t.name: t for t in decrits.monter(FastMCP("gsc"), "google_search_console",
                                                 descriptions.charger("google_search_console"))}
    yield outils, resolus
    connector_verify._REGISTRY.pop("google_search_console", None)
    connector_verify._COUVERTURE.pop("google_search_console", None)


def test_search_console_is_inscribed_with_its_service_account_key():
    c = providers.REGISTRY["google_search_console"]
    assert (c.namespaces, c.auth_modes, c.secret_kind, c.keyed, c.decrit) == (
        ("google_search_console",), frozenset({"byo_org"}), "fields", False, True)
    assert [(f.name, f.secret, f.whitespace_significant, f.help) for f in c.secret_fields] == [
        ("service_account_key", True, True,
         "JSON key of the service account; add its email as a user of the Search Console property")]
    assert descriptions.refus_de_montage(descriptions.charger("google_search_console")) == []


def test_search_console_serves_one_reading_tool(search_console):
    outils, _ = search_console
    assert set(outils) == {"google_search_console"}
    assert outils["google_search_console"].parameters["properties"]["op"]["enum"] == [
        "list_sites", "query_search_analytics", "inspect_url", "list_sitemaps", "get_sitemap"]
    assert descriptions.effets(descriptions.charger("google_search_console")) == {}


def test_search_console_exchanges_the_key_then_calls_with_the_token(search_console, vendeur):
    outils, resolus = search_console
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real", "token_type": "Bearer", "expires_in": 3599}),
                            (200, {"rows": [{"keys": ["oto"], "clicks": 3}]})])
    rendu = _appel(outils["google_search_console"], op="query_search_analytics", site_url="sc-domain:example.com",
                   start_date="2026-10-01", end_date="2026-10-31", dimensions=["query"], row_limit=100)
    assert rendu == {"rows": [{"keys": ["oto"], "clicks": 3}]} and resolus == ["google_search_console"]
    jeton, appel = (essai.envoyee(r) for r in vendeur.requests)
    assert (jeton["method"], jeton["url"]) == ("POST", "https://oauth2.googleapis.com/token")
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in jeton["body"]
    assert appel["headers"]["authorization"] == "Bearer ya29.not-real"
    assert vendeur.requests[1].url == ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                                       "sc-domain%3Aexample.com/searchAnalytics/query")
    assert appel["body"] == {"startDate": "2026-10-01", "endDate": "2026-10-31", "dimensions": ["query"],
                             "rowLimit": 100}


def test_search_console_names_a_property_not_shared(search_console, vendeur):
    outils, _ = search_console
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real", "expires_in": 3599}),
                            (403, {"error": {"code": 403, "message": "User does not have sufficient permission for "
                                                                    "site 'sc-domain:example.com'."}})])
    with pytest.raises(decrits.RefusNomme) as refus:
        _appel(outils["google_search_console"], op="list_sitemaps", site_url="sc-domain:example.com")
    assert (refus.value.code, refus.value.status_code) == ("property_not_shared", 403)


def test_search_console_probe_reads_the_vault_field(search_console, vendeur, cle_de_compte_de_service):
    sonde = connector_verify.probe_for("google_search_console")
    assert connector_verify.couverture("google_search_console") == connector_verify.AUTH
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real", "expires_in": 3599}),
                            (200, {"siteEntry": [{"siteUrl": "sc-domain:example.com"}]})])
    assert sonde({"service_account_key": cle_de_compte_de_service}, {}) is None
    vendeur.answers.append((200, {}))
    with pytest.raises(RuntimeError, match="answered without siteEntry"):
        sonde({"service_account_key": cle_de_compte_de_service}, {})


def test_search_console_probe_says_a_revoked_key(search_console, vendeur, cle_de_compte_de_service):
    vendeur.answers.append((400, {"error": "invalid_grant"}))
    with pytest.raises(connector_verify.NonAutorise, match="invalid_grant"):
        connector_verify.probe_for("google_search_console")({"service_account_key": cle_de_compte_de_service}, {})


def test_search_console_refuses_a_key_that_is_not_json(search_console, vendeur):
    with pytest.raises(engine.Refused, match="service_account_key_not_json"):
        connector_verify.probe_for("google_search_console")({"service_account_key": "not json"}, {})
    assert vendeur.requests == []
