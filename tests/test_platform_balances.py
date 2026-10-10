"""`admin.platform_balances` — every platform key, its probe, its verdict.

No DB: the vault is stubbed. What is pinned here is the READING of a result — a
balance not measured is null and never zero, a connector without a probe says so,
a failure is classified per key without failing the sweep — and that no secret
leaves the call.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import credentials_store as cs
from oto_mcp.capabilities import platform_balances as PB
from oto_mcp.connectors import verify as cv

SECRET = "TOP-SECRET-KEY-VALUE"


@pytest.fixture
def coffre(monkeypatch):
    """Three platform keys: one with a quota probe, one with an auth-only probe, one
    without any probe. Registry restored after the test."""
    lignes = [{"provider": "q_conn", "label": "main"},
              {"provider": "a_conn", "label": "main"},
              {"provider": "n_conn", "label": "main"}]
    monkeypatch.setattr(cs, "list_platform_credentials",
                        lambda provider=None: [r for r in lignes
                                               if provider in (None, r["provider"])])
    monkeypatch.setattr(cs, "get_credential_with_meta",
                        lambda et, eid, c, account="": {"secret": SECRET, "meta": {}})
    monkeypatch.setattr(cs, "unpack_secret", lambda c, s: {"key": s})
    monkeypatch.setattr(cv, "_porteur", lambda c: c)
    reg, cov = dict(cv._REGISTRY), dict(cv._COUVERTURE)
    yield cv
    cv._REGISTRY.clear(); cv._REGISTRY.update(reg)
    cv._COUVERTURE.clear(); cv._COUVERTURE.update(cov)


def _sweep(provider=None) -> dict:
    return asyncio.run(PB._platform_balances(None, PB.PlatformBalancesInput(provider=provider)))


def _par(res) -> dict:
    return {k["provider"]: k for k in res["keys"]}


def test_chaque_cle_rend_son_verdict_et_son_solde(coffre):
    vu = {}

    def _quota(fields, config, instance=None):
        vu.update(fields=fields, instance=instance)
        return {"quota": {"restant": 12, "unite": "credits"}}
    coffre.register("q_conn", _quota, couvre=cv.AUTH_QUOTA)
    coffre.register("a_conn", lambda f, c: None)

    par = _par(_sweep())
    assert par["q_conn"]["verdict"] == cv.OK
    assert par["q_conn"]["balance"] == {"restant": 12, "unite": "credits"}
    assert par["q_conn"]["coverage"] == cv.AUTH_QUOTA
    # The probe is told it runs on the PLATFORM row it was given, not the cascade's.
    assert vu["instance"] == (cs.PLATFORM, "main", "")
    assert vu["fields"] == {"key": SECRET}


def test_un_solde_NON_MESURE_est_null_jamais_zero(coffre):
    coffre.register("a_conn", lambda f, c: None)
    a = _par(_sweep())["a_conn"]
    assert a["verdict"] == cv.OK and a["coverage"] == cv.AUTH
    assert a["balance"] is None


def test_sans_sonde_on_le_dit_au_lieu_de_dire_ok(coffre):
    n = _par(_sweep())["n_conn"]
    assert n["verdict"] == PB.NO_PROBE
    assert n["balance"] is None and n["coverage"] is None
    assert "oto_admin_monitoring" in n["next_step"]


def test_un_compte_a_sec_se_classe_no_quota_sans_casser_le_balayage(coffre):
    def _sec(f, c):
        raise cv.QuotaEpuise("drained")
    coffre.register("q_conn", _sec, couvre=cv.AUTH_QUOTA)

    def _boom(f, c):
        raise RuntimeError("upstream exploded")
    coffre.register("a_conn", _boom)

    par = _par(_sweep())
    assert par["q_conn"]["verdict"] == cv.NO_QUOTA
    assert par["q_conn"]["next_step"] == cv.CONDUITE[cv.NO_QUOTA]
    assert par["a_conn"]["verdict"] == cv.UNKNOWN
    assert "exploded" in par["a_conn"]["error"]
    assert len(par) == 3


def test_une_ligne_illisible_est_unknown(coffre, monkeypatch):
    coffre.register("q_conn", lambda f, c: {"quota": {"restant": 1}}, couvre=cv.AUTH_QUOTA)
    monkeypatch.setattr(cs, "get_credential_with_meta", lambda *a, **k: None)
    q = _par(_sweep("q_conn"))["q_conn"]
    assert q["verdict"] == cv.UNKNOWN
    assert "oto_admin_vault_health" in q["error"]


def test_le_filtre_provider_restreint(coffre):
    coffre.register("q_conn", lambda f, c: None)
    assert [k["provider"] for k in _sweep("q_conn")["keys"]] == ["q_conn"]


def test_aucun_secret_ne_sort(coffre):
    def _fuit(f, c):
        raise RuntimeError("refused")
    coffre.register("q_conn", _fuit, couvre=cv.AUTH_QUOTA)
    coffre.register("a_conn", lambda f, c: None)
    assert SECRET not in repr(_sweep())


def test_la_capacite_est_reservee_aux_admins_plateforme():
    from oto_mcp.capabilities._authz import PLATFORM_ADMIN
    from oto_mcp.capabilities.registry import CAPABILITIES

    cap = next(c for c in CAPABILITIES if c.key == "admin.platform_balances")
    assert cap.authz is PLATFORM_ADMIN
    assert cap.mcp == "oto_admin_platform_balances"
    # The served shape validates what the handler returns.
    PB.PlatformBalances.model_validate({"checked_at": 0.0, "keys": [
        {"provider": "x", "label": "y", "verdict": PB.NO_PROBE}]})
