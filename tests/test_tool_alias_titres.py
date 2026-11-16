"""Chaque outil servi porte un TITRE lisible, unique (`tool_alias.poser_les_titres`).

L'annuaire des connecteurs d'Anthropic refuse un serveur dont un outil n'a pas
`annotations.title`. Le titre est dérivé (`<libellé du connecteur>: <action>`), posé au
montage, et suit le nom dans la liste servie (alias déprécié, préfixe de tenant).
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext

from oto_mcp import deprecations, tenancy, tool_alias
from oto_mcp.middleware.alias import ToolAliasMiddleware


async def _renvoie(valeur):
    return valeur


async def _liste_servie(monkeypatch, sub):
    """La liste reçue par un client : les VRAIS outils montés, à travers le VRAI
    middleware d'alias (le dernier à retoucher la liste)."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token", lambda: sub)
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    return await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie(tools))


def _titres(tools) -> dict[str, str]:
    """`{nom: titre}` tel que le protocole le sert — champ de premier niveau ET
    annotation, qui doivent dire la même chose."""
    out = {}
    for t in tools:
        servi = t.to_mcp_tool()
        assert servi.title, f"{t.name} : pas de titre"
        assert servi.annotations is not None and servi.annotations.title == servi.title, (
            f"{t.name} : annotations.title {getattr(servi.annotations, 'title', None)!r} "
            f"≠ title {servi.title!r}")
        out[t.name] = servi.title
    return out


def _sans_doublon(titres: dict[str, str]) -> None:
    par_titre: dict[str, list[str]] = {}
    for nom, titre in titres.items():
        par_titre.setdefault(titre, []).append(nom)
    doublons = {t: n for t, n in par_titre.items() if len(n) > 1}
    assert not doublons, f"titres portés par plusieurs outils : {doublons}"


@pytest.mark.asyncio
async def test_tout_outil_monte_a_un_titre_unique(monkeypatch):
    tools = await _liste_servie(monkeypatch, None)
    assert len(tools) > 500, "le montage réel, pas un catalogue vide"
    _sans_doublon(_titres(tools))


@pytest.mark.asyncio
async def test_un_alias_deprecie_a_son_propre_titre(monkeypatch):
    titres = _titres(await _liste_servie(monkeypatch, None))
    for ancien, nouveau in deprecations.TOOLS.items():
        if nouveau in titres:
            assert titres[ancien] == f"{titres[nouveau]} (deprecated name: {ancien})"
    _sans_doublon(titres)


@pytest.mark.asyncio
async def test_le_titre_suit_le_prefixe_du_tenant(monkeypatch):
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "tool_prefix": "acme",
                  "issuer": "https://auth.acme.test/oidc"}])))
    try:
        titres = _titres(await _liste_servie(monkeypatch, "acme:u-1"))
    finally:
        tenancy.install(avant)
    assert titres["acme_doc"] == "Acme: Doc"
    assert not any(t.startswith(tool_alias.PLATFORM_LABEL + ":") for t in titres.values())
    _sans_doublon(titres)


@pytest.mark.parametrize("nom,titre", [
    ("pennylane_act", "Pennylane: Actions"),
    ("oto_doc", "Oto: Doc"),
    ("data_rows", "Oto: Data rows"),
    ("feedback", "Oto: Feedback"),
    ("loi_article", "Info légale FR: Loi article"),
    ("ccn_article", "Info légale FR: Ccn article"),
])
def test_derivation(nom, titre):
    assert tool_alias.title_of(nom) == titre


def test_un_outil_sans_connecteur_leve():
    with pytest.raises(tool_alias.TitreIntrouvable):
        tool_alias.title_of("zzinconnu_search")


def test_un_titre_pose_a_la_main_est_conserve():
    from fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    m = FastMCP("t")

    @m.tool(name="apollo_search_people",
            annotations=ToolAnnotations(title="Find people", readOnlyHint=True))
    def _f() -> str:
        return ""

    tool_alias.poser_les_titres(m)
    outil = next(c for c in m.local_provider._components.values()
                 if getattr(c, "name", None) == "apollo_search_people")
    assert outil.title == "Find people" and outil.annotations.readOnlyHint is True


def test_les_constantes_partagees_ne_sont_pas_touchees():
    from oto_mcp.tools.lecture import ENVOI, LECTURE
    _test_mcp()
    assert LECTURE.title is None and ENVOI.title is None
