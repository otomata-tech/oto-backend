"""Connecteur TheirStack — offres d'emploi par employeur + technologies (api.theirstack.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection, hors socle),
la surface MCP sous le namespace `theirstack` (2 tools, chacun avec une description —
régression du piège f-string-docstring), la jointure tool↔client oto-core (garde
version-skew), la sonde « tester la connexion », et les deux points où le module ne fait
pas que passer le plat : la construction du payload (args typés puis `extra` fusionné EN
DERNIER) et la projection du retour (`full=False` resserre chaque item, l'enveloppe
`metadata` reste ; `full=True` rend le brut).
"""
import asyncio
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError
from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {"theirstack_jobs_search", "theirstack_companies_search"}


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
    from oto_mcp.tools import theirstack

    m = FastMCP("t")
    theirstack.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_theirstack_is_keyed_connector_platform_grant_only():
    c = providers.REGISTRY["theirstack"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    assert c.platform_key_open is False           # revente au crédit → grant explicite
    assert c.default_active is False               # deny-by-default
    assert c.default_quota == 0
    assert "theirstack" in providers.KEY_PROVIDERS
    assert c.category == "Lead & company data"
    assert c.publisher_name == "TheirStack"
    assert c.label == "TheirStack"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["theirstack"] == "theirstack.com"


def test_theirstack_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["theirstack"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_theirstack_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "theirstack"
               for t in all_tools if t.startswith("theirstack_"))


def test_theirstack_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_docstrings_carry_the_billing_and_coverage_facts(all_tools):
    for name in EXPECTED_TOOLS:
        d = all_tools[name].description
        assert "per company record" in d
        assert "8%" in d and "not an error" in d


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("theirstack")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.theirstack.client import TheirStackClient
    for meth in ("search_jobs", "search_companies", "credit_balance"):
        assert callable(getattr(TheirStackClient, meth, None)), \
            f"TheirStackClient.{meth} manquant"


# --- payload + projection (client mocké) --------------------------------------

_JOB = {"id": 1, "job_title": "Comptable", "url": "https://x/1", "date_posted": "2026-08-01",
        "company": "ENTREPRISE TEMOIN", "location": "Perpignan", "description": "long…",
        "salary_string": None, "technology_slugs": ["sap"], "company_object": {"id": "c1"}}
_COMPANY = {"id": "c1", "name": "ENTREPRISE TEMOIN", "domain": "entreprise-temoin.example", "employee_count": 40,
            "industry": "wholesale", "technology_names": ["SAP"], "technology_slugs": ["sap"],
            "num_jobs": 3, "jobs_found": [], "linkedin_url": "https://linkedin.com/company/x"}


def test_jobs_search_builds_payload_and_projects():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        inst = cls.return_value
        inst.search_jobs.return_value = {"metadata": {"total_results": 1, "truncated_results": 0},
                                         "data": [_JOB]}
        out = _tool("theirstack_jobs_search").fn(
            company_names=["ENTREPRISE TEMOIN", "  "], job_country_code_or=["FR"], limit=10, page=2,
            extra={"job_title_or": ["comptable"], "limit": 5})

    payload = inst.search_jobs.call_args.args[0]
    assert payload["company_name_or"] == ["ENTREPRISE TEMOIN"]      # vides retirés
    assert payload["posted_at_max_age_days"] == 90            # défaut : satisfait l'exigence API
    assert payload["job_country_code_or"] == ["FR"]
    assert payload["page"] == 2
    assert payload["limit"] == 5                              # `extra` prime (fusionné en dernier)
    assert payload["job_title_or"] == ["comptable"]
    # Projection : l'item est resserré, l'enveloppe reste.
    assert out["metadata"] == {"total_results": 1, "truncated_results": 0}
    assert out["data"] == [{"company": "ENTREPRISE TEMOIN", "job_title": "Comptable",
                            "date_posted": "2026-08-01", "url": "https://x/1",
                            "location": "Perpignan"}]


def test_jobs_search_full_returns_raw_records():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_jobs.return_value = {"metadata": {}, "data": [_JOB]}
        out = _tool("theirstack_jobs_search").fn(company_names=["ENTREPRISE TEMOIN"], full=True)
    assert out["data"] == [_JOB]


def test_companies_search_builds_payload_and_projects():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        inst = cls.return_value
        inst.search_companies.return_value = {
            "metadata": {"total_companies": 1, "truncated_companies": 0}, "data": [_COMPANY]}
        out = _tool("theirstack_companies_search").fn(
            company_names=["ENTREPRISE TEMOIN"], company_country_code_or=["FR"],
            extra={"company_technology_slug_or": ["sap"]})

    payload = inst.search_companies.call_args.args[0]
    assert payload == {"page": 0, "limit": 25, "company_name_or": ["ENTREPRISE TEMOIN"],
                       "company_country_code_or": ["FR"],
                       "company_technology_slug_or": ["sap"]}
    assert "posted_at_max_age_days" not in payload
    assert out["metadata"]["truncated_companies"] == 0
    assert out["data"] == [{"name": "ENTREPRISE TEMOIN", "domain": "entreprise-temoin.example", "employee_count": 40,
                            "industry": "wholesale", "technology_names": ["SAP"]}]


def test_companies_search_empty_result_is_not_an_error():
    """≈ 8 % de couverture sur les petits grossistes : `data: []` doit revenir tel quel."""
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_companies.return_value = {
            "metadata": {"total_results": 0, "truncated_results": 0}, "data": []}
        out = _tool("theirstack_companies_search").fn(company_names=["Inconnue SARL"])
    assert out["metadata"] == {"total_results": 0, "truncated_results": 0}
    assert out["data"] == []
    assert out["credits_estimes"] == 0   # une page vide ne coûte rien


# --- crédits : TheirStack ne rend aucun compteur, on l'estime (oto#174) --------

def test_jobs_search_estime_un_credit_par_offre_rendue():
    """Retour 819 (08/09/2026) : des procédures disaient « compte les crédits depuis
    `metadata` » — l'API n'en rend aucun. L'estimation suit le barème publié."""
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_jobs.return_value = {"metadata": {}, "data": [_JOB] * 4}
        out = _tool("theirstack_jobs_search").fn(company_names=["ENTREPRISE TEMOIN"])
    assert out["credits_estimes"] == 4
    assert "published rate card" in out["credits_estimes_source"]
    assert "credit-balance" in out["credits_estimes_source"]


def test_companies_search_estime_trois_credits_par_entreprise_meme_en_full():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_companies.return_value = {
            "metadata": {}, "data": [_COMPANY] * 2}
        out = _tool("theirstack_companies_search").fn(company_names=["X"], full=True)
    assert out["credits_estimes"] == 6
    assert out["data"] == [_COMPANY] * 2      # le brut reste intact


def test_la_description_servie_dit_que_l_amont_ne_rend_aucun_compteur(all_tools):
    for name in EXPECTED_TOOLS:
        desc = all_tools[name].description
        assert "credits_estimes" in desc and "NO credit counter" in desc, name


# --- métrage par unité (facturation du partenaire, 21/08) ─────────────────────

def test_jobs_search_traces_the_returned_job_count():
    with patch("oto_mcp.tools.theirstack.session_org.note_call_trace") as trace, \
         patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_jobs.return_value = {
            "metadata": {}, "data": [_JOB, _JOB, _JOB]}
        _tool("theirstack_jobs_search").fn(company_names=["ENTREPRISE TEMOIN"])
    trace.assert_called_once_with(quantity=3)


def test_jobs_search_traces_by_returned_count_not_by_limit():
    with patch("oto_mcp.tools.theirstack.session_org.note_call_trace") as trace, \
         patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_jobs.return_value = {"metadata": {}, "data": [_JOB]}
        _tool("theirstack_jobs_search").fn(company_names=["ENTREPRISE TEMOIN"], limit=25)
    trace.assert_called_once_with(quantity=1)  # not 25 (`limit`), what was ACTUALLY returned


def test_companies_search_traces_the_returned_company_count():
    with patch("oto_mcp.tools.theirstack.session_org.note_call_trace") as trace, \
         patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_companies.return_value = {
            "metadata": {}, "data": [_COMPANY, _COMPANY]}
        _tool("theirstack_companies_search").fn(company_names=["ENTREPRISE TEMOIN"])
    trace.assert_called_once_with(quantity=2)


def test_companies_search_traces_zero_on_an_empty_result():
    """`data: []` is a NORMAL result (module docstring), not an error — the trace
    call fires with `quantity=0`, and the sink STORES that zero (`>= 0`). A traced
    zero is not an untraced call: NULL means "this tool doesn't trace", which a
    billing consumer reads as 1. Dropping the zero would bill 3 credits for a
    search that found no company."""
    with patch("oto_mcp.tools.theirstack.session_org.note_call_trace") as trace, \
         patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_companies.return_value = {"metadata": {}, "data": []}
        _tool("theirstack_companies_search").fn(company_names=["Inconnue SARL"])
    trace.assert_called_once_with(quantity=0)


def test_invalid_args_never_hit_the_client():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        with pytest.raises(McpError):
            _tool("theirstack_jobs_search").fn(company_names="TEMOIN")       # pas une liste
        with pytest.raises(McpError):
            _tool("theirstack_jobs_search").fn(extra=["not", "a", "dict"])
        with pytest.raises(McpError):
            _tool("theirstack_companies_search").fn(limit=0)
    cls.return_value.search_jobs.assert_not_called()
    cls.return_value.search_companies.assert_not_called()


def test_upstream_402_becomes_an_actionable_tool_error():
    from oto.tools.common.errors import UpstreamHTTPError
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_companies.side_effect = UpstreamHTTPError(
            402, {"detail": "Not enough credits"}, service="theirstack")
        with pytest.raises(McpError, match="credits"):
            _tool("theirstack_companies_search").fn(company_names=["X"])


def test_upstream_422_names_the_required_filters():
    from oto.tools.common.errors import UpstreamHTTPError
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.search_jobs.side_effect = UpstreamHTTPError(
            422, {"detail": "at least one filter"}, service="theirstack")
        with pytest.raises(McpError, match="posted_at_max_age_days"):
            _tool("theirstack_jobs_search").fn(extra={"posted_at_max_age_days": None})


def test_verify_probe_uses_the_free_credit_balance_call():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.credit_balance.return_value = {"api_credits": 10,
                                                        "used_api_credits": 2}
        from oto_mcp.tools import theirstack
        theirstack._verify({"key": "k"}, {})
    cls.assert_called_once_with(api_key="k")
    cls.return_value.credit_balance.assert_called_once_with()
    cls.return_value.search_companies.assert_not_called()


# --- clé plateforme (oto-backend#405 ; câblée ici le 2026-09-09) ---------------

def _search_returning(n, monkeypatch, *, is_platform):
    """Joue une recherche qui rend `n` records, sous le mode de clé demandé."""
    from unittest.mock import patch
    monkeypatch.setattr("oto_mcp.access.resolve_api_key",
                        lambda provider, account=None: ("k", is_platform))
    payload = {"data": [{"id": i} for i in range(n)], "metadata": {}}
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls, \
         patch("oto_mcp.tools.theirstack.session_org.note_call_trace") as trace:
        cls.return_value.search_jobs.return_value = payload
        _tool("theirstack_jobs_search").fn(company_names=["acme"])
    rec = [c.kwargs["units"] for c in trace.call_args_list if "units" in c.kwargs]
    return rec, trace


def test_une_cle_plateforme_debite_le_quota_oto_au_nombre_de_RECORDS(monkeypatch):
    """TheirStack nous facture au record (1 crédit/offre), pas à l'appel : un
    appel qui rend 50 offres doit débiter 50, sinon notre propre quota compte
    des appels pendant que le fournisseur compte des records."""
    rec, _ = _search_returning(50, monkeypatch, is_platform=True)
    assert rec == [50]


def test_une_cle_du_CLIENT_ne_debite_jamais_notre_quota(monkeypatch):
    """Le client apporte sa clé : c'est SON quota chez TheirStack qui bouge."""
    rec, _ = _search_returning(50, monkeypatch, is_platform=False)
    assert rec == []


def test_le_metrage_lui_est_INCONDITIONNEL(monkeypatch):
    """`quantity` est tracé dans les deux modes — c'est `tool_calls.key_mode`,
    posé au résolveur, qui dit sous quelle clé l'appel est passé. Séparer les
    deux est ce qui permet à la facturation de ne retenir que la clé du partenaire sans
    que le tool ait à connaître la règle de facturation."""
    for platform in (True, False):
        _, trace = _search_returning(7, monkeypatch, is_platform=platform)
        trace.assert_any_call(quantity=7)


def test_une_page_vide_sur_cle_plateforme_debite_1_comme_avant(monkeypatch):
    """Zéro record rendu = zéro crédit chez TheirStack, mais notre quota en débitait 1 :
    l'ancien compteur plafonnait tout débit à 1 au minimum. L8 garde le montant
    (`docs/credits-au-moment-de-l-appel.md`, écarts connus) ; la quantité facturée,
    elle, reste 0."""
    rec, trace = _search_returning(0, monkeypatch, is_platform=True)
    assert rec == [1]
    trace.assert_any_call(quantity=0)   # 0 TRACÉ ≠ non tracé


def test_verify_probe_counts_a_decimal_usage():
    """Un usage décimal était IGNORÉ : le solde se lisait comme l'allocation entière."""
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.credit_balance.return_value = {"api_credits": 10,
                                                        "used_api_credits": 2.5}
        from oto_mcp.tools import theirstack
        assert theirstack._verify({"key": "k"}, {})["quota"]["restant"] == 7.5


def test_verify_probe_refuses_an_unreadable_usage():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.credit_balance.return_value = {"api_credits": 10,
                                                        "used_api_credits": "n/a"}
        from oto_mcp.tools import theirstack
        with pytest.raises(RuntimeError, match="readable API credit balance"):
            theirstack._verify({"key": "k"}, {})


def test_verify_probe_without_usage_reads_the_allocation():
    with patch("oto.tools.theirstack.client.TheirStackClient") as cls:
        cls.return_value.credit_balance.return_value = {"api_credits": 10}
        from oto_mcp.tools import theirstack
        assert theirstack._verify({"key": "k"}, {})["quota"]["restant"] == 10
