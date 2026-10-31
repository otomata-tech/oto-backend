"""Connecteur Apify — actors de scraping hébergés (api.apify.com).

Verrouille : l'entrée de registre (keyed byo-only, catégorie Prospection), la
surface MCP sous le namespace `apify`, la jointure tool↔client oto-core (garde
version-skew), la sonde « tester la connexion », et les deux endroits où le module
protège l'utilisateur : le 408 du mode synchrone doit ORIENTER vers le mode
asynchrone, et les garde-fous de coût doivent atteindre le client.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from oto_mcp import providers
from oto_mcp.connectors import verify as connector_verify
from oto_mcp.tool_visibility import namespace_of

EXPECTED_TOOLS = {
    "apify_store_search", "apify_actors", "apify_actor",
    "apify_run_sync", "apify_run", "apify_run_status", "apify_abort_run",
    "apify_dataset_items",
}


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


def _tool(name):
    from fastmcp import FastMCP
    from oto_mcp.tools import apify

    m = FastMCP("t")
    apify.register(m)
    return asyncio.run(m.get_tool(name))


# --- registre -----------------------------------------------------------------

def test_apify_is_keyed_connector_platform_grant_only():
    c = providers.REGISTRY["apify"]
    assert c.kind == "tools"
    assert c.keyed and c.secret_kind == "api_key"
    assert c.auth_modes == frozenset({"byo_user", "byo_org", "platform"})
    # platform sur grant explicite (revente au crédit) : jamais en libre-service.
    assert c.platform_key_open is False
    assert c.default_quota == 0
    assert "apify" in providers.KEY_PROVIDERS
    assert c.category == "Prospecting"
    assert c.publisher_name == "Apify"


def test_apify_has_onboarding_doc():
    kinds = {s.kind for s in providers.REGISTRY["apify"].doc_sections}
    assert {"prerequisite", "usage"} <= kinds


# --- surface MCP --------------------------------------------------------------

def test_apify_tools_register_under_namespace(all_tools):
    assert EXPECTED_TOOLS <= all_tools
    assert all(namespace_of(t) == "apify"
               for t in all_tools if t.startswith("apify_"))


def test_verify_probe_registered(all_tools):
    # La sonde s'enregistre dans `register(mcp)` — au boot, `_build_mcp` → `register_all`.
    # `all_tools` rejoue ce chemin : sans lui, ce test ne passait que si un AUTRE test du
    # même processus l'avait déjà fait (rouge isolé, révélé par la suite en parts, #1111).
    assert connector_verify.supports("apify")


# --- jointure tool ↔ client oto-core (garde version-skew) ---------------------

def test_client_exposes_methods_called_by_tools():
    from oto.tools.apify.client import ApifyClient
    for meth in ("store_search", "actors", "actor",
                 "run", "run_status", "abort_run", "dataset_items"):
        assert callable(getattr(ApifyClient, meth, None)), f"ApifyClient.{meth} manquant"


# --- garde-fous de coût & orientation sur 408 ---------------------------------

@pytest.fixture(autouse=True)
def _sans_base():
    """Ces tests ne parlent pas à la base : le suivi des lancements a son fichier
    (`test_apify_spend_control.py`), et son repli journalise."""
    with patch("oto_mcp.tools.apify._org_de_lancement", return_value=(False, None)), \
            patch("oto_mcp.tools.apify._plafond_par_defaut", return_value=1.0):
        yield


def _with_fake_client():
    key = patch("oto_mcp.access.resolve_api_key", return_value=("fake-key", False))
    cls = patch("oto.tools.apify.client.ApifyClient")
    return key, cls


def test_run_sync_forwards_cost_guards():
    key, cls = _with_fake_client()
    with key, cls as client_cls:
        inst = client_cls.return_value
        inst.run.return_value = {"data": {"id": "r1", "status": "SUCCEEDED",
                                          "usageTotalUsd": 0.01,
                                          "defaultDatasetId": "ds1"}}
        inst.dataset_items.return_value = []
        _tool("apify_run_sync").fn(
            actor_id="apify/google-maps-scraper",
            run_input={"searchStringsArray": ["boulangerie Marseille"]},
            max_items=20, max_total_charge_usd=1.5,
        )

        kwargs = inst.run.call_args.kwargs
        assert kwargs["max_items"] == 20
        assert kwargs["max_total_charge_usd"] == 1.5
        assert kwargs["wait_for_finish"] == 60, "Apify plafonne waitForFinish à 60 s"


def test_sync_timeout_points_to_the_async_path():
    """Un 408 amont (ex. au lancement) garde son orientation vers le mode asynchrone."""
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls:
        client_cls.return_value.run.side_effect = UpstreamHTTPError(
            408, {"error": "timeout"}, service="apify")
        with pytest.raises(McpError, match="apify_run_status"):
            _tool("apify_run_sync").fn(actor_id="apify/x")


# --- la ligne FACTURÉE : le coût réel d'un run TERMINÉ, une fois par run -------
#
# `apify_run_status` est le seul outil qui lit le coût d'un run (`usageTotalUsd`).
# Il le trace en micro-dollars (unité de Jev) avec le run pour JOB de la ligne
# (`apify_run_id`, servi `job_id` par la lentille) — et SEULEMENT quand le run est
# dans un état terminal : avant, le chiffre est partiel, et un facturier qui garde
# le premier chiffre vu pour un job garderait celui-là.

RUN = "HG7ML7M8z78YcAPEB"   # la FORME d'un id de run Apify, fictif


def _statut(reponse, run_id=RUN, **kwargs):
    """Appelle `apify_run_status` sur une réponse amont donnée ; rend
    `(sortie, traces)` — `traces` = chaque `note_call_trace` de l'appel."""
    key, cls = _with_fake_client()
    with key, cls as client_cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
        client_cls.return_value.run_status.return_value = reponse
        out = _tool("apify_run_status").fn(run_id=run_id, **kwargs)
    return out, [c.kwargs for c in trace.call_args_list]


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"])
def test_a_finished_run_traces_its_cost_in_microdollars_and_its_run(status):
    """Un run en échec, coupé ou arrêté a coûté aussi : les quatre états terminaux."""
    reponse = {"data": {"id": RUN, "status": status, "usageTotalUsd": 0.0421,
                        "defaultDatasetId": "ds1"}}
    out, traces = _statut(reponse)
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]
    assert out == reponse, "la réponse est servie telle quelle"


@pytest.mark.parametrize("status", ["READY", "RUNNING", "TIMING-OUT", "ABORTING",
                                    "", "succeeded"])
def test_a_run_that_is_not_finished_traces_a_free_poll_without_its_job(status):
    """Le coût n'est pas final : la consultation trace 0 (gratuite, jamais NULL que le
    registre lirait 1) et SANS le run — un facturier qui garde le premier chiffre d'un
    job garderait sinon un partiel. `TIMING-OUT` et `ABORTING` sont des états de
    TRANSITION, pas des états terminaux."""
    _, traces = _statut({"data": {"id": RUN, "status": status, "usageTotalUsd": 0.5}})
    assert traces == [{"quantity": 0}]


@pytest.mark.parametrize("run", [
    {"id": RUN, "status": "SUCCEEDED"},                               # champ absent
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": None},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": "0.04"},      # pas un nombre
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": True},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": -0.01},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": float("nan")},
    {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": float("inf")},
])
def test_a_finished_run_without_a_readable_cost_traces_0_and_says_so(run, caplog):
    """Un chiffre absent n'est pas un zéro facturé, un chiffre deviné serait facturé,
    et un chiffre muet serait perdu : 0 sans le run, et un avertissement qui nomme le
    run et le TYPE reçu — jamais la valeur."""
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces = _statut({"data": run})
    assert traces == [{"quantity": 0}]
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


@pytest.mark.parametrize("status", [None, ["SUCCEEDED"], 3])
def test_an_unreadable_status_traces_0_and_says_so(status, caplog):
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces = _statut({"data": {"id": RUN, "status": status, "usageTotalUsd": 0.5}})
    assert traces == [{"quantity": 0}]
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


@pytest.mark.parametrize("usd,micro", [
    (0, 0),                    # mesuré, rien à facturer : un 0 TRACÉ
    (0.0, 0),
    (1, 1_000_000),
    (0.000001, 1),
    (0.0000004, 1),            # arrondi au micro-dollar SUPÉRIEUR, comme Jev
    (3 * 1e-05, 30),           # 3.0000000000000004e-05 : pas 31
    (12.345678, 12_345_678),
])
def test_the_cost_is_rounded_up_to_the_microdollar_like_jev(usd, micro):
    from oto_mcp.tools import jev

    _, traces = _statut({"data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": usd}})
    assert traces == [{"quantity": micro, "apify_run_id": RUN}]
    assert micro == jev._microdollars(usd), "même unité et même arrondi que Jev"


def test_the_job_is_the_run_apify_names_else_the_one_that_was_asked():
    """L'id de la RÉPONSE fait foi ; sans lui, celui de l'appel — jamais une ligne
    facturable sans job, que le facturier ne saurait pas compter une seule fois."""
    fini = {"status": "SUCCEEDED", "usageTotalUsd": 0.01}
    _, traces = _statut({"data": {**fini, "id": RUN}}, run_id="  autre  ")
    assert traces == [{"quantity": 10000, "apify_run_id": RUN}]
    _, traces = _statut({"data": dict(fini)}, run_id=RUN)
    assert traces == [{"quantity": 10000, "apify_run_id": RUN}]


def test_a_flat_answer_is_read_like_the_enveloped_one():
    """Le client oto-core rend l'enveloppe `{data: {…}}` de l'API v2 ; un corps à plat
    se lit pareil plutôt que de cesser de facturer en silence."""
    _, traces = _statut({"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.002})
    assert traces == [{"quantity": 2000, "apify_run_id": RUN}]


@pytest.mark.parametrize("reponse", [{}, {"data": None}, {"data": []}, [], None])
def test_an_unreadable_answer_traces_0_says_so_and_does_not_break_the_call(reponse, caplog):
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        out, traces = _statut(reponse)
    assert traces == [{"quantity": 0}] and out == reponse
    assert len(caplog.records) == 1 and RUN in caplog.records[0].getMessage()


def test_a_refused_status_call_traces_nothing():
    from oto_mcp.mcp_errors import McpError
    from oto.tools.common.errors import UpstreamHTTPError

    key, cls = _with_fake_client()
    with key, cls as client_cls, \
            patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
        client_cls.return_value.run_status.side_effect = UpstreamHTTPError(
            404, {"error": "not found"}, service="apify")
        with pytest.raises(McpError):
            _tool("apify_run_status").fn(run_id=RUN)
    trace.assert_not_called()


def test_every_other_apify_tool_traces_a_free_call():
    """Lancer, arrêter, lire : chacun trace 0 — jamais NULL, que le registre lirait 1 —
    même quand l'amont rend un run TERMINÉ avec son coût (`apify_run` avec
    `wait_for_finish`, `apify_abort_run`). Le coût d'un run se relève par
    `apify_run_status` — et par `apify_run_sync` pour le run qu'il a lui-même mené à
    terme (tests plus bas)."""
    fini = {"data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.5,
                     "defaultDatasetId": "ds1"}}
    appels = {
        "apify_run": ("run", {"actor_id": "apify/x", "wait_for_finish": 60}),
        "apify_abort_run": ("abort_run", {"run_id": RUN}),
        "apify_dataset_items": ("dataset_items", {"dataset_id": "ds1"}),
        "apify_actor": ("actor", {"actor_id": "apify/x"}),
        "apify_actors": ("actors", {}),
        "apify_store_search": ("store_search", {"search": "maps"}),
    }
    assert set(appels) == EXPECTED_TOOLS - {"apify_run_status", "apify_run_sync"}
    for outil, (methode, kwargs) in appels.items():
        key, cls = _with_fake_client()
        with key, cls as client_cls, \
                patch("oto_mcp.tools.apify.session_org.note_call_trace") as trace:
            getattr(client_cls.return_value, methode).return_value = fini
            _tool(outil).fn(**kwargs)
        assert [c.kwargs for c in trace.call_args_list] == [{"quantity": 0}], outil


# --- apify_run_sync : lancer, attendre par tranches, relever le coût du run --------
#
# Le point d'entrée synchrone d'Apify ne rend que les éléments (ni id de run ni coût) :
# un run passé par lui n'était JAMAIS facturé. L'outil lance donc le run, l'attend par
# tranches de 60 s (plafond de `waitForFinish`) dans 300 s au total, lit son dataset, et
# trace le coût du run TERMINÉ comme `apify_run_status` — même unité, même job.

def _sync(run_reponse, statuts=(), items=None, horloge=None, **kwargs):
    """Appelle `apify_run_sync` : `run` rend `run_reponse`, chaque `run_status` le
    statut suivant de `statuts` (une exception y est levée), `dataset_items` rend
    `items` (ou la lève si c'en est une). Rend `(sortie | exception, traces, client)`."""
    from oto_mcp.mcp_errors import McpError

    key, cls = _with_fake_client()
    with ExitStack() as pile:
        pile.enter_context(key)
        inst = pile.enter_context(cls).return_value
        trace = pile.enter_context(
            patch("oto_mcp.tools.apify.session_org.note_call_trace"))
        if horloge is not None:
            pile.enter_context(
                patch("oto_mcp.tools.apify._maintenant", side_effect=horloge))
        inst.run.return_value = run_reponse
        inst.run_status.side_effect = list(statuts)
        if isinstance(items, Exception):
            inst.dataset_items.side_effect = items
        else:
            inst.dataset_items.return_value = [{"a": 1}] if items is None else items
        try:
            out = _tool("apify_run_sync").fn(actor_id="apify/x", **kwargs)
        except McpError as e:
            out = e
    return out, [c.kwargs for c in trace.call_args_list], inst


def _r(status, usd=0.0421):
    return {"data": {"id": RUN, "status": status, "usageTotalUsd": usd,
                     "defaultDatasetId": "ds1"}}


def test_run_sync_finished_at_once_returns_the_items_and_bills_the_run():
    out, traces, inst = _sync(_r("SUCCEEDED"), limit=5, fields=["title"])
    assert out == [{"a": 1}], "même forme qu'avant : la LISTE des éléments"
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]
    inst.run_status.assert_not_called()
    inst.dataset_items.assert_called_once_with("ds1", limit=5, fields=["title"])


def test_run_sync_waits_by_slices_until_the_run_finishes_then_bills_once():
    out, traces, inst = _sync(_r("RUNNING", 0.01),
                              statuts=[_r("RUNNING", 0.02), _r("SUCCEEDED", 0.03)],
                              horloge=[0, 70, 140])
    assert out == [{"a": 1}]
    assert traces == [{"quantity": 30000, "apify_run_id": RUN}], \
        "seul le coût FINAL est tracé, une fois — jamais un partiel"
    waits = [c.kwargs["wait_for_finish"] for c in inst.run_status.call_args_list]
    assert waits == [60, 60]


def test_run_sync_never_waits_past_300_s_in_all():
    _, _, inst = _sync(_r("RUNNING"), statuts=[_r("SUCCEEDED")], horloge=[0, 270])
    assert inst.run_status.call_args.kwargs["wait_for_finish"] == 30


def test_run_sync_out_of_budget_names_the_run_bills_nothing_and_aborts_nothing():
    """Le run continue chez Apify : l'erreur le nomme pour `apify_run_status`, qui
    relèvera son coût une fois terminé — rien n'est tracé ici, donc rien deux fois."""
    from oto_mcp.mcp_errors import McpError

    out, traces, inst = _sync(_r("RUNNING"), statuts=[_r("RUNNING")] * 5,
                              horloge=[0, 100, 200, 299.5])
    assert isinstance(out, McpError)
    assert RUN in str(out) and "apify_run_status" in str(out)
    assert traces == [{"quantity": 0}]
    inst.abort_run.assert_not_called()
    inst.dataset_items.assert_not_called()


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED"])
def test_run_sync_on_a_run_that_did_not_succeed_is_billed_and_answered(status):
    """Un run en échec a coûté, et une erreur n'est pas un appel facturé : il est
    relevé ICI et RENDU (pas levé), avec ce qu'il a produit et son run id."""
    out, traces, inst = _sync(_r(status), items=[{"partiel": 1}])
    assert out["run_id"] == RUN and out["status"] == status
    assert RUN in out["error"] and "apify_run_status" in out["error"]
    assert out["items"] == [{"partiel": 1}]
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]
    inst.dataset_items.assert_called_once()


def _ppe(usd, comptes, reserves):
    """Un run pay-per-event : `usageTotalUsd` ne compte que les événements RÉSERVÉS."""
    run = _r("SUCCEEDED", usd=usd)
    run["data"].update({
        "chargedEventCounts": comptes,
        "accountedChargedEventCounts": reserves,
        "pricingInfo": {"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {
            "actorChargeEvents": {
                "place-scraped": {"eventPriceUsd": 0.003},
                "filter-applied": {"eventPriceUsd": 0.001},
                "apify-actor-start": {"eventPriceUsd": 0.00005}}}}})
    return run


def test_events_counted_but_not_yet_booked_are_billed_at_their_price():
    """Mesuré le 14/10/2026 (run Dlvmeb19jTs5K0k1a, Google Maps, 3 lieux) : $0.0002 à
    SUCCEEDED (le démarrage seul), $0.0122 neuf secondes plus tard. Le facturier garde
    le PREMIER chiffre d'un job : l'écart se chiffre ici, au prix de l'acteur."""
    tot = {"place-scraped": 3, "filter-applied": 3, "apify-actor-start": 4}
    tot_0 = {"place-scraped": 0, "filter-applied": 0, "apify-actor-start": 4}
    _, avant, _ = _sync(_ppe(0.0002, tot, tot_0))
    _, apres, _ = _sync(_ppe(0.0122, tot, tot))
    assert avant == apres == [{"quantity": 12200, "apify_run_id": RUN}]


def test_a_run_without_event_counts_bills_its_usage_total():
    """Un acteur payé à l'usage (pas d'événements) : `usageTotalUsd` seul, inchangé."""
    _, traces, _ = _sync(_r("SUCCEEDED", usd=0.0421))
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]


def test_without_booked_counts_the_gap_is_not_guessed_and_it_is_said(caplog):
    """Sans `accountedChargedEventCounts`, rien ne dit ce que `usageTotalUsd` contient
    déjà : ajouter tous les événements comptés pourrait les facturer deux fois. Le
    chiffre est `usageTotalUsd` seul, et le journal dit que l'écart n'est pas chiffré."""
    run = _ppe(0.0122, {"place-scraped": 3, "filter-applied": 3}, None)
    del run["data"]["accountedChargedEventCounts"]
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces, _ = _sync(run)
    assert traces == [{"quantity": 12200, "apify_run_id": RUN}], "pas 0.0244 : aucun doublon"
    assert len(caplog.records) == 1
    assert RUN in caplog.records[0].getMessage()
    assert "apify_run_sync" in caplog.records[0].getMessage()


def test_an_event_without_a_readable_price_is_left_out_and_it_is_said(caplog):
    run = _ppe(0.0002, {"place-scraped": 3, "mystery-event": 5},
               {"place-scraped": 0, "mystery-event": 0})
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _, traces, _ = _sync(run)
    # 0.0002 + 3 × 0.003 ; « mystery-event » n'a pas de prix : laissé hors du chiffre, DIT.
    assert traces == [{"quantity": 9200, "apify_run_id": RUN}]
    assert len(caplog.records) == 1
    assert "mystery-event" in caplog.records[0].getMessage()
    assert RUN in caplog.records[0].getMessage()


def test_run_sync_bills_a_finished_run_even_when_its_output_cannot_be_read():
    """Le run est terminé, donc il a coûté : il est relevé AVANT la lecture du
    dataset, et l'échec de lecture est RENDU (seul un appel répondu est facturé),
    avec le run et le dataset pour la reprise — jamais un second lancement."""
    from oto.tools.common.errors import UpstreamHTTPError

    erreur = UpstreamHTTPError(503, "unavailable", service="apify")
    out, traces, inst = _sync(_r("SUCCEEDED"), items=erreur)
    assert traces == [{"quantity": 42100, "apify_run_id": RUN}]
    assert out["run_id"] == RUN and out["status"] == "SUCCEEDED"
    assert out["dataset_id"] == "ds1" and out["items"] is None
    assert RUN in out["error"] and "apify_dataset_items" in out["error"]


def test_run_sync_names_the_run_when_waiting_for_it_fails():
    """Une panne PENDANT l'attente : le run existe et tourne encore — l'erreur le
    nomme pour `apify_run_status`, rien n'est facturé ici (son coût n'est pas final)."""
    from oto.tools.common.errors import UpstreamHTTPError
    from oto_mcp.mcp_errors import McpError

    erreur = UpstreamHTTPError(503, "unavailable", service="apify")
    out, traces, inst = _sync(_r("RUNNING"), statuts=[erreur], horloge=[0, 10])
    assert isinstance(out, McpError)
    assert RUN in str(out) and "apify_run_status" in str(out)
    assert traces == []
    inst.dataset_items.assert_not_called()


def test_a_cost_warning_names_the_tool_that_read_it(caplog):
    run = _r("SUCCEEDED", usd=None)
    with caplog.at_level("WARNING", logger="oto_mcp.tools.apify"):
        _sync(run)
        _statut(run)
    noms = [r.getMessage().split(" ")[0] for r in caplog.records]
    assert noms == ["apify_run_sync", "apify_run_status"]


@pytest.mark.parametrize("reponse", [{}, {"data": {"status": "READY"}}, None])
def test_run_sync_without_a_run_id_fails_loudly(reponse):
    from oto_mcp.mcp_errors import McpError

    out, _, _ = _sync(reponse)
    assert isinstance(out, McpError) and "run id" in str(out)


def test_the_run_reaches_the_journal_as_the_job_of_the_billed_line():
    """Bout en bout sur le VRAI relevé (pas un mock) : ce que l'outil consigne passe la
    liste fermée des args journalisés et la lentille le lit sous ce nom comme `job_id` ;
    l'argument `run_id`, lui, n'est PAS un nom de job — d'autres outils le portent pour
    autre chose (`run_finish`). Clé qui facture : gardée sur la ligne CIBLE d'un
    `oto_call`, jamais sur l'enveloppe."""
    from oto_mcp import server, session_org
    from oto_mcp.calllog import apply_call_trace, truncated_args
    from oto_mcp.db import usage as dbu
    from oto_mcp.tools import meta

    assert "apify_run_id" in server._TRACED_ARGS
    assert "apify_run_id" in dbu.BILLABLE_JOB_ARGS
    assert "apify_run_id" in meta._BILLING_TRACE_KEYS
    assert "run_id" not in dbu.BILLABLE_JOB_ARGS and "job_id" not in dbu.BILLABLE_JOB_ARGS

    key, cls = _with_fake_client()
    releve: dict = {}
    tok = session_org.set_call_trace(releve)
    try:
        with key, cls as client_cls:
            client_cls.return_value.run_status.return_value = {
                "data": {"id": RUN, "status": "SUCCEEDED", "usageTotalUsd": 0.0421}}
            _tool("apify_run_status").fn(run_id=RUN)
    finally:
        session_org.reset_call_trace(tok)

    row = apply_call_trace(
        {"args": truncated_args({"run_id": RUN}, tool="apify_run_status")},
        releve, server._TRACED_ARGS)
    assert row["quantity"] == 42100
    assert row["args"] == {"run_id": RUN, "apify_run_id": RUN}
