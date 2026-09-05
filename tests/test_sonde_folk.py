"""La sonde de connexion Folk — otomata-tech/oto#69.

Elle couvre `auth` SEUL : `GET /v1/users/me` prouve que la clé authentifie et
désigne quelqu'un. Elle ne lit pas de quota — Folk sert des en-têtes de débit
(600 requêtes par minute) et non un solde, et rendre une cadence là où l'on
attend une jauge ferait croire à un crédit qui n'existe pas.
"""
from __future__ import annotations

import pytest

from oto_mcp.tools import folk as F


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def get_current_user(self):
        self.appels.append("users/me")
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.folk.client as fc
    monkeypatch.setattr(fc, "FolkClient", lambda **kw: client)
    return client


def test_une_cle_valide_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient(
        {"id": "u_1", "fullName": "Une Personne", "email": "x@y.z"}))
    F._verify({"api_key": "k"})
    assert cli.appels == ["users/me"], "un seul appel, en lecture"


def test_une_cle_refusee_leve(monkeypatch):
    """Le message d'exception EST le retour d'erreur rendu à l'utilisateur : il
    doit remonter tel quel, pas être avalé."""
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid api key")))
    with pytest.raises(RuntimeError, match="401"):
        F._verify({"api_key": "k"})


def test_une_reponse_200_SANS_identite_est_un_echec(monkeypatch):
    """Le cas qui rendrait la sonde décorative : Folk répond, donc pas d'exception,
    mais la clé ne désigne personne. Conclure « connecté » sur un compte qu'on ne
    peut pas nommer, c'est exactement le verdict creux qu'une sonde doit empêcher."""
    _brancher(monkeypatch, _FauxClient({}))
    with pytest.raises(RuntimeError, match="sans identifier"):
        F._verify({"api_key": "k"})


def test_la_sonde_est_enregistree_au_registre():
    """Écrite mais pas enregistrée, elle ne servirait jamais — et rien ne le dirait."""
    from fastmcp import FastMCP

    from oto_mcp.connectors import verify as cv

    F.register(FastMCP("t"))
    assert cv.supports("folk")
    assert cv.probe_for("folk") is F._verify


def test_la_sonde_ne_pretend_pas_lire_un_quota():
    """Ce qu'elle couvre est écrit là où on le lit. Une sonde qui laisserait croire
    qu'elle vérifie le solde ferait conclure « il reste du crédit » à qui n'a vérifié
    qu'une authentification."""
    doc = F._verify.__doc__ or ""
    assert "auth` SEUL" in doc or "auth SEUL" in doc, doc[:200]
    assert "Ne lit PAS le quota" in doc
