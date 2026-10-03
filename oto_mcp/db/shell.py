"""Le RAIL — les nœuds qu'une personne voit, rangés en sections, en deux requêtes.

Surface de lecture PRÉCOCE du modèle de nœuds (décision du 16/08 : « faire vivre les
deux en parallèle »), contractée avec le front dans `shell-contract.md`. Ce module ne
rend que des LIGNES ; le rangement en sections, l'arbre et les noms vivent au-dessus
(`capabilities/shell.py`) — ici, le SQL et rien d'autre.

⚠️ **Le prédicat `kind <> 'ligne'` est dans CHAQUE requête, pour DEUX raisons — et la
seconde est celle qu'on oublie :**

1. **Le modèle.** 0054-D4 : une ligne de tableau est un nœud ENFANT de son tableau et
   n'apparaît pas dans la spine. Sans le prédicat, le rail affiche le datastore entier
   (43 584 lignes en production). Le front l'avait vu venir, et sa formulation vaut
   d'être gardée : « la panne ressemblerait à un problème de performance, pas de
   modèle » — on chercherait un index pendant que le bug est dans le SELECT.
2. **L'index.** `idx_nodes_owner_scoped` est PARTIEL (`WHERE kind <> 'ligne'`) : une
   lecture par propriétaire qui ne porte pas son genre **ne peut pas l'utiliser** et
   retombe en parcours séquentiel — vérifié et mesuré au lot M4 (16 kB d'index contre
   312 kB nu). Le prédicat n'est donc pas une garde qu'on ajoute par prudence : c'est
   la condition pour que la requête soit indexée.

Figé par `tests/test_shell_v0.py` — le test lit le SQL, pas le résultat : un banc peuplé
de nœuds sans lignes passerait sans rien prouver.
"""
from __future__ import annotations

import hashlib
from typing import Iterable, Optional

from ._conn import _connect
from ._partage_vivant import PARTAGE_VIVANT

# Un nœud du rail : ce qu'il faut pour l'ADRESSER, le RANGER et l'AFFICHER — jamais
# son corps. Le rail est le chrome ; le contenu se lit en ouvrant le nœud.
# `role` voyage à côté du titre : c'est lui qui donne sa NATURE à la ligne du rail
# (une procédure se rend en `agent`), et le lire ici évite une seconde requête par nœud.
# On extrait la clé plutôt que de rendre `props` entier — le rail n'a que faire d'un
# corps, et c'est tout le principe d'une vue de tri.
#
# `legacy`, `legacy_id` et `slug` : la RÉFÉRENCE de procédure d'un nœud agent (#417,
# `capabilities/node_procedure_ref`). Trois clés de plus dans le même SELECT, pas une
# jointure ni une requête par nœud — elles sont dans `props` depuis la conversion.
_COLS = ("n.public_id, n.parent_id, n.id, n.kind, n.owner_type, n.owner_id, "
         "n.position, n.props->>'title' AS title, n.props->>'role' AS role, "
         "n.props->>'legacy' AS legacy, n.props->>'legacy_id' AS legacy_id, "
         "n.props->>'slug' AS slug")

# Le prédicat, écrit UNE fois et interpolé partout : deux endroits qui l'écrivent
# finissent par diverger, et celui qui l'oublie ne le montre pas.
_HORS_LIGNES = "n.kind <> 'ligne'"

# `resource_grants` désigne les objets d'AVANT la conversion ; un nœud converti garde
# sa clé legacy (`props->>'legacy'` / `legacy_id`) et son `public_id` en est DÉRIVÉ.
# Le pont est donc gratuit et bidirectionnel — le re-keying des grants sur `public_id`
# (chantier M-h) reste dû, mais il ne bloque pas cette lecture.
#
# ✅ **La famille du kind `doctrine` existe depuis le 21/08.** Ce commentaire a dit le contraire
# jusque-là — « les procédures ne sont pas encore des nœuds, leur partage est compté et
# rendu à part » — et c'était vrai à l'écriture : `grants_sans_noeud` existait pour
# qu'une section « Partagé » incomplète ne se lise pas comme « rien de partagé ». La
# conversion des procédures (lot ⑧) referme ce trou : les trois natures de partage
# désignent maintenant un nœud, et ce compteur doit rester à zéro.
#
# ⚠️ Il n'est PAS retiré pour autant : c'est lui qui signalera la prochaine nature de
# grant sans nœud, le jour où elle arrivera. Un compteur qu'on retire parce qu'il vaut
# zéro est un compteur qu'on ne remettra pas.
_FAMILLE_PAR_GRANT = {"project": "prj", "datastore_namespace": "tbl",
                      "doctrine": "prc"}


def _public_id_derive(famille: str, legacy_id: str) -> str:
    """L'identifiant public d'un nœud CONVERTI, recalculé depuis sa clé legacy.

    Miroir exact de `db/nodes._public_id_sql` — même formule, autre langage. Le
    dupliquer est un choix : l'alternative serait un `IN` sur une sous-requête qui
    recalcule le md5 en SQL pour chaque grant, alors qu'ils se comptent en dizaines.
    Si la formule bouge, les deux bougent — d'où le test qui compare les deux
    implémentations sur les mêmes entrées plutôt que de figer une constante.
    """
    return "nod_" + hashlib.md5(
        f"{famille}:{legacy_id}".encode(), usedforsecurity=False).hexdigest()[:24]


def nodes_for_owners(owners: Iterable[tuple[str, str]]) -> list[dict]:
    """Les nœuds (hors lignes) possédés par ces couples `(owner_type, owner_id)`.

    UNE requête pour les trois premières sections — org, équipes, soi. Les découper
    par section ferait N+1 sur le chemin le plus chaud du produit, pour un partitionnement
    que Python fait sur la clé qu'il vient de lire.
    """
    owners = list(owners)
    if not owners:
        return []
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM nodes n "
            f"WHERE {_HORS_LIGNES} AND (n.owner_type, n.owner_id) IN "
            f"({','.join(['(%s, %s)'] * len(owners))}) "
            "ORDER BY n.position NULLS LAST, n.props->>'title'",
            [v for pair in owners for v in pair]).fetchall()
    return [dict(r) for r in rows]


def direct_grants(sub: str) -> list[dict]:
    """Les partages reçus EN DIRECT par cette personne — `(user, sub)`, rien d'autre.

    Un grant à une ÉQUIPE ou à l'ORG n'est pas un partage direct : il se range dans la
    section de cette équipe ou dans « tout le monde ». C'est la garantie « pas de
    doublon » du contrat, et elle se joue ici, dans le `principal_type`.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT resource_type, resource_id, granted_by, granted_at, role "
            "FROM resource_grants "
            # Un partage d'AGENT (`runner_trigger`) n'est pas un contenu du rail : il
            # se lit sur l'écran des agents, et gonflerait ici `grants_sans_noeud`.
            f"WHERE principal_type = 'user' AND principal_id = %s AND {PARTAGE_VIVANT} "
            "AND resource_type <> 'runner_trigger'",
            (sub,)).fetchall()
    return [dict(r) for r in rows]


def nodes_by_public_id(public_ids: Iterable[str]) -> list[dict]:
    """Les nœuds désignés, hors lignes. Sert la résolution des partages directs."""
    ids = [i for i in dict.fromkeys(public_ids) if i]
    if not ids:
        return []
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM nodes n "
            f"WHERE {_HORS_LIGNES} AND n.public_id = ANY(%s)", (ids,)).fetchall()
    return [dict(r) for r in rows]


def resolve_grant_nodes(grants: list[dict]) -> tuple[dict, int]:
    """`{public_id: grant}` pour les partages projetables, et le NOMBRE des autres.

    Le second membre n'est pas un détail de journal : c'est ce qui empêche « la section
    Partagé est vide » de se lire comme « personne ne t'a rien partagé » alors que des
    partages existent et ne sont pas encore adressables (les procédures, cf. l'entête).
    """
    par_id: dict = {}
    sans_noeud = 0
    for g in grants:
        famille = _FAMILLE_PAR_GRANT.get(g.get("resource_type") or "")
        if not famille:
            sans_noeud += 1
            continue
        par_id[_public_id_derive(famille, str(g.get("resource_id")))] = g
    return par_id, sans_noeud


def recent_runs(sub: str, org_id: Optional[int], limit: int = 60) -> list[dict]:
    """Les derniers runs d'un (sub, org) — lus du JOURNAL, pas de la table.

    Simple réexport : le rail n'a qu'une porte vers la base, et cette lecture-là est
    déjà écrite (`db/usage.recent_runs`). La réécrire ici produirait une seconde
    définition de « les runs de quelqu'un », qui divergerait au premier correctif.
    """
    from . import usage
    return usage.recent_runs(sub, org_id, limit=limit)


def names_of(subs: Iterable[str]) -> dict[str, str]:
    """`{sub: nom affichable}` — le `sharedBy` du contrat est un NOM, pas un identifiant.

    Repli sur l'email puis sur le sub : une section « Partagé » qui n'ose pas nommer
    l'auteur du partage perd sa seule information utile.
    """
    subs = [s for s in dict.fromkeys(subs) if s]
    if not subs:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, name, email FROM users WHERE sub = ANY(%s)", (subs,)).fetchall()
    return {r["sub"]: (r["name"] or r["email"] or r["sub"]) for r in rows}
