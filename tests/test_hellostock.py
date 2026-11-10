"""Connecteur HelloStock administration — l'API admin de la marketplace.

Les outils appellent le VRAI client d'oto-core ; seul `requests.Session.request` est
remplacé par un faux serveur qui rejoue le contrat (`_hellostock_fake.py`). Ce banc
verrouille ce que le connecteur ajoute au contrat :

- le registre : jeton personnel (`byo_user` seul), hors socle, éditeur déclaré ;
- les refus 401 / 403 rendus comme DEUX consignes distinctes, chacune actionnable ;
- les listes projetées qui NOMMENT ce qu'elles retirent, curseur et total intacts ;
- l'envoi : aperçu par défaut, aucun renvoi en double sans `allow_resend`, message
  borné avant l'appel, 502 non réessayé ;
- les écritures lues AVANT (aucun `PATCH` sur un identifiant inconnu) et, pour une
  offre, relues APRÈS (ce qui est stocké, pas ce qui a été demandé) ;
- ADR 0047 : aucun défaut d'`op` n'écrit, aucune lecture n'atteint une écriture.
"""
from __future__ import annotations

import asyncio

import pytest
from _hellostock_fake import ADMIN, MEMBRE, FakeHelloStock
from mcp.types import INTERNAL_ERROR

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.mcp_errors import McpError
from oto_mcp.tool_visibility import namespace_of

LECTURES = {"hellostock_demande", "hellostock_offre", "hellostock_membre",
            "hellostock_positionnements"}
ECRITURES = {"hellostock_demande_send", "hellostock_demande_set_status",
             "hellostock_offre_update"}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name: t for t in asyncio.run(m._list_tools())}


@pytest.fixture()
def fake(monkeypatch):
    srv = FakeHelloStock()
    monkeypatch.setattr("requests.Session.request",
                        lambda session, method, url, **kw: srv(session, method, url, **kw))
    monkeypatch.setattr("oto.tools.hellostock.client.time.sleep", lambda _s: None)
    return srv


@pytest.fixture()
def jeton(monkeypatch):
    """Le jeton que résout le coffre pour l'appelant ; admin par défaut."""
    box = {"key": ADMIN}
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: (box["key"], False))
    return box


def _outil(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import hellostock, hellostock_ecritures

    m = FastMCP("t")
    hellostock.register(m)
    hellostock_ecritures.register(m)
    return asyncio.run(m.get_tool(name)).fn


# --- registre et surface ------------------------------------------------------

def test_hellostock_is_a_personal_token_connector_off_by_default():
    c = providers.REGISTRY["hellostock"]
    assert c.kind == "tools" and c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user"})
    assert c.default_active is False
    assert c.publisher_name == "HelloStock" and c.category == "Custom & industry apps"
    assert [f.name for f in c.secret_fields] == ["key"]
    assert "hs_" in c.secret_fields[0].label
    assert {s.kind for s in c.doc_sections} >= {"prerequisite", "usage", "note"}


def test_the_namespace_serves_exactly_seven_tools(all_tools):
    got = {n for n in all_tools if namespace_of(n) == "hellostock"}
    assert got == LECTURES | ECRITURES
    assert all((all_tools[n].description or "").strip() for n in got)


def test_the_verify_probe_is_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("hellostock")


def test_the_send_tool_is_a_dry_run_by_default_in_the_served_schema(all_tools):
    props = all_tools["hellostock_demande_send"].parameters["properties"]
    assert props["dry_run"]["default"] is True
    for name in ("hellostock_demande_set_status", "hellostock_offre_update"):
        assert all_tools[name].parameters["properties"]["dry_run"]["default"] is False


# --- 401 / 403 : deux refus, deux gestes -----------------------------------------

def test_a_revoked_token_says_recreate_it_and_where(fake, jeton):
    jeton["key"] = "hs_revoque"
    with pytest.raises(McpError) as e:
        _outil("hellostock_demande")()
    msg = e.value.error.message
    assert "401" in msg and "revoked" in msg and "API tokens" in msg
    assert "/account" in msg


def test_a_non_admin_token_says_the_role_is_missing_not_the_token(fake, jeton):
    jeton["key"] = MEMBRE
    with pytest.raises(McpError) as e:
        _outil("hellostock_membre")(op="get", membre_id=2)
    msg = e.value.error.message
    assert "403" in msg and "administrator" in msg
    assert "recreating a token will not change that" in msg


def test_a_refused_filter_relays_the_servers_reason(fake, jeton):
    with pytest.raises(McpError) as e:
        _outil("hellostock_demande")(status="archived")
    assert "status inconnu : archived" in e.value.error.message


def test_a_server_error_is_left_to_the_taxonomy_as_retryable(fake, jeton):
    from oto.tools.common.errors import UpstreamHTTPError
    fake.force[("GET", "/api/admin/offres")] = (503, {"error": "indisponible"})
    with pytest.raises(UpstreamHTTPError) as e:
        _outil("hellostock_offre")()
    assert e.value.status_code == 503


# --- lectures -------------------------------------------------------------------

def test_a_list_names_what_it_drops_and_keeps_cursor_and_total(fake, jeton):
    page = _outil("hellostock_demande")(limit=2)
    assert [d["id"] for d in page["items"]] == [5, 4]
    assert page["nextCursor"] == "4" and page["total"] == 5
    assert all("data" not in d and "provenance" not in d for d in page["items"])
    assert page["projection"]["omitted"] == ["data", "provenance"]
    brut = _outil("hellostock_demande")(limit=2, full=True)
    assert "data" in brut["items"][0] and "projection" not in brut


def test_the_cursor_is_passed_through(fake, jeton):
    page = _outil("hellostock_demande")(limit=2, cursor="4")
    assert [d["id"] for d in page["items"]] == [3, 2]
    assert fake.log[-1][2]["cursor"] == "4"


def test_a_member_list_drops_the_company_card_it_summarises(fake, jeton):
    item = _outil("hellostock_membre")()["items"][0]
    assert "entreprise" not in item and "phone" not in item
    assert item["company"] and item["location"] and item["services"]


def test_a_record_is_never_projected(fake, jeton):
    d = _outil("hellostock_demande")(op="get", demande_id=3)
    assert d["data"] and d["provenance"] and "envois" in d


@pytest.mark.parametrize("kwargs", [
    {"op": "get"},                                   # id manquant
    {"op": "get", "demande_id": 1, "status": "closed"},  # filtre sur une fiche
    {"op": "list", "demande_id": 1},                 # id sur une liste
])
def test_an_argument_that_does_not_apply_is_refused_not_ignored(fake, jeton, kwargs):
    with pytest.raises(McpError):
        _outil("hellostock_demande")(**kwargs)
    assert fake.log == []


def test_no_read_default_writes(fake, jeton):
    for name in LECTURES:
        _outil(name)()
    assert fake.ecritures() == []


# --- envoi d'une demande -----------------------------------------------------

def test_the_default_send_is_a_preview_that_sends_nothing(fake, jeton):
    out = _outil("hellostock_demande_send")(demande_id=2, user_ids=[3, 4])
    assert out["dry_run"] is True and fake.ecritures() == []
    assert [r["id"] for r in out["recipients"]] == [3, 4]
    assert out["demande"]["nuance"] == "304L" and "reference" not in out["demande"]
    # la demande 2 n'est pas publiée : les liens du courriel mènent à une page vide
    assert any("Demande indisponible" in w for w in out["warnings"])


def test_the_preview_names_ids_that_are_not_members(fake, jeton):
    out = _outil("hellostock_demande_send")(demande_id=1, user_ids=[3, 999])
    assert [r["id"] for r in out["recipients"]] == [3]
    assert any("999" in w for w in out["warnings"])


def test_a_real_send_posts_the_recipients_and_the_message(fake, jeton):
    out = _outil("hellostock_demande_send")(demande_id=1, user_ids=[3, 4],
                                           message="Bonjour", dry_run=False)
    assert fake.ecritures() == [("POST", "/api/admin/demandes/1/envoyer")]
    assert fake.log[-1][3] == {"userIds": [3, 4], "message": "Bonjour"}
    assert out["envoyes"] == 2 and out["noop"] is False


def test_a_member_who_already_received_it_is_refused_unless_allow_resend(fake, jeton):
    send = _outil("hellostock_demande_send")
    send(demande_id=1, user_ids=[3], dry_run=False)
    with pytest.raises(McpError) as e:
        send(demande_id=1, user_ids=[3, 4], dry_run=False)
    assert "Already received by 3" in e.value.error.message
    assert fake.ecritures().count(("POST", "/api/admin/demandes/1/envoyer")) == 1
    apercu = send(demande_id=1, user_ids=[3, 4])
    assert [a["user_id"] for a in apercu["already_sent"]] == [3]
    send(demande_id=1, user_ids=[3], allow_resend=True, dry_run=False)
    assert fake.ecritures().count(("POST", "/api/admin/demandes/1/envoyer")) == 2


@pytest.mark.parametrize("kwargs", [
    {"user_ids": []}, {"user_ids": [3, 3]}, {"user_ids": list(range(1, 52))},
    {"user_ids": [3], "message": "x" * 2001},
])
def test_a_malformed_send_is_refused_before_anything_leaves(fake, jeton, kwargs):
    with pytest.raises(McpError):
        _outil("hellostock_demande_send")(demande_id=1, dry_run=False, **kwargs)
    assert fake.log == []


def test_no_email_could_leave_is_said_and_not_retried(fake, jeton):
    fake.force[("POST", "/api/admin/demandes/1/envoyer")] = (
        502, {"error": "Aucun courriel n'a pu être envoyé", "echecs": ["m3@example.test"]})
    with pytest.raises(McpError) as e:
        _outil("hellostock_demande_send")(demande_id=1, user_ids=[3], dry_run=False)
    assert e.value.error.code == INTERNAL_ERROR
    assert "no send was recorded" in e.value.error.message
    assert fake.ecritures() == [("POST", "/api/admin/demandes/1/envoyer")]


def test_a_mailer_that_is_not_configured_is_said(fake, jeton):
    fake.noop = True
    out = _outil("hellostock_demande_send")(demande_id=1, user_ids=[3], dry_run=False)
    assert "NO email went out" in out["note"]


# --- statut et mots-clés ------------------------------------------------------

def test_set_status_reads_first_and_never_patches_an_unknown_demande(fake, jeton):
    with pytest.raises(McpError) as e:
        _outil("hellostock_demande_set_status")(demande_id=99, status="closed")
    assert "404" in e.value.error.message and fake.ecritures() == []


def test_set_status_reports_the_transition_and_its_public_effect(fake, jeton):
    out = _outil("hellostock_demande_set_status")(demande_id=2, status="published")
    assert (out["from"], out["to"], out["success"]) == ("qualified", "published", True)
    assert "public marketplace" in out["effect"]
    assert fake.ecritures() == [("PATCH", "/api/admin/demandes/2")]


def test_set_status_to_the_current_status_writes_nothing(fake, jeton):
    out = _outil("hellostock_demande_set_status")(demande_id=1, status="published")
    assert out["unchanged"] is True and fake.ecritures() == []


def test_a_status_dry_run_writes_nothing(fake, jeton):
    out = _outil("hellostock_demande_set_status")(demande_id=2, status="closed",
                                                 dry_run=True)
    assert out["dry_run"] is True and fake.ecritures() == []


def test_keywords_return_what_is_stored_not_what_was_asked(fake, jeton):
    out = _outil("hellostock_offre_update")(offre_id=1,
                                           keywords=["304L", "  AISI  304L ", "304l"])
    assert out["from"] == {"keywords": []}
    assert out["written"] == {"keywords": ["304l", "aisi 304l"]}
    assert fake.log[-2][3] == {"keywords": ["304L", "  AISI  304L ", "304l"]}


def test_a_keyword_carrying_an_identity_is_refused_with_the_term(fake, jeton):
    with pytest.raises(McpError) as e:
        _outil("hellostock_offre_update")(offre_id=1, keywords=["304L", "coulée 88213"])
    assert "coulée 88213" in e.value.error.message
    assert fake.offres[1]["keywords"] == []


def test_an_offre_update_needs_something_to_write(fake, jeton):
    with pytest.raises(McpError):
        _outil("hellostock_offre_update")(offre_id=1)
    assert fake.log == []


# --- la sonde -----------------------------------------------------------------

def test_the_probe_refuses_a_secret_that_is_not_a_hellostock_token(fake):
    from oto_mcp.tools import hellostock
    with pytest.raises(ValueError, match="hs_"):
        hellostock._verify({"key": "sk_live_x"})
    assert fake.log == []


@pytest.mark.parametrize("token, attendu", [("hs_revoque", "revoked"),
                                            (MEMBRE, "administrator")])
def test_the_probe_names_the_right_remedy(fake, token, attendu):
    from oto_mcp.tools import hellostock
    with pytest.raises(ValueError, match=attendu):
        hellostock._verify({"key": token})


def test_the_probe_passes_an_admin_token_with_one_read(fake):
    from oto_mcp.tools import hellostock
    assert hellostock._verify({"key": ADMIN}) is None
    assert fake.log == [("GET", "/api/admin/demandes", {"limit": 1}, None)]
