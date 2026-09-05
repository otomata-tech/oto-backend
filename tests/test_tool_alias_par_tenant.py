"""Les outils portent le nom du PRODUIT du tenant, pas le nôtre.

Constaté chez un client sur le connecteur d'un partenaire : la conversation affichait
`Oto doc`, `Oto doc`, `Oto doc`… à chaque appel, sous SA marque, dans SON produit.
Même famille que le socle d'instructions (« Sur <tenant> (Oto), tu es… », 13/08) et que
les liens qui portaient notre domaine — sauf qu'ici ce n'est pas de la prose : c'est
l'identifiant d'un outil, et il est réaffiché à chaque tour.

Les invariants gardés ici, dans l'ordre de ce qui coûterait le plus cher :

1. **Inertie.** Un compte de la plateforme, et un tenant qui ne déclare pas de
   préfixe, voient l'octet d'avant. Déclarer un tenant ne renomme rien.
2. **Le serveur ne voit JAMAIS le nom du tenant.** La traduction retour est faite au
   bord du protocole : tout ce qui est en aval (gates `_org=`, rédaction par
   namespace, visibilité, journal `tool_calls`, références `<tool:slug>`) continue de
   lire un seul nom pour un seul outil.
3. **Les deux noms sont acceptés à l'appel.** La prose déjà écrite (procédures,
   guides, guides) cite les canoniques : un agent qui suit une procédure de
   2026-07 doit continuer à aboutir.
4. **La liste et la consigne parlent la même langue.** Traduire la liste sans traduire
   la prose injectée ferait rappeler `oto_doc` par l'agent — et réafficher `Oto doc`
   à l'écran, exactement le défaut qu'on corrige.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import re

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext

from oto_mcp import instructions, providers, server, tenancy, tool_alias
from oto_mcp.middleware.alias import ToolAliasMiddleware

_SUB_TENANT = "acme:u-1"
_SUB_PLATEFORME = "bn01jfy76a5n"


def _registre(**colonnes):
    """Le registre d'émetteurs avec un tenant `acme` porteur des colonnes données."""
    return tenancy.IssuerRegistry(tenancy.build(
        "https://auth.oto.ninja/oidc",
        tenants=[{"slug": "acme", "name": "Acme",
                  "issuer": "https://auth.acme.test/oidc", **colonnes}]))


@pytest.fixture
def tenant_avec_prefixe():
    avant = tenancy.current()
    tenancy.install(_registre(tool_prefix="acme"))
    yield
    tenancy.install(avant)


@pytest.fixture
def tenant_sans_prefixe():
    avant = tenancy.current()
    tenancy.install(_registre())
    yield
    tenancy.install(avant)


def _comme(monkeypatch, sub):
    """Le middleware et les tools méta lisent l'identité par le même hook."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token", lambda: sub)


async def _liste_servie(monkeypatch, sub):
    """La liste telle qu'un client la reçoit — à travers le VRAI middleware, sur les
    VRAIS outils montés (le montage réel, pas trois objets de fixture)."""
    _comme(monkeypatch, sub)
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    return await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie(tools))


async def _renvoie(valeur):
    return valeur


async def _nom_recu_par_le_serveur(monkeypatch, sub, demande):
    """Le nom que la chaîne EN AVAL du middleware voit passer."""
    _comme(monkeypatch, sub)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name=demande, arguments={}),
        method="tools/call")
    vus: list[str] = []

    async def _suivant(c):
        vus.append(c.message.name)
        return "ok"

    assert await ToolAliasMiddleware().on_call_tool(ctx, _suivant) == "ok"
    return vus[0]


# ── 1. Inertie ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_un_compte_de_la_plateforme_voit_les_noms_canoniques(
        tenant_avec_prefixe, monkeypatch):
    noms = {t.name for t in await _liste_servie(monkeypatch, _SUB_PLATEFORME)}
    assert "oto_doc" in noms and not any(n.startswith("acme_") for n in noms)


@pytest.mark.asyncio
async def test_un_tenant_qui_ne_declare_rien_ne_renomme_rien(
        tenant_sans_prefixe, monkeypatch):
    """LE garde-fou d'inertie : déclarer un tenant ne touche pas à ses outils. Un
    renommage rompt ses procédures et sa prose — ça se décide, ça ne s'attrape pas."""
    noms = {t.name for t in await _liste_servie(monkeypatch, _SUB_TENANT)}
    assert "oto_doc" in noms


@pytest.mark.asyncio
async def test_sans_compte_la_liste_est_inchangee(tenant_avec_prefixe, monkeypatch):
    """L'endpoint anonyme d'un projet publié et la découverte n'ont pas de sub."""
    noms = {t.name for t in await _liste_servie(monkeypatch, None)}
    assert "oto_doc" in noms


# ── 2. Ce que voit l'utilisateur, ce que voit le serveur ─────────────────────

@pytest.mark.asyncio
async def test_la_liste_servie_porte_le_nom_du_produit(tenant_avec_prefixe, monkeypatch):
    noms = {t.name for t in await _liste_servie(monkeypatch, _SUB_TENANT)}
    assert "acme_doc" in noms and "oto_doc" not in noms
    # Seul le namespace de la plateforme bouge : `data_*`, `run_*`, `feedback` et les
    # connecteurs portent une CAPACITÉ ou un FOURNISSEUR, pas notre marque.
    assert "data_write" in noms and "run_start" in noms and "feedback" in noms


@pytest.mark.asyncio
async def test_le_serveur_ne_voit_jamais_le_nom_du_tenant(
        tenant_avec_prefixe, monkeypatch):
    """L'invariant qui protège le journal, les gates et les toggles : un seul nom pour
    un seul outil, partout en aval."""
    assert await _nom_recu_par_le_serveur(monkeypatch, _SUB_TENANT, "acme_doc") == "oto_doc"


@pytest.mark.asyncio
async def test_le_nom_canonique_reste_appelable(tenant_avec_prefixe, monkeypatch):
    """Les procédures, guides et guides déjà écrits citent les canoniques — et
    personne ne peut les réécrire d'un coup."""
    assert await _nom_recu_par_le_serveur(monkeypatch, _SUB_TENANT, "oto_doc") == "oto_doc"


@pytest.mark.asyncio
async def test_un_outil_hors_plateforme_traverse_intact(
        tenant_avec_prefixe, monkeypatch):
    assert await _nom_recu_par_le_serveur(
        monkeypatch, _SUB_TENANT, "data_write") == "data_write"


@pytest.mark.asyncio
async def test_la_traduction_est_reversible_sur_tout_le_registre(
        tenant_avec_prefixe, monkeypatch):
    """Sur le montage RÉEL : aucun nom ne se perd à l'aller-retour, sinon un outil
    listé serait injoignable."""
    tools = await _test_mcp().list_tools(run_middleware=False)
    for t in tools:
        assert tool_alias.canonical(tool_alias.public(t.name, "acme"), "acme") == t.name


# ── 3. Ce qu'un préfixe n'a pas le droit d'être ──────────────────────────────

def test_un_prefixe_qui_est_un_namespace_de_connecteur_est_refuse():
    """`apollo_search` désignerait alors à la fois l'alias d'`oto_search` et l'outil
    réel du connecteur : la traduction retour choisirait au hasard lequel exécuter.
    Exercé contre le VRAI registre, pas contre une liste recopiée."""
    assert providers.connector_for_namespace("apollo") is not None, (
        "connecteur témoin disparu — choisir un autre namespace réellement déclaré")
    assert tool_alias.normalize_prefix("apollo") == ""


@pytest.mark.parametrize("spine", ["oto", "data", "run", "feedback"])
def test_un_prefixe_spine_est_refuse(spine):
    assert tool_alias.normalize_prefix(spine) == ""


@pytest.mark.parametrize("mauvais", ["", "Acme", "ac me", "acme-corp", "acme_corp",
                                     "1acme", "a", "a" * 30, None])
def test_une_forme_impossible_pour_un_nom_doutil_est_refusee(mauvais):
    assert tool_alias.normalize_prefix(mauvais) == ""


@pytest.mark.asyncio
async def test_un_prefixe_refuse_laisse_les_noms_canoniques(monkeypatch):
    """Un refus dégrade vers l'état d'avant — il ne coupe pas la session."""
    avant = tenancy.current()
    tenancy.install(_registre(tool_prefix="apollo"))
    try:
        noms = {t.name for t in await _liste_servie(monkeypatch, _SUB_TENANT)}
        assert "oto_doc" in noms and "apollo_doc" not in noms
    finally:
        tenancy.install(avant)


@pytest.mark.asyncio
async def test_un_alias_ne_recouvre_jamais_un_outil_reel(
        tenant_avec_prefixe, monkeypatch):
    """Ceinture ET bretelles : `normalize_prefix` interdit déjà le cas, mais si un
    alias tombait sur un nom pris, il éclipserait un outil sans que rien ne le dise."""
    _comme(monkeypatch, _SUB_TENANT)
    faux = [t for t in await _test_mcp().list_tools(run_middleware=False)
            if t.name in ("oto_doc", "oto_whoami")]
    assert len(faux) == 2
    occupant = faux[0].model_copy(update={"name": "acme_doc"})
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(
        ctx, lambda _c: _renvoie(faux + [occupant]))
    noms = [t.name for t in rendus]
    assert noms.count("acme_doc") == 1, "un alias a écrasé un outil réel"
    assert "oto_doc" in noms, "l'outil éclipsé doit rester servi sous son nom canonique"


# ── 4. La liste et la consigne parlent la même langue ────────────────────────

@pytest.mark.asyncio
async def test_la_prose_injectee_cite_les_noms_du_produit(
        tenant_avec_prefixe, monkeypatch):
    """Traduire la liste sans traduire la consigne ne corrige rien : l'agent
    rappellerait `oto_doc`, et le client réafficherait `Oto doc`."""
    monkeypatch.setattr(instructions, "_c_layers", lambda sub, org_id: [])
    artefact = instructions.compose_session(_SUB_TENANT, None)
    assert "acme_procedure" in artefact
    assert not re.search(r"\boto_[a-z]", artefact)
    # Et l'invariant de composition tient toujours (vue de transparence).
    couches = instructions.session_layers(_SUB_TENANT, None)
    assert "\n\n".join(c["body"] for c in couches if c["body"]) == artefact


@pytest.mark.asyncio
async def test_la_prose_dun_compte_plateforme_est_inchangee(
        tenant_avec_prefixe, monkeypatch):
    monkeypatch.setattr(instructions, "_c_layers", lambda sub, org_id: [])
    assert "oto_procedure" in instructions.compose_session(_SUB_PLATEFORME, None)


def test_tout_token_oto_de_la_prose_servie_est_bien_un_outil():
    """LE garde-fou de la réécriture de prose, sur le montage RÉEL.

    `rewrite_prose` traduit tout token `oto_<identifiant>` : elle ne peut le faire
    que parce que cet espace de noms est réservé aux outils de la plateforme. Ce test
    est ce qui rend l'hypothèse vérifiable — il mord si quelqu'un écrit un `oto_…`
    qui n'est pas un outil (ou en écorche le nom), car il serait alors traduit en un
    identifiant qui ne désigne rien.

    Une famille écrite au pluriel (`oto_use_*`) compte : elle PRÉFIXE des outils
    réels, et sa traduction reste juste.
    """
    import asyncio
    noms = {t.name for t in asyncio.run(_test_mcp().list_tools(run_middleware=False))}
    tokens = set(re.findall(r"\boto_[a-z][a-z0-9_]*\b", instructions.render()))
    assert tokens, "la prose servie ne cite plus aucun outil — vérifier le seed"
    orphelins = [t for t in tokens
                 if t not in noms and not any(n.startswith(t) for n in noms)]
    assert not orphelins, (
        f"{orphelins} : cités dans la prose servie, absents du registre. Un nom "
        "d'outil écorché y est invisible — et le renommage par tenant le traduirait "
        "en un identifiant qui ne désigne rien.")


# ── 5. Le catalogue et le dispatch parlent la même langue ────────────────────
#
# Cinq tools prennent un NOM en argument (`oto_list_my_tools`, `oto_tool_schema`,
# `oto_call`, `oto_disable_tool`, `oto_enable_tool`) : ce sont les seuls endroits où
# un nom traverse un HANDLER au lieu du bord du protocole, donc les seuls que le
# middleware ne couvre pas. Sans eux, un compte de tenant lisait `acme_doc` dans son
# catalogue et se voyait répondre « Unknown tool » en le recopiant.

import fastmcp.server.context as _fc  # noqa: E402
from oto_mcp.mcp_errors import McpError  # noqa: E402

from oto_mcp import access, db  # noqa: E402
from oto_mcp.tools import meta as _meta  # noqa: E402


@pytest.fixture
def compte_acme(tenant_avec_prefixe, monkeypatch):
    """Un compte du tenant `acme`, vu par les tools méta."""
    monkeypatch.setattr(_meta, "current_user_sub_from_token", lambda: _SUB_TENANT)


async def _appelle(nom_du_tool: str, args: dict):
    """Le tool RÉELLEMENT monté, exécuté dans un contexte fastmcp — pas la fonction
    Python recopiée (elle est une closure de `register`, invisible d'ici)."""
    tool = await _test_mcp().get_tool(nom_du_tool)
    async with _fc.Context(fastmcp=_test_mcp()):
        return (await tool.run(args)).structured_content


@pytest.mark.asyncio
async def test_le_schema_se_lit_sous_le_nom_montre(compte_acme):
    out = await _appelle("oto_tool_schema", {"name": "acme_doc"})
    assert out["name"] == "acme_doc", "l'agent recopie ce nom dans son appel suivant"
    assert out["namespace"] == "acme", "le namespace interne n'a rien à faire là"
    assert out["input_schema"], "c'est bien le schéma d'`oto_doc` qui a été résolu"


@pytest.mark.asyncio
async def test_le_schema_reste_lisible_sous_le_nom_canonique(compte_acme):
    """Une procédure écrite avant le renommage cite `oto_doc` : elle doit aboutir."""
    assert (await _appelle("oto_tool_schema", {"name": "oto_doc"}))["name"] == "acme_doc"


@pytest.mark.asyncio
async def test_un_nom_inconnu_est_renvoye_TEL_QUE_TAPE(compte_acme):
    """Répondre « Unknown tool `oto_nawak` » à quelqu'un qui a tapé `acme_nawak` le
    ferait douter de sa propre requête, et lui montrerait un nom interne."""
    with pytest.raises(McpError) as ei:
        await _appelle("oto_tool_schema", {"name": "acme_nawak"})
    assert "acme_nawak" in str(ei.value) and "oto_nawak" not in str(ei.value)


@pytest.mark.asyncio
async def test_oto_call_reconnait_un_spine_sous_son_nom_de_produit(compte_acme):
    """Le gate méta/spine (anti-boucle, ADR 0036) raisonne sur le namespace : sans
    remise en canonique, `acme_doc` résout un namespace inconnu, passe le gate, et
    part se faire dispatcher."""
    with pytest.raises(McpError) as ei:
        await _appelle("oto_call", {"name": "acme_doc", "arguments": {}})
    assert "méta/spine" in str(ei.value) and "acme_doc" in str(ei.value)


@pytest.mark.asyncio
async def test_le_catalogue_annonce_les_noms_du_produit(compte_acme, monkeypatch):
    monkeypatch.setattr(access, "current_org", lambda sub: 1)
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    monkeypatch.setattr(access, "org_admin_hidden_tools", lambda org: frozenset())
    monkeypatch.setattr(access, "group_admin_hidden_tools", lambda g: frozenset())
    monkeypatch.setattr(db, "list_user_disabled_tools", lambda sub, org: [])
    monkeypatch.setattr(db, "list_user_enabled_tools", lambda sub, org: [])
    out = await _appelle("oto_list_my_tools", {})
    noms = {e["name"] for e in out["tools"]}
    assert "acme_doc" in noms and "acme_whoami" in noms
    assert not any(n.startswith("oto_") for n in noms)
    # Le reste du catalogue est intact — seul le namespace de la plateforme bouge.
    assert "data_write" in noms and "feedback" in noms
    # …et l'état de visibilité reste calculé sur le nom canonique : un outil
    # masqué-par-défaut doit toujours ressortir masqué sous son nom de produit.
    par_nom = {e["name"]: e for e in out["tools"]}
    assert par_nom["email_send"]["enabled"] is False


# ── 6. Tout ce que l'agent LIT, pas seulement ce qu'il appelle ───────────────
#
# Renommer la liste sans renommer ce qui la commente laisse fuir le nom interne par
# la porte de derrière : une description qui dit « resolve it with oto_kb », un
# premier mur qui dit « appelle-le via oto_call(…) ». L'agent obéit au nom près — et
# le client réaffiche notre marque chez quelqu'un qui n'est pas notre client.

from mcp.types import ErrorData, INVALID_PARAMS  # noqa: E402


@pytest.mark.asyncio
async def test_les_descriptions_citent_les_noms_du_produit(
        tenant_avec_prefixe, monkeypatch):
    """Sur le montage RÉEL : c'est sur la description que le modèle choisit."""
    tools = await _test_mcp().list_tools(run_middleware=False)
    citantes = [t.name for t in tools if "oto_" in (t.description or "")]
    assert citantes, ("aucune description ne cite d'outil — le cas que ce test garde "
                      "a disparu, vérifier avant de le supprimer")
    servis = {t.name: (t.description or "")
              for t in await _liste_servie(monkeypatch, _SUB_TENANT)}
    for canonique in citantes:
        desc = servis[tool_alias.public(canonique, "acme")]
        assert not re.search(r"\boto_[a-z]", desc), (
            f"la description de {canonique} renvoie encore à un nom interne")


@pytest.mark.asyncio
async def test_le_premier_mur_parle_la_langue_du_produit(
        tenant_avec_prefixe, monkeypatch):
    """`tool_not_mounted` est le texte le plus lu après le socle : il DIT quoi faire
    ensuite, en nommant des outils."""
    _comme(monkeypatch, _SUB_TENANT)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name="acme_doc", arguments={}),
        method="tools/call")

    async def _mur(_c):
        raise McpError(ErrorData(
            code=INVALID_PARAMS, message="`oto_doc` n'est pas monté.",
            data={"oto": {"code": "tool_not_mounted", "retryable": False,
                          "hint": "appelle-le via oto_call(name='oto_doc')"}}))

    with pytest.raises(McpError) as ei:
        await ToolAliasMiddleware().on_call_tool(ctx, _mur)
    assert "acme_doc" in ei.value.error.message and "oto_doc" not in ei.value.error.message
    assert ei.value.error.data["oto"]["hint"] == "appelle-le via acme_call(name='acme_doc')"
    assert ei.value.error.data["oto"]["code"] == "tool_not_mounted", "le reste intact"


@pytest.mark.asyncio
async def test_une_erreur_qui_ne_nomme_aucun_outil_traverse_intacte(
        tenant_avec_prefixe, monkeypatch):
    _comme(monkeypatch, _SUB_TENANT)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name="acme_doc", arguments={}),
        method="tools/call")
    origine = McpError(ErrorData(code=INVALID_PARAMS, message="quota dépassé"))

    async def _boum(_c):
        raise origine

    with pytest.raises(McpError) as ei:
        await ToolAliasMiddleware().on_call_tool(ctx, _boum)
    assert ei.value is origine, "aucune raison de reconstruire une erreur inchangée"


@pytest.mark.asyncio
async def test_un_data_de_forme_inattendue_ne_perd_ni_le_message_ni_les_donnees(
        tenant_avec_prefixe, monkeypatch):
    """`data` n'est pas toujours l'enveloppe `{oto: {...}}` — un tool peut y mettre ce
    qu'il veut (`oto_tool_schema` y met un schéma). La traduction ne touche alors que
    le message, et recopie `data` sans l'interpréter."""
    _comme(monkeypatch, _SUB_TENANT)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name="acme_doc", arguments={}),
        method="tools/call")
    origine = McpError(ErrorData(code=INVALID_PARAMS,
                                 message="oto_doc a échoué", data=["inattendu"]))

    async def _boum(_c):
        raise origine

    with pytest.raises(McpError) as ei:
        await ToolAliasMiddleware().on_call_tool(ctx, _boum)
    assert ei.value.error.message == "acme_doc a échoué"
    assert ei.value.error.data == ["inattendu"]


@pytest.mark.asyncio
async def test_une_erreur_a_traduire_ne_devient_jamais_une_erreur_de_traduction(
        tenant_avec_prefixe, monkeypatch):
    """LE chemin de rattrapage, PROVOQUÉ — une clause `except` que rien n'exerce ne
    prouve rien (conventions, 13/08). Ce code ne tourne que quand quelque chose a déjà
    mal tourné : y lever remplacerait un diagnostic utile par une trace sans rapport.
    """
    _comme(monkeypatch, _SUB_TENANT)

    def _casse(_texte, _prefix):
        raise RuntimeError("regex compilée de travers")

    monkeypatch.setattr(tool_alias, "rewrite_prose", _casse)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name="acme_doc", arguments={}),
        method="tools/call")
    origine = McpError(ErrorData(code=INVALID_PARAMS, message="oto_doc a échoué"))

    async def _boum(_c):
        raise origine

    with pytest.raises(McpError) as ei:
        await ToolAliasMiddleware().on_call_tool(ctx, _boum)
    assert ei.value is origine, "l'erreur d'origine doit survivre à sa traduction"


@pytest.mark.asyncio
async def test_un_renommage_qui_echoue_sert_la_liste_canonique(
        tenant_avec_prefixe, monkeypatch):
    """L'autre chemin de rattrapage, PROVOQUÉ. `tools/list` est le premier échange
    d'une session : une exception ici ne coûterait pas un nom mal affiché, elle
    coûterait la connexion.

    Ce qui est gardé : **aucun outil ne disparaît**, et aucun ne porte le nom du
    tenant. Pas l'égalité stricte des deux listes — le même hook sert aussi les
    alias DÉPRÉCIÉS (#519), qui ne dépendent pas du préfixe et n'ont donc aucune
    raison de tomber avec lui."""
    _comme(monkeypatch, _SUB_TENANT)
    monkeypatch.setattr(tool_alias, "public",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("boum")))
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie(tools))
    noms = {t.name for t in rendus}
    assert {t.name for t in tools} <= noms, "un outil a disparu de la liste servie"
    assert not any(n.startswith("acme_") for n in noms)


# ── 7. Le handshake annonce le produit, pas la plateforme ────────────────────

def _handshake():
    return mt.InitializeResult(
        protocolVersion="2025-06-18", capabilities=mt.ServerCapabilities(),
        serverInfo=mt.Implementation(name="oto", version="1.0.0"))


async def _serverinfo_servi(monkeypatch, sub):
    _comme(monkeypatch, sub)
    ctx = MiddlewareContext(
        message=mt.InitializeRequest(
            method="initialize",
            params=mt.InitializeRequestParams(
                protocolVersion="2025-06-18", capabilities=mt.ClientCapabilities(),
                clientInfo=mt.Implementation(name="client", version="0"))),
        method="initialize")
    rendu = await ToolAliasMiddleware().on_initialize(
        ctx, lambda _c: _renvoie(_handshake()))
    return rendu.serverInfo


@pytest.mark.asyncio
async def test_le_handshake_dun_compte_plateforme_est_inchange(
        tenant_avec_prefixe, monkeypatch):
    info = await _serverinfo_servi(monkeypatch, _SUB_PLATEFORME)
    assert (info.name, info.title) == ("oto", None)


@pytest.mark.asyncio
async def test_le_handshake_porte_le_nom_du_produit(tenant_avec_prefixe, monkeypatch):
    """`serverInfo.name` disait `oto` dans le produit du partenaire pendant que tous
    ses outils s'appelaient `acme_…`. `name` suit le préfixe (l'identifiant), `title`
    le nom déclaré du tenant (le libellé humain)."""
    info = await _serverinfo_servi(monkeypatch, _SUB_TENANT)
    assert (info.name, info.title) == ("acme", "Acme")
    assert info.version == "1.0.0", "la version reste celle du serveur"


@pytest.mark.asyncio
async def test_sans_prefixe_le_handshake_garde_lidentifiant_mais_prend_le_libelle(
        tenant_sans_prefixe, monkeypatch):
    """Un tenant sans `tool_prefix` garde `name=oto` (ses outils s'appellent encore
    `oto_…` — renommer l'un sans l'autre ferait diverger les deux) ; son `name` de
    tenant, lui, est DÉCLARÉ (il sert déjà la découverte PRM) donc le `title` le suit."""
    info = await _serverinfo_servi(monkeypatch, _SUB_TENANT)
    assert (info.name, info.title) == ("oto", "Acme")


@pytest.mark.asyncio
async def test_sans_compte_le_handshake_est_inchange(tenant_avec_prefixe, monkeypatch):
    info = await _serverinfo_servi(monkeypatch, None)
    assert (info.name, info.title) == ("oto", None)


@pytest.mark.asyncio
async def test_un_handshake_qui_echoue_sert_lannonce_canonique(
        tenant_avec_prefixe, monkeypatch):
    """Une identité d'affichage ne coûte jamais la connexion (fail-open, provoqué)."""
    _comme(monkeypatch, _SUB_TENANT)
    monkeypatch.setattr(tool_alias, "server_identity_for",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("boum")))
    ctx = MiddlewareContext(
        message=mt.InitializeRequest(
            method="initialize",
            params=mt.InitializeRequestParams(
                protocolVersion="2025-06-18", capabilities=mt.ClientCapabilities(),
                clientInfo=mt.Implementation(name="client", version="0"))),
        method="initialize")
    rendu = await ToolAliasMiddleware().on_initialize(
        ctx, lambda _c: _renvoie(_handshake()))
    assert rendu.serverInfo.name == "oto"
