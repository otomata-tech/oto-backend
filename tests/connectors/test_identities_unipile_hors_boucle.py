"""oto-backend#867 — la liste d'identités Unipile (et `oto_identity`) ne bloque
plus la boucle d'événements, et un Unipile lent rend une erreur nommée.

Même méthode que `tests/test_capacites_hors_boucle.py` (incident du 2026-09-01,
mode n°4 de `docs/event-loop-perf.md`) : **on OBSERVE le thread**, pas le source —
aucune analyse statique ne dit où un appel synchrone finit par s'exécuter. Et un
contrôle qui MORD : le même appel joué NU dans la boucle (la forme d'avant le
correctif) doit être détecté par la même sonde — sinon un « vert » ne prouve rien.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from oto_mcp import access
from oto_mcp.access import ResolvedCredential
from oto_mcp.connectors import identities as I
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.connectors import identities as CI


def _joue(coro):
    """Joue une coroutine dans une boucle neuve ; rend le thread de CETTE boucle."""
    porteur: dict = {}

    async def _run():
        porteur["boucle"] = threading.current_thread()
        return await coro

    asyncio.run(_run())
    return porteur["boucle"]


class _FakeUnipile:
    """Client Unipile qui NOTE le thread appelant, sur les deux méthodes en jeu."""

    def __init__(self, vu: dict, accounts=None, delay: float = 0.0, boom: bool = False):
        self._vu, self._accounts, self._delay, self._boom = vu, accounts or [], delay, boom

    def list_accounts(self):
        self._vu["list_accounts_thread"] = threading.current_thread()
        if self._delay:
            import time
            time.sleep(self._delay)
        if self._boom:
            raise RuntimeError("Unipile 500")
        return self._accounts

    def account_alive(self, aid):
        self._vu["account_alive_thread"] = threading.current_thread()
        return True


def _wire_byo(monkeypatch, cli):
    monkeypatch.setattr(access, "credential_mode_for", lambda sub, prov: "org")
    monkeypatch.setattr(access, "resolve_credential",
                        lambda prov, want="auto", sub=None: ResolvedCredential(
                            "unipile", "KEY", False, "org", "org", "39"))
    monkeypatch.setattr("oto.tools.unipile.make_unipile_client", lambda **k: cli)


# --- 1. `_call_unipile` — le mécanisme de base -------------------------------

def test_call_unipile_sort_du_thread_de_la_boucle():
    vu: dict = {}

    def _sync(x):
        vu["thread"] = threading.current_thread()
        return x * 2

    boucle = _joue(I._call_unipile(_sync, 21))
    assert vu["thread"] is not boucle, (
        "un appel Unipile sync a tourné dans le thread de l'event loop — c'est "
        "exactement ce qui a gelé la production 87.8s le 04/09 (oto-backend#867)")


def test_call_unipile_borne_un_appel_qui_ne_repond_pas(monkeypatch):
    monkeypatch.setattr(I, "_UNIPILE_TIMEOUT_S", 0.05)

    def _lent():
        import time
        time.sleep(1)

    with pytest.raises(TimeoutError):
        asyncio.run(I._call_unipile(_lent))


def test_le_controle_mord__un_appel_NU_dans_la_boucle_est_detecte():
    """Contrôle négatif : la sonde de thread doit voir la régression si quelqu'un
    revient à un appel nu (sans `_call_unipile`) — sinon un vert ne prouverait rien."""
    vu: dict = {}

    async def _nu():
        vu["thread"] = threading.current_thread()
        return 1

    boucle = _joue(_nu())
    assert vu["thread"] is boucle, "la sonde elle-même doit savoir dire « dans la boucle »"


# --- 2. `_unipile_list` (BYO) — hors boucle, et une panne se nomme -----------

def test_unipile_list_byo_tourne_hors_boucle(monkeypatch):
    vu: dict = {}
    _wire_byo(monkeypatch, _FakeUnipile(vu, accounts=[
        {"id": "A1", "name": "Jane", "type": "LINKEDIN", "sources": [{"status": "OK"}]}]))
    monkeypatch.setattr(I, "_own_unipile_account_id", lambda sub, ch: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 39)

    boucle = _joue(I._unipile_list("u1"))
    assert vu["list_accounts_thread"] is not boucle, (
        "`cli.list_accounts()` de la liste d'identités a tourné dans la boucle")


def test_unipile_list_byo_en_panne_leve_au_lieu_de_rendre_vide(monkeypatch):
    """oto-backend#867 — la liste n'avale plus l'échec en silence (ancien
    `except Exception: accounts = []`) : c'est ICI la liste elle-même, pas une
    sonde de statut annexe, et un Unipile en panne doit se dire."""
    _wire_byo(monkeypatch, _FakeUnipile({}, boom=True))
    with pytest.raises(RuntimeError, match="500"):
        asyncio.run(I._unipile_list("u1"))


def test_unipile_list_timeout_devient_une_erreur_nommee_502(monkeypatch):
    monkeypatch.setattr(I, "_UNIPILE_TIMEOUT_S", 0.05)
    _wire_byo(monkeypatch, _FakeUnipile({}, delay=1))
    monkeypatch.setattr(CI, "_require_known_connector", lambda name: None)
    monkeypatch.setattr(CI, "_require_scope", lambda ctx, scope, write: None)

    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CI._list(ResolvedCtx(sub="u1", org_id=39),
                             CI.IdentitiesInput(connector="unipile")))
    assert e.value.code == "unipile_list_failed" and e.value.status == 502


# --- 3. `_unipile_select` — même règle, jumeau de `_list` --------------------

def test_unipile_select_tourne_hors_boucle(monkeypatch):
    vu: dict = {}
    cli = _FakeUnipile(vu, accounts=[{"id": "A1", "name": "Jane", "type": "LINKEDIN"}])
    _wire_byo(monkeypatch, cli)
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    monkeypatch.setattr("oto_mcp.db.set_unipile_account", lambda *a, **k: None)
    monkeypatch.setattr(access, "current_org", lambda sub: 39)

    boucle = _joue(I._unipile_select("u1", "A1"))
    assert vu["list_accounts_thread"] is not boucle, (
        "`cli.list_accounts()` du sélecteur d'identité a tourné dans la boucle")


def test_unipile_select_timeout_nest_pas_confondu_avec_id_inconnu(monkeypatch):
    """Un Unipile qui ne répond pas doit rendre une erreur nommée `unipile_list_failed`
    (502) — jamais `unknown_identity` (404), qui dirait à tort que l'id n'existe pas."""
    monkeypatch.setattr(I, "_UNIPILE_TIMEOUT_S", 0.05)
    _wire_byo(monkeypatch, _FakeUnipile({}, delay=1))
    monkeypatch.setattr("oto_mcp.db.list_unipile_accounts", lambda sub: [])
    monkeypatch.setattr(CI, "_require_known_connector", lambda name: None)
    monkeypatch.setattr(CI, "_require_scope", lambda ctx, scope, write: None)

    with pytest.raises(AuthzDenied) as e:
        asyncio.run(CI._set_default(ResolvedCtx(sub="u1", org_id=39),
                                    CI.SetIdentityInput(connector="unipile",
                                                        identity_id="A1")))
    assert e.value.code == "unipile_list_failed" and e.value.status == 502
