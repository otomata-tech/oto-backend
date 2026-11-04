"""The Higgsfield platform key: closed, quota-bound, and billed only for a job that completes.

Higgsfield charges a job once it COMPLETES (failed, nsfw, canceled: free), and a job's
status carries no price. What this file locks:
- the shared key is never served without a grant, and the grant's quota is checked for
  the job's PRICE (micro-dollars) before anything is sent — the real resolution path;
- a submission on the shared key notes its price as `units`, records the job, and traces
  NO `quantity`; on the customer's own key nothing is recorded;
- on the shared key, reading, canceling or deleting a job is refused unless the caller's
  org submitted it — absent or another org's, the same `job_not_found`, before anything
  is sent; an unreadable record raises;
- the read of a COMPLETED job by the org that submitted it traces the recorded price with
  the job; any other read traces 0 (a job failed or still running, the customer's own
  key);
- a submission the table could not record is an ERROR (Sentry), never a warning;
- the settlement bills a completed job nobody read under the org that submitted it, with
  the line the read would have written, and closes the others at no cost.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from oto_mcp import access as _access
from oto_mcp import session_org
from oto_mcp.mcp_errors import McpError

#: Read on the facade at import, before any fixture stubs it.
_VRAIE_RESOLUTION = _access.resolve_api_key

_RID = "d7e6c0f3-6699-4f6c-bb45-2ad7fd9158ff"
_KLING = "kling-video/v3.0/pro/image-to-video"
_PARAMS = {"prompt": "a lamp", "image_url": "https://x.test/a.png", "duration": 5}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("OTO_HIGGSFIELD_MAX_USD_PAR_APPEL", raising=False)
    inst, built = MagicMock(), []

    def fabrique(**kw):
        built.append(kw)
        return inst

    monkeypatch.setattr("oto.tools.higgsfield.HiggsfieldClient", fabrique)
    inst.built = built
    inst.estimate.return_value = {"usd": "0.56", "credits": 56}
    inst.generate.return_value = {"request_id": _RID, "status": "queued"}
    return inst


@pytest.fixture
def releve():
    """A real call record, as the edge opens one: what the resolver and the tool note."""
    holder: dict = {}
    jeton = session_org.set_call_trace(holder)
    yield holder
    session_org.reset_call_trace(jeton)


@pytest.fixture
def cle(monkeypatch, releve):
    """The resolver stubbed: `mode` says which key serves, `units` what it was asked."""
    etat = {"mode": "platform", "units": []}

    def resoudre(provider, account=None, units=1):
        assert provider == "higgsfield"
        etat["units"].append(units)
        session_org.note_call_trace(key_mode=etat["mode"])
        return ("PLAT" if etat["mode"] == "platform" else "OWN"), etat["mode"] == "platform"

    monkeypatch.setattr("oto_mcp.access.resolve_api_key", resoudre)
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "s")
    monkeypatch.setattr(_access, "current_org", lambda sub: 7)
    monkeypatch.setattr(session_org, "current_call_run", lambda: "run-1")
    return etat


def _tool(name: str):
    from fastmcp import FastMCP
    from oto_mcp.tools import higgsfield as H
    m = FastMCP("t")
    H.register(m)
    return asyncio.run(m.get_tool(name)).fn


def _soumettre():
    return _tool("higgsfield_act")(op="create_generation", model=_KLING, params=_PARAMS,
                                   max_usd=1.0)


# --- the submission ------------------------------------------------------------------

def test_on_the_shared_key_the_quota_is_checked_for_the_price_and_the_job_recorded(
        client, cle, releve):
    with patch("oto_mcp.db.higgsfield_jobs.noter") as noter:
        out = _soumettre()
    assert out["request_id"] == _RID
    assert cle["units"][-1] == 560000, "the second resolution asks the quota for the price"
    assert releve["units"] == 560000
    assert "quantity" not in releve, "a submission bills nothing: only completion does"
    noter.assert_called_once_with(_RID, org_id=7, sub="s", run_id="run-1", model=_KLING,
                                  prix_microdollars=560000, approximatif=False)


def test_on_the_customers_own_key_nothing_is_recorded_nor_noted(client, cle, releve):
    cle["mode"] = "org"
    with patch("oto_mcp.db.higgsfield_jobs.noter") as noter:
        _soumettre()
    noter.assert_not_called()
    assert "units" not in releve and "quantity" not in releve


def test_a_missed_record_never_fails_the_submission_but_is_an_error(client, cle, caplog):
    """The job runs on our balance and will never be billed: an ERROR, which the
    logging integration sends to Sentry — a warning would stay in the journal."""
    with caplog.at_level("WARNING", logger="oto_mcp.tools.higgsfield"), \
            patch("oto_mcp.db.higgsfield_jobs.noter", side_effect=RuntimeError("base")):
        assert _soumettre()["request_id"] == _RID
    niveaux = {r.levelname for r in caplog.records if _RID in r.getMessage()}
    assert niveaux == {"ERROR"}


def test_a_job_back_without_a_request_id_is_an_error(client, cle, caplog):
    client.generate.return_value = {"status": "queued"}
    with caplog.at_level("WARNING", logger="oto_mcp.tools.higgsfield"), \
            patch("oto_mcp.db.higgsfield_jobs.noter") as noter:
        _soumettre()
    noter.assert_not_called()
    assert [r.levelname for r in caplog.records
            if "without a request_id" in r.getMessage()] == ["ERROR"]


def test_an_approximate_price_is_served_and_recorded_as_such(client, cle):
    client.estimate.return_value = {"type": "description", "pricing_description": (
        "For 16:9 video without video input, your request costs roughly $0.2056 per second "
        "of generated video at 480p, $0.4622 at 720p, and $1.1372 at 1080p.")}
    with patch("oto_mcp.db.higgsfield_jobs.noter") as noter:
        _tool("higgsfield_act")(op="create_generation", model="bytedance/seedance-2.5/x",
                                params={"prompt": "p", "duration": 5, "resolution": "720p"},
                                max_usd=3.0)
    assert noter.call_args.kwargs["approximatif"] is True
    assert noter.call_args.kwargs["prix_microdollars"] == 2311000


def test_the_price_is_rounded_up_to_the_micro_dollar():
    from oto_mcp.tools.higgsfield import microdollars
    assert microdollars(0.004) == 4000
    assert microdollars(0.1 + 0.2) == 300000, "float noise is not billed"
    assert microdollars(0.0000001) == 1


# --- the read --------------------------------------------------------------------------

def _lire(client, statut, ligne, org_courante=7, panne=None):
    client.get_request.return_value = {"status": statut, "request_id": _RID}
    with patch("oto_mcp.db.higgsfield_jobs.lire", return_value=ligne,
               side_effect=panne) as lire, \
            patch("oto_mcp.db.higgsfield_jobs.marquer") as marquer, \
            patch.object(_access, "current_org", return_value=org_courante):
        _tool("higgsfield")(op="get_request", request_id=_RID)
    return lire, marquer


_LIGNE = {"request_id": _RID, "org_id": 7, "prix_microdollars": 560000}


def test_a_completed_job_read_by_its_org_traces_its_price_with_the_job(client, cle, releve):
    _, marquer = _lire(client, "completed", _LIGNE)
    assert releve["quantity"] == 560000 and releve["higgsfield_request_id"] == _RID
    assert releve["units"] == 0, "a read spends none of the quota"
    marquer.assert_called_once_with(_RID, statut="completed")


@pytest.mark.parametrize("statut", ["failed", "nsfw", "canceled"])
def test_a_job_higgsfield_does_not_charge_closes_at_no_cost(client, cle, releve, statut):
    _, marquer = _lire(client, statut, _LIGNE)
    assert releve["quantity"] == 0 and "higgsfield_request_id" not in releve
    marquer.assert_called_once_with(_RID, statut=statut)


@pytest.mark.parametrize("statut", ["queued", "in_progress", None])
def test_a_running_job_is_free_and_its_record_untouched(client, cle, releve, statut):
    _, marquer = _lire(client, statut, _LIGNE)
    assert releve["quantity"] == 0
    marquer.assert_not_called()


# --- on the shared key, a job is its submitter's: read, cancel, delete ----------------

_GESTES = [("higgsfield", "get_request", "get_request"),
           ("higgsfield_act", "cancel", "cancel_request"),
           ("higgsfield_act", "delete", "delete_request")]


def _geste(client, outil, op, ligne, org_courante=7, panne=None):
    with patch("oto_mcp.db.higgsfield_jobs.lire", return_value=ligne,
               side_effect=panne) as lire, \
            patch("oto_mcp.db.higgsfield_jobs.marquer") as marquer, \
            patch.object(_access, "current_org", return_value=org_courante):
        _tool(outil)(op=op, request_id=_RID)
    return lire, marquer


def _refus(client, outil, op, methode, ligne, org_courante=7) -> str:
    with pytest.raises(McpError) as e:
        _geste(client, outil, op, ligne, org_courante)
    getattr(client, methode).assert_not_called()
    assert e.value.error.message.startswith("job_not_found:")
    return e.value.error.message


@pytest.mark.parametrize("outil,op,methode", _GESTES)
def test_another_orgs_job_is_refused_before_anything_is_sent(client, cle, releve,
                                                             outil, op, methode):
    _refus(client, outil, op, methode, _LIGNE, org_courante=8)
    assert "higgsfield_request_id" not in releve


@pytest.mark.parametrize("outil,op,methode", _GESTES)
def test_a_job_the_table_does_not_know_is_refused_alike(client, cle, outil, op, methode):
    """Absent or another org's: the SAME refusal — it never says whether the job exists."""
    absent = _refus(client, outil, op, methode, None)
    client.reset_mock()
    autre = _refus(client, outil, op, methode, _LIGNE, org_courante=8)
    assert absent == autre


@pytest.mark.parametrize("outil,op,methode", _GESTES)
def test_a_job_recorded_without_an_org_is_nobodys(client, cle, outil, op, methode):
    _refus(client, outil, op, methode, {**_LIGNE, "org_id": None}, org_courante=None)


@pytest.mark.parametrize("outil,op,methode", _GESTES)
def test_an_unreadable_record_raises_rather_than_guesses(client, cle, outil, op, methode):
    with pytest.raises(psycopg.OperationalError):
        _geste(client, outil, op, None, panne=psycopg.OperationalError("base"))
    getattr(client, methode).assert_not_called()


@pytest.mark.parametrize("outil,op,methode", _GESTES[1:])
def test_its_own_org_cancels_and_deletes_its_job(client, cle, outil, op, methode):
    getattr(client, methode).return_value = {"ok": True}
    _geste(client, outil, op, _LIGNE)
    getattr(client, methode).assert_called_once_with(_RID)


@pytest.mark.parametrize("outil,op,methode", _GESTES)
def test_on_the_customers_own_key_the_table_is_not_read(client, cle, outil, op, methode):
    cle["mode"] = "org"
    getattr(client, methode).return_value = {"status": "completed"}
    lire, _ = _geste(client, outil, op, None)
    lire.assert_not_called()
    getattr(client, methode).assert_called_once_with(_RID)


def test_a_failed_state_write_does_not_stop_the_bill(client, cle, releve):
    client.get_request.return_value = {"status": "completed"}
    with patch("oto_mcp.db.higgsfield_jobs.lire", return_value=_LIGNE), \
            patch("oto_mcp.db.higgsfield_jobs.marquer",
                  side_effect=psycopg.OperationalError("base")):
        _tool("higgsfield")(op="get_request", request_id=_RID)
    assert releve["quantity"] == 560000


def test_on_the_customers_own_key_a_read_is_free_and_the_table_not_read(client, cle, releve):
    cle["mode"] = "user"
    lire, _ = _lire(client, "completed", _LIGNE)
    assert releve["quantity"] == 0
    lire.assert_not_called()


@pytest.mark.parametrize("op,kw", [("list_models", {}),
                                   ("estimate", {"model": _KLING, "params": _PARAMS})])
def test_the_other_reads_trace_zero_never_null(client, cle, releve, op, kw):
    client.list_models.return_value = {"items": []}
    _tool("higgsfield")(op=op, **kw)
    assert releve["quantity"] == 0


# --- the shared key is CLOSED without a grant: the real resolution path ------------------
# Same seams as `tests/test_elevenlabs_tools.py::cle_partagee`.

@pytest.fixture
def cle_partagee(monkeypatch, client, releve, sans_droit_declare):
    from oto_mcp import access, grants_chain
    etat = {"share_down": [], "rate_limit_by": {}, "used": 0}
    monkeypatch.setattr(access, "resolve_api_key", _VRAIE_RESOLUTION)
    monkeypatch.setattr(access.db, "get_member_api_key", lambda sub, org, p: None)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "current_org", lambda sub: None)
    monkeypatch.setattr(access.credentials_store, "list_platform_instances", lambda p: [{
        "label": "shared", "share_mode": "closed", "share_down": list(etat["share_down"]),
        "meta": {"rate_limit_by": dict(etat["rate_limit_by"])}}])
    monkeypatch.setattr(access.credentials_store, "get_credential",
                        lambda et, eid, p, account="": "PLAT")
    monkeypatch.setattr(access.db, "get_usage_today", lambda sub, p: etat["used"])
    monkeypatch.setattr(grants_chain.db_grants, "edges_for", lambda ref, grantees: [])
    monkeypatch.setattr("oto_mcp.links.ou_poser_la_cle", lambda *a, **k: "")
    monkeypatch.setattr("oto_mcp.auth.hooks.current_user_sub_from_token", lambda: "user-sub")
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "user-sub")
    return etat


def _accorder(etat, quota):
    etat["share_down"] = ["user:user-sub"]
    etat["rate_limit_by"] = {"user:user-sub": quota}


def test_without_a_grant_the_shared_key_is_never_served(client, cle_partagee):
    with pytest.raises(McpError, match="No `higgsfield` key configured"):
        _soumettre()
    assert client.built == [] and not client.method_calls


def test_with_a_grant_the_shared_key_is_served(client, cle_partagee):
    _accorder(cle_partagee, 10_000_000)
    with patch("oto_mcp.db.higgsfield_jobs.noter") as noter:
        _soumettre()
    assert client.built and all(b == {"api_key": "PLAT"} for b in client.built)
    client.generate.assert_called_once()
    assert noter.called


def test_a_job_dearer_than_what_is_left_of_the_quota_is_never_sent(client, cle_partagee):
    """$9.50 of $10 spent today, the clip costs $0.56: refused whole, before submitting."""
    _accorder(cle_partagee, 10_000_000)
    cle_partagee["used"] = 9_500_000
    with pytest.raises(McpError):
        _soumettre()
    client.generate.assert_not_called()


def test_the_store_refuses_a_grant_without_a_quota(monkeypatch):
    from oto_mcp import credentials_store
    monkeypatch.setattr(credentials_store, "_connect",
                        lambda: pytest.fail("the vault must not be touched"))
    for quota in (None, 0):
        with pytest.raises(credentials_store.QuotaDOctroiRequis, match="micro-dollars"):
            credentials_store.platform_grant("higgsfield", "org:2", daily_quota=quota)


# --- the settlement ----------------------------------------------------------------------

_A_RELEVER = {"request_id": _RID, "sub": "s", "org_id": 7, "run_id": "tr",
              "prix_microdollars": 560000, "soumis_le": "2026-01-01T00:00:00+00:00"}


def _relever(status, dry_run=False, effet=None):
    from oto_mcp import higgsfield_releve as R
    hf = MagicMock()
    hf.get_request.return_value = status
    hf.get_request.side_effect = effet
    with patch("oto.tools.higgsfield.HiggsfieldClient", return_value=hf), \
            patch.object(R.higgsfield_jobs, "a_relever", return_value=[_A_RELEVER]), \
            patch.object(R.higgsfield_jobs, "marquer") as marquer, \
            patch.object(R.higgsfield_jobs, "purger", return_value=0), \
            patch.object(R.usage, "insert_tool_call") as inserer:
        compte = R.relever("k", dry_run=dry_run)
    return compte, marquer, inserer


def test_the_settlement_bills_the_submitter_with_the_line_of_a_read():
    from oto_mcp.db import journal_jour
    compte, marquer, inserer = _relever({"status": "completed"})
    ligne = inserer.call_args.args[0]
    assert ligne["tool"] == "higgsfield" and ligne["org_id"] == 7 and ligne["sub"] == "s"
    assert ligne["kind"] == journal_jour.KIND_RELEVE
    assert ligne["key_mode"] == "platform" and ligne["ok"] is True
    assert ligne["quantity"] == 560000
    assert ligne["args"]["higgsfield_request_id"] == _RID
    marquer.assert_called_once_with(_RID, statut="completed")
    assert compte["factures"] == 1


@pytest.mark.parametrize("statut", ["failed", "nsfw", "canceled"])
def test_a_job_higgsfield_did_not_charge_closes_without_a_line(statut):
    compte, marquer, inserer = _relever({"status": statut})
    inserer.assert_not_called()
    marquer.assert_called_once_with(_RID, statut=statut)
    assert compte["gratuits"] == 1


def test_a_running_job_waits():
    compte, marquer, inserer = _relever({"status": "in_progress"})
    assert compte["en_cours"] == 1
    inserer.assert_not_called()
    marquer.assert_not_called()


def test_dry_run_writes_nothing():
    compte, marquer, inserer = _relever({"status": "completed"}, dry_run=True)
    assert compte["factures"] == 1
    inserer.assert_not_called()
    marquer.assert_not_called()


def test_a_job_unknown_to_higgsfield_closes_without_a_cost():
    from oto.tools.common.errors import UpstreamHTTPError
    compte, marquer, inserer = _relever(None, effet=UpstreamHTTPError(404, {}, service="h"))
    assert compte["introuvables"] == 1
    inserer.assert_not_called()
    marquer.assert_called_once_with(_RID, statut="introuvable")


def test_an_outage_leaves_the_job_for_tomorrow():
    from oto.tools.common.errors import UpstreamHTTPError
    compte, marquer, _ = _relever(None, effet=UpstreamHTTPError(500, {}, service="h"))
    assert compte["echecs"] == 1
    marquer.assert_not_called()


def test_without_a_platform_key_nothing_happens():
    from oto_mcp import higgsfield_releve as R
    with patch.object(R.credentials_store, "list_platform_credentials", return_value=[]):
        assert R.maintenance() == {"cle_plateforme": False}


def test_several_platform_keys_raise_a_named_error():
    from oto_mcp import higgsfield_releve as R
    with patch.object(R.credentials_store, "list_platform_credentials",
                      return_value=[{"label": "a"}, {"label": "b"}]), \
            pytest.raises(R.PlusieursClesPlateforme):
        R.maintenance()


def test_the_maintenance_job_is_in_the_daily_timer():
    from oto_mcp import maintenance
    assert "higgsfield-jobs" in maintenance._TRAVAUX
    assert "higgsfield-jobs" in maintenance._ALL


def test_the_job_id_reaches_the_billing_lens_and_the_oto_call_target():
    from oto_mcp import server
    from oto_mcp.db import usage
    from oto_mcp.tools import meta
    assert "higgsfield_request_id" in usage.BILLABLE_JOB_ARGS
    assert "higgsfield_request_id" in server._TRACED_ARGS
    assert "higgsfield_request_id" in meta._BILLING_TRACE_KEYS
