"""Les trois flux qui vivaient hors du point de passage (#300 §1).

Google, Atlassian et Folk fédéré démarraient leur connexion par une route REST
écrite à la main. Elles rendaient `{auth_url}` — mais **par coïncidence** : rien ne
les y obligeait, et le garde-fou qui impose la forme commune ne voit que les flux
déclarés. Conséquence pour le front : garder une fonction par connecteur, là où le
point de passage existe précisément pour qu'il n'ait pas à savoir lequel il branche.

Ces flux sont désormais DÉCLARÉS. Les routes historiques restent en place et
appellent le même constructeur d'URL — aucune rupture pour le front, qui pourra
basculer sur le verbe commun quand il le voudra.
"""
from __future__ import annotations

import pytest

from oto_mcp.auth import atlassian as atlassian_oauth
from oto_mcp.auth import folk as folk_oauth
from oto_mcp.auth import google as google_oauth
from oto_mcp.connectors import flow as connector_flow
from oto_mcp.capabilities._types import AuthzDenied


class _Ctx:
    sub = "user-1"
    org_id = 2


FEDERES = [
    ("atlassian", atlassian_oauth),
    ("folkmcp", folk_oauth),
    ("google", google_oauth),
]


@pytest.mark.parametrize("name,module", FEDERES, ids=[n for n, _ in FEDERES])
def test_le_flux_est_declare(name, module):
    assert connector_flow.supports(name), f"{name} : geste « connecter » hors du seam"


@pytest.mark.parametrize("name,module", FEDERES, ids=[n for n, _ in FEDERES])
@pytest.mark.asyncio
async def test_le_flux_rend_la_forme_commune(monkeypatch, name, module):
    """Le point de passage VÉRIFIE le type de retour : un flux qui rendrait un dict
    maison lèverait ici. C'est tout l'objet de l'exercice."""
    # `return_app` depuis oto-backend#877 : le front qui a demandé la connexion
    # voyage jusqu'au state. Le double doit accepter la signature réelle, sinon il
    # valide un appel qui n'existe plus.
    monkeypatch.setattr(module, "build_auth_url",
                        lambda sub, return_app="": f"https://exemple/{sub}")
    out = await connector_flow.start(name, _Ctx(), {})
    assert isinstance(out, connector_flow.FlowStart)
    assert out.as_dict() == {"auth_url": "https://exemple/user-1", "details": {}}


@pytest.mark.parametrize("name,module", FEDERES, ids=[n for n, _ in FEDERES])
@pytest.mark.asyncio
async def test_le_flux_part_du_compte_appelant(monkeypatch, name, module):
    """L'URL est liée au compte : c'est le `sub` du contexte qui doit la construire,
    jamais une valeur passée par l'appelant — sinon on signerait un état pour un tiers."""
    vus = []
    monkeypatch.setattr(module, "build_auth_url",
                        lambda sub, return_app="": vus.append(sub) or "https://exemple/x")
    await connector_flow.start(name, _Ctx(), {"sub": "quelquun-dautre"})
    assert vus == ["user-1"]


def test_le_descripteur_reste_muet_sur_les_chemins():
    """Le catalogue est servi sans authentification : les trois nouveaux flux ne
    doivent pas y publier d'URL ni de nom de capacité (contrat déjà gardé, mais ces
    flux portent un `callback_path`, donc la vérification vaut d'être refaite ici)."""
    for name, _ in FEDERES:
        blob = repr(connector_flow.describe(name))
        for interdit in ("http", "/api/", "me.", "oto_"):
            assert interdit not in blob, f"{name} : « {interdit} » dans le descripteur"


@pytest.mark.asyncio
async def test_google_traduit_une_config_absente_en_refus_nomme(monkeypatch):
    """La route rendait 500 sur une app OAuth non configurée. Ce n'est pas une panne :
    c'est un état qui empêche d'aboutir, et réessayer n'y changera rien."""
    def _boom(sub, return_app=""):
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID absente")

    monkeypatch.setattr(google_oauth, "build_auth_url", _boom)
    with pytest.raises(AuthzDenied) as e:
        await connector_flow.start("google", _Ctx(), {})
    assert e.value.code == "oauth_misconfigured"
