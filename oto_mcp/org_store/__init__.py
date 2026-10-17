"""Façade du store d'ORGANISATION (package `org_store`).

Le palier org était un seul fichier de 1 773 lignes ; il est découpé par couture,
un module par question posée à l'org — **sans rien changer d'autre** : chaque bloc
a été déplacé verbatim, la surface `org_store.<symbole>` est identique au geste
près, et aucun appelant n'a bougé.

    orgs          la fiche d'org (CRUD, marque, baseline connecteurs, ancre KB, quota)
    members       l'appartenance + l'org MAISON (`org_members`, ADR 0023)
    vault         les secrets partagés de l'org (façade du coffre chiffré)
    settings      les réglages en colonne JSONB : redaction, email, MFA
    personal      l'org perso (`personal_of`) + le rattrapage de boot
    invitations   plateforme / org / équipe : émission, listing, acceptation
    instructions  les procédures (`org_instructions`) versionnées — plan CONTENU
    instruction_ownership  les mêmes, plan GOUVERNANCE (ADR 0030) : identité par
                  `id` surrogate, copie, déplacement, inventaires
    library       la bibliothèque publique de guides (vue `guide_library`)
    essai         la fin d'essai de l'org (`trial_ends_at`), poussée par le commerce

Le graphe interne est un **DAG à deux étages**, sans cycle possible :

    étage 0 (feuilles) : orgs   members   vault   settings   instructions   essai
                          │  ╲   ╱   │                        │      │
    étage 1              │   ╳      │                         │      │
                       personal  invitations       instruction_ownership  library

soit `personal → {orgs, members}`, `invitations → {orgs, members}`,
`library → instructions`, `instruction_ownership → instructions`. Les feuilles
n'importent aucun frère.

⚠️ **Aucun module du package n'importe `group_store`** (qui, lui, dépend
d'org_store — l'importer ici ferait le cycle). L'invariant org↔groupe est tenu en
SQL direct dans `members`, et les paliers voisins (`roles`, `group_store`,
`mfa_mirror`, `discovery`) restent en import PARESSEUX au point d'appel. Le
cliquet `tests/test_org_store_surface_frozen.py` vérifie les deux règles.

⚠️ **Aucun module de ce package n'est importé par son nom ailleurs** — c'est
l'effet recherché du ré-export : les appelants écrivent `org_store.<fn>`. Un
inventaire qui cherche des IMPORTS STATIQUES les déclare donc tous orphelins, et
c'est un faux positif systématique (même piège que le package `db`, note du
27/08). Pour juger qu'un module d'ici est mort, chercher ses SYMBOLES
(`org_store.<fn>`, y compris posés par `monkeypatch.setattr`), jamais son nom de
module — puis croiser avec `tests/test_org_store_surface_frozen.py`.

**Ré-export plat + report des écritures.** Ce `__init__` recopie l'intégralité du
namespace des sous-modules (publics ET privés consommés dehors — `_connect`,
`_snippet`, `BASE_SLUG`…) pour que `org_store.<nom>` reste ce qu'il était. Il va
un cran plus loin que la façade du package `db` : **poser un attribut sur la
façade le pose AUSSI sur le(s) module(s) qui le détiennent**. Sans ça, un
`monkeypatch.setattr(org_store, "create_org", …)` — la forme employée par une
dizaine de tests — écrirait sur la façade pendant que `personal.py` continuerait
d'appeler le vrai `orgs.create_org` : le stub serait MORT SILENCIEUSEMENT, et
c'est exactement le faux vert qu'une découpe ne doit pas introduire. Le report
est ce qui rend « surface figée » vrai jusque sous les tests.

Le report DESCEND, il ne remonte pas : une écriture sur un sous-module
(`org_store.vault.get_org_secret`) n'atteint pas qui lit la façade. Dehors, on lit et
on patche donc `org_store.<nom>`, jamais un sous-module — règle de #896, gardée à zéro
par `tests/test_facades_lecteurs_cible.py` (même cible que le paquet `access`).
"""
from __future__ import annotations

import logging
import sys as _sys
from types import ModuleType as _ModuleType

from . import (  # noqa: F401  (ré-exportés en masse ci-dessous)
    essai,
    instruction_ownership,
    instructions,
    instructions_cycle,
    invitations,
    library,
    members,
    orgs,
    personal,
    settings,
    vault,
)

# nom -> modules qui le détiennent (un même nom peut vivre dans plusieurs, ex.
# `_connect`, importé par les 8). Sert au ré-export ET au report d'écriture.
_OWNERS: dict = {}
_g = globals()
for _mod in (orgs, members, vault, settings, personal, invitations, instructions,
             instructions_cycle, instruction_ownership, library, essai):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            _g[_name] = getattr(_mod, _name)
            _OWNERS.setdefault(_name, []).append(_mod)
del _g, _mod, _name

# Le logger de la façade garde le nom historique `oto_mcp.org_store` (les
# sous-modules loguent sous leurs noms fils, mêmes handlers, meilleure attribution).
_log = logging.getLogger(__name__)


class _Facade(_ModuleType):
    """Module `org_store` : ré-export plat dont les écritures redescendent.

    `org_store.X = v` pose `v` sur la façade **et** sur chaque sous-module qui
    détient `X`. Les appels cross-module du package passent tous par
    `<module>.<nom>` (jamais par un nom importé à plat), donc un stub posé sur la
    façade est vu par tout le package — comme au temps du fichier unique.
    """

    def __setattr__(self, name, value):
        for _owner in _OWNERS.get(name, ()):
            setattr(_owner, name, value)
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _Facade
del _sys, _ModuleType
