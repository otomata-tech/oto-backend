"""Le SOCLE (spine) présenté à l'agent — déclaré ici, **peuplé par dérivation**.

⚠️ **Ce module a d'abord été posé dans `providers/`, et c'était une faute.** Le
répertoire `providers/` n'est pas « là où vivent les déclarations », c'est le REGISTRE
DES CONNECTEURS : `tests/test_providers_registry_snapshot.py` y tient l'invariant « un
domicile, un seul » et lit tout fichier du paquet comme un connecteur à inscrire dans
`_DECLARATIONS` — son allowlist d'infrastructure ne contient que `__init__` et `_model`,
et le préfixe `_` n'exempte de rien. Un `providers/_spine.py` y était donc lu comme un
connecteur livré que rien n'atteint : la suite complète est tombée sur le tronc le
08/09/2026 et a bloqué une session cliente. Élargir l'allowlist aurait émoussé une garde
qui protège du code livré et inatteignable — pour un fichier qui, lui, décrit ce que la
plateforme porte elle-même, pas un fournisseur tiers. Le domicile était le problème.

Le socle emprunte le RÉGIME des connecteurs, il n'habite pas leur maison : pendant de
`providers/<nom>.py` par la forme, pas par le rangement. Le connecteur déclare son
`label`/`help` à côté de lui-même et `render_namespace_catalog` en dérive une ligne :
la moitié « connecteurs » de la carte couvre TOUT le registre, elle grandit seule.

La moitié « plateforme » n'avait pas ce régime. C'était un tuple de quatre entrées
écrit à la main (`SPINE_CONCEPTS`), que rien ne faisait grandir : `oto_resource`,
`oto_doc`, `oto_kb`, `oto_project`, `oto_guide`, `oto_procedure`, `oto_identity` n'y
étaient nommés nulle part, alors que la carte s'annonce « le catalogue COMPLET des
capacités de la plateforme ». Un agent qui la lisait en concluait sincèrement qu'oto ne
sait pas transférer une ressource à une équipe — trois signaux faux le 08/09/2026
(#794, #809, #810), rétractés par #795 et #811, puis instruits par le signal #813, qui
a mesuré le coût : trois sessions et deux jours d'attente d'un correctif inexistant.

Ce n'était pas un déséquilibre éditorial, c'était un défaut de fabrication — la même
forme que la surcharge écrite à la main posée au-dessus d'une dérivation juste, qui a
fait mentir le catalogue public pendant deux mois et vingt et un jours.

**Le régime, désormais.** Une famille déclare ce qu'elle affiche (`display`), les
préfixes de noms d'outils qu'elle revendique (`prefixes`) et sa ligne (`help`).
`render_spine` reçoit les outils spine RÉELLEMENT montés et vérifie la couverture :
un outil qu'aucune famille ne revendique obtient **sa propre ligne**, marquée non
classée. Aucune omission silencieuse n'est donc possible — c'est la propriété qu'on
répare, et `tests/test_namespace_catalog.py` la prouve sur le registre vivant.

Ajouter un outil DANS une famille existante : rien à faire, il est déjà décrit.
Ajouter un outil hors des familles : la carte le dit d'elle-même, et le test le nomme
pour qu'on lui écrive sa ligne.

Module PUR (aucun import `oto_mcp` au niveau module) : la dérivation par défaut de
`spine_tool_names()` importe en corps de fonction. `providers/__init__.py` l'importe
de la même façon — cet agrégateur-là DOIT rester pur (sa docstring dit pourquoi), donc
il ne peut pas nous charger au niveau module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class SpineFamily:
    """Une famille du socle : ce qui s'affiche, ce qu'elle revendique, ce qu'elle dit.

    `display` est écrit (pas dérivé de l'appartenance) parce que la carte est rendue à
    DEUX moments — au boot pour `_SERVER_INSTRUCTIONS`, où seul le registre des
    capacités est peuplé, et en session où tout est monté. Un affichage dérivé de
    l'appartenance donnerait deux textes différents pour la même carte ; le texte servi
    doit être le même partout. La cohérence entre `display` et `prefixes` est tenue par
    un test, pas par la confiance."""
    display: str
    prefixes: tuple[str, ...]
    help: str


# L'ORDRE est l'ordre de LECTURE servi à l'agent : ce qu'il doit connaître d'abord en
# tête. Il ne gouverne aucun calcul.
SPINE_FAMILIES: tuple[SpineFamily, ...] = (
    SpineFamily(
        "oto_context / oto_whoami / oto_profile / oto_use_* / oto_clear_*",
        ("oto_context", "oto_whoami", "oto_profile", "oto_use_", "oto_clear_"),
        "contexte d'appel : les règles de travail de ton org et ce qu'oto sait de toi "
        "(à lire en premier), sous quel compte tu agis, et comment agir sous une autre "
        "org, équipe ou projet",
    ),
    SpineFamily(
        "oto_list_my_tools / oto_tool_schema / oto_call / oto_enable_tool / oto_disable_tool",
        ("oto_list_my_tools", "oto_tool_schema", "oto_call", "oto_enable_tool",
         "oto_disable_tool"),
        "les outils eux-mêmes : le catalogue, le schéma exact d'un outil, l'appel d'un "
        "outil NON listé (`oto_call`), et ce que tu affiches dans ta toolbox",
    ),
    SpineFamily(
        "oto_project*",
        ("oto_project",),
        "projets : le cadre de tout travail — créer, retrouver, y joindre des documents "
        "bruts (PDF, HTML…)",
    ),
    SpineFamily(
        "oto_doc* / oto_search",
        ("oto_doc", "oto_search"),
        "documents : les pages markdown d'un projet — les faits et sources d'un "
        "travail s'y capturent, dans son projet — et la recherche transverse sur tout "
        "ce qui est lisible",
    ),
    SpineFamily(
        "oto_resource* / oto_account_access",
        ("oto_resource", "oto_account_access"),
        "gouvernance d'une ressource possédée (ADR 0030) : propriétaire, TRANSFERT à une "
        "personne / une équipe / une org, partage, publication",
    ),
    SpineFamily(
        "data_*",
        ("data_",),
        "datastore tabulaire per-user (PG natif, schéma libre) : tableaux, lignes, "
        "schéma, partage, et la file de travail (claim/release)",
    ),
    SpineFamily(
        "oto_guide / oto_procedure",
        ("oto_guide", "oto_procedure"),
        "guides & procédures : les règles et le mode de travail de ton org (écrits par "
        "ses admins), ta propre note (`scope=user`) et la bibliothèque publique de "
        "procédures",
    ),
    SpineFamily(
        "oto_org* / oto_group / oto_list_orgs",
        ("oto_org", "oto_group", "oto_list_orgs"),
        "organisations & équipes : cycle de vie, membres, réglages (expéditeurs email, "
        "heures calmes), observabilité de ton org",
    ),
    SpineFamily(
        "oto_connector* / oto_instance / oto_identity",
        ("oto_connector", "oto_instance", "oto_identity"),
        "connecteurs : catalogue et installation, instances (connecteur × auth), règles "
        "d'accès interne, comptes connectés sous lesquels agir",
    ),
    SpineFamily(
        "email_send / oto_scheduled_emails",
        ("email_send", "oto_scheduled_emails"),
        "email & boîte : envoi per-org rendu à la charte (différé, heures calmes), file "
        "des envois programmés",
    ),
    SpineFamily(
        "run_* / feedback / oto_run_thread",
        ("run_", "feedback", "oto_run_"),
        "boucle d'usage : `run_start`/`run_finish` encadrent un déroulé, `oto_run_thread` "
        "suit un run hébergé, `feedback` remonte un manque ou un défaut",
    ),
    SpineFamily(
        "oto_fleet / oto_trigger",
        ("oto_fleet", "oto_trigger"),
        "agents hébergés DE L'ORG (flotte oto, clé de l'org) : ce que fait passer une "
        "flotte, sur quel tableau, et ses déclencheurs — pas les routines que "
        "l'assistant planifie pour lui-même (→ guide `procedure-en-routine`)",
    ),
    SpineFamily(
        "oto_function",
        ("oto_function",),
        "fonctions : du code pur stocké et exécuté par Oto pour un calcul exact "
        "(entrée JSON → résultat, avertissements, fichiers), versionné, publié par la "
        "plateforme après ses tests",
    ),
    SpineFamily(
        "oto_recipe",
        ("oto_recipe",),
        "recipes: move a connector tool's results into a table on the server, with no "
        "model reading or retyping the rows — written once, versioned, run on demand",
    ),
    SpineFamily(
        "oto_upload_url / oto_import",
        ("oto_upload_url", "oto_import"),
        "bring a big file into oto without passing it through the conversation: push "
        "it to a signed URL, or have the server fetch it (link, Drive, project file, "
        "attachment)",
    ),
    SpineFamily(
        "oto_node* / oto_shell",
        ("oto_node", "oto_shell"),
        "l'arbre de contenus et le rail de l'application — la surface que rend le "
        "dashboard (nœuds, pages de lignes)",
    ),
    SpineFamily(
        "oto_admin_*",
        ("oto_admin_",),
        "administration de la plateforme — comptes, orgs, tenants, clés, monitoring, "
        "signaux d'usage ; RÉSERVÉ aux administrateurs, refusé aux autres",
    ),
)


def family_of(name: str) -> Optional[SpineFamily]:
    """La famille qui revendique `name`, au PLUS LONG préfixe (même règle que
    `tool_visibility.namespace_of`). `None` = non classée → sa propre ligne."""
    best: Optional[SpineFamily] = None
    best_len = -1
    for fam in SPINE_FAMILIES:
        for p in fam.prefixes:
            if (name == p or name.startswith(p)) and len(p) > best_len:
                best, best_len = fam, len(p)
    return best


def spine_tool_names() -> tuple[str, ...]:
    """Les outils SPINE que cette dérivation voit : le **registre des capacités**
    (ADR 0009), peuplé à l'import — c'est le chemin par lequel toute capacité NEUVE
    arrive, et le seul disponible au boot, quand `_SERVER_INSTRUCTIONS` est construit
    et que l'instance FastMCP n'existe pas encore.

    ⚠️ Ce que cette fonction NE voit pas : les outils des modules historiques montés
    par `register_all` (`data_*` du datastore, `run_*`, `email_send`, les méta). Ils
    ne sont pas au registre des capacités, et l'instance FastMCP ne rend son
    inventaire qu'en asynchrone — impossible à lire ici sans changer la nature de
    l'appel. Ils sont couverts par leurs familles déclarées, et c'est
    `tests/test_namespace_catalog.py` qui vérifie la couverture sur l'instance
    RÉELLEMENT montée : la garantie « aucune omission » est tenue par le test, pas par
    cette dérivation-là.

    Un manque ici ne peut de toute façon pas retirer une ligne de la carte — les
    lignes sont déclarées ; il ne sert qu'à repérer un outil qu'aucune famille ne
    revendique.
    """
    try:
        from .capabilities import registry as _registry
    except Exception:   # noqa: SILENT — carte déclarée seule plutôt que boot cassé
        return ()
    noms = {c.mcp for c in _registry.caps_with_mcp() if c.mcp and c.is_exposed()}
    return tuple(sorted(n for n in noms if not is_connector_tool(n)))


def is_connector_tool(name: str) -> bool:
    """True si `name` appartient à un CONNECTEUR du registre — donc déjà présenté par
    la moitié « connecteurs » de la carte, et hors socle.

    Le filtre n'est pas cosmétique : quatre outils de connecteur sont déclarés comme
    des capacités (`salesforce_connect`, `zoho_connect`, `zohoanalytics_orgs`,
    `routine_fire`). Sans lui, ils apparaîtraient « non classés » au socle, donc
    DEUX FOIS dans la carte, dont une fois au mauvais endroit."""
    try:
        from .providers import connector_for_namespace
        from .tool_visibility import namespace_of
    except Exception:   # noqa: SILENT — sans registre, rien n'est un connecteur
        return False
    return connector_for_namespace(namespace_of(name)) is not None


def render_spine(tools: Optional[Iterable[str]] = None) -> list[str]:
    """Les lignes du socle : une par famille déclarée, puis une par outil que personne
    ne revendique. `tools=None` → dérivation par défaut."""
    noms = sorted(set(tools)) if tools is not None else list(spine_tool_names())
    lines = [f"• {fam.display} — {fam.help}" for fam in SPINE_FAMILIES]
    orphelins = [n for n in noms if family_of(n) is None]
    lines += [
        f"• {n} — capacité NON CLASSÉE : elle existe et s'appelle, aucune famille du "
        f"socle ne la décrit encore." for n in orphelins
    ]
    return lines
