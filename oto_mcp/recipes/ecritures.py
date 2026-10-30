"""Les outils qui ÉCRIVENT chez un tiers et qu'une recette-code peut appeler (#1202,
lot 3) — une classe FORCÉE pour quelques outils, et la règle générale pour tous les autres.

**Règle générale (28/10/2026)** : une recette-code tourne avec les droits de QUI
L'EXÉCUTE. Tout outil de connecteur (ni outil de plateforme, ni connecteur à modèle) lui
est donc ouvert, classé par le verbe de son op (`recipes/effets.py`) :

- une op qui LIT (verbe de `effets.LIRE`, ou un outil déclaré `LECTURE`) passe sans
  déclaration, en épreuve comprise ;
- une op qui ÉCRIT ou ENVOIE doit être DÉCLARÉE par la recette (`writes:
  ["slack_act:send", …]`), n'est jamais jouée en épreuve, reste dans l'org du run et
  dans la limite de `max_writes` (et de `max_sends` pour un envoi, s'il est posé) ; un
  envoi exige en plus `allow_sending: true` ;
- une op qui DÉTRUIT (`delete`, `merge`, `bulk_*`…) est refusée : une recette relancée
  repart du début, ses écritures doivent être sans dégât.

**`ECRITURES` reste la classe FORCÉE de ses outils**, décidée outil par outil : chacun
avec SES ops —

- `lire` : les ops qui ne changent rien chez le tiers (chercher un doublon avant de
  créer). Appelables sans déclaration, en épreuve comprise.
- `ecrire` : les ops qui créent ou mettent à jour une fiche. Chacune doit être DÉCLARÉE.

Tout autre op d'un outil de cette liste est refusée, quel que soit son verbe (une file
d'Allo remplacée par `create_queue`, un SMS). Un LOT d'une op permise (`items` / `ids` /
`entries`, 50 fiches au plus) passe : le pont le compte fiche par fiche dans
`max_writes` (`pont._fiches`). Un outil multiplexé par `op` exige une `op` LITTÉRALE
dans l'appel (son défaut est une lecture, jamais présumé). Un outil sans `op`
(`ops=None`) est une écriture en entier. Un outil qui peut déclencher un ENVOI (une
piste ajoutée à une campagne active reçoit sa séquence) est un envoi.

⚠️ `tests/test_recettes_ecritures.py` vérifie que chaque outil forcé est servi, que
chaque op est une op qu'il connaît, et qu'aucune ne détruit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .. import providers
from ..tool_visibility import namespace_of
from . import effets


@dataclass(frozen=True)
class Ecriture:
    #: Ops qui lisent (permises sans déclaration) et ops qui écrivent (à déclarer).
    #: `None` pour les deux : l'outil n'a pas d'`op`, il écrit.
    lire: Optional[frozenset] = None
    ecrire: Optional[frozenset] = None
    envoie: bool = False


ECRITURES: dict[str, Ecriture] = {
    "hubspot_object": Ecriture(frozenset({"search", "get"}), frozenset({"create", "update"})),
    "folk_record": Ecriture(frozenset({"search", "get"}),
                            frozenset({"create", "update", "add_to_group"})),
    "attio_record": Ecriture(frozenset({"search", "get"}), frozenset({"create", "update"})),
    "pipedrive_record": Ecriture(frozenset({"get"}), frozenset({"create", "update"})),
    "salesforce_record": Ecriture(frozenset({"get"}),
                                  frozenset({"create", "update", "upsert"})),
    "lemlist_create_lead": Ecriture(envoie=True),
    # Allo : fiche CRM et file du Power Dialer ; ni envoi (`send_sms`) ni remplacement
    # de file (`create_queue`). Ses lectures sont l'outil `allo`, déjà en lecture.
    # `add_queue_numbers` ne dédoublonne pas chez Allo : la recette lit la file avant.
    "allo_act": Ecriture(frozenset(), frozenset({"create_person", "update_person",
                                                 "create_company", "update_company",
                                                 "add_queue_numbers"})),
    # Multiplexé avec des ops qui écrivent (create, pause, export_email…) : il ne peut
    # pas se déclarer `LECTURE` en entier. Ses comptes et ses exports se LISENT — ce
    # qu'une recette compare aux leads d'un tableau avant de pousser. Rien à déclarer.
    # Pas `export_status` : sans `export_start`, une recette n'en a jamais l'id.
    # Entrée TRANSITOIRE tant que lemlist est dans `A_MIGRER`
    # (`tests/test_convention_act.py`) : à sa paire, ces ops vont au lecteur `lemlist`.
    "lemlist_campaign": Ecriture(frozenset({"reports", "batch_stats", "export_leads",
                                            "statutes"}), frozenset()),
    # Même cas, même transition : des lecteurs multiplexés avec des ops qui écrivent.
    # Les prises d'une watch list (`signals`), avant de chercher qui contacter ; la liste
    # de désinscription et les fiches du CRM lemlist, avant de pousser — le garde-fou
    # « déjà exclu » / « déjà connu » sans modèle. Rien à déclarer : `create`, `update`,
    # `delete`, `push_signals`, `add`, `var_*` d'écriture, `upsert`, `list_create`,
    # `list_manage` restent refusés.
    "lemlist_watchlist": Ecriture(frozenset({"list", "filters", "filter_values",
                                             "library", "history", "signals"}),
                                  frozenset()),
    "lemlist_unsubscribe": Ecriture(frozenset({"list", "get", "export", "var_list",
                                               "var_get", "var_export", "contact_status",
                                               "contact_export"}), frozenset()),
    "lemlist_contact": Ecriture(frozenset({"list", "get", "lists", "list_export"}),
                                frozenset()),
}

#: Ce qu'aucune op permise ne peut être : détruire, fusionner, une op `bulk_*` (un lot
#: passe par `items` / `ids` d'une op permise, compté par fiche au pont).
OPS_DESTRUCTRICES = frozenset({"delete", "merge", "bulk_create", "bulk_update",
                               "bulk_delete", "bulk_upsert"})


def nom_declare(outil: str, op: Optional[str]) -> str:
    """Le nom d'une écriture dans `writes` : `outil:op`, ou `outil` sans op."""
    return f"{outil}:{op}" if op else outil


def declarables() -> frozenset:
    """Tout ce qu'une recette peut déclarer dans `writes`."""
    out = set()
    for outil, e in ECRITURES.items():
        if e.ecrire is None:
            out.add(outil)
        else:
            out.update(nom_declare(outil, op) for op in e.ecrire)
    return frozenset(out)


def envoient(declarees) -> list[str]:
    """Les écritures déclarées qui peuvent déclencher un envoi : un outil forcé qui envoie,
    ou le verbe d'envoi d'un autre (`effets.ENVOI`)."""
    def envoie(d: str) -> bool:
        outil, _, op = d.partition(":")
        if outil in ECRITURES:
            return ECRITURES[outil].envoie
        return effets.classer(outil, op or None) == "envoyer"
    return sorted(d for d in declarees if envoie(d))


def juger(outil: str, arguments: dict) -> tuple[str, Optional[str]]:
    """`("lire" | "ecrire", nom déclaré)` pour un appel d'un outil de la liste, ou
    `("refus", message)`. L'outil DOIT être dans `ECRITURES`."""
    e = ECRITURES[outil]
    if e.ecrire is None:
        return "ecrire", nom_declare(outil, None)
    op = arguments.get("op")
    if not isinstance(op, str) or not op:
        return "refus", (f"`{outil}` reads or writes depending on its `op`: pass it "
                         f"literally (reads: {sorted(e.lire or ())}, writes: "
                         f"{sorted(e.ecrire)}).")
    if op in (e.lire or ()):
        return "lire", None
    if op in e.ecrire:
        return "ecrire", nom_declare(outil, op)
    return "refus", (f"op `{op}` of `{outil}` is not open to a recipe (reads: "
                     f"{sorted(e.lire or ())}, writes: {sorted(e.ecrire)}; never delete, "
                     "merge or a bulk_* op — batch a permitted op with `items` / `ids`).")


#: Le nom d'une écriture déclarée : `outil:op`, ou `outil` pour un outil sans op.
_NOM_DECLARE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_]+)?$")


def hors_connecteur(outil: str) -> Optional[str]:
    """Le refus d'un outil qui n'est pas un outil de connecteur sans modèle, sinon `None`."""
    from . import contrat
    ns = namespace_of(outil)
    if ns in contrat.NAMESPACES_INTERDITS:
        return f"`{outil}` is a platform tool: a recipe only calls connector tools."
    if ns in contrat.NAMESPACES_A_MODELE:
        return f"`{outil}` runs a model: a recipe never calls one."
    if not _est_connecteur(ns):
        return f"`{outil}` is not a connector tool."
    return None


def _est_connecteur(ns: str) -> bool:
    return providers.connector_for_namespace(ns) is not None


def _refus_destruction(nom: str) -> str:
    return (f"`{nom}` deletes, merges or acts in bulk: never open to a recipe, which "
            "starts over from the top when it runs again.")


def classer_appel(outil: str, arguments: dict, *,
                  multiplexe: bool) -> tuple[str, Optional[str]]:
    """`("lire" | "ecrire" | "envoyer", nom déclaré)` pour un appel d'outil de connecteur
    qui n'est pas déclaré `LECTURE`, ou `("refus", message)`. Un outil forcé suit sa
    classe (`juger`) ; tout autre suit le verbe de son op (`effets`). `multiplexe` : l'outil
    prend une `op`, qui doit alors être littérale."""
    if outil in ECRITURES:
        sens, nom = juger(outil, arguments)
        if sens == "ecrire" and ECRITURES[outil].envoie:
            return "envoyer", nom
        return sens, nom
    refus = hors_connecteur(outil)
    if refus:
        return "refus", refus
    op = arguments.get("op") if multiplexe else None
    if multiplexe and (not isinstance(op, str) or not op):
        return "refus", (f"`{outil}` acts depending on its `op`: pass it literally, and "
                         "declare `tool:op` in `writes` when it writes.")
    nom = nom_declare(outil, op)
    classe = effets.classer(outil, op)
    if classe == "detruire":
        return "refus", _refus_destruction(nom)
    return classe, nom


def probleme_de_declaration(nom: str) -> Optional[str]:
    """Pourquoi `nom` ne peut pas figurer dans `writes`, ou `None`."""
    if not isinstance(nom, str) or not _NOM_DECLARE.match(nom):
        return f"`{nom}`: a write is named `tool:op`, or `tool` for a tool without `op`"
    outil, _, op = nom.partition(":")
    if outil in ECRITURES:
        if nom in declarables():
            return None
        ouvertes = sorted(d for d in declarables() if d.split(":", 1)[0] == outil)
        return f"`{nom}` is not open to a recipe (for `{outil}`: {ouvertes})"
    refus = hors_connecteur(outil)
    if refus:
        return refus
    classe = effets.classer(outil, op or None)
    if classe == "lire":
        return f"`{nom}` reads: call it without declaring it"
    if classe == "detruire":
        return _refus_destruction(nom)
    return None
