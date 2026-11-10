"""Connecteur Tally — formulaires, questions/blocs, réponses, analytics,
espaces/dossiers, organisation, webhooks (api.tally.so).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Métier), la doc
how-to, la surface MCP (6 tools, chacun avec une description — régression du
piège f-string-docstring), la sonde « tester la connexion », la jointure
tool↔client oto-core (garde version-skew, les 38 méthodes), le dispatch `op=`
(required manquant refusé, arg non pertinent pour CET op refusé), et les trois
comportements que la couche tool AJOUTE au transport :
  1. la jointure questions × réponses (et son refus d'écraser un titre dupliqué),
  2. `dry_run` sur toute mutation, qui n'appelle JAMAIS le client mutant,
  3. le merge de `PATCH /webhooks/{id}`, qui est un remplacement complet.
"""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from oto_mcp.mcp_errors import McpError

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of
from oto_mcp.tools import tally

# Ce fichier confronte le connecteur à la surface d'oto-core : il est NON
# CONCLUANT quand le venv n'exécute pas le tag épinglé — `oto.tools.tally`
# n'existe simplement pas dans un oto-core antérieur à son ajout. Marqué au
# MODULE et pas test par test : contre un mauvais oto-core, les assertions qui
# passent ne valent pas mieux que celles qui échouent. Cf. `tests/_oto_core_pin.py`.
pytestmark = pytest.mark.exige_pin_oto_core

EXPECTED_TOOLS = {"tally_form", "tally_submission", "tally_analytics",
                  "tally_workspace", "tally_account", "tally_webhook"}

#: Les 38 opérations de l'API publique, une méthode de client chacune.
CLIENT_METHODS = (
    "get_me",
    "list_forms", "get_form", "create_form", "update_form", "delete_form",
    "list_questions", "update_question", "get_blocks", "update_blocks",
    "list_submissions", "get_submission", "delete_submission",
    "analytics_metrics", "analytics_visits", "analytics_submissions",
    "analytics_dimensions", "analytics_drop_off",
    "list_workspaces", "get_workspace", "create_workspace", "update_workspace",
    "delete_workspace",
    "list_folders", "create_folder", "update_folder", "delete_folder",
    "list_organization_users", "remove_organization_user", "list_invites",
    "create_invites", "cancel_invite",
    "list_webhooks", "create_webhook", "update_webhook", "delete_webhook",
    "list_webhook_events", "retry_webhook_event",
)


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


@contextmanager
def _mock_client():
    """Enregistre le module avec `TallyClient` mocké, DANS le patch (sinon le
    `from ... import TallyClient` de `register()` capture la vraie classe avant
    que le patch ne s'applique).

    Contextmanager et pas `patcher.start()` nu : une première version laissait
    le patch actif après le test, et les tests SUIVANTS inspectaient un
    MagicMock en croyant lire la vraie classe.
    """
    from fastmcp import FastMCP

    with patch("oto.tools.tally.client.TallyClient") as cls:
        m = FastMCP("t")
        tally.register(m)
        yield m, cls.return_value


def _fn(m, tool_name):
    return asyncio.run(m.get_tool(tool_name)).fn


def _call(tool_name, **kwargs):
    with _mock_client() as (m, inst):
        return _fn(m, tool_name)(**kwargs), inst


# --- registre -----------------------------------------------------------------

def test_tally_is_keyed_byo_only_connector():
    c = providers.REGISTRY["tally"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org"})
    # Pas de clé plateforme, et ce n'est pas un arbitrage de catalogue : une clé
    # Tally est nominative et meurt avec le compte qui l'a créée.
    assert "platform" not in c.auth_modes
    assert c.default_active is False
    assert c.default_quota == 0
    assert "tally" in providers.KEY_PROVIDERS
    assert c.category == "Marketing & SEO"
    assert c.publisher_name == "Tally"
    assert c.label == "Tally"
    assert providers._LOGO_DOMAIN_BY_CONNECTOR["tally"] == "tally.so"


def test_tally_registered_among_keyed_connectors():
    """Appartenance, pas position : deux connecteurs fusionnés le même jour ne
    peuvent pas être « le dernier » tous les deux."""
    assert "tally" in [c.name for c in providers._REGISTRY_LIST if c.keyed]


def test_tally_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["tally"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP ------------------------------------------------------------------

def test_tally_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= set(all_tools)
    assert all(namespace_of(t) == "tally" for t in all_tools if t.startswith("tally_"))


def test_tally_tools_all_have_descriptions(all_tools):
    for name in EXPECTED_TOOLS:
        assert all_tools[name].description, f"{name} has no description"


def test_verify_probe_registered():
    with _mock_client():
        pass
    assert connector_verify.supports("tally")


def test_client_exposes_every_method_the_tools_call():
    """Garde version-skew : les 38 opérations doivent exister au tag oto-core
    épinglé. `test_tools_client_methods_exist` vérifie l'EXISTENCE via son
    walk statique ; ici on l'énonce nommément et exhaustivement."""
    from oto.tools.tally.client import TallyClient
    for meth in CLIENT_METHODS:
        assert callable(getattr(TallyClient, meth, None)), f"TallyClient.{meth} manquant"


def test_full_api_coverage_is_38_operations():
    from oto.tools.tally.client import TallyClient
    public = {n for n in dir(TallyClient)
              if not n.startswith("_") and callable(getattr(TallyClient, n))
              and n not in {"BASE_URL"}}
    assert public == set(CLIENT_METHODS)


# --- le client épingle la version datée de l'API ---------------------------------

def test_client_pins_the_dated_api_version():
    """Une clé Tally est figée à la version du jour de sa création : sans
    en-tête explicite, deux clients de la même org obtiennent des formes de
    réponse DIFFÉRENTES (une clé de 2025 ne rend pas `formattedAnswer`)."""
    from oto.tools.tally.client import DEFAULT_API_VERSION, TallyClient
    c = TallyClient(api_key="tly-x")
    assert c.session.headers["tally-version"] == DEFAULT_API_VERSION
    assert TallyClient(api_key="tly-x", api_version=None).session.headers.get(
        "tally-version") is None


# --- dispatch `op=` ---------------------------------------------------------------

def test_missing_required_arg_is_refused_before_the_network():
    with pytest.raises(McpError):
        _call("tally_submission", op="list")          # form_id manquant
    with pytest.raises(McpError):
        _call("tally_analytics", op="metrics", form_id="f")  # period manquant


def test_irrelevant_arg_for_this_op_is_refused_not_ignored():
    """`tally_form(op="get", limit=5)` rendrait UN formulaire en laissant croire
    que `limit` a borné quelque chose."""
    with pytest.raises(McpError):
        _call("tally_form", op="get", form_id="f", limit=5)
    with pytest.raises(McpError):
        _call("tally_submission", op="get", form_id="f", submission_id="s",
              filter="completed")


def test_unknown_analytics_period_is_refused():
    with pytest.raises(McpError):
        _call("tally_analytics", op="metrics", form_id="f", period="last-tuesday")


# --- 1. la jointure questions × réponses ------------------------------------------

_PAGE = {
    "page": 1, "limit": 50, "hasMore": False,
    "totalNumberOfSubmissionsPerFilter": {"all": 1, "completed": 1, "partial": 0},
    "questions": [
        {"id": "Q1", "type": "INPUT_TEXT", "title": "SIREN"},
        {"id": "Q2", "type": "FILE_UPLOAD", "title": "Export PayFit"},
    ],
    "submissions": [{
        "id": "S1", "formId": "F", "respondentId": "R1", "isCompleted": True,
        "submittedAt": "2026-08-31T10:00:00.000Z",
        "previewUrl": "https://tally.so/p/S1", "pdfUrl": "https://tally.so/pdf/S1",
        "responses": [
            {"questionId": "Q1", "answer": "552100554", "formattedAnswer": "552100554"},
            {"questionId": "Q2", "answer": [{"url": "https://f/x.csv"}]},
        ],
    }],
}


def test_submissions_are_joined_to_their_questions():
    with _mock_client() as (m, inst):
        inst.list_submissions.return_value = _PAGE
        out = _fn(m, "tally_submission")(op="list", form_id="F")
    sub = out["submissions"][0]
    assert sub["pdf_url"] == "https://tally.so/pdf/S1"
    assert sub["respondent_id"] == "R1"
    titles = {a["title"]: a for a in sub["answers"]}
    assert titles["SIREN"]["answer"] == "552100554"
    # une question FILE_UPLOAD répond par l'URL du fichier : c'est par `answers`
    # qu'on atteint les pièces jointes, il n'y a pas d'endpoint de fichiers.
    assert titles["Export PayFit"]["answer"] == [{"url": "https://f/x.csv"}]
    assert sub["answers_by_title"]["SIREN"] == "552100554"
    assert out["counts"] == {"all": 1, "completed": 1, "partial": 0}


def test_duplicate_question_titles_suppress_the_by_title_map():
    """Deux questions du même intitulé : une map par titre en écraserait une.
    On l'omet et on dit lesquelles, plutôt que de perdre une réponse."""
    page = {**_PAGE, "questions": [
        {"id": "Q1", "type": "INPUT_TEXT", "title": "Nom"},
        {"id": "Q2", "type": "INPUT_TEXT", "title": "Nom"},
    ]}
    with _mock_client() as (m, inst):
        inst.list_submissions.return_value = page
        out = _fn(m, "tally_submission")(op="list", form_id="F")
    assert out["title_collisions"] == ["Nom"]
    assert "answers_by_title" not in out["submissions"][0]


def test_raw_returns_tallys_payload_untouched():
    with _mock_client() as (m, inst):
        inst.list_submissions.return_value = _PAGE
        out = _fn(m, "tally_submission")(op="list", form_id="F", raw=True)
    assert out is _PAGE


def test_after_id_is_forwarded_as_the_incremental_cursor():
    _, inst = _call("tally_submission", op="list", form_id="F", after_id="S9")
    assert inst.list_submissions.call_args.kwargs["afterId"] == "S9"


# --- 2. dry_run n'appelle jamais la mutation --------------------------------------

def test_dry_run_delete_submission_previews_and_writes_nothing():
    with _mock_client() as (m, inst):
        inst.get_submission.return_value = _PAGE
        out = _fn(m, "tally_submission")(
            op="delete", form_id="F", submission_id="S1", dry_run=True)
    assert out["dry_run"] is True
    assert out["recoverable"].startswith("no")        # pas de corbeille pour les réponses
    inst.delete_submission.assert_not_called()


def test_dry_run_update_form_returns_a_real_diff():
    with _mock_client() as (m, inst):
        inst.get_form.return_value = {"id": "F", "name": "Ancien", "status": "DRAFT"}
        out = _fn(m, "tally_form")(
            op="update", form_id="F", name="Nouveau", dry_run=True)
    assert out["current_available"] is True
    assert out["changes"] == {"name": {"from": "Ancien", "to": "Nouveau"}}
    inst.update_form.assert_not_called()


def test_dry_run_remove_user_names_the_key_revocation():
    with _mock_client() as (m, inst):
        inst.list_organization_users.return_value = [{"id": "U1", "email": "a@b.c"}]
        out = _fn(m, "tally_account")(
            op="remove_user", organization_id="O", user_id="U1", dry_run=True)
    assert out["current"]["email"] == "a@b.c"
    assert "API keys" in out["warning"]
    inst.remove_organization_user.assert_not_called()


def test_dry_run_retry_warns_it_is_a_real_delivery():
    with _mock_client() as (m, inst):
        out = _fn(m, "tally_webhook")(
            op="retry", webhook_id="W", event_id="E", dry_run=True)
    assert out["would_retry"] == "E"
    inst.retry_webhook_event.assert_not_called()


def test_update_blocks_dry_run_says_it_replaces_everything():
    with _mock_client() as (m, inst):
        inst.get_blocks.return_value = [{"uuid": "a"}, {"uuid": "b"}, {"uuid": "c"}]
        out = _fn(m, "tally_form")(
            op="update_blocks", form_id="F", blocks=[{"uuid": "a"}], dry_run=True)
    assert out["blocks_before"] == 3 and out["blocks_after"] == 1
    inst.update_blocks.assert_not_called()


# --- 3. le merge du PATCH webhook (remplacement complet) --------------------------

_WEBHOOK = {"id": "W", "formId": "F", "url": "https://old", "isEnabled": True,
            "eventTypes": ["FORM_RESPONSE"]}


def test_webhook_update_merges_instead_of_clearing_omitted_fields():
    """`PATCH /webhooks/{id}` exige formId, url, eventTypes ET isEnabled : un
    `op="update"` qui ne passe que `is_enabled=False` ne doit pas effacer l'URL."""
    with _mock_client() as (m, inst):
        inst.list_webhooks.return_value = {"webhooks": [_WEBHOOK], "hasMore": False}
        _fn(m, "tally_webhook")(
            op="update", webhook_id="W", is_enabled=False)
    args = inst.update_webhook.call_args.args
    assert args[0] == "W"
    assert args[1] == "F"                    # formId repris de l'état actuel
    assert args[2] == "https://old"          # url NON effacée
    assert args[3] == ["FORM_RESPONSE"]
    assert args[4] is False                  # le seul champ demandé


def test_webhook_update_refuses_when_current_state_is_unreadable():
    """Sans état actuel, une modification partielle effacerait les champs non
    fournis — on refuse au lieu d'écrire un webhook mutilé."""
    with _mock_client() as (m, inst):
        inst.list_webhooks.return_value = {"webhooks": [], "hasMore": False}
        with pytest.raises(McpError):
            _fn(m, "tally_webhook")(
                op="update", webhook_id="ABSENT", is_enabled=False)


def test_webhook_create_does_not_echo_the_signing_secret():
    with _mock_client() as (m, inst):
        out = _fn(m, "tally_webhook")(
            op="create", form_id="F", url="https://x", event_types=["FORM_RESPONSE"],
            signing_secret="s3cr3t", http_headers=[{"name": "X-Key", "value": "v"}],
            dry_run=True)
    assert out["signed"] is True
    assert "s3cr3t" not in str(out)
    assert out["header_names"] == ["X-Key"]   # les noms, jamais les valeurs


def test_single_submission_envelope_is_singular_not_plural():
    """`GET /forms/{f}/submissions/{s}` rend `{questions, submission}` au
    SINGULIER, là où la liste rend `submissions`. Ne lire que le pluriel
    rendait la lecture d'UNE réponse silencieusement vide."""
    single = {"questions": _PAGE["questions"], "submission": _PAGE["submissions"][0]}
    with _mock_client() as (m, inst):
        inst.get_submission.return_value = single
        out = _fn(m, "tally_submission")(
            op="get", form_id="F", submission_id="S1")
    assert len(out["submissions"]) == 1
    assert out["submissions"][0]["answers_by_title"]["SIREN"] == "552100554"


# --- un 401 de Tally ne veut PAS dire « clé invalide » (vécu en live) -------------

def test_ambiguous_401_does_not_blame_the_key():
    """Live 2026-08-31, compte FREE : `GET /webhooks` rend 401 tant qu'aucun
    webhook n'a jamais été créé (puis 200, même après en avoir supprimé tous),
    et `GET /forms/{id}/blocks` rend 401 sur ce plan. Dire « clé rejetée »
    envoie l'utilisateur retourner une clé parfaitement saine."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.tools.tally import _upstream_message

    e = UpstreamHTTPError(401, {"message": "..."}, service="tally")
    for ctx in ("webhook_list", "blocks", "workspace_write", "question_write", None):
        msg = _upstream_message(e, ctx)
        # même SANS contexte : on ne suppose jamais que la clé est en cause,
        # parce que Tally rend 401 pour un gate de plan aussi souvent.
        assert "does NOT mean" in msg
        assert 'tally_account(op="me")' in msg
    assert "does not open" in _upstream_message(e, "workspace_write")
