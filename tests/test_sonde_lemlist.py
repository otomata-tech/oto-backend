"""La sonde de connexion Lemlist — otomata-tech/oto#69. Couvre `auth` + DROITS.

`GET /team` (déjà dans le client). `billing.ok` distingue une équipe en règle
d'une équipe suspendue — PAS les crédits d'enrichissement, qui ne gatent qu'une
seule fonctionnalité (`lemlist_enrich`) : les lire comme un quota global
dirait « connecteur mort » d'une clé qui peut encore tout faire sur
campagnes/séquences/leads.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.connectors import verify as cv
from oto_mcp.tools import lemlist as L


def _fields(secret: str) -> dict:
    """Champs EXACTEMENT comme la capacité verify les produit — coupler le test
    au vrai unpack empêche le drift sonde↔schéma (régression 05/09 sur
    folk/hunter/pennylane, `tests/connectors/test_verify_sondes_champs_reels.py`)."""
    return credentials_store.unpack_secret("lemlist", secret)


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def get_team(self):
        self.appels.append("team")
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.lemlist as pkg
    monkeypatch.setattr(pkg, "LemlistClient", lambda **kw: client)
    return client


def test_une_equipe_en_regle_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(
        {"_id": "tea_1", "name": "PiedPiper", "billing": {"ok": True, "plan": "pro"}}))
    L._verify(_fields("k"))
    assert cli.appels == ["team"], "un seul appel, en lecture"


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid credentials")))
    with pytest.raises(RuntimeError, match="401"):
        L._verify(_fields("k"))


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        L._verify(_fields("k"))


def test_une_facturation_suspendue_est_un_echec_nomme(monkeypatch):
    """Le cas qui rendrait la sonde décorative : 200, équipe identifiée, mais
    `billing.ok: false` — abonnement impayé ou suspendu."""
    _brancher(monkeypatch, _FauxClient(
        {"_id": "tea_1", "name": "PiedPiper", "billing": {"ok": False, "plan": "pro"}}))
    with pytest.raises(RuntimeError) as e:
        L._verify(_fields("k"))
    assert "PiedPiper" in str(e.value) and "facturation" in str(e.value)


def test_des_credits_d_enrichissement_a_sec_ne_font_PAS_echouer(monkeypatch):
    """L'axe délibérément PAS mesuré : les crédits ne gatent que l'enrichissement,
    pas le connecteur entier — les lire comme un quota global mentirait."""
    _brancher(monkeypatch, _FauxClient(
        {"_id": "tea_1", "name": "PiedPiper", "billing": {"ok": True}}))
    L._verify(_fields("k"))  # ne lève pas, même si des crédits étaient à zéro ailleurs


def test_la_sonde_est_enregistree_avec_la_couverture_auth():
    from fastmcp import FastMCP

    L.register(FastMCP("t"))
    assert cv.supports("lemlist")
    assert cv.probe_for("lemlist") is L._verify
    assert cv.couverture("lemlist") == cv.AUTH
