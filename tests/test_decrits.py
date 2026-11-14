"""The generic reader of a description (`tools/decrits.py`, `connectors/descriptions.py`), decided 05/11/2026, which
runs each call by the library's engine (`oto.connectors.engine`, 09/11/2026).

On a described connector of test (`_decrit_d_essai`), against a fake vendor (the engine's own `requests` session,
mounted on an adapter, no network): the pair and its schemas, the call with the caller's key, the named refusal,
`strip` and `projection`, the pages, the probe, the effects and batches derived (`effect: send` and `destroy`
included), recipes and the call log, the inscription that changes nothing else; then the real Search Console
description, its service account key and its probe; then the real SE Ranking description, a key and its act side.
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
from oto_mcp.tools.lecture import ACTE, LECTURE, SIMULATION
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
    monkeypatch.setattr(decrits.engine, "execute", refuse)
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
    assert {"google_search_console", "seranking"} <= inscrits and "amplemarket" not in inscrits


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


def test_search_console_serves_its_reads_and_an_act_side_that_submits(search_console):
    outils, _ = search_console
    assert set(outils) == {"google_search_console", "google_search_console_act"}
    assert outils["google_search_console"].parameters["properties"]["op"]["enum"] == [
        "list_sites", "query_search_analytics", "inspect_url", "list_sitemaps", "get_sitemap"]
    assert outils["google_search_console_act"].parameters["properties"]["op"]["enum"] == ["add_sitemap"]
    assert outils["google_search_console_act"].annotations == ACTE
    assert descriptions.effets(descriptions.charger("google_search_console")) == {
        "google_search_console_act": {"envoyer": "add_sitemap"}}


def _scope_de_l_assertion(requete) -> str:
    """The `scope` claim of the service account's assertion, read in the token request's form."""
    import base64
    from urllib.parse import parse_qs
    jwt = parse_qs(essai.envoyee(requete)["body"])["assertion"][0]
    claims = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))["scope"]


def test_search_console_reads_at_the_default_level_with_the_read_scope(search_console, vendeur):
    """No level set: the description's `default_level`, `read` — the key asks for webmasters.readonly, as before the
    levels existed."""
    outils, _ = search_console
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real", "expires_in": 3599}),
                            (200, {"siteEntry": []})])
    _appel(outils["google_search_console"], op="list_sites")
    assert _scope_de_l_assertion(vendeur.requests[0]) == "https://www.googleapis.com/auth/webmasters.readonly"


def test_search_console_refuses_a_submit_at_read_before_sending_anything(search_console, vendeur):
    outils, _ = search_console
    with pytest.raises(decrits.RefusNomme) as refus:
        _appel(outils["google_search_console_act"], op="add_sitemap", site_url="sc-domain:example.com",
               feedpath="https://www.example.com/sitemap.xml")
    assert (refus.value.code, refus.value.status_code) == ("access_level_denied", None)
    message = str(refus.value)
    assert "'read'" in message and "oto_connector_access_level(op='set', scope='org'" in message
    assert "level='full'" in message and "Nothing was sent." in message
    assert vendeur.requests == []
    info = error_taxonomy.classify(refus.value)
    assert (info.code, info.retryable) == ("access_level_denied", False)


@pytest.fixture
def niveau(monkeypatch):
    """The level the org set, as the guard resolves it at the call."""
    def poser(valeur):
        monkeypatch.setattr(decrits.access_level_gate, "level_of_the_call", lambda nom: valeur)
    return poser


def test_search_console_submits_a_sitemap_at_full(search_console, vendeur, niveau):
    outils, _ = search_console
    niveau("full")
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real-full", "expires_in": 3599}), (200, {})])
    rendu = _appel(outils["google_search_console_act"], op="add_sitemap", site_url="sc-domain:example.com",
                   feedpath="https://www.example.com/sitemap.xml")
    assert rendu == {}
    jeton, appel = vendeur.requests
    assert _scope_de_l_assertion(jeton) == "https://www.googleapis.com/auth/webmasters"
    assert appel.method == "PUT" and appel.url == (
        "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps/"
        "https%3A%2F%2Fwww.example.com%2Fsitemap.xml")


def test_search_console_names_a_property_shared_restricted(search_console, vendeur, niveau):
    """`full` bounds, it does not grant: a property that shares the key as Restricted refuses the submit, under the
    description's code and message (the gesture: Owner or Full), and the key is not painted red."""
    outils, _ = search_console
    niveau("full")
    vendeur.answers.extend([(200, {"access_token": "ya29.not-real-full", "expires_in": 3599}),
                            (403, {"error": {"code": 403, "message": "User does not have sufficient permission for "
                                                                    "site 'sc-domain:example.com'.",
                                             "errors": [{"reason": "insufficientPermissions"}]}})])
    with pytest.raises(decrits.RefusNomme) as refus:
        _appel(outils["google_search_console_act"], op="add_sitemap", site_url="sc-domain:example.com",
               feedpath="https://www.example.com/sitemap.xml")
    assert (refus.value.code, refus.value.status_code) == ("owner_or_full_required", 403)
    info = error_taxonomy.classify(refus.value)
    assert (info.code, info.hint) == ("owner_or_full_required", None)
    assert "Owner or Full" in info.message
    assert not error_taxonomy.credential_rejected_in_chain(refus.value)


def test_search_console_refuses_a_level_its_description_does_not_declare(search_console, vendeur, niveau):
    outils, _ = search_console
    niveau("admin")
    with pytest.raises(decrits.RefusNomme) as refus:
        _appel(outils["google_search_console"], op="list_sites")
    assert refus.value.code == "unknown_access_level"
    assert "oto_connector_access_level(op='set'" in str(refus.value) and "Nothing was sent." in str(refus.value)
    assert vendeur.requests == []


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


# --- SE Ranking: one key, a read side and an act side that spends credits ------------------------------------------


_SERANKING_LIT = ["get_subscription", "list_sites", "list_site_search_engines", "list_site_keywords",
                  "get_site_summary", "get_site_positions", "list_competitors", "get_competitor_positions",
                  "list_audits", "get_audit_status", "get_audit_report"]
_SERANKING_AGIT = ["domain_overview", "domain_overview_history", "domain_keywords", "domain_competitors",
                   "backlinks_summary", "list_backlinks", "list_referring_domains", "keyword_metrics",
                   "similar_keywords", "related_keywords", "question_keywords", "recheck_positions", "create_audit"]


@pytest.fixture
def seranking(vendeur, monkeypatch):
    """The real description mounted as the registry inscribes it, the caller's key resolved by `resolve_api_key`."""
    resolus = []

    def resoudre(nom, *a, **k):
        resolus.append(nom)
        return "key-not-real", False
    monkeypatch.setattr(decrits.access, "resolve_api_key", resoudre)
    outils = {t.name: t for t in decrits.monter(FastMCP("seranking"), "seranking", descriptions.charger("seranking"))}
    yield outils, resolus
    connector_verify._REGISTRY.pop("seranking", None)
    connector_verify._COUVERTURE.pop("seranking", None)


def test_seranking_is_inscribed_with_one_key():
    c = providers.REGISTRY["seranking"]
    assert (c.namespaces, c.auth_modes, c.secret_kind, c.keyed, c.decrit) == (
        ("seranking",), frozenset({"byo_user", "byo_org"}), "api_key", True, True)
    assert [(f.name, f.secret, f.whitespace_significant) for f in c.secret_fields] == [("key", True, False)]
    assert descriptions.refus_de_montage(descriptions.charger("seranking")) == []


def test_seranking_serves_a_reading_tool_and_an_acting_one(seranking):
    outils, _ = seranking
    assert set(outils) == {"seranking", "seranking_act"}
    assert outils["seranking"].parameters["properties"]["op"]["enum"] == _SERANKING_LIT
    assert outils["seranking_act"].parameters["properties"]["op"]["enum"] == _SERANKING_AGIT
    assert (outils["seranking"].annotations, outils["seranking_act"].annotations) == (LECTURE, ACTE)
    assert descriptions.effets(descriptions.charger("seranking")) == {
        "seranking_act": {"envoyer": " ".join(_SERANKING_AGIT)}}
    assert effets.EFFETS["seranking_act"] == {"envoyer": " ".join(_SERANKING_AGIT)}


def test_seranking_a_recipe_classes_its_ops_by_the_description():
    assert effets_recette.classer("seranking_act", "domain_overview") == "envoyer"
    assert effets_recette.classer("seranking_act", "create_audit") == "envoyer"
    assert effets_recette.classer("seranking", "list_sites") == "lire"


def test_seranking_every_op_that_spends_credits_is_on_the_act_side():
    """The cost the library declares (`oto.connectors.declared`): every function with a cost acts, so a call that
    spends the account's Data API credits is never on the reading tool. The credits it spends are the customer's
    own key's (`byo_user`, `byo_org`, no platform key): `access/credits.py` reserves none of them."""
    from oto.connectors.declared import DECLARED
    couts = {n: d["cost"] for n, d in DECLARED["seranking"].items() if "cost" in d}
    assert set(couts) == set(_SERANKING_AGIT) - {"recheck_positions", "create_audit"}
    assert couts["domain_overview"] == {"per": "request", "quantity": 100, "unit": "credits"}
    assert couts["keyword_metrics"] == {"per": "item", "items": "keywords", "unit": "keywords"}
    assert "platform" not in providers.REGISTRY["seranking"].auth_modes


def test_seranking_calls_with_the_token_header(seranking, vendeur):
    outils, resolus = seranking
    vendeur.answers.append((200, {"organic": {"keywords_count": 12}}))
    rendu = _appel(outils["seranking_act"], op="domain_overview", source="fr", domain="example.com")
    assert rendu == {"organic": {"keywords_count": 12}} and resolus == ["seranking"]
    envoi = essai.envoyee(vendeur.requests[0])
    assert (envoi["method"], envoi["url"]) == ("GET", "https://api.seranking.com/v1/domain/overview/db")
    assert envoi["query"] == [("source", "fr"), ("domain", "example.com")]
    assert envoi["headers"]["authorization"] == "Token key-not-real"


def test_seranking_strips_the_share_link_of_a_project(seranking, vendeur):
    outils, _ = seranking
    vendeur.answers.append((200, [{"id": 1, "name": "example.com", "guest_link": "https://share.example.test/x"}]))
    rendu = _appel(outils["seranking"], op="list_sites")
    assert "share.example.test" not in json.dumps(rendu) and "example.com" in json.dumps(rendu)


def test_seranking_names_credits_exhausted_and_a_refused_key(seranking, vendeur):
    outils, _ = seranking
    e = _refus(outils["seranking_act"], vendeur, (400, {"message": "Insufficient funds"}),
               op="domain_overview", source="fr", domain="example.com")
    assert (e.code, e.status_code, e.retryable, e.marks_account) == ("credits_exhausted", 400, False, "quota_exhausted")
    e = _refus(outils["seranking"], vendeur, (403, {"message": "Incorrect token"}), op="list_sites")
    assert e.code == "key_refused" and error_taxonomy.credential_rejected_in_chain(e)


def test_seranking_probe_reads_the_credits_left(seranking, vendeur):
    sonde = connector_verify.probe_for("seranking")
    assert connector_verify.couverture("seranking") == connector_verify.AUTH_QUOTA
    vendeur.answers.append((200, {"subscription_info": {"status": "active", "units_left": 4200}}))
    assert sonde({"key": "k"}, {}) == {"quota": {"restant": 4200, "unite": "credits", "seuil": 1}}
    assert essai.envoyee(vendeur.requests[-1])["headers"]["authorization"] == "Token k"
    vendeur.answers.append((200, {"subscription_info": {"status": "active", "units_left": 0}}))
    with pytest.raises(connector_verify.QuotaEpuise):
        sonde({"key": "k"}, {})
    vendeur.answers.append((403, {"message": "Incorrect token"}))
    with pytest.raises(connector_verify.NonAutorise):
        sonde({"key": "k"}, {})


def test_seranking_lists_come_back_reduced_to_what_an_agent_reads(seranking, vendeur):
    """seranking 1.1.0: a list of backlinks keeps its declared keys only; a keyword search keeps its `total` and
    loses each keyword's history trend."""
    outils, _ = seranking
    vendeur.answers.append((200, {"backlinks": [{"url_from": "https://a.example.test/", "url_to": "https://example.com/",
                                                 "anchor": "oto", "inlink_rank": 3, "ip_from": "192.0.2.1"}]}))
    rendu = _appel(outils["seranking_act"], op="list_backlinks", target="example.com", mode="domain", limit=10)
    assert "ip_from" not in json.dumps(rendu) and "url_from" in json.dumps(rendu)
    vendeur.answers.append((200, {"total": 42, "keywords": [{"keyword": "crm", "volume": 900,
                                                             "history_trend": {"2026-10": 880}}]}))
    rendu = _appel(outils["seranking_act"], op="similar_keywords", source="fr", keyword="crm", limit=10)
    assert rendu["total"] == 42 and "history_trend" not in json.dumps(rendu) and "volume" in json.dumps(rendu)


# --- a login and a password, and the platform key (11/11/2026) ------------------------------------------------------


def _basique() -> dict:
    """`essai` with a login and a password (`basic`), and a platform key."""
    d = essai.description()
    d["connector"]["auth"] = {"kind": "basic", "username": "login", "password": "password"}
    d["connector"]["credential"] = [{"name": "login", "label": "Login", "secret": False},
                                    {"name": "password", "label": "Password", "secret": True}]
    d["connector"]["modes"] = ["byo_user", "byo_org", "platform"]
    d["functions"][1]["cost"] = {"per": "request", "quantity": 5000, "unit": "microdollars",
                                 "settle": {"path": "cost", "scale": 1000000}}
    return d


class _Resolu:
    """What `access.resolve_credential` returns, reduced to what the reader reads."""

    def __init__(self, fields: dict, is_platform: bool):
        self.fields, self.is_platform = fields, is_platform


@pytest.fixture
def basique(vendeur, monkeypatch):
    """`essai` in `basic`, mounted; `resolve_credential` answers `etat["rc"]` and notes what it was asked."""
    etat = {"rc": _Resolu({"login": "login-0", "password": "pass-0"}, False), "demandes": []}

    def resoudre(nom, want="auto", *a, **k):
        etat["demandes"].append((nom, want))
        return etat["rc"]
    monkeypatch.setattr(decrits.access, "resolve_credential", resoudre)
    monkeypatch.setattr("oto.connectors.cost._declared",
                        lambda c, f: {"cost": _basique()["functions"][1]["cost"]} if f == "list_things" else {})
    outils = {t.name: t for t in decrits.monter(FastMCP("essai"), "essai", _basique())}
    yield outils, etat
    connector_verify._REGISTRY.pop("essai", None)
    connector_verify._COUVERTURE.pop("essai", None)


def _avec_releve(fn):
    from oto_mcp import session_org
    releve: dict = {}
    jeton = session_org.set_call_trace(releve)
    try:
        return fn(), releve
    finally:
        session_org.reset_call_trace(jeton)


def test_a_login_and_a_password_are_sent_as_basic_auth(basique, vendeur):
    import base64
    outils, etat = basique
    vendeur.answers.append((200, {"id": 7, "credits": 3}))
    assert _appel(outils["essai"], op="get_account") == {"id": 7, "credits": 3}
    assert etat["demandes"] == [("essai", "auto")]
    attendu = "Basic " + base64.b64encode(b"login-0:pass-0").decode()
    assert essai.envoyee(vendeur.requests[0])["headers"]["authorization"] == attendu


def test_a_credential_without_its_password_is_refused_before_sending(basique, vendeur):
    outils, etat = basique
    etat["rc"] = _Resolu({"login": "login-0"}, False)
    with pytest.raises(McpError, match="lacks password"):
        _appel(outils["essai"], op="get_account")
    assert vendeur.requests == []


def test_each_call_notes_the_quantity_its_cost_settles(basique, vendeur):
    """`settle.scale`: 0.0126 USD read in the answer is 12600 microdollars; a function without cost notes 0, never
    NULL; a refused call notes nothing."""
    outils, _ = basique
    vendeur.answers.append((200, {"data": [{"id": 1}], "cost": 0.0126}))
    _, releve = _avec_releve(lambda: _appel(outils["essai"], op="list_things", limit=10))
    assert releve["quantity"] == 12600
    vendeur.answers.append((200, {"id": 7, "credits": 3}))
    _, releve = _avec_releve(lambda: _appel(outils["essai"], op="get_account"))
    assert releve["quantity"] == 0
    from oto_mcp import session_org
    vendeur.answers.append((404, {}))
    releve = {}
    jeton = session_org.set_call_trace(releve)
    try:
        with pytest.raises(decrits.RefusNomme):
            _appel(outils["essai"], op="get_thing", thing_id=1)
    finally:
        session_org.reset_call_trace(jeton)
    assert "quantity" not in releve


def test_the_probe_function_is_refused_on_the_platform_key(basique, vendeur):
    """The probe reads the account itself: on the shared key, the platform's account — refused, nothing sent. The
    other functions are served on it."""
    outils, etat = basique
    etat["rc"] = _Resolu({"login": "platform-login", "password": "platform-pass"}, True)
    with pytest.raises(McpError, match="shared platform key"):
        _appel(outils["essai"], op="get_account")
    assert vendeur.requests == []
    vendeur.answers.append((200, {"data": [], "cost": 0}))
    assert _appel(outils["essai"], op="list_things", limit=10)["items"] == []


def test_the_probe_of_a_login_and_a_password_reads_both_fields(basique, vendeur):
    import base64
    sonde = connector_verify.probe_for("essai")
    vendeur.answers.append((200, {"id": 7, "credits": 3}))
    assert sonde({"login": "l", "password": "p"}, {}) == {"quota": {"restant": 3, "unite": "credits", "seuil": 1}}
    assert essai.envoyee(vendeur.requests[-1])["headers"]["authorization"] == (
        "Basic " + base64.b64encode(b"l:p").decode())


@pytest.mark.parametrize("champs", [
    [{"name": "login", "label": "L", "secret": True}, {"name": "password", "label": "P", "secret": True}],
    [{"name": "login", "label": "L", "secret": False}, {"name": "password", "label": "P", "secret": False}],
    [{"name": "login", "label": "L", "secret": False}, {"name": "password", "label": "P", "secret": True},
     {"name": "extra", "label": "E", "secret": True}],
    [{"name": "login", "label": "L", "secret": False},
     {"name": "password", "label": "P", "secret": True, "holder": "host"}],
])
def test_basic_is_served_only_as_a_login_and_a_password_of_the_account(champs):
    d = _basique()
    d["connector"]["credential"] = champs
    with pytest.raises(descriptions.DescriptionNonServie, match="nor its login and password"):
        decrits.monter(FastMCP("x"), "essai", d)


def test_decrit_reads_a_login_and_a_password_and_the_platform_choices(monkeypatch):
    from oto_mcp.providers._model import _decrit
    monkeypatch.setattr(descriptions, "charger", lambda nom: _basique())
    aide = {"login": "where the login is", "password": "where the password is"}
    c = _decrit("essai", help="h", href="https://example.test", key_help=aide, platform_key_open=False,
                default_quota=0)
    assert (c.keyed, c.secret_kind, c.decrit, c.platform_key_open, c.default_quota) == (
        False, "fields", True, False, 0)
    assert "platform" in c.auth_modes
    assert [(f.name, f.label, f.secret, f.help) for f in c.secret_fields] == [
        ("login", "Login", False, "where the login is"), ("password", "Password", True, "where the password is")]
    with pytest.raises(ValueError, match="`platform_key_open` and `default_quota` are to be given"):
        _decrit("essai", help="h", href="https://example.test", key_help=aide)
    with pytest.raises(ValueError, match="names the help of each credential field"):
        _decrit("essai", help="h", href="https://example.test", key_help="one help", platform_key_open=False,
                default_quota=0)
    monkeypatch.setattr(descriptions, "charger", lambda nom: essai.description())
    with pytest.raises(ValueError, match="no mode platform"):
        _decrit("essai", help="h", href="https://example.test", key_help="k", platform_key_open=True,
                default_quota=5)


# --- DataForSEO, served from its description since 11/11/2026 ------------------------------------------------------


@pytest.fixture
def dataforseo(vendeur, monkeypatch):
    """The real description mounted as the registry inscribes it; `resolve_credential` answers `etat["rc"]`."""
    etat = {"rc": _Resolu({"login": "login-not-real", "password": "pass-not-real"}, False)}
    monkeypatch.setattr(decrits.access, "resolve_credential", lambda nom, *a, **k: etat["rc"])
    outils = {t.name: t for t in decrits.monter(FastMCP("dataforseo"), "dataforseo",
                                                descriptions.charger("dataforseo"))}
    yield outils, etat
    connector_verify._REGISTRY.pop("dataforseo", None)
    connector_verify._COUVERTURE.pop("dataforseo", None)


def _tache(resultat: list, cout: float = 0.0126, code: int = 20000) -> dict:
    return {"status_code": 20000, "cost": cout, "tasks": [
        {"id": "t-1", "status_code": code, "status_message": "Ok.", "cost": cout, "path": ["v3"], "data": {},
         "time": "0.1 sec.", "result": resultat}]}


def test_dataforseo_is_inscribed_as_the_hand_written_entry_was():
    """The two fields under the same names (`login` not secret, `password` secret) and the same JSON pack: every
    credential already posted (byo_user, byo_org, the platform key) unpacks unchanged."""
    from oto_mcp import credentials_store
    c = providers.REGISTRY["dataforseo"]
    assert (c.namespaces, c.auth_modes, c.secret_kind, c.keyed, c.decrit) == (
        ("dataforseo",), frozenset({"byo_user", "byo_org", "platform"}), "fields", False, True)
    assert (c.platform_key_open, c.default_quota, c.modules) == (False, 0, ())
    assert [(f.name, f.label, f.secret) for f in c.secret_fields] == [
        ("login", "API login", False), ("password", "API password", True)]
    pose = credentials_store.pack_secret("dataforseo", {"login": "l", "password": "p"})
    assert json.loads(pose) == {"login": "l", "password": "p"}
    assert credentials_store.unpack_secret("dataforseo", pose) == {"login": "l", "password": "p"}
    assert descriptions.refus_de_montage(descriptions.charger("dataforseo")) == []


def test_dataforseo_serves_one_tool_that_simulates(dataforseo):
    outils, _ = dataforseo
    assert set(outils) == {"dataforseo"}
    ops = outils["dataforseo"].parameters["properties"]["op"]["enum"]
    assert len(ops) == 62 and {"keyword_overview", "domain_technologies", "user_data", "serp_organic_post",
                               "serp_organic_get", "goto_url_resolve"} <= set(ops)
    assert outils["dataforseo"].annotations == SIMULATION
    assert effets.EFFETS["dataforseo"]["ecrire"] == "onpage_crawl_start serp_organic_post google_reviews_post"
    assert effets_recette.classer("dataforseo", "keyword_overview") == "lire"


def test_dataforseo_sends_one_task_with_basic_auth_and_bills_its_real_cost(dataforseo, vendeur):
    import base64
    outils, _ = dataforseo
    vendeur.answers.append((200, _tache([{"items": [{"keyword": "crm"}]}])))
    rendu, releve = _avec_releve(lambda: _appel(outils["dataforseo"], op="keyword_overview", keywords=["crm"],
                                                location_code=2250, language_code="fr"))
    envoi = essai.envoyee(vendeur.requests[0])
    assert (envoi["method"], envoi["url"]) == (
        "POST", "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live")
    assert envoi["body"] == [{"keywords": ["crm"], "location_code": 2250, "language_code": "fr"}]
    assert envoi["headers"]["authorization"] == "Basic " + base64.b64encode(b"login-not-real:pass-not-real").decode()
    assert releve["quantity"] == 12600
    assert rendu["tasks"][0]["result"] == [{"items": [{"keyword": "crm"}]}] and "data" not in rendu["tasks"][0]


def test_dataforseo_names_an_error_carried_in_a_200(dataforseo, vendeur):
    outils, _ = dataforseo
    e = _refus(outils["dataforseo"], vendeur, (200, {"status_code": 40200, "status_message": "Payment required."}),
               op="domain_technologies", target="example.com")
    assert (e.code, e.retryable, e.marks_account) == ("balance_exhausted", False, "quota_exhausted")
    e = _refus(outils["dataforseo"], vendeur, (200, _tache(None, cout=0, code=40104)),
               op="domain_technologies", target="example.com")
    assert e.code == "account_not_verified"


def test_dataforseo_reads_the_balance_of_its_own_key_never_of_the_shared_one(dataforseo, vendeur):
    outils, etat = dataforseo
    vendeur.answers.append((200, _tache([{"login": "login-0", "money": {"balance": 10.5}}], cout=0)))
    assert _appel(outils["dataforseo"], op="user_data")["tasks"][0]["result"][0]["money"] == {"balance": 10.5}
    etat["rc"] = _Resolu({"login": "platform-login", "password": "platform-pass"}, True)
    with pytest.raises(McpError, match="shared platform key"):
        _appel(outils["dataforseo"], op="user_data")
    vendeur.answers.append((200, _tache([{"technologies": {}}], cout=0.012)))
    _appel(outils["dataforseo"], op="domain_technologies", target="example.com")


def test_dataforseo_probe_raises_on_an_account_run_dry(dataforseo, vendeur):
    """The probe promises the quota (`balance`, min 1 USD): an account whose balance is under it, or that DataForSEO
    refuses for its balance (40200, under HTTP 200), raises `QuotaEpuise` — never a green probe; a refused login is
    `NonAutorise`; a funded account reads its balance."""
    import base64
    sonde = connector_verify.probe_for("dataforseo")
    assert connector_verify.couverture("dataforseo") == connector_verify.AUTH_QUOTA
    champs = {"login": "l", "password": "p"}

    def compte(solde):
        return (200, _tache([{"login": "l", "money": {"balance": solde, "total": 50}}], cout=0))
    vendeur.answers.append(compte(10.5))
    assert sonde(champs, {}) == {"quota": {"restant": 10.5, "unite": "USD", "seuil": 1}}
    envoi = essai.envoyee(vendeur.requests[-1])
    assert (envoi["method"], envoi["url"]) == ("GET", "https://api.dataforseo.com/v3/appendix/user_data")
    assert envoi["headers"]["authorization"] == "Basic " + base64.b64encode(b"l:p").decode()
    vendeur.answers.append(compte(0.4))
    with pytest.raises(connector_verify.QuotaEpuise):
        sonde(champs, {})
    vendeur.answers.append((200, {"status_code": 40200, "status_message": "Payment required.", "tasks": []}))
    with pytest.raises(connector_verify.QuotaEpuise):
        sonde(champs, {})
    vendeur.answers.append((200, {"status_code": 40100, "status_message": "Not authorized.", "tasks": []}))
    with pytest.raises(connector_verify.NonAutorise):
        sonde(champs, {})


def test_dataforseo_old_tool_names_refuse_naming_the_new_call():
    from oto_mcp import outils_retires
    for r in outils_retires.RENOMMAGES_DECRITS:
        assert r.op in {f["name"] for f in descriptions.charger("dataforseo")["functions"]}, r
        message = outils_retires.retrait(r.ancien).message
        assert f'`{r.outil}(op="{r.op}"' in message and "11/11/2026" in message, r
    assert outils_retires.retrait("dataforseo") is None
