"""Deux préfixes `linkedin_`, deux CHOSES différentes, et la règle qui les sépare.

- `linkedin_*` — **ta session LinkedIn**, opérée pour toi (recherche connectée,
  profils, posts, réseau, jobs, messagerie). Elle rend ce que TON compte voit, sous
  ton identité, au rythme que LinkedIn t'impose. Sa clé est celle du compte hébergé.
- `linkedin_aiark_*` — **de la donnée achetée au crédit** chez AI Ark : recherche de
  personnes et de sociétés, export d'une personne AVEC son email, reverse-lookup
  depuis un contact, mobile. Aucun compte connecté, facturé au résultat, clé AI Ark.

Ce ne sont pas deux fournisseurs interchangeables d'une même capacité :
`linkedin_chat` n'a aucun équivalent AI Ark, `linkedin_aiark_person(op="mobile")`
aucun équivalent dans la session. Router silencieusement de l'une à l'autre
produirait des trous que l'agent lirait comme des absences de résultat.

**Le nom nu appartient à la session depuis le 2026-08-28** — il ne lui a pas été
pris : il avait été LIBÉRÉ le 2026-08-10 par le dépôt de l'ex-connecteur `linkedin`
(AI Ark en app-credits, #231, même vendeur et même client qu'`aiark`, il n'en
différait que par le mode d'auth). `aiark` garde son namespace `linkedin_aiark`.

Ce que ce fichier verrouille, c'est la cohabitation : `namespace_of` résout au plus
long préfixe DÉCLARÉ au registre, donc `linkedin_aiark_person` va à `aiark` et
`linkedin_post` va à `linkedin`. Sans cette règle, le 1er token enverrait les tools
d'AI Ark sous la session — mauvaise clé, mauvaise activation, mauvaise sélection.
C'est LE cas d'usage de la règle, et il est vivant.
"""
import asyncio

import pytest

from oto_mcp import providers
from oto_mcp.tool_visibility import namespace_of


@pytest.fixture(scope="module")
def all_tools():
    from fastmcp import FastMCP
    from oto_mcp.tools import register_all

    m = FastMCP("t")
    register_all(m)
    return {t.name for t in asyncio.run(m._list_tools())}


# --- chaque préfixe va à SON connecteur ---------------------------------------

@pytest.mark.parametrize("prefix,connector", [
    ("linkedin_aiark_", "aiark"),     # le plus long préfixe déclaré gagne…
    ("linkedin_", "linkedin"),        # …et le nu prend le reste
])
def test_namespace_resolves_to_its_own_connector(all_tools, prefix, connector):
    """Le gate d'un tool suit son NAMESPACE. Sans la résolution au plus long préfixe
    DÉCLARÉ, le 1er token (`linkedin`) mettrait les deux familles sous le même
    connecteur, et le gate en désignerait une au hasard.

    ⚠️ Le filtre du cas `linkedin_` doit EXCLURE `linkedin_aiark_` — un préfixe est
    préfixe de l'autre, c'est toute la difficulté. Sans l'exclusion, ce test-ci
    échouerait ; c'est aussi pour ça que le code, lui, va du plus long au plus court."""
    tools = {t for t in all_tools if t.startswith(prefix)}
    if prefix == "linkedin_":
        tools -= {t for t in tools if t.startswith("linkedin_aiark_")}
    assert tools, f"aucun tool {prefix}* monté"
    for t in tools:
        assert namespace_of(t) == prefix.rstrip("_")
        assert providers.connector_for_namespace(namespace_of(t)).name == connector


def test_les_deux_familles_sont_disjointes(all_tools):
    session = {t for t in all_tools if t.startswith("linkedin_")
               and not t.startswith("linkedin_aiark_")}
    donnee = {t for t in all_tools if t.startswith("linkedin_aiark_")}
    assert session and donnee
    assert session.isdisjoint(donnee)


# --- ce que chacun EST, structurellement --------------------------------------

def test_la_session_se_connecte_et_emprunte_sa_cle():
    con = providers.REGISTRY["linkedin"]
    assert con.namespaces == ("linkedin",)
    assert con.hosted_channel == "LINKEDIN"      # ça se CONNECTE
    assert con.credential_of == "unipile"        # et ça emprunte la clé du compte
    assert "linkedin" not in providers.CREDENTIAL_PROVIDERS


def test_aiark_detient_sa_cle_et_reste_intact():
    """AI Ark n'a pas bougé : namespace, credential et mode plateforme d'origine.

    Le seul geste du lot le concernant est de NE PAS lui prendre son namespace —
    d'où l'assertion sur `linkedin_aiark`, qui échouerait si quelqu'un « nettoyait »
    ce préfixe en le trouvant redondant. Il ne l'est pas : c'est lui qui empêche ses
    tools de tomber sous la session."""
    con = providers.REGISTRY["aiark"]
    assert con.namespaces == ("linkedin_aiark",)
    assert con.credential_of is None             # il DÉTIENT sa clé
    assert "aiark" in providers.CREDENTIAL_PROVIDERS
    assert "platform" in con.auth_modes          # packaging « offert par oto » (#231)
    assert providers.is_byo_user("aiark")        # et le BYO reste ouvert


def test_lex_connecteur_linkedin_ne_revient_pas_comme_donnee_achetee():
    """Le nom nu désigne la session, et rien d'autre. S'il se remettait à pointer un
    connecteur qui DÉTIENT une clé, c'est qu'on aurait reconfondu les deux."""
    assert providers.REGISTRY["linkedin"].credential_of == "unipile"
    assert providers.connector_for_namespace("linkedin").name == "linkedin"
