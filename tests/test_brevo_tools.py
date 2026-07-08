"""Connecteur Brevo (API v3) — verrouille la surface MCP et les invariants.

Deux modules pour un namespace (`brevo` + `brevo_crm`), coexistence propre avec le
connecteur `brevoauto` (automations, session navigateur), et la décision « pas
d'écriture destructive exposée » — qui, non gardée, dériverait au premier ajout.
"""
import asyncio

import pytest

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of


@pytest.fixture(scope="module")
def brevo_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name for t in tools}


# --- registre -----------------------------------------------------------------

def test_brevo_is_keyed_api_connector():
    c = providers.REGISTRY["brevo"]
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    assert "brevo" in providers.KEY_PROVIDERS


def test_brevo_declares_two_modules_one_namespace():
    c = providers.REGISTRY["brevo"]
    assert c.modules == ("brevo", "brevo_crm")
    assert c.namespaces == ("brevo",)


def test_brevoauto_is_the_session_connector_distinct_from_brevo():
    a = providers.REGISTRY["brevoauto"]
    assert a.secret_kind == "cookie" and a.personal_session
    assert "brevoauto" not in providers.KEY_PROVIDERS  # pas de clé API
    assert "brevoauto" in providers.CREDENTIAL_PROVIDERS


# --- surface MCP --------------------------------------------------------------

def test_both_modules_register_under_brevo_namespace(brevo_tools):
    brevo = {t for t in brevo_tools if namespace_of(t) == "brevo"}
    # le CRM (module séparé) et l'email (module principal) sont bien là
    assert "brevo_crm_list" in brevo
    assert "brevo_send_email" in brevo
    assert len(brevo) >= 25


def test_brevoauto_namespace_does_not_collide(brevo_tools):
    # `brevoauto_*` → namespace `brevoauto`, jamais absorbé par `brevo`
    assert namespace_of("brevoauto_add_step") == "brevoauto"
    assert all(namespace_of(t) == "brevo"
               for t in brevo_tools if t.startswith("brevo_"))


@pytest.mark.parametrize("forbidden", [
    "brevo_send_campaign", "brevo_delete_contact", "brevo_delete_list",
    "brevo_delete_campaign", "brevo_delete_template", "brevo_delete_hardbounces",
])
def test_destructive_writes_are_not_exposed(brevo_tools, forbidden):
    assert forbidden not in brevo_tools


def test_transactional_send_stays_exposed(brevo_tools):
    # l'envoi unitaire (destinataires explicites) reste ; c'est l'envoi de MASSE
    # (campagne) et les suppressions qui sont retirés
    assert "brevo_send_email" in brevo_tools
    assert "brevo_campaign_test" in brevo_tools
