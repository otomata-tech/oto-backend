"""Lemlist lead-lifecycle tools (create/launch/variables) added on top of the
native read-only surface. Locks in: the tool↔client join (version-skew guard),
that `lemlist_create_lead` filters None fields and merges custom variables into
the lead payload before calling the client, that `lemlist_launch_lead` is
masked by default (it pushes a lead into a live send — a bad LLM call
shouldn't do that by accident) while the other new tools stay visible, and
platform-usage recording on all three.
"""
import asyncio
from unittest.mock import patch

import pytest

from oto_mcp.tool_visibility import DEFAULT_HIDDEN_TOOLS, namespace_of

EXPECTED_NEW_TOOLS = {
    "lemlist_create_lead", "lemlist_launch_lead", "lemlist_add_lead_variables",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name for t in tools}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import lemlist

    m = FastMCP("t")
    lemlist.register(m)
    return asyncio.run(m.get_tool(name))


def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.lemlist.LemlistClient")
    return key, cls


# --- surface MCP --------------------------------------------------------------

def test_new_lemlist_tools_registered_under_namespace(all_tools):
    assert EXPECTED_NEW_TOOLS <= all_tools
    assert all(namespace_of(t) == "lemlist" for t in EXPECTED_NEW_TOOLS)


# --- jointure tool <-> client oto-core (garde version-skew) -------------------

def test_client_exposes_methods_called_by_new_tools():
    from oto.tools.lemlist import LemlistClient
    for meth in ("create_lead", "launch_lead", "add_lead_variables"):
        assert callable(getattr(LemlistClient, meth, None)), f"LemlistClient.{meth} manquant"


# --- visibilité : seul launch_lead est masqué par défaut ----------------------

def test_only_launch_lead_is_hidden_by_default():
    assert "lemlist_launch_lead" in DEFAULT_HIDDEN_TOOLS
    assert "lemlist_create_lead" not in DEFAULT_HIDDEN_TOOLS
    assert "lemlist_add_lead_variables" not in DEFAULT_HIDDEN_TOOLS


# --- lemlist_create_lead : shaping du payload ---------------------------------

def test_create_lead_drops_none_fields_and_merges_custom_variables():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.create_lead.return_value = {"_id": "lea_1"}
        tool = _tool("lemlist_create_lead")

        tool.fn(
            campaign_id="cam_1", email="a@acme.fr", first_name="A",
            custom_variables={"industry": "SaaS"},
        )

        args, kwargs = inst.create_lead.call_args
        assert args[0] == "cam_1"
        lead = args[1]
        assert lead == {"email": "a@acme.fr", "firstName": "A", "industry": "SaaS"}
        assert kwargs == {
            "deduplicate": False, "linkedin_enrichment": False,
            "find_email": False, "verify_email": False, "find_phone": False,
        }


def test_create_lead_forwards_enrichment_flags():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.create_lead.return_value = {"_id": "lea_1"}
        tool = _tool("lemlist_create_lead")

        tool.fn(campaign_id="cam_1", email="a@acme.fr", find_email=True, deduplicate=True)

        kwargs = inst.create_lead.call_args.kwargs
        assert kwargs["find_email"] is True
        assert kwargs["deduplicate"] is True
        assert kwargs["find_phone"] is False


# --- lemlist_launch_lead / lemlist_add_lead_variables : passthrough -----------

def test_launch_lead_delegates_to_client():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.launch_lead.return_value = {"ok": True}
        tool = _tool("lemlist_launch_lead")

        out = tool.fn(lead_id="lea_1")

        inst.launch_lead.assert_called_once_with("lea_1")
        assert out == {"ok": True}


def test_add_lead_variables_delegates_to_client():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.add_lead_variables.return_value = {"ok": True}
        tool = _tool("lemlist_add_lead_variables")

        out = tool.fn(lead_id="lea_1", variables={"customField1": "x"})

        inst.add_lead_variables.assert_called_once_with("lea_1", {"customField1": "x"})
        assert out == {"ok": True}


# --- enrichissement : surface async + garde-fous -----------------------------
#
# L'enrichissement n'envoie rien mais DÉPENSE des crédits lemlist, d'où deux
# invariants verrouillés ici : aucune action n'est demandée par défaut (un appel
# sans action échoue localement, sans round-trip ni crédit), et rien n'attend
# in-process (signal #252 : au-delà de ~60s le client MCP raccroche, le résultat
# est perdu et les crédits, eux, sont consommés).

ENRICH_TOOLS = {
    "lemlist_enrich", "lemlist_enrich_lead",
    "lemlist_enrich_result", "lemlist_enrich_bulk",
}


def test_enrich_tools_registered_under_namespace(all_tools):
    assert ENRICH_TOOLS <= all_tools
    assert all(namespace_of(t) == "lemlist" for t in ENRICH_TOOLS)


def test_enrich_tools_visible_by_default():
    # Contrairement à launch_lead, enrichir ne déclenche aucun envoi.
    assert not (ENRICH_TOOLS & DEFAULT_HIDDEN_TOOLS)


def test_client_exposes_enrichment_methods():
    from oto.tools.lemlist import LemlistClient
    for meth in ("enrich", "get_enrichment", "enrich_lead", "bulk_enrich"):
        assert callable(getattr(LemlistClient, meth, None)), f"LemlistClient.{meth} manquant"


def test_enrich_forwards_identity_and_actions():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.enrich.return_value = {"id": "enr_1"}
        tool = _tool("lemlist_enrich")

        out = tool.fn(
            first_name="John", last_name="Lempire",
            company_domain="lempire.com", find_email=True,
        )

        kwargs = inst.enrich.call_args.kwargs
        assert kwargs["first_name"] == "John"
        assert kwargs["company_domain"] == "lempire.com"
        assert kwargs["find_email"] is True
        assert kwargs["find_phone"] is False
        assert out["enrichment_id"] == "enr_1"


def test_enrich_without_any_action_fails_locally():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_enrich")

        with pytest.raises(McpError):
            tool.fn(email="a@acme.fr")

        # Aucun appel amont : pas de crédit brûlé sur le 400 documenté de lemlist.
        inst.enrich.assert_not_called()


def test_enrich_lead_without_any_action_fails_locally():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_enrich_lead")

        with pytest.raises(McpError):
            tool.fn(lead_id="lea_1")

        inst.enrich_lead.assert_not_called()


def test_enrich_result_reports_in_progress_without_waiting():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_enrichment.return_value = {
            "enrichmentId": "enr_1", "enrichmentStatus": "in-progress",
            "input": {"firstName": "John"}, "data": {},
        }
        tool = _tool("lemlist_enrich_result")

        out = tool.fn(enrichment_id="enr_1")

        # Un seul relevé, pas de boucle d'attente.
        assert inst.get_enrichment.call_count == 1
        assert out["all_done"] is False
        assert out["results"][0]["done"] is False
        assert out["results"][0]["status"] == "in-progress"


def test_enrich_result_marks_not_found_as_terminal():
    # lemlist répond 404 avec un corps légitime : re-poller ne le fera pas
    # apparaître, donc c'est un état terminal, pas une attente.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_enrichment.return_value = {
            "enrichmentId": "enr_x", "enrichmentStatus": "not-found",
            "error": "Enrichment not found", "data": {},
        }
        tool = _tool("lemlist_enrich_result")

        out = tool.fn(enrichment_id="enr_x")

        assert out["all_done"] is True
        assert out["results"][0]["done"] is True


def test_enrich_result_accepts_a_list_of_ids():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_enrichment.side_effect = [
            {"enrichmentId": "enr_1", "enrichmentStatus": "done",
             "input": {}, "data": {"email": {"email": "a@acme.fr", "notFound": False}}},
            {"enrichmentId": "enr_2", "enrichmentStatus": "in-progress",
             "input": {}, "data": {}},
        ]
        tool = _tool("lemlist_enrich_result")

        out = tool.fn(enrichment_id=["enr_1", "enr_2"])

        assert [r["enrichment_id"] for r in out["results"]] == ["enr_1", "enr_2"]
        assert out["results"][0]["data"]["email"]["email"] == "a@acme.fr"
        assert out["all_done"] is False
        assert "enr_2" not in out["results"][0]["enrichment_id"]


def test_enrich_bulk_maps_actions_to_the_v2_vocabulary():
    # Piège de l'API : en v2 la vérification d'email s'appelle `verify`, PAS
    # `verify_email` — un snake_case mécanique des flags v1 enverrait du faux.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.bulk_enrich.return_value = [{"id": "enr_1", "metadata": {"index": "0"}}]
        tool = _tool("lemlist_enrich_bulk")

        out = tool.fn(people=[{
            "linkedin_url": "https://www.linkedin.com/in/lempire",
            "actions": ["verify_email", "find_phone"],
        }])

        items = inst.bulk_enrich.call_args.args[0]
        assert items[0]["enrichmentRequests"] == ["verify", "find_phone"]
        assert items[0]["input"] == {"linkedinUrl": "https://www.linkedin.com/in/lempire"}
        assert out["enrichment_ids"] == ["enr_1"]


def test_enrich_bulk_rejects_unknown_or_missing_actions():
    from oto_mcp.mcp_errors import McpError
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_enrich_bulk")

        with pytest.raises(McpError):
            tool.fn(people=[{"email": "a@acme.fr", "actions": ["find_everything"]}])
        with pytest.raises(McpError):
            tool.fn(people=[{"email": "a@acme.fr"}])

        inst.bulk_enrich.assert_not_called()


def test_enrich_bulk_surfaces_per_entry_errors():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.bulk_enrich.return_value = [
            {"id": "enr_1", "metadata": {"index": "0"}},
            {"error": "MISSING_INPUTS", "metadata": {"index": "1"}},
        ]
        tool = _tool("lemlist_enrich_bulk")

        out = tool.fn(people=[
            {"email": "a@acme.fr", "actions": ["find_phone"]},
            {"actions": ["find_phone"]},
        ])

        assert out["enrichment_ids"] == ["enr_1"]
        assert out["submitted"][1]["error"] == "MISSING_INPUTS"


def test_enrich_notes_no_platform_units():
    # lemlist n'a pas de clé plateforme (`auth_modes`) : le débit d'avant L8 était mort,
    # il est retiré sans remplacement — rien n'est noté pour le règlement.
    key = patch("oto_mcp.access.resolve_api_key", return_value=("platform-key", True))
    cls = patch("oto.tools.lemlist.LemlistClient")
    note = patch("oto_mcp.session_org.note_call_trace")
    with key, cls as client_cls, note as noted:
        client_cls.return_value.enrich.return_value = {"id": "enr_1"}
        _tool("lemlist_enrich").fn(email="a@acme.fr", find_phone=True)
        assert not any("units" in c.kwargs for c in noted.call_args_list)


def test_bulk_action_vocabulary_matches_the_client():
    # Garde version-skew : le tool écrit le vocabulaire v2 en dur (pour rester
    # validable sous mock), le client le porte aussi. Les deux doivent coïncider.
    from oto.tools.lemlist import LemlistClient
    from oto_mcp.tools.lemlist import BULK_ACTIONS

    assert BULK_ACTIONS == LemlistClient.ENRICH_BULK_ACTIONS
    assert BULK_ACTIONS["verify_email"] == "verify"


def test_enrich_bulk_notes_no_platform_units():
    # Même raison : pas de clé plateforme chez lemlist, donc aucun quota à débiter.
    key = patch("oto_mcp.access.resolve_api_key", return_value=("platform-key", True))
    cls = patch("oto.tools.lemlist.LemlistClient")
    note = patch("oto_mcp.session_org.note_call_trace")
    with key, cls as client_cls, note as noted:
        client_cls.return_value.bulk_enrich.return_value = [
            {"id": "enr_1"}, {"id": "enr_2"}, {"id": "enr_3"},
        ]
        _tool("lemlist_enrich_bulk").fn(people=[
            {"email": f"a{i}@acme.fr", "actions": ["find_email"]} for i in range(3)
        ])
        assert not any("units" in c.kwargs for c in noted.call_args_list)


def test_enrich_result_union_survives_the_mcp_validation_boundary():
    # Les autres tests appellent `tool.fn` — la fonction brute, sans la
    # validation pydantic de FastMCP. Ici on passe par `tool.run`, le vrai
    # chemin d'un client MCP, pour que `str | list[str]` soit prouvé des deux
    # côtés et pas seulement dans la boucle Python.
    def _run(payload, side_effect):
        key, cls = _with_fake_client()
        with key, cls as client_cls:
            client_cls.return_value.get_enrichment.side_effect = side_effect
            tool = _tool("lemlist_enrich_result")
            res = asyncio.run(tool.run(payload))
            return res.structured_content

    def _done(eid):
        return {"enrichmentId": eid, "enrichmentStatus": "done", "input": {}, "data": {}}

    out = _run({"enrichment_id": ["enr_1", "enr_2"]}, [_done("enr_1"), _done("enr_2")])
    assert [r["enrichment_id"] for r in out["results"]] == ["enr_1", "enr_2"]

    out = _run({"enrichment_id": "enr_1"}, [_done("enr_1")])
    assert [r["enrichment_id"] for r in out["results"]] == ["enr_1"]


def test_enrich_bulk_falls_back_to_position_when_metadata_is_unusable():
    # lemlist renvoie `metadata` dans deux formes différentes dans sa propre
    # doc : la lire aveuglément casserait l'appariement.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.bulk_enrich.return_value = [
            {"id": "enr_1", "metadata": "some_id"},   # chaîne nue
            {"id": "enr_2", "metadata": {"index": "1"}},
        ]
        out = _tool("lemlist_enrich_bulk").fn(people=[
            {"email": "a@acme.fr", "actions": ["find_email"]},
            {"email": "b@acme.fr", "actions": ["find_email"]},
        ])
        assert [r["index"] for r in out["submitted"]] == [0, 1]


# --- ce que le live a appris (2026-08-25, clé réelle Folk GTM) ----------------


def test_enrich_result_flags_done_but_empty_as_not_settled():
    # Relevé en live : lemlist bascule parfois sur `done` avant d'avoir posé la
    # charge utile (`data` vide, peuplé au relevé suivant). Compter ça comme
    # terminé ferait conclure « pas trouvé » sur une donnée qui arrive juste après.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_enrichment.return_value = {
            "enrichmentId": "enr_1", "enrichmentStatus": "done", "input": {}, "data": {},
        }
        out = _tool("lemlist_enrich_result").fn(enrichment_id="enr_1")

        assert "warning" in out["results"][0]
        assert out["recheck_suggested"] == ["enr_1"]
        # …mais `all_done` reste vrai : un résultat légitimement vide le
        # resterait pour toujours, et une boucle `while not all_done` ne
        # terminerait jamais. Le re-relevé est une suggestion, pas une attente.
        assert out["all_done"] is True


def test_enrich_result_digests_only_what_carries_a_value():
    # `data` porte la clé de l'axe demandé même vide, et `notFound: false` a été
    # vu sur une charge sans numéro : seule la valeur fait foi.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_enrichment.return_value = {
            "enrichmentId": "enr_1", "enrichmentStatus": "done", "input": {},
            "data": {
                "email": {"email": "jane.doe@acme.test", "notFound": False,
                          "status": "deliverable"},
                "phone": {"notFound": False},        # pas de numéro malgré notFound=false
                "linkedin": {},                       # profil non résolu
            },
        }
        out = _tool("lemlist_enrich_result").fn(enrichment_id="enr_1")

        found = out["results"][0]["found"]
        assert found["email"] == "jane.doe@acme.test"
        assert found["email_status"] == "deliverable"
        assert "phone" not in found
        assert "linkedin" not in found
        assert out["all_done"] is True
        assert "warning" not in out["results"][0]


def test_enrich_result_digests_a_verify_only_payload():
    # `verify_email` seul ne rend qu'un statut, sans adresse.
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_enrichment.return_value = {
            "enrichmentId": "enr_1", "enrichmentStatus": "done", "input": {},
            "data": {"email": {"status": "undeliverable"}},
        }
        out = _tool("lemlist_enrich_result").fn(enrichment_id="enr_1")

        assert out["results"][0]["found"] == {"email_status": "undeliverable"}
        assert out["all_done"] is True


def test_enrich_result_digests_a_linkedin_profile():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.get_enrichment.return_value = {
            "enrichmentId": "enr_1", "enrichmentStatus": "done", "input": {},
            "data": {"linkedin": {"firstName": "Bill", "lastName": "Gates",
                                  "tagline": "Chair, Gates Foundation",
                                  "linkedinMemberId": "251749025"}},
        }
        out = _tool("lemlist_enrich_result").fn(enrichment_id="enr_1")

        li = out["results"][0]["found"]["linkedin"]
        assert li["firstName"] == "Bill"
        assert "linkedinMemberId" not in li  # digest, pas recopie du profil entier


# --- gestion de campagne ------------------------------------------------------
#
# Ce que ce bloc verrouille tient en une phrase : la borne du connecteur porte
# désormais sur CE QUI ENVOIE, pas sur l'écriture. Trois invariants, chacun
# cassable par une réécriture innocente — le tool nu pour `start` (le masquage a
# le grain du tool, pas de l'op), le refus d'`autoReview` (il ferait de
# `lemlist_create_lead`, visible, un chemin d'envoi), et le drapeau de troncature
# d'une liste plafonnée.

CAMPAIGN_TOOLS = {
    "lemlist_campaign", "lemlist_campaign_start", "lemlist_sequence",
    "lemlist_schedule",
}


def test_campaign_tools_registered_under_namespace(all_tools):
    assert CAMPAIGN_TOOLS <= all_tools
    assert all(namespace_of(t) == "lemlist" for t in CAMPAIGN_TOOLS)


@pytest.mark.exige_pin_oto_core   # lit la vraie classe : faux si le venv retarde
def test_client_exposes_methods_called_by_campaign_tools():
    """Garde version-skew : ces méthodes arrivent avec l'oto-core de la même
    fenêtre — tant que le pin n'est pas bumpé, c'est CE test qui le dit."""
    from oto.tools.lemlist import LemlistClient
    for meth in (
        "list_all_campaigns", "create_campaign", "update_campaign",
        "start_campaign", "pause_campaign", "duplicate_campaign",
        "get_campaign_statutes", "get_campaign_reports",
        "get_campaign_stats_v2", "get_batch_campaign_stats",
        "delete_step", "create_ab_variant", "get_ab_variant",
        "update_ab_variant", "delete_ab_variant", "select_ab_winner",
        "list_schedules", "get_schedule", "create_schedule", "update_schedule",
        "delete_schedule", "get_campaign_schedules", "associate_schedule",
    ):
        assert callable(getattr(LemlistClient, meth, None)), \
            f"LemlistClient.{meth} manquant"


# --- visibilité : le geste d'envoi est SEUL dans son tool, et masqué ----------

def test_campaign_start_is_hidden_and_the_rest_stays_visible():
    assert "lemlist_campaign_start" in DEFAULT_HIDDEN_TOOLS
    for visible in ("lemlist_campaign", "lemlist_sequence", "lemlist_schedule"):
        assert visible not in DEFAULT_HIDDEN_TOOLS


def test_start_is_not_reachable_as_an_op_of_the_visible_tool():
    """La raison d'être du tool nu. `DEFAULT_HIDDEN_TOOLS` a le grain du TOOL :
    une op `start` logée dans `lemlist_campaign` serait exposée sans que le
    masquage puisse l'atteindre. Si quelqu'un l'y replie un jour, ce test tombe."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign")

        with pytest.raises(Exception, match="unknown op"):
            tool.fn(op="start", campaign_id="cam_1")
        inst.start_campaign.assert_not_called()


# --- le verrou autoReview -----------------------------------------------------

@pytest.mark.parametrize("op,extra", [
    ("create", {"name": "Q4"}),
    ("update", {"campaign_id": "cam_1"}),
])
@pytest.mark.parametrize("key_name", ["autoReview", "autoReviewConditions"])
def test_auto_review_is_refused_before_any_call(op, extra, key_name):
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign")

        with pytest.raises(Exception, match="added lead go out WITHOUT review"):
            tool.fn(op=op, settings={key_name: True}, **extra)

        # Refusé AU BORD : aucun aller-retour, donc rien qui puisse partir.
        inst.create_campaign.assert_not_called()
        inst.update_campaign.assert_not_called()


def test_create_refuses_settings_rather_than_dropping_them():
    """`POST /campaigns` ne prend que name+timezone : un `settings` avalé rendrait
    une campagne d'apparence réglée dont rien n'aurait pris."""
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign")

        with pytest.raises(Exception, match="does not apply at creation"):
            tool.fn(op="create", name="Q4", settings={"stopOnEmailReplied": True})
        inst.create_campaign.assert_not_called()

        tool.fn(op="create", name="Q4", timezone="Europe/Paris")
        inst.create_campaign.assert_called_once_with("Q4", timezone="Europe/Paris")


def test_update_without_autoreview_goes_through():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign")

        tool.fn(op="update", campaign_id="cam_1", name="Q4",
                sender_user_ids=["usr_1"], settings={"stopOnEmailReplied": True})

        inst.update_campaign.assert_called_once_with("cam_1", {
            "stopOnEmailReplied": True, "name": "Q4", "sendUserIds": ["usr_1"],
        })


def test_update_refuses_an_empty_patch():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_campaign")

        with pytest.raises(Exception, match="nothing to update"):
            tool.fn(op="update", campaign_id="cam_1")
        inst.update_campaign.assert_not_called()


# --- liste : la troncature se dit ---------------------------------------------

def test_list_campaigns_surfaces_truncation():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        from oto.tools.lemlist import Campaign
        inst = client_cls.return_value
        inst.list_all_campaigns.return_value = (
            [Campaign(id="cam_1", name="Q3", status="running", senders=[])], True)
        tool = _tool("lemlist_list_campaigns")

        out = tool.fn(status="running", newest_first=True, max_campaigns=250)

        assert out["truncated"] is True
        assert out["count"] == 1
        assert out["campaigns"][0]["id"] == "cam_1"
        # 250 demandés ⇒ 3 pages de 100, et les filtres passent en snake_case.
        assert inst.list_all_campaigns.call_args.kwargs == {
            "max_pages": 3, "status": "running", "sort_order": "desc",
        }


# --- stats : le vrai endpoint, et son détail projeté --------------------------

def test_campaign_stats_reads_the_real_counters_over_the_whole_life():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_campaign_stats_v2.return_value = {"nbLeads": 12, "opened": 3}
        tool = _tool("lemlist_get_campaign_stats")

        out = tool.fn(campaign_id="cam_1")

        kwargs = inst.get_campaign_stats_v2.call_args.kwargs
        assert kwargs["start_date"].startswith("2015-")
        assert kwargs["end_date"].endswith("Z")
        # Surtout PAS l'ancien dérivé d'activités, plafonné à 1000 événements.
        inst.get_campaign_stats.assert_not_called()
        assert out["nbLeads"] == 12


# Signal d'usage 1744 : une fenêtre d'UN jour (start_date = end_date, dates seules)
# partait telle quelle chez lemlist, qui lit `2026-11-12` comme minuit : une fenêtre
# de largeur nulle, tous les compteurs à 0 alors que `nbLeads` (qui ne dépend pas de
# la fenêtre) restait juste. Une date seule désigne désormais le jour UTC entier.

def test_window_date_only_bounds_cover_the_whole_utc_day():
    from oto_mcp.tools.lemlist import _default_window

    assert _default_window("2026-11-12", "2026-11-12") == (
        "2026-11-12T00:00:00.000Z", "2026-11-12T23:59:59.999Z")
    # Plusieurs jours : le dernier est compris, il n'est plus perdu.
    assert _default_window("2026-11-01", "2026-11-30") == (
        "2026-11-01T00:00:00.000Z", "2026-11-30T23:59:59.999Z")


def test_window_instants_pass_through_untouched():
    from oto_mcp.tools.lemlist import _default_window

    start, end = "2026-11-11T23:00:00.000Z", "2026-11-12T22:59:59.999Z"
    assert _default_window(start, end) == (start, end)
    assert _default_window("2026-11-12T08:00:00+01:00", "2026-11-12T18:00") == (
        "2026-11-12T08:00:00+01:00", "2026-11-12T18:00")


def test_window_defaults_still_mean_the_whole_life():
    from oto_mcp.tools.lemlist import STATS_EPOCH, _default_window

    start, end = _default_window(None, None)
    assert start == STATS_EPOCH
    assert end.endswith("Z") and "T" in end
    # Une seule borne donnée : l'autre garde son défaut.
    start, end = _default_window("2026-11-12", None)
    assert start == "2026-11-12T00:00:00.000Z" and end.endswith("Z")
    assert _default_window(None, "2026-11-12")[1] == "2026-11-12T23:59:59.999Z"


def test_batch_stats_same_day_window_reaches_lemlist_as_the_whole_day():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_batch_campaign_stats.return_value = {"results": []}
        tool = _tool("lemlist_campaign")

        tool.fn(op="batch_stats", campaign_ids=["cam_1", "cam_2"],
                start_date="2026-11-12", end_date="2026-11-12")

        kwargs = inst.get_batch_campaign_stats.call_args.kwargs
        assert kwargs["start_date"] == "2026-11-12T00:00:00.000Z"
        assert kwargs["end_date"] == "2026-11-12T23:59:59.999Z"


def test_campaign_stats_same_day_window_reaches_lemlist_as_the_whole_day():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_campaign_stats_v2.return_value = {"nbLeads": 12}
        tool = _tool("lemlist_get_campaign_stats")

        tool.fn(campaign_id="cam_1", start_date="2026-11-12", end_date="2026-11-12")

        kwargs = inst.get_campaign_stats_v2.call_args.kwargs
        assert kwargs["start_date"] == "2026-11-12T00:00:00.000Z"
        assert kwargs["end_date"] == "2026-11-12T23:59:59.999Z"


def test_campaign_stats_drops_the_per_step_detail_and_names_it():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.get_campaign_stats_v2.return_value = {
            "nbLeads": 12, "steps": [{"a": 1}, {"b": 2}], "perChannel": {"email": {}},
        }
        tool = _tool("lemlist_get_campaign_stats")

        out = tool.fn(campaign_id="cam_1")
        assert "steps" not in out and "perChannel" not in out
        assert out["projection"]["dropped"] == {"steps": 2, "perChannel": 1}

        full = tool.fn(campaign_id="cam_1", full=True)
        assert full["steps"] == [{"a": 1}, {"b": 2}]
        assert "projection" not in full


# --- séquences & plannings : dispatch et arguments requis ---------------------

def test_sequence_ops_reach_the_right_client_method():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_sequence")

        tool.fn(op="add_step", sequence_id="seq_1",
                step={"type": "email", "subject": "hi"})
        inst.add_step.assert_called_once_with("seq_1", {"type": "email", "subject": "hi"})

        tool.fn(op="delete_step", sequence_id="seq_1", step_id="stp_1")
        inst.delete_step.assert_called_once_with("seq_1", "stp_1")

        tool.fn(op="ab_delete", sequence_id="seq_1", step_id="stp_1")
        inst.delete_ab_variant.assert_called_once_with("seq_1", "stp_1", variant="B")

        tool.fn(op="ab_winner", sequence_id="seq_1", step_id="stp_1", variant="A")
        inst.select_ab_winner.assert_called_once_with("seq_1", "stp_1", "A")


def test_sequence_refuses_incomplete_calls_locally():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_sequence")

        with pytest.raises(Exception, match="sequence_id"):
            tool.fn(op="add_step", step={"type": "email"})
        with pytest.raises(Exception, match="step_id"):
            tool.fn(op="delete_step", sequence_id="seq_1")
        with pytest.raises(Exception, match="variant"):
            tool.fn(op="ab_winner", sequence_id="seq_1", step_id="stp_1")
        with pytest.raises(Exception, match="unknown op"):
            tool.fn(op="rename_step", sequence_id="seq_1")
        assert not inst.method_calls


def test_schedule_create_forwards_only_the_fields_given():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_schedule")

        tool.fn(op="create", name="Matinées", start="08:00", weekdays=[1, 2, 3, 4])
        inst.create_schedule.assert_called_once_with(
            "Matinées", start="08:00", weekdays=[1, 2, 3, 4])

        tool.fn(op="associate", campaign_id="cam_1", schedule_id="skd_1")
        inst.associate_schedule.assert_called_once_with("cam_1", "skd_1")


def test_schedule_update_uses_the_api_key_names():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        tool = _tool("lemlist_schedule")

        tool.fn(op="update", schedule_id="skd_1", seconds_to_wait=600, end="17:00")
        inst.update_schedule.assert_called_once_with(
            "skd_1", {"end": "17:00", "secondsToWait": 600})

        with pytest.raises(Exception, match="nothing to update"):
            tool.fn(op="update", schedule_id="skd_1")
