"""Le renommage par tenant ne touche ni le worker, ni ce qui s'écrit.

Deux défauts relevés avant de poser `tool_prefix` sur un tenant qui héberge des agents
(15/09/2026) — silencieux tous les deux :

1. **Le worker perdait ses outils.** Un travail du runner agit AU NOM de son demandeur
   (`runner_jobs._delegue`) : sous un compte de tenant, `tools/list` lui arrivait en
   `acme_*`, alors que l'allowlist du travail est canonique et que le worker la
   confronte EXACTEMENT (`oto_runner/mcp.py::schemas`). Aucun nom ne correspondait :
   l'agent tournait sans un outil, et rien ne le disait.
2. **Ce qui se lit traduit revenait traduit en base.** Un agent qui relit un guide servi
   en `acme_doc` et le réenregistre, ou qui déclare `tools=["acme_doc"]`, stockait le
   nom du produit — que rien de ce qui lit la base (allowlist déduite des `<tool:…>`,
   worker, retour arrière du préfixe) ne sait résoudre.
"""
from __future__ import annotations

import asyncio
import types

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext

from _mcp_app import static_mcp as _test_mcp
from oto_mcp import instructions, org_store, tenancy, tool_alias, tool_registry
from oto_mcp.capabilities import guides, runner_fleets, runner_triggers
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.groups import guide as groups_guide
from oto_mcp.capabilities.orgs import instructions as oi
from oto_mcp.middleware.alias import ToolAliasMiddleware

_SUB_TENANT = "acme:u-1"
_SUB_PLATEFORME = "bn01jfy76a5n"
_NOMS = ["data_write", "linkedin_aiark_search", "oto_admin_org", "oto_call", "oto_doc",
         "oto_procedure", "oto_use_org", "oto_use_project"]


@pytest.fixture
def tenant_acme():
    avant = tenancy.current()
    tenancy.install(tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme", "issuer": "https://auth.acme.test/oidc",
                  "tool_prefix": "acme"}])))
    yield
    tenancy.install(avant)


@pytest.fixture
def registre(monkeypatch):
    monkeypatch.setattr(tool_registry, "boot_tool_names", lambda: list(_NOMS))


def _jeton(monkeypatch, kind):
    """Le jeton de la requête MCP en cours, tel que `server._verify_api_token` le pose."""
    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token", lambda: types.SimpleNamespace(
        claims={"sub": _SUB_TENANT, "token_kind": kind}))


# ── 1. Le worker est servi en canonique ──────────────────────────────────────

def test_un_travail_du_runner_garde_les_noms_canoniques(tenant_acme, monkeypatch):
    _jeton(monkeypatch, "delegation")
    assert tool_alias.prefix_for(_SUB_TENANT) == ""
    assert tool_alias.server_identity_for(_SUB_TENANT) == ("", "")


def test_une_personne_du_tenant_voit_toujours_le_nom_du_produit(tenant_acme, monkeypatch):
    _jeton(monkeypatch, "user")
    assert tool_alias.prefix_for(_SUB_TENANT) == "acme"
    assert tool_alias.server_identity_for(_SUB_TENANT) == ("acme", "Acme")


def test_hors_requete_mcp_le_renommage_est_celui_davant(tenant_acme, monkeypatch):
    import fastmcp.server.dependencies as deps

    def _hors_requete():
        raise RuntimeError("pas de requête MCP")

    monkeypatch.setattr(deps, "get_access_token", _hors_requete)
    assert tool_alias.prefix_for(_SUB_TENANT) == "acme"


@pytest.mark.asyncio
async def test_la_liste_servie_au_worker_porte_les_noms_de_son_allowlist(
        tenant_acme, monkeypatch):
    """Le cas qui vidait l'agent : l'allowlist canonique confrontée au `tools/list` servi."""
    _jeton(monkeypatch, "delegation")
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: _SUB_TENANT)
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")

    async def _suivant(_c):
        return tools

    servis = {t.name for t in await ToolAliasMiddleware().on_list_tools(ctx, _suivant)}
    assert {"oto_procedure", "oto_doc"} <= servis
    assert not any(n.startswith("acme_") for n in servis)


# ── 2. Ce qui s'écrit revient au canonique ───────────────────────────────────

def test_une_allowlist_declaree_revient_au_canonique(tenant_acme):
    assert tool_alias.canonical_names(
        ["acme_doc", "data_write", "linkedin_aiark_search", "oto_call"], _SUB_TENANT
    ) == ["oto_doc", "data_write", "linkedin_aiark_search", "oto_call"]


def test_un_compte_de_la_plateforme_ecrit_tel_quel(tenant_acme, registre):
    assert tool_alias.canonical_names(["acme_doc"], _SUB_PLATEFORME) == ["acme_doc"]
    assert tool_alias.canonical_prose("<tool:acme_doc>", _SUB_PLATEFORME) == "<tool:acme_doc>"


def test_la_prose_ecrite_ne_ramene_que_des_outils(tenant_acme, registre):
    """`acme_<x>` n'est pas un espace réservé : une table, un mot du client s'y écrivent."""
    texte = ("Lis avec <tool:acme_procedure>, écris avec `acme_doc`, bascule via "
             "acme_use_*, administre par acme_admin. La table `acme_leads` et le mot "
             "acme_ restent à leur auteur.")
    assert tool_alias.canonical_prose(texte, _SUB_TENANT) == (
        "Lis avec <tool:oto_procedure>, écris avec `oto_doc`, bascule via "
        "oto_use_*, administre par oto_admin. La table `acme_leads` et le mot "
        "acme_ restent à leur auteur.")


def test_registre_froid_rien_nest_touche(tenant_acme, monkeypatch):
    monkeypatch.setattr(tool_registry, "boot_tool_names", lambda: [])
    assert tool_alias.canonical_prose("`acme_doc`", _SUB_TENANT) == "`acme_doc`"


def test_relire_puis_reenregistrer_rend_le_texte_dorigine(tenant_acme, monkeypatch):
    """L'aller (`rewrite_prose`) puis le retour (`canonical_prose`), sur la prose SERVIE
    réelle et le registre réel : un agent qui relit et réenregistre ne change aucun nom."""
    noms = [t.name for t in asyncio.run(_test_mcp().list_tools(run_middleware=False))]
    monkeypatch.setattr(tool_registry, "boot_tool_names", lambda: sorted(noms))
    texte = instructions.render()
    lu = tool_alias.rewrite_prose(texte, "acme")
    assert lu != texte, "la prose servie ne cite plus d'outil — le test ne mord plus"
    assert tool_alias.canonical_prose(lu, _SUB_TENANT) == texte


def test_une_procedure_secrit_au_canonique(tenant_acme, registre, monkeypatch):
    ecrit: dict = {}
    monkeypatch.setattr(oi.org_store, "get_instruction", lambda *a, **k: None)

    def _set_instruction(otype, oid, slug, body_md, **kw):
        ecrit["body_md"] = body_md
        return 1

    monkeypatch.setattr(oi.org_store, "set_instruction", _set_instruction)
    oi._write_instruction(ResolvedCtx(sub=_SUB_TENANT, org_id=3),
                          oi.ConsoleInstrSetInput(slug="p", scope="org",
                                                  body_md="1. <tool:acme_doc> op=create"))
    assert ecrit["body_md"] == "1. <tool:oto_doc> op=create"


def test_un_guide_relu_puis_reecrit_garde_le_canonique(tenant_acme, registre, monkeypatch):
    ecrit: dict = {}
    monkeypatch.setattr(guides, "_owner_for_write", lambda ctx, scope, owner_id: ctx.sub)

    def _set_guide(scope, owner, slug, body, title, description):
        ecrit["body"] = body
        return {"slug": slug}

    monkeypatch.setattr(guides.guide_store, "set_guide", _set_guide)
    guides._set(ResolvedCtx(sub=_SUB_TENANT),
                guides.GuideSetInput(scope="user", slug="notes", body_md="Range avec `acme_doc`."))
    assert ecrit["body"] == "Range avec `oto_doc`."


def test_une_procedure_dequipe_secrit_au_canonique(tenant_acme, registre, monkeypatch):
    ecrit: dict = {}
    monkeypatch.setattr(org_store, "set_instruction",
                        lambda *a, **k: ecrit.update(body=a[3]) or 2)
    groups_guide._set(types.SimpleNamespace(sub=_SUB_TENANT, org_id=2, group_id=None),
                      groups_guide.InstrSetInput(group_id=7, slug="relance",
                                                 body_md="<tool:acme_call>"))
    assert ecrit["body"] == "<tool:oto_call>"


def test_une_flotte_declaree_depuis_le_produit_stocke_le_canonique(tenant_acme, registre):
    inp = runner_fleets.FleetInput(
        op="create", tools=["acme_procedure", "data_write"], input="Lis avec acme_procedure.",
        descriptions_outils={"defaut": 200, "entieres": ["acme_procedure"]})
    out = runner_fleets._noms_canoniques(ResolvedCtx(sub=_SUB_TENANT, org_id=1), inp)
    assert out.tools == ["oto_procedure", "data_write"]
    assert out.input == "Lis avec oto_procedure."
    assert out.descriptions_outils == {"defaut": 200, "entieres": ["oto_procedure"]}


def test_un_declencheur_declare_depuis_le_produit_stocke_le_canonique(tenant_acme, registre):
    inp = runner_triggers.TriggerInput(op="create", tools=["acme_doc"],
                                       input="Écris avec `acme_doc`.")
    out = runner_triggers._noms_canoniques(ResolvedCtx(sub=_SUB_TENANT, org_id=1), inp)
    assert out.tools == ["oto_doc"]
    assert out.input == "Écris avec `oto_doc`."
