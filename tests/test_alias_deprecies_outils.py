"""Un outil renommé répond sous ses DEUX noms — et l'ancien annonce sa date (#519).

Lot B1 de #519 : le produit ne dit plus « doctrine », il dit **guide**. Renommer un
outil MCP casserait tout appelant qui vit hors de ce dépôt — dashboard, extension,
CLI, fronts partenaires, flotte d'agents, et toute la prose déjà écrite (procédures
d'org, guides, corps de guide) qui cite l'ancien nom. Alors on ne renomme pas : on
DOUBLE. Le nouveau nom naît, l'ancien continue de répondre, et il porte en tête de
sa description la date à laquelle il s'en va (lot D, #526).

Ce que ces tests gardent, dans l'ordre de ce qui coûterait le plus cher :

1. **L'ancien nom appelle le nouvel outil**, avec les mêmes arguments, à l'octet.
   C'est le seul invariant qui compte pour un appelant : sa requête d'hier aboutit.
2. **Le serveur ne voit JAMAIS l'ancien nom.** La traduction est faite au bord du
   protocole ; tout ce qui est en aval — gates de contexte d'appel, denylist de
   visibilité, journal `tool_calls`, refs `<tool:slug>` — continue de lire un seul
   nom pour un seul outil. Sans ça, un toggle posé sur l'ancien nom ne mordrait pas
   sur le nouveau, et le journal compterait un outil deux fois.
3. **Les deux entrées de `tools/list` sont identiques** hors le nom et l'avis :
   schéma d'entrée, schéma de sortie, annotations. Un alias qui dérive de son outil
   est un piège — l'agent lit un contrat et en appelle un autre.
4. **La date est écrite une fois** (`deprecations.RETRAIT`) et recopiée depuis là.
   Une date en dur dans une description est une date qu'on oubliera de décaler.
5. **La table ne ment pas** : chaque cible est un outil réellement monté, et aucun
   ancien nom ne recouvre un outil réel.
"""
from __future__ import annotations

from _mcp_app import static_mcp as _test_mcp

import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext

from oto_mcp import deprecations, server
from oto_mcp.middleware.alias import ToolAliasMiddleware

_ARGS = {"op": "list", "org_id": 1}


async def _renvoie(valeur):
    return valeur


async def _liste_servie(monkeypatch, sub=None):
    """La liste telle qu'un client la reçoit — le VRAI middleware sur les VRAIS
    outils montés."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: sub)
    tools = await _test_mcp().list_tools(run_middleware=False)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    return await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie(tools))


async def _recu_en_aval(monkeypatch, demande, args, sub=None):
    """(nom, arguments) que la chaîne EN AVAL du middleware voit passer."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: sub)
    ctx = MiddlewareContext(
        message=mt.CallToolRequestParams(name=demande, arguments=dict(args)),
        method="tools/call")
    vus = []

    async def _suivant(c):
        vus.append((c.message.name, dict(c.message.arguments or {})))
        return "réponse"

    assert await ToolAliasMiddleware().on_call_tool(ctx, _suivant) == "réponse"
    return vus[0]


# ── 1 & 2. L'ancien nom appelle le nouvel outil, et lui seul ─────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("ancien,canonique", sorted(deprecations.TOOLS.items()))
async def test_lancien_nom_appelle_exactement_le_nouvel_outil(
        monkeypatch, ancien, canonique):
    """L'empreinte de ce qui atteint le serveur est la MÊME pour les deux noms."""
    par_lancien = await _recu_en_aval(monkeypatch, ancien, _ARGS)
    par_le_neuf = await _recu_en_aval(monkeypatch, canonique, _ARGS)
    assert par_lancien == par_le_neuf == (canonique, _ARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("ancien", sorted(deprecations.TOOLS))
async def test_le_serveur_ne_connait_pas_lancien_nom(ancien):
    """L'alias vit au BORD du protocole, pas dans le registre : c'est ce qui garantit
    qu'aucun gate, aucune denylist et aucun journal n'a deux noms à tenir en phase."""
    noms = {t.name for t in await _test_mcp().list_tools(run_middleware=False)}
    assert ancien not in noms, (
        f"`{ancien}` est monté comme un VRAI outil. Un alias de dépréciation se sert "
        "au bord du protocole ; monté, il doublerait le journal, échapperait au "
        "toggle posé sur son canonique, et survivrait au lot D sans qu'on le voie.")


# ── 3. Les deux entrées servies décrivent le même contrat ───────────────────

@pytest.mark.asyncio
async def test_la_liste_sert_les_deux_noms(monkeypatch):
    noms = {t.name for t in await _liste_servie(monkeypatch)}
    for ancien, canonique in deprecations.TOOLS.items():
        assert canonique in noms, f"`{canonique}` n'est pas servi"
        assert ancien in noms, (
            f"`{ancien}` a disparu de `tools/list` avant sa date de retrait — un "
            "client qui ne lit que la liste ne saura jamais qu'il devait migrer.")


@pytest.mark.asyncio
async def test_lalias_decrit_le_meme_contrat_que_son_outil(monkeypatch):
    """Hors le nom et l'avis en tête : même schéma d'entrée, même schéma de sortie."""
    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    for ancien, canonique in deprecations.TOOLS.items():
        a, c = servis[ancien], servis[canonique]
        assert (a.model_dump(exclude={"name", "description"})
                == c.model_dump(exclude={"name", "description"})), (
            f"`{ancien}` a dérivé de `{canonique}` : l'agent lirait un contrat et en "
            "appellerait un autre.")
        assert a.description == deprecations.avis(canonique) + (c.description or "")


@pytest.mark.asyncio
async def test_lavis_est_en_TETE_de_la_description(monkeypatch):
    """En tête, pas en queue : un client qui tronque à 400 caractères n'aurait averti
    personne, et c'est la première chose que le modèle lit pour choisir."""
    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    for ancien, canonique in deprecations.TOOLS.items():
        debut = servis[ancien].description[:80]
        assert debut.startswith("Déprécié : utilisez `" + canonique + "`")
        assert deprecations.date_de_retrait() in debut


# ── 4. La date vit à UN endroit ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_la_date_servie_suit_la_constante(monkeypatch):
    """Décaler le retrait doit être UN geste. Si ce test tombe pour une date écrite
    en dur ailleurs, c'est la date en dur qu'il faut retirer, pas ce test."""
    import datetime
    faux = datetime.date(2027, 1, 15)
    monkeypatch.setattr(deprecations, "RETRAIT", faux)
    servis = {t.name: t for t in await _liste_servie(monkeypatch)}
    for ancien in deprecations.TOOLS:
        assert "15/01/2027" in servis[ancien].description


# ── 5. La table ne ment pas ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chaque_cible_est_un_outil_reellement_monte():
    noms = {t.name for t in await _test_mcp().list_tools(run_middleware=False)}
    orphelines = sorted({c for c in deprecations.TOOLS.values() if c not in noms})
    assert not orphelines, (
        f"{orphelines} : cibles d'un alias déprécié, absentes du registre. L'ancien "
        "nom serait alors servi et injoignable — pire qu'un retrait sec.")


@pytest.mark.asyncio
async def test_aucun_ancien_nom_ne_recouvre_un_outil_reel(monkeypatch):
    """Ceinture ET bretelles : une collision ferait éclipser un outil par un alias,
    sans que rien ne le dise. La liste servie garde le nom RÉEL."""
    vrais = [t for t in await _test_mcp().list_tools(run_middleware=False)
             if t.name in deprecations.TOOLS.values()]
    assert vrais, "aucun outil déprécié monté — table vide ou cible écorchée"
    usurpateur = vrais[0].model_copy(update={"name": next(iter(deprecations.TOOLS))})
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: None)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(
        ctx, lambda _c: _renvoie(vrais + [usurpateur]))
    noms = [t.name for t in rendus]
    assert noms.count(usurpateur.name) == 1, "un alias a écrasé un nom déjà pris"


@pytest.mark.asyncio
async def test_un_outil_absent_de_la_liste_ne_revient_pas_par_son_ancien_nom(
        monkeypatch):
    """L'alias est dérivé de la liste RÉELLEMENT servie : il hérite du filtrage de
    visibilité. Un outil masqué pour ce compte ne doit pas réapparaître par la porte
    de derrière — ce serait un contournement de la denylist, pas une compatibilité."""
    monkeypatch.setattr("oto_mcp.middleware.alias.current_user_sub_from_token",
                        lambda: None)
    ctx = MiddlewareContext(message=mt.ListToolsRequest(method="tools/list"),
                            method="tools/list")
    rendus = await ToolAliasMiddleware().on_list_tools(ctx, lambda _c: _renvoie([]))
    assert rendus == []


def test_le_retrait_est_une_date_a_venir_ou_le_lot_D_est_du():
    """Un alias qui a dépassé sa date n'est plus un préavis : c'est un mensonge servi.
    Ce test devient la sonnerie — quand il rougit, le lot D (#526) est dû."""
    import datetime
    assert deprecations.RETRAIT >= datetime.date.today(), (
        f"La date de retrait ({deprecations.date_de_retrait()}) est passée et les "
        f"alias sont toujours servis : {sorted(deprecations.TOOLS)}. Retire-les "
        "(lot D, #526) ou décale RETRAIT en le décidant — mais ne sers plus une date "
        "dépassée à des intégrateurs qui la lisent.")

# ── 6. Le préavis tient l'engagement CONTRACTUEL ────────────────────────────
# L'Art 8.2 du contrat de service promet un préavis de deux mois avant toute rupture
# d'interface (référence et constat d'écart : #767). Ces deux tests existent pour qu'on ne
# puisse pas raccourcir ce préavis sans savoir ce qu'on touche — il a valu 30 jours du
# 29/08 au 01/09/2026 (#767), et personne ne l'avait décidé contre l'article.


def test_le_preavis_annonce_tient_les_deux_mois_ecrits_au_contrat():
    """PLANCHER, pas égalité : allonger un préavis est toujours favorable à celui à
    qui il est dû ; descendre sous deux mois est un manquement, pas un réglage."""
    import datetime
    assert deprecations.PREAVIS_MOIS >= 2, (
        f"Le préavis servi est de {deprecations.PREAVIS_MOIS} mois. L'Art 8.2 en "
        "promet DEUX avant toute rupture d'interface : ce chiffre n'est pas un "
        "réglage d'opportunité, c'est ce qui a été écrit au client. Pour le "
        "raccourcir, il faut d'abord amender l'engagement.")
    plancher = deprecations._plus_de_mois(deprecations.ANNONCE, 2)
    assert deprecations.RETRAIT >= plancher, (
        f"Retrait annoncé le {deprecations.date_de_retrait()}, soit AVANT les deux "
        f"mois dus depuis l'annonce du {deprecations.ANNONCE:%d/%m/%Y} (plancher : "
        f"{plancher:%d/%m/%Y}).")


def test_le_preavis_se_compte_en_mois_CALENDAIRES_jamais_en_jours():
    """Deux mois ≠ 60 jours. L'approximation en jours tombe un jour trop tôt neuf
    fois sur douze, et toujours du même côté : contre le consommateur. C'est la
    famille d'écart de #767/#768 — un jour pris sans que personne l'ait décidé."""
    import datetime
    d = deprecations._plus_de_mois
    assert d(datetime.date(2026, 8, 29), 2) == datetime.date(2026, 10, 29)
    # ...et l'approximation, elle, aurait rendu la veille.
    assert datetime.date(2026, 8, 29) + datetime.timedelta(days=60) \
        == datetime.date(2026, 10, 28)
    # fin de mois : on retombe sur le dernier jour existant, jamais sur un 31/02.
    assert d(datetime.date(2026, 12, 31), 2) == datetime.date(2027, 2, 28)
    assert d(datetime.date(2026, 11, 30), 2) == datetime.date(2027, 1, 30)
