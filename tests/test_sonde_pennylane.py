"""La sonde de connexion Pennylane — otomata-tech/oto#69.

`GET /api/external/v2/me` prouve l'authentification ET rend les droits de la clé.
Les deux comptent : le modèle Pennylane est une clé par personne ou par équipe,
chacune avec son périmètre, et Pennylane a éclaté ses scopes. Une clé peut donc
parfaitement authentifier sans rien pouvoir faire — et « connecté » serait alors
un verdict creux, celui-là même qu'une sonde existe pour empêcher.
"""
from __future__ import annotations

import pytest

from oto_mcp.tools import pennylane as P


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom
        self.appels = []

    def get_company_info(self):
        self.appels.append("me")
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, client):
    import oto.tools.pennylane as pkg
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: client)
    return client


def test_une_cle_valide_avec_des_droits_passe(monkeypatch):
    cli = _brancher(monkeypatch, _FauxClient({
        "user": {"id": 1}, "company": {"id": 7, "name": "UNE SOCIÉTÉ"},
        "scopes": ["customers:all", "customer_invoices:all"]}))
    P._verify({"api_key": "k"})
    assert cli.appels == ["me"], "un seul appel, en lecture"


def test_une_cle_refusee_leve(monkeypatch):
    _brancher(monkeypatch, _FauxClient(boom=RuntimeError("HTTP 401: invalid token")))
    with pytest.raises(RuntimeError, match="401"):
        P._verify({"api_key": "k"})


def test_une_cle_SANS_AUCUN_DROIT_est_un_echec_nomme(monkeypatch):
    """Le cas qui rendrait la sonde décorative : Pennylane répond 200, la société
    est reconnue, et la clé ne peut rien faire. Le message doit nommer la société
    (donc dire que l'authentification, elle, marche) ET le geste qui corrige."""
    _brancher(monkeypatch, _FauxClient({
        "user": {"id": 1}, "company": {"id": 7, "name": "UNE SOCIÉTÉ"}, "scopes": []}))
    with pytest.raises(RuntimeError) as e:
        P._verify({"api_key": "k"})
    msg = str(e.value)
    assert "AUCUN droit" in msg and "UNE SOCIÉTÉ" in msg, msg
    assert "Régénère" in msg, "le refus doit dire quoi faire"


def test_une_reponse_sans_societe_est_un_echec(monkeypatch):
    _brancher(monkeypatch, _FauxClient({"user": {"id": 1}}))
    with pytest.raises(RuntimeError, match="sans désigner de société"):
        P._verify({"api_key": "k"})


def test_des_scopes_absents_ne_font_PAS_echouer(monkeypatch):
    """L'autre sens, et il compte : `scopes` manquant n'est pas `scopes` vide.
    Un fournisseur qui cesserait de servir le champ ferait alors échouer toutes
    les clés valides — un garde-fou qui casse ce qu'il surveille."""
    _brancher(monkeypatch, _FauxClient({"user": {"id": 1}, "company": {"id": 7}}))
    P._verify({"api_key": "k"})


def test_la_sonde_est_enregistree_au_registre():
    from fastmcp import FastMCP

    from oto_mcp.connectors import verify as cv

    P.register(FastMCP("t"))
    assert cv.supports("pennylane") and cv.probe_for("pennylane") is P._verify


def test_la_sonde_dit_ce_qu_elle_couvre_et_ce_qu_elle_ne_couvre_pas():
    doc = P._verify.__doc__ or ""
    assert "Ne lit PAS de quota" in doc
    assert "pas une ligne qui dit" in doc.replace("PAS", "pas"), (
        "la limite de ce que la doc du fournisseur établit doit rester écrite : "
        "aucune ligne ne dit « gratuit », c'est l'absence de compteur qui l'indique")
