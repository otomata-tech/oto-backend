"""Les outils qu'une cascade appellera pour une procédure qui la lance — lus dans sa prose,
jamais devinés.

Chaque étape d'une cascade est un appel ordinaire de l'outil de son fournisseur, avec SES
gardes, dont la liste d'outils d'un travail hébergé (`outils_du_travail`, voir le module du
paquet). Un agent qui a `waterfall_email_act` sans `apollo_match_person` voit donc chaque
étape Apollo refusée, et la ligne revient sans réponse (`no_step_answered`) : rien ne disait,
sur l'agent, qu'il lui manquait l'outil.

Né du 26/10/2026 : un agent hébergé dont la procédure lançait `waterfall_email_act` avec
`providers=["apollo", "aiark"]` a passé deux runs sans trouver une adresse, chaque étape
refusée au travail, et a conclu à un quota épuisé.

On ne lit que les fournisseurs ÉCRITS à côté de l'appel (`providers=[…]`, comme le prend
l'outil) : sans eux, l'ordre se résout au run (le réglage de la carte, sinon la table par
défaut, et seulement les fournisseurs dont une clé existe) — l'inférer ici mettrait toute
la table dans `missing_tools`, et une liste trop large est exactement ce que la liste d'un
agent existe pour empêcher. Comme pour les recettes : un plancher, jamais un plafond.
"""
from __future__ import annotations

import re

from .etape import Step
from .plan import STEPS_BY_NAMESPACE

#: Un appel de cascade cité dans la prose : l'outil d'un contact ou son mode tableau (`_act`).
_CASCADE = re.compile(r"\b(" + "|".join(STEPS_BY_NAMESPACE) + r")(?:_act)?\b")
#: Les outils de cascade (un contact, ou le mode tableau `_act`) : un agent qui n'en a
#: aucun n'appellera aucune étape.
CASCADES = frozenset(STEPS_BY_NAMESPACE) | frozenset(f"{n}_act" for n in STEPS_BY_NAMESPACE)
#: Les fournisseurs écrits à côté : `providers=["apollo", "aiark"]` (ou `providers: [...]`).
_FOURNISSEURS = re.compile(r"""\bproviders\s*[=:]\s*\[([^\]]*)\]""")
_CLE = re.compile(r"[a-z][a-z0-9_]*")
#: Jusqu'où, après le nom de la cascade, `providers=` appartient encore à SON appel : un
#: appel écrit en entier tient en quelques centaines de caractères.
_PORTEE = 800
#: La liste BLANCHE des outils d'étape qu'on PROPOSE à la personne qui pose un agent
#: (`tools_to_add`, `runner_triggers._appeles_par_ses_outils`) : des recherches et des
#: enrichissements d'un contact ou d'une société, rien d'autre. Écrite à la main, pas
#: déduite des effets déclarés (la plupart des outils d'étape ne déclarent rien encore) :
#: une étape neuve n'est proposée qu'une fois rangée ici ou dans `NON_PROPOSES`
#: (`tests/test_outils_non_accordes.py` le vérifie sur toute la table).
PROPOSABLES = frozenset({
    "apollo_enrich_organization", "apollo_match_person", "apollo_reveal_phone",
    "apollo_reveal_phone_result", "apollo_search_people", "cognism_enrich_account",
    "cognism_enrich_contact", "dropcontact_enrich",
    "dropcontact_result", "forager_person", "fr_get", "fr_search", "fullenrich_enrich_linkedin",
    "fullenrich_result", "hunter_email_finder", "kaspr_enrich_linkedin", "lemlist_database",
    "lemlist_enrich", "lemlist_enrich_result", "linkedin_aiark_person", "pipecorn",
    "linkedin_aiark_search", "linkedin_unipile_profile", "lusha_search_and_enrich",
    "serper_search", "theirstack_companies_search",
})
#: Les outils d'étape jamais proposés : l'étape s'en sert pour UN acteur fixé, mais
#: l'outil en lance n'importe lequel. Ils restent dans `missing_tools`, au geste.
#: `dataforseo` (servi par sa description depuis le 11/11/2026) : l'étape n'en lit que
#: `domain_technologies`, l'outil sert toutes les fonctions de DataForSEO, un crawl de site
#: facturé compris.
NON_PROPOSES = frozenset({"apify_run_sync", "dataforseo"})


def outils_de_l_etape(step: Step) -> list[str]:
    """Tout ce qu'une étape appelle : son outil, celui qui collecte une réponse tardive,
    celui qui révèle une trouvaille masquée, et ceux de ses variantes."""
    noms = [step.tool]
    if step.collect is not None:
        noms.append(step.collect.tool)
    if step.reveal:
        noms.append(step.reveal)
    for variante in step.variants:
        noms += outils_de_l_etape(variante)
    return noms


def etapes_des_cascades(texte: str) -> list[tuple[str, str]]:
    """`[(cascade telle que citée, outil)]` : les outils des étapes que la procédure nomme
    à côté d'un appel de cascade (`waterfall_email_act(… providers=["apollo"])`), dans
    l'ordre écrit. Une clé qu'aucune étape de cette cascade ne porte est ignorée : l'outil
    la refusera au run, ce n'est pas un outil qui manque."""
    texte = texte or ""
    appels = list(_CASCADE.finditer(texte))
    cites: list[tuple[str, str]] = []
    for i, appel in enumerate(appels):
        fin = appels[i + 1].start() if i + 1 < len(appels) else len(texte)
        trouve = _FOURNISSEURS.search(texte, appel.end(), min(fin, appel.end() + _PORTEE))
        if trouve is None:
            continue
        etapes = {s.key: s for s in STEPS_BY_NAMESPACE[appel.group(1)]}
        for cle in _CLE.findall(trouve.group(1).lower()):
            if cle in etapes:
                cites += [(appel.group(0), nom) for nom in outils_de_l_etape(etapes[cle])]
    return cites


def outils_des_cascades(texte: str) -> list[tuple[str, str]]:
    """`[(outil, qui l'appelle)]`, la forme de `missing_tools` (`needed_by`)."""
    return [(nom, f"`{cascade}` (providers)") for cascade, nom in etapes_des_cascades(texte)]
