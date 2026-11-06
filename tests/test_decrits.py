"""The generic reader of a description (`tools/decrits.py`, `connectors/descriptions.py`), decided 05/11/2026.

On a described connector of test (`_decrit_d_essai`): the pair and its schemas, the call with the caller's key, the
named refusal, `strip` and `projection`, the pages, the probe, the effects and batches derived (`effect: send` and
`destroy` included), recipes and the call log, the inscription that changes nothing else; then the real Amplemarket
description with its real client.
"""
from __future__ import annotations

import asyncio

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

pytestmark = pytest.mark.exige_pin_oto_core   # the library's served descriptions and `serve`


@pytest.fixture
def client():
    essai.ClientDEssai.CALLS = []
    essai.ClientDEssai.ANSWERS = {}
    essai.ClientDEssai.RAISES = {}
    return essai.ClientDEssai


@pytest.fixture
def monte(client, monkeypatch):
    monkeypatch.setattr(decrits.access, "resolve_api_key", lambda name, *a, **k: (f"key-of-{name}", False))
    mcp = FastMCP("essai")
    outils = {t.name: t for t in decrits.monter(mcp, "essai", essai.description(), client)}
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


def test_a_call_reaches_the_method_with_the_callers_key(monte, client):
    client.ANSWERS["get_thing"] = {"id": 3, "owner": {"photo": "https://files.example.test/p.png", "name": "a"}}
    assert _appel(monte["essai"], op="get_thing", thing_id=3) == {"id": 3, "owner": {"name": "a"}}
    assert client.CALLS == [("key-of-essai", "get_thing", {"thing_id": 3})]


def test_a_list_is_shaped_and_pages_followed_on_all_pages(monte, client):
    pages = {None: {"data": [{"id": 1, "secret": "s", "extra": 0}], "next": "c1"},
             "c1": {"data": [{"id": 2, "owner": {"photo": "p"}}], "next": "c2"},
             "c2": {"data": [{"id": 3}], "next": None}}
    client.ANSWERS["list_things"] = lambda args: pages[args["cursor"]]
    one = _appel(monte["essai"], op="list_things", limit=10)
    assert one == {"items": [{"id": 1}], "next_cursor": "c1", "pages": 1}
    every = _appel(monte["essai"], op="list_things", limit=10, all_pages=True, max_pages=2)
    assert every == {"items": [{"id": 1}, {"id": 2, "owner": {}}], "next_cursor": "c2", "pages": 2}
    assert [c[2] for c in client.CALLS[1:]] == [{"limit": 10, "cursor": None}, {"limit": 10, "cursor": "c1"}]


@pytest.mark.parametrize("arguments, attendu", [
    ({"op": "get_thing"}, "'thing_id' is a required property"),
    ({"op": "get_thing", "thing_id": 0}, "0 is less than the minimum of 1 (at thing_id)"),
    ({"op": "get_thing", "thing_id": 1, "limit": 3}, "Additional properties are not allowed"),
    ({"op": "update_thing", "thing_id": 1}, "`op` must be one of get_account, list_things"),
    ({"thing_id": 1}, "`op` must be one of"),
])
def test_a_malformed_call_is_refused_with_the_op_s_detail(monte, client, arguments, attendu):
    with pytest.raises(McpError) as refus:
        asyncio.run(monte["essai"].run(arguments))
    message = refus.value.error.message
    assert attendu in message and "Nothing was done." in message
    if "op" in arguments and arguments["op"] == "get_thing":
        assert "Get thing. More detail here." in message and "Input schema:" in message
        assert 'Example: {"op": "get_thing", "thing_id": 1}' in message
    assert client.CALLS == []


def test_a_local_refusal_of_the_client_hides_the_personal_values(monte):
    with pytest.raises(McpError) as refus:
        _appel(monte["essai_act"], op="update_thing", thing_id=1, name="bad")
    assert refus.value.error.message == "`name` '[personal]' is refused. Nothing was done."


# --- the named refusal --------------------------------------------------------------------------------------------


def _refus(outil, client, nom, erreur, **arguments):
    client.RAISES[nom] = erreur
    try:
        _appel(outil, op=nom, **arguments)
    except Exception as e:  # noqa: BLE001 — classified below
        return e
    pytest.fail("the call should have been refused")


def test_the_function_s_refusal_comes_before_the_table(monte, client):
    e = _refus(monte["essai"], client, "get_thing", essai.erreur(404, {"detail": "nope"}), thing_id=9)
    assert isinstance(e, decrits.RefusNomme) and e.code == "thing_not_found"
    info = error_taxonomy.classify(e)
    assert (info.code, info.message) == ("not_found", "No thing with this id. (thing_not_found)")


def test_a_refusal_that_marks_the_key_is_read_by_the_taxonomy(monte, client):
    e = _refus(monte["essai"], client, "get_thing", essai.erreur(401, "Unauthorized"), thing_id=9)
    assert error_taxonomy.credential_rejected_in_chain(e)
    assert error_taxonomy.classify(e).code == "not_authorized"


def test_a_condition_on_the_body_and_the_retry(monte, client):
    e = _refus(monte["essai_act"], client, "update_thing", essai.erreur(422, {"error": "Record LOCKED"}), thing_id=1)
    assert (e.code, e.retryable, e.credential_rejected) == ("locked", False, False)
    e = _refus(monte["essai"], client, "get_account", essai.erreur(429, {}))
    assert error_taxonomy.classify(e).code == "rate_limited"


def test_an_undeclared_status_passes_as_the_client_raised_it(monte, client):
    erreur = essai.erreur(418, {"x": 1})
    assert _refus(monte["essai"], client, "get_account", erreur) is erreur


# --- the probe ----------------------------------------------------------------------------------------------------


def test_the_probe_is_registered_with_its_balance(monte, client):
    assert connector_verify.couverture("essai") == connector_verify.AUTH_QUOTA
    sonde = connector_verify.probe_for("essai")
    client.ANSWERS["get_account"] = {"id": "a", "credits": 7}
    assert sonde({"key": "k"}, {}) == {"quota": {"restant": 7, "unite": "credits", "seuil": 1}}
    assert client.CALLS[-1][0] == "k"
    client.ANSWERS["get_account"] = {"id": "a", "credits": 0}
    with pytest.raises(connector_verify.QuotaEpuise, match="under 1 credits"):
        sonde({"key": "k"}, {})
    client.ANSWERS["get_account"] = {"id": "", "credits": 3}
    with pytest.raises(RuntimeError, match="Essai answered without id."):
        sonde({"key": "k"}, {})
    client.RAISES["get_account"] = essai.erreur(401, {})
    with pytest.raises(connector_verify.NonAutorise, match="key_refused"):
        sonde({"key": "k"}, {})
    client.RAISES["get_account"] = essai.erreur(402, {})
    with pytest.raises(connector_verify.QuotaEpuise):
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


def test_a_sensitive_function_without_effect_is_refused_at_mount(client):
    d = essai.description()
    next(f for f in d["functions"] if f["name"] == "delete_thing").pop("effect")
    with pytest.raises(descriptions.DescriptionNonServie, match="delete_thing: sensitive without `effect`"):
        decrits.monter(FastMCP("x"), "essai", d, client)


@pytest.mark.parametrize("mutate, raison", [
    (lambda d: d["functions"][0].update({"async": {"collect": "get_thing"}}), "function get_account: async"),
    (lambda d: d["functions"][0].update({"background": True}), "function get_account: background"),
    (lambda d: d["functions"][0].update({"output": {"file": {"from": "body"}}}), "function get_account: output.file"),
    (lambda d: d["connector"]["modes"].append("platform"), "mode platform"),
    (lambda d: d["connector"]["credential"].append({"name": "secret", "label": "S", "secret": True}),
     "the credential is not one secret of the account"),
    (lambda d: d["connector"].update(auth={"kind": "oauth2_user"}), "the credential is not one secret"),
    (lambda d: d["connector"]["errors"].insert(0, {"status": 200, "code": "x", "message": "x", "retryable": False,
                                                   "body": {"path": "e", "equals": [1]}}), "under a success status"),
])
def test_what_the_reader_cannot_serve_is_refused_at_mount(client, mutate, raison):
    d = essai.description()
    mutate(d)
    with pytest.raises(descriptions.DescriptionNonServie, match=raison):
        decrits.monter(FastMCP("x"), "essai", d, client)


def test_a_client_that_does_not_follow_its_description_is_refused(client):
    class Ecart(essai.ClientDEssai):
        def get_thing(self, thing_id: int, extra: str):  # noqa: D102 — the gap under test
            return {}
    class Sourd(essai.ClientDEssai):
        def get_thing(self):  # noqa: D102 — the gap under test
            return {}
    del_ = type("SansMethode", (), {"__init__": lambda self, api_key=None: None})
    with pytest.raises(descriptions.DescriptionNonServie, match=r"get_thing: \['extra'\] required by the client"):
        decrits.verifier_client(essai.description(), Ecart)
    with pytest.raises(descriptions.DescriptionNonServie, match=r"get_thing: takes no \['thing_id'\]"):
        decrits.verifier_client(essai.description(), Sourd)
    with pytest.raises(descriptions.DescriptionNonServie, match="no method get_account"):
        decrits.verifier_client(essai.description(), del_)


def test_a_parameter_the_description_does_not_serve_is_admitted_with_a_default():
    class Garde(essai.ClientDEssai):
        def get_thing(self, thing_id: int, headers=None):  # set by the consumer's own code, never by the reader
            return self._call("get_thing", thing_id=thing_id)
    decrits.verifier_client(essai.description(), Garde)


def test_a_tool_declared_by_hand_and_by_its_description_raises():
    with pytest.raises(ValueError, match=r"\['amplemarket_act'\] declared twice"):
        effets._fusionner(dict(effets.EFFETS), {"amplemarket_act": {"lire": "x"}}, "EFFETS")


# --- recipes and the call log --------------------------------------------------------------------------------------


@pytest.fixture
def inscrit(monkeypatch):
    """`essai` inscribed as the registry would (`_decrit`), the caches of the index reset around."""
    entree = type("Entree", (), {"name": "essai", "client": "_decrit_d_essai:ClientDEssai"})()
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


def test_the_importer_names_module_and_class(inscrit):
    assert decrits.importer("_decrit_d_essai:ClientDEssai") is essai.ClientDEssai
    with pytest.raises(descriptions.DescriptionNonServie, match="expected 'module:Class'"):
        decrits.importer("_decrit_d_essai.ClientDEssai")


# --- the inscription ----------------------------------------------------------------------------------------------


def test_decrit_reads_the_entry_in_the_description(monkeypatch):
    from oto_mcp.providers._model import _decrit
    monkeypatch.setattr(descriptions, "charger", lambda nom: essai.description())
    c = _decrit("essai", client="_decrit_d_essai:ClientDEssai", help="h", href="https://example.test",
                key_help="where to find it")
    assert (c.label, c.namespaces, c.auth_modes, c.keyed, c.secret_kind, c.client) == (
        "Essai", ("essai",), frozenset({"byo_user", "byo_org"}), True, "api_key", "_decrit_d_essai:ClientDEssai")
    assert [(f.name, f.label, f.secret, f.help) for f in c.secret_fields] == [
        ("key", "Essai API key", True, "where to find it")]


def test_nothing_is_inscribed_but_what_declares_a_client():
    """Every inscribed entry carries its client; a hand-written connector carries none, and keeps its tool file."""
    for c in providers.REGISTRY.values():
        assert (c in descriptions.inscrits()) == bool(c.client), c.name
    assert "amplemarket" not in {c.name for c in descriptions.inscrits()}


# --- a real description, with its real client ----------------------------------------------------------------------


def test_the_real_amplemarket_description_and_client_are_judged():
    """Amplemarket, described and served by hand today: its client follows its description (its searches take their
    filters as `**keywords`), and the reader would still refuse it, saying why — three functions submit an asynchronous
    job, and its sensitive functions do not say their effect."""
    from oto.tools.amplemarket import AmplemarketClient
    d = descriptions.charger("amplemarket")
    decrits.verifier_client(d, AmplemarketClient)
    raisons = descriptions.refus_de_montage(d)
    assert "function start_person_enrichment: async" in raisons
    assert all(r.startswith("function ") for r in raisons), raisons
