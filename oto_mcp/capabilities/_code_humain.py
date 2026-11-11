"""Exécuter du code non publié est un geste de PERSONNE, jamais d'un agent hébergé.

Tester ou publier une fonction, éprouver ou publier une recette-code, c'est exécuter un
code que personne n'a encore mis en service — souvent écrit par un autre. Ces gestes
sont déjà réservés à la plateforme ; ils le sont aussi à une personne : sous un jeton de
délégation (`verrou_org.travail_de_la_requete`, posé au bord MCP et au bord REST pour
un travail hébergé), un agent qui lit des données tierces les ferait sur une injection
de prompt. Refuser une version proposée suit la même règle : c'est la décision qui
accompagne la publication. Le motif de `platform_tools_human_only`.

Le client déclaré par la session (`client_name`) n'entre pas en compte : il se lit,
il ne s'oppose pas."""
from __future__ import annotations

from ._personne_seule import depuis_un_agent_heberge
from ._types import AuthzDenied, DeclaredError

CODE = "code_human_only"

REFUS = DeclaredError(
    403, CODE,
    "un geste qui exécute ou décide du code non publié (fonction : test, publish, refuse ; "
    "recette-code : test, publish) depuis un agent hébergé (jeton de délégation)")


def exiger_une_personne(op: str) -> None:
    """Lève `code_human_only` sous un jeton de délégation ; hors de lui, rien ne change."""
    if depuis_un_agent_heberge():
        raise AuthzDenied(403, "code_human_only",
                          f"op={op} runs or decides unpublished code: a person does it, "
                          "from their own account, never a hosted agent.")
