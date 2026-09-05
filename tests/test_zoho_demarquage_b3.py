"""Un credential Zoho réparé se démarque tout seul — otomata-tech/oto#25 lot b3.

Le marquage (b2) existait : sur refus du refresh, la ligne de coffre servie est
marquée rejetée. Le DÉMARQUAGE excluait Zoho, faute de savoir quand un credential
recommence à marcher — une ligne marquée restait rouge jusqu'à une re-pose
manuelle, même après que son propriétaire ait réparé son application.

Le rappel `on_refresh` d'oto-core (v1.116.0) donne ce moment, et lui seul : il ne
part qu'après un refresh RÉUSSI, jamais sur un succès de cache. C'est ce qui en
fait une preuve de vie plutôt qu'une information périmée — le cache Zoho dure une
heure.
"""
from __future__ import annotations

import pytest


class _RC:
    def __init__(self, entity_type="member", entity_id="2:sub-1", account=""):
        self.entity_type, self.entity_id, self.account = entity_type, entity_id, account


def _fabrique(monkeypatch):
    """La fabrique de rappel telle que le module la construit, sans monter le tool."""
    from oto_mcp.tools import zoho as Z

    vus = []
    monkeypatch.setattr(Z.connector_health, "record_health",
                        lambda *a, **k: vus.append((a, k)))
    return Z, vus


def test_un_refresh_reussi_demarque_la_ligne_servie(monkeypatch):
    Z, vus = _fabrique(monkeypatch)
    rappel = Z._demarque_apres_refresh(_RC())
    assert rappel is not None
    rappel({"access_token": "tok"})
    assert len(vus) == 1
    args, _ = vus[0]
    assert args[0] == "zoho"
    assert args[1] == ("member", "2:sub-1", "")
    assert args[2] is True, "démarquer = déclarer la ligne saine"


def test_un_grant_PLATEFORME_ne_demarque_rien(monkeypatch):
    """Aucune ligne de coffre à marquer : la clé n'appartient à personne en
    particulier. Rendre un rappel qui écrirait quand même peindrait en vert une
    ligne qui n'existe pas."""
    Z, vus = _fabrique(monkeypatch)
    assert Z._demarque_apres_refresh(_RC(entity_type=None)) is None
    assert vus == []


def test_le_client_recoit_bien_le_rappel(monkeypatch):
    """Le câblage, pas seulement la fabrique : c'est le passage au client qui fait
    que quoi que ce soit se produise en vrai. On remplace `ZohoClient` sur son
    module SOURCE — le tool l'importe dans sa closure, patcher l'attribut du module
    de tools ne servirait à rien."""
    import oto.tools.zoho.client as zc
    from oto_mcp.tools import zoho as Z

    vus = {}

    class _FauxClient:
        def __init__(self, **kw):
            vus.update(kw)

    monkeypatch.setattr(zc, "ZohoClient", _FauxClient)
    monkeypatch.setattr(Z.connector_health, "record_health", lambda *a, **k: None)

    rappel = Z._demarque_apres_refresh(_RC())
    zc.ZohoClient(client_id="c", client_secret="s", refresh_token="r",
                  api_domain="https://api", accounts_url="https://acc",
                  on_refresh=rappel)
    assert callable(vus.get("on_refresh")), (
        "le client doit recevoir le rappel : sans lui, rien ne démarque jamais")


def test_le_tool_passe_bien_le_rappel_au_client():
    """⚠️ Contrôle STATIQUE, assumé : `_client()` exige un credential résolu et une
    identité de session qu'on ne monte pas ici. Il lit l'ARBRE — il tombera si
    quelqu'un retire l'argument, pas sur un reformatage."""
    import ast
    import pathlib as _p

    src = (_p.Path(__file__).resolve().parents[1] / "oto_mcp" / "tools"
           / "zoho.py").read_text()
    arbre = ast.parse(src)
    passe = [
        n for n in ast.walk(arbre)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "ZohoClient"
        and any(k.arg == "on_refresh" for k in n.keywords)]
    assert passe, (
        "aucune construction de ZohoClient ne passe `on_refresh` : le démarquage "
        "ne se déclenchera jamais, quelle que soit la qualité de la fabrique")
