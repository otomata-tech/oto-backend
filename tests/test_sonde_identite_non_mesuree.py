"""Une sonde qui n'a pas répondu ne dit plus « ok » sans le signaler (oto#42, règle 1).

`_live_status` retombait sur `"ok"` dans deux cas indistinguables du succès : la sonde
échouée en bloc (map vide) et le compte absent de la map. « Une valeur qu'on n'a pas
pu établir n'est jamais rendue par son défaut » — et `ok` est le pire des défauts,
puisqu'il affirme que ça marche. Un compte réellement mort s'affichait connecté.

C'est le défaut #201/#236 par un TROISIÈME chemin : non plus le statut périmé, mais la
PANNE DE SONDE. Le fail-soft était assumé et documenté ; ce qui manquait, c'est qu'il
se dise.

La décision de contrat est prise (10/10/2026, signal 1360) : la valeur servie devient
`unknown` quand elle n'a pas été mesurée, et le fait est dit à côté.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import db as _db
from oto_mcp.connectors import identities as I


@pytest.fixture()
def revente(monkeypatch):
    """Mode revente : pas de client BYO, un compte hébergé en base."""
    monkeypatch.setattr(I, "_unipile_client", lambda sub: None)
    # `db` est importé DANS la fonction (`from .. import db`) : c'est le module
    # source qu'il faut patcher, pas un attribut du module de test.
    monkeypatch.setattr(_db, "list_unipile_accounts", lambda sub: [
        {"account_id": "acc-1", "account_name": "Un compte", "provider": "LINKEDIN",
         "org_id": 7}])
    from oto_mcp import access
    monkeypatch.setattr(access, "current_org", lambda sub: 7)
    monkeypatch.setattr(I, "_own_unipile_account_id", lambda sub, ch: None)


def test_sonde_en_panne_le_statut_est_dit_NON_mesure(revente, monkeypatch):
    """La sonde échoue en bloc → map vide. Le statut est `unknown`, jamais le défaut
    « ok » (signal 1360, décision du 10/10/2026 : un compte mort se lisait vivant
    pendant que chaque appel rendait 500), et l'appelant apprend pourquoi."""
    async def _live_map(sub):
        return {}
    monkeypatch.setattr(I, "_unipile_live_status_map", _live_map)
    ident = asyncio.run(I._unipile_list("u1"))[0]
    assert ident["status"] == "unknown"
    assert ident["status_measured"] is False
    assert "UNKNOWN, not ok" in ident["status_hint"]
    assert "dead" in ident["status_hint"], "le hint doit nommer le risque, pas le taire"


def test_sonde_qui_repond_ne_dit_RIEN_de_plus(revente, monkeypatch):
    """Pas d'écart, pas de bruit : quand la sonde a mesuré, la réponse ne s'encombre
    pas d'un champ que personne ne lira."""
    async def _live_map(sub):
        return {"acc-1": "disconnected"}
    monkeypatch.setattr(I, "_unipile_live_status_map", _live_map)
    ident = asyncio.run(I._unipile_list("u1"))[0]
    assert ident["status"] == "disconnected"
    assert "status_measured" not in ident and "status_hint" not in ident


def test_compte_absent_de_la_sonde_est_aussi_non_mesure(revente, monkeypatch):
    """Le second chemin, plus fin : la sonde a répondu, mais PAS pour ce compte-là.
    Il retombait sur « ok » exactement comme si elle l'avait constaté vivant."""
    async def _live_map(sub):
        return {"autre": "ok"}
    monkeypatch.setattr(I, "_unipile_live_status_map", _live_map)
    ident = asyncio.run(I._unipile_list("u1"))[0]
    assert ident["status"] == "unknown"
    assert ident["status_measured"] is False
