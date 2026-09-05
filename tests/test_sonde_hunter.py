"""La sonde Hunter — première `auth+quota` (otomata-tech/oto#69).

Elle lit le SOLDE, pas seulement l'authentification. C'est ce qui lui permet de
distinguer les deux refus qu'un appelant confond toujours : une clé fausse, qu'il
faut remplacer, et un compte à sec, qu'il faut recharger. Reconnecter dans le
second cas ne sert à rien — et c'est pourtant le réflexe.

Le solde est RENDU, jamais affiché sur la fiche : un chiffre affiché promettrait
une fraîcheur que la plateforme ne tient qu'en interrogeant, et interroger coûte.
"""
from __future__ import annotations

import pytest

from oto_mcp.connectors import verify as cv
from oto_mcp.tools import hunter as H


class _FauxClient:
    def __init__(self, reponse=None, boom=None):
        self._reponse, self._boom = reponse, boom

    def account_info(self):
        if self._boom:
            raise self._boom
        return self._reponse


def _brancher(monkeypatch, reponse=None, boom=None):
    import oto.tools.hunter.client as hc
    monkeypatch.setattr(hc, "HunterClient", lambda **kw: _FauxClient(reponse, boom))


def _compte(dispo, utilise):
    return {"data": {"requests": {"searches": {"available": dispo, "used": utilise}}}}


def test_un_solde_disponible_est_rendu_avec_son_unite_et_son_instant(monkeypatch):
    _brancher(monkeypatch, _compte(500, 120))
    out = H._verify({"api_key": "k"})
    q = out["quota"]
    assert q["restant"] == 380 and q["utilise"] == 120 and q["inclus"] == 500
    assert q["unite"] == "recherches", (
        "un nombre nu se lit comme on veut : l'unité doit voyager avec")
    assert q["mesure_a"], "ce chiffre vieillit dès qu'il est lu — l'instant compte"


def test_un_compte_a_SEC_est_un_refus_de_QUOTA_pas_d_AUTH(monkeypatch):
    """LE point de cette sonde. Classé `no_quota`, la conduite servie dit de
    recharger — pas de reconnecter, qui n'y changerait rien."""
    _brancher(monkeypatch, _compte(500, 500))
    with pytest.raises(cv.QuotaEpuise) as e:
        H._verify({"api_key": "k"})
    assert cv.classer(e.value) == cv.NO_QUOTA
    assert "recharge" in str(e.value).lower()


def test_une_cle_refusee_reste_un_refus_d_AUTH(monkeypatch):
    """L'autre sens : ne pas confondre non plus dans ce sens-là."""
    err = RuntimeError("unauthorized")
    err.status_code = 401
    _brancher(monkeypatch, boom=err)
    with pytest.raises(RuntimeError) as e:
        H._verify({"api_key": "k"})
    assert cv.classer(e.value) == cv.UNAUTHORIZED


def test_un_solde_ILLISIBLE_ne_fabrique_pas_de_quota(monkeypatch):
    """Si Hunter changeait la forme de sa réponse, inventer un solde serait pire
    que n'en rendre aucun : la sonde dirait « il reste X » sans l'avoir mesuré."""
    _brancher(monkeypatch, {"data": {"first_name": "X"}})
    assert H._verify({"api_key": "k"}) == {}


def test_une_reponse_vide_est_un_echec(monkeypatch):
    _brancher(monkeypatch, {})
    with pytest.raises(RuntimeError, match="sans information de compte"):
        H._verify({"api_key": "k"})


def test_la_couverture_declaree_est_auth_quota():
    """Déclarer `auth` alors qu'on lit le solde priverait l'appelant de ce qu'il a ;
    déclarer `auth+quota` sans le lire serait le vert trompeur qu'on combat."""
    from fastmcp import FastMCP

    H.register(FastMCP("t"))
    assert cv.couverture("hunter") == cv.AUTH_QUOTA


def test_le_seam_propage_les_mesures():
    """Le chemin complet : une sonde qui rend un solde doit le voir arriver au
    bout. Sans ça, la sonde mesurerait pour rien."""
    import asyncio

    def _sonde(fields, config):
        return {"quota": {"restant": 7, "unite": "recherches"}}

    assert asyncio.run(cv.executer(_sonde, {}, {}))["quota"]["restant"] == 7


def test_une_sonde_qui_ne_mesure_rien_rend_un_dict_vide():
    """Non-régression du contrat d'avant : la quinzaine de sondes `auth` rendent
    `None`, et ça doit continuer de passer sans inventer une mesure à zéro."""
    import asyncio

    assert asyncio.run(cv.executer(lambda f, c: None, {}, {})) == {}
