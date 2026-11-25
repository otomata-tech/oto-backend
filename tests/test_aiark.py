"""Connecteur AI Ark — requalifié de mount fédéré (#152) en connecteur classique
`kind="tools"` (#160). Verrouille : l'entrée registre (keyed API, mode plateforme,
plus de mount), la surface MCP curée, la jointure tool↔client oto-core (garde
version-skew pour un module `_client()->tuple`, hors périmètre de la sonde AST) et
le contrat du client (shaping des requêtes + 404 = introuvable, pas une erreur).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
import requests

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of

# Surface consolidée 6 → 3 et renommée `linkedin_aiark_*` le 2026-08-10 (ADR 0010
# §Amendement + ADR 0047 §Amendement, oto-backend#279) : le namespace porte la
# CAPACITÉ (LinkedIn) suffixée du FOURNISSEUR, AI Ark et Unipile n'étant pas
# substituables (donnée achetée vs session opérée).
EXPECTED_TOOLS = {
    "linkedin_aiark_credits",
    "linkedin_aiark_search",
    "linkedin_aiark_person",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    tools = asyncio.run(m._list_tools())
    return {t.name for t in tools}


# --- registre -----------------------------------------------------------------

def test_aiark_is_classic_keyed_connector():
    c = providers.REGISTRY["aiark"]
    assert c.kind == "tools"            # plus un MCP fédéré
    assert c.keyed and c.secret_kind == "api_key"
    assert "aiark" in providers.KEY_PROVIDERS


def test_aiark_supports_platform_mode():
    c = providers.REGISTRY["aiark"]
    # mode plateforme désormais possible (`units` notés par les handlers, L8)
    assert "platform" in c.auth_modes
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})


# --- surface MCP --------------------------------------------------------------

def test_aiark_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "linkedin_aiark"
               for t in all_tools if t.startswith("linkedin_aiark_"))


def test_aiark_verify_is_probe_not_tool(all_tools):
    # « tester la connexion » = sonde générique (oto_instance op=verify), plus un
    # tool MCP dédié par connecteur.
    from oto_mcp.connectors import verify as connector_verify
    assert "linkedin_aiark_verify_key" not in all_tools
    assert connector_verify.supports("aiark")


def test_aiark_async_bulk_endpoints_not_exposed(all_tools):
    # v1 = synchrone seulement ; les exports/find-emails EN LOT (webhook) sont hors
    # périmètre → aucun tool "bulk"/"track" exposé.
    assert not any("bulk" in t or "track" in t for t in all_tools
                   if t.startswith("linkedin_aiark_"))


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.aiark.client import AiArkClient
    for meth in ("verify_key", "credits", "search_companies", "search_people",
                 "export_person", "reverse_lookup", "mobile_phone"):
        assert callable(getattr(AiArkClient, meth, None)), f"AiArkClient.{meth} manquant"


# --- contrat du client (HTTP mocké) -------------------------------------------

def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.content = b"{}" if body is not None else b""
    r.json.return_value = body if body is not None else {}
    if status >= 400:
        from requests import HTTPError
        r.raise_for_status.side_effect = HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def test_client_sends_x_token_and_json():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(200, {"total": 42})
        out = AiArkClient(api_key="secret").credits()
    assert out == {"total": 42}
    _, kwargs = req.call_args
    assert kwargs["headers"]["X-TOKEN"] == "secret"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert req.call_args[0] == ("GET",
                                "https://api.ai-ark.com/api/developer-portal/v1/payments/credits")


def test_search_people_body_shape():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(200, {"content": [], "totalElements": 0})
        AiArkClient(api_key="k").search_people(
            contact={"seniority": {"any": {"include": ["founder"]}}},
            page=2, size=50)
    _, kwargs = req.call_args
    assert kwargs["json"] == {
        "page": 2, "size": 50,
        "contact": {"seniority": {"any": {"include": ["founder"]}}},
    }


def test_export_person_404_returns_none():
    from oto.tools.aiark.client import AiArkClient
    with patch("oto.tools.aiark.client.requests.request") as req:
        req.return_value = _resp(404)
        assert AiArkClient(api_key="k").export_person(url="https://lnkd.in/x") is None


def test_export_person_requires_id_or_url():
    from oto.tools.aiark.client import AiArkClient
    with pytest.raises(ValueError):
        AiArkClient(api_key="k").export_person()


def test_mobile_phone_requires_linkedin_or_domain_name():
    from oto.tools.aiark.client import AiArkClient
    with pytest.raises(ValueError):
        AiArkClient(api_key="k").mobile_phone(domain="acme.com")  # name manquant


# --- filtres morts (acceptés par AI Ark, jamais appliqués) --------------------

def test_dead_filter_website_is_refused():
    """`account.website` rendait la base entière (72 M) en la faisant passer pour un
    résultat filtré — vérifié par différentiel le 15/08/2026. Refus, pas avertissement."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import aiark

    with pytest.raises(McpError) as e:
        aiark._reject_dead_filters(
            account={"website": {"any": {"include": ["finecobank.com"]}}}, contact=None)
    assert "domain" in str(e.value)          # l'erreur NOMME le remplaçant


def test_dead_filter_linkedin_url_is_refused():
    """Même pathologie que `website`, mesurée le 02/09/2026 : `account.linkedin_url`
    est avalé et rend `totalElements` 72 508 445 — la base entière servie comme un
    résultat filtré. Rien dans la réponse ne le signale, d'où le refus."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import aiark

    with pytest.raises(McpError) as e:
        aiark._reject_dead_filters(
            account={"linkedin_url": {"any": {"include": [
                "https://linkedin.com/company/clopinette/"]}}}, contact=None)
    assert "domain" in str(e.value)          # l'erreur NOMME le remplaçant


def test_dead_filter_title_is_refused():
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import aiark

    with pytest.raises(McpError):
        aiark._reject_dead_filters(
            account=None, contact={"title": {"any": {"include": ["cmo"]}}})


def test_dead_filter_department_is_refused():
    """Signal #694 (03/09) : `contact.department` est accepté et silencieusement
    ignoré, alors que la description l'annonçait comme supporté.

    Différentiel strict, deux domaines : editeur-a.example seul → 63 enregistrements ;
    + `department: ["human_resources"]` → 63, LES MÊMES dans le MÊME ordre, dont
    aucun RH. Le piège est plus fin que pour `title` : `department.departments` EST
    présent sur chaque enregistrement rendu, donc on voit la donnée et on croit
    pouvoir la filtrer. Coût mesuré : 63 enregistrements facturés pour en garder
    zéro ou un."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import aiark

    with pytest.raises(McpError) as e:
        aiark._reject_dead_filters(
            account=None,
            contact={"department": {"any": {"include": ["human_resources"]}}})
    # Le refus NOMME le remplaçant, comme ses trois jumeaux — sinon il déplace le
    # problème au lieu de le résoudre.
    assert "CLIENT-SIDE" in str(e.value) or "client-side" in str(e.value).lower()


def test_la_description_n_annonce_plus_department_comme_supporte():
    """Le silence n'était que la moitié du défaut : la description citait
    explicitement `department` parmi les champs supportés. Un refus posé sans
    corriger le texte laisserait l'agent tenter puis se faire refuser — le contrat
    doit cesser de le promettre."""
    import inspect
    from oto_mcp.tools import aiark

    src = inspect.getsource(aiark)
    assert "Supports seniority,\n                location, department" not in src
    assert "`title` and `department` are REFUSED" in src


def test_live_filters_pass_through():
    """La garde ne mord QUE sur les clés mortes : `domain` et `seniority` passent."""
    from oto_mcp.tools import aiark

    aiark._reject_dead_filters(
        account={"domain": {"any": {"include": ["finecobank.com"]}}},
        contact={"seniority": {"any": {"include": ["c_suite"]}}})


# --- échec de TRANSPORT : ni un refus, ni une absence (signal #675) ------------
# 2026-09-03, org 196 : l'endpoint EXPORT rend des « Read timed out. (read
# timeout=30) » par rafales — 7 fois sur 4 URLs de profil — pendant que la RECHERCHE
# répond normalement dans les mêmes minutes ; rejouer l'appel IDENTIQUE finit par
# passer. Le coût n'est pas l'échec, c'est sa LECTURE : une procédure qui prend un
# export raté pour un miss écrit `email_status=not_found` sur quelqu'un que personne
# n'a résolu.

_TIMEOUT_REEL = ("HTTPSConnectionPool(host='api.ai-ark.com', port=443): "
                 "Read timed out. (read timeout=30)")


def _appeler_outil(nom, args, *, http):
    """Appelle l'outil TEL QU'IL EST SERVI (registre réel, chaîne complète
    tool → client oto-core → `requests`), la seule couche substituée étant le
    `requests.request` d'oto-core.

    Rend `(structured, exception, mock_http, mock_pause)` — les deux mocks sont
    rendus pour qu'un banc VÉRIFIE sa substitution avant de lire son résultat : un
    patch qui rate ne rougit pas, il verdit.
    """
    from fastmcp import FastMCP
    from oto_mcp import access
    from oto_mcp.tools import aiark

    async def go():
        m = FastMCP("t")
        aiark.register(m)
        outil = next(t for t in await m._list_tools() if t.name == nom)
        with patch("oto.tools.aiark.client.requests.request", side_effect=http) as req, \
             patch("oto_mcp.tools.aiark.time.sleep") as pause, \
             patch.object(access, "resolve_api_key", return_value=("k", False)):
            try:
                res = await outil.run(args)
                return res.structured_content, None, req, pause
            except Exception as e:                      # noqa: BLE001 — c'est l'objet du banc
                return None, e, req, pause

    return asyncio.run(go())


def test_le_timeout_de_transport_est_repris_puis_rendu_RETRYABLE():
    """Le verdict servi à l'agent, mesuré sur la chaîne réelle.

    Avant correction : UNE tentative, puis l'échec emballé en
    `McpError(INVALID_PARAMS)` — que `error_taxonomy.classify` sert
    `code="invalid_input"`, `retryable=false`, c'est-à-dire « corrige ton appel, ne
    réessaie pas », sur un appel qu'AI Ark n'a jamais lu. Le MÊME timeout non emballé
    est classé `upstream_timeout` / `retryable=true` : notre traduction inversait le
    verdict que la plateforme sait déjà rendre.
    """
    from oto_mcp import error_taxonomy
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import aiark

    structured, exc, req, pause = _appeler_outil(
        "linkedin_aiark_person",
        {"op": "export", "url": "https://www.linkedin.com/in/une-personne/"},
        http=requests.exceptions.ReadTimeout(_TIMEOUT_REEL))

    # ① la substitution a bien eu lieu, et la reprise est BORNÉE — sans cette
    #    première assertion, tout ce qui suit peut être du vert obtenu sans rien
    #    exercer (patch mal ciblé = zéro appel = zéro reprise = zéro preuve).
    assert req.call_count == aiark._TRANSPORT_TENTATIVES == 2, (
        f"substitution non exercée ou reprise non bornée : {req.call_count} appel(s)")
    assert pause.call_count == 1, "une pause entre les deux tentatives, pas plus"

    # ② l'échec ne se déguise pas en absence
    assert structured is None and exc is not None
    assert not isinstance(exc, McpError), (
        "emballer le timeout en McpError le rend `retryable: false` — "
        "`classify` traite toute McpError en premier et n'en rend jamais une retryable")

    # ③ ce que l'agent reçoit
    info = error_taxonomy.classify(exc)
    assert info.code == "upstream_timeout"
    assert info.retryable is True


def test_la_reprise_rend_le_profil_quand_la_seconde_tentative_passe():
    """« Rejouer l'appel identique finit par passer » : la reprise doit RÉUSSIR, pas
    seulement mieux échouer. Sans elle, l'agent paie un aller-retour pour ce que le
    handler peut absorber."""
    profil = {"id": "p1", "email": {"output": [{"address": "a@b.co", "status": "valid"}]}}
    structured, exc, req, pause = _appeler_outil(
        "linkedin_aiark_person", {"op": "export", "id": "p1"},
        http=[requests.exceptions.ReadTimeout(_TIMEOUT_REEL), _resp(200, profil)])

    assert req.call_count == 2, "substitution non exercée"
    assert pause.call_count == 1
    assert exc is None
    assert structured == {"found": True, **profil}


def test_une_absence_ne_se_rejoue_pas_et_reste_une_absence():
    """404 = AI Ark a RÉPONDU « je ne connais pas » : un résultat, pas un échec. La
    reprise ne mord que sur le transport — la rejouer facturerait deux fois la même
    absence, et le contrat `{"found": false}` doit survivre au changement."""
    structured, exc, req, pause = _appeler_outil(
        "linkedin_aiark_person", {"op": "export", "url": "https://x.test/in/y"},
        http=[_resp(404)])

    assert req.call_count == 1, "substitution non exercée, ou 404 rejoué"
    assert pause.call_count == 0
    assert exc is None
    assert structured == {"found": False}


def test_une_reponse_d_erreur_reste_emballee_et_actionnable():
    """Un 5xx est une RÉPONSE : AI Ark a lu l'appel. Il garde son message curé (et
    son `retryable: false`), la reprise transport ne doit pas le rejouer — sinon on
    facture deux fois un refus, et le message qui nomme la cause disparaît."""
    from oto_mcp.mcp_errors import McpError

    structured, exc, req, pause = _appeler_outil(
        "linkedin_aiark_person", {"op": "export", "id": "p1"}, http=[_resp(500)])

    assert req.call_count == 1, "substitution non exercée, ou 5xx rejoué"
    assert pause.call_count == 0
    assert isinstance(exc, McpError)
    assert "server error (500)" in str(exc.error.message)


def test_la_description_servie_dit_qu_un_echec_n_est_pas_une_absence():
    """Le message servi sur ce chemin est GÉNÉRIQUE (« Délai d'attente dépassé. ») :
    la taxonomie n'écho pas le nôtre quand elle rend un verdict retryable. Ce que le
    message ne peut pas porter, la description doit le dire — elle, l'agent la relit
    à chaque appel."""
    from fastmcp import FastMCP
    from oto_mcp.tools import aiark

    async def go():
        m = FastMCP("t")
        aiark.register(m)
        return {t.name: (t.description or "") for t in await m._list_tools()}

    d = asyncio.run(go())["linkedin_aiark_person"]
    assert "never record a not-found" in d      # l'instruction, mot pour mot
    assert "retryable: true" in d               # le verdict machine, nommé
    assert "bill a second credit" in d          # le coût de la reprise, assumé

# --- métrage par unité (facturation du partenaire, 21/08) ─────────────────────

def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import aiark

    m = FastMCP("t")
    aiark.register(m)
    return asyncio.run(m.get_tool(name))


def test_search_traces_the_returned_result_count_not_the_requested_size():
    """`tool_calls.quantity` doit refléter ce qu'AI Ark a RENDU (et facture — "BILLS
    CREDITS per returned record"), pas `size` demandé : une dernière page peut en
    rendre moins."""
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.aiark.session_org.note_call_trace") as trace, \
         patch("oto.tools.aiark.client.AiArkClient") as client_cls:
        client_cls.return_value.search_people.return_value = {
            "content": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            "totalElements": 3, "totalPages": 1,
        }
        _tool("linkedin_aiark_search").fn(op="people", size=50)

    trace.assert_called_once_with(quantity=3)


def test_search_companies_also_traces_by_returned_count():
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.aiark.session_org.note_call_trace") as trace, \
         patch("oto.tools.aiark.client.AiArkClient") as client_cls:
        client_cls.return_value.search_companies.return_value = {
            "content": [{"id": "1"}], "totalElements": 1, "totalPages": 1,
        }
        _tool("linkedin_aiark_search").fn(op="companies")

    trace.assert_called_once_with(quantity=1)


def test_search_does_not_trace_when_content_is_missing():
    """Un payload sans `content` liste (forme inattendue) ne doit pas planter la
    trace — juste ne rien poser (le consommateur traite l'absence comme 1)."""
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto_mcp.tools.aiark.session_org.note_call_trace") as trace, \
         patch("oto.tools.aiark.client.AiArkClient") as client_cls:
        client_cls.return_value.search_people.return_value = {"error": "malformed"}
        _tool("linkedin_aiark_search").fn(op="people")

    trace.assert_not_called()


def test_a_bare_domain_list_gets_its_matcher_envelope():
    """#1937: `{"domain": ["example.com"]}` went out bare and AI Ark answered 400. The
    plain-list matchers get `{"any": {"include": [...]}}`; a matcher already given, a
    RANGE or the SMART wrapper pass unchanged."""
    with patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False)), \
         patch("oto.tools.aiark.client.AiArkClient") as client_cls:
        client_cls.return_value.search_people.return_value = {"content": [], "totalElements": 0}
        rng = {"type": "RANGE", "range": [{"start": 1, "end": 50}]}
        _tool("linkedin_aiark_search").fn(
            op="people",
            account={"domain": ["example.com"], "employeeSize": rng,
                     "location": {"any": {"include": ["France"]}}},
            contact={"seniority": "founder"})
        kw = client_cls.return_value.search_people.call_args.kwargs
    assert kw["account"] == {"domain": {"any": {"include": ["example.com"]}},
                             "employeeSize": rng,
                             "location": {"any": {"include": ["France"]}}}
    assert kw["contact"] == {"seniority": {"any": {"include": ["founder"]}}}
