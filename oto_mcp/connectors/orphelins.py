"""Références orphelines : une ligne qui NOMME un connecteur absent du catalogue servi (#905).

Le catalogue des connecteurs vit dans le code (`providers.REGISTRY`) ; les tables qui en
nomment un portent un texte libre, sans clé étrangère. Renommer ou retirer un connecteur
ne met donc rien à jour, et une installation qui ne résout plus se taisait au chargement :
119 installations ont tourné à vide pendant sept tags (#295).

Ce module ne répare rien, il SIGNALE :

- `signaler(table, noms)` : appelé par les lectures qui CHARGENT des références (la
  sélection d'un membre, l'exposition, les credentials d'une entité, les réglages, le kit
  d'une org quand il équipe un nouveau membre). Un nom
  absent du catalogue est journalisé en ERREUR, une fois par (table, nom) et par
  processus, en nommant le connecteur et la table. La lecture rend ses lignes telles
  quelles : pas de repli, pas de suppression ; un nom orphelin ne monte toujours aucun
  outil, mais il ne disparaît plus sans un mot.
- `constat()` : la lecture, seule et bornée, de TOUTES les tables qui nomment un
  connecteur, par table et par nom. Servie à l'admin plateforme par
  `oto_admin_monitoring op=connecteurs_orphelins`.

Hors de son périmètre : le catalogue en table et les clés étrangères (le neuf, ailleurs).

Exclus du constat, et pourquoi :
- un credential (et son instance) REMOTE, défini par sa donnée (`meta.base_url`) et non
  par le registre (`credentials_store.set_credential`) : il n'a pas de carte à résoudre ;
- les JOURNAUX qui nomment un connecteur (`credential_disparitions`, `platform_key_rate`,
  `access_shadow_l7`, `tool_calls`) : une trace du passé, pas une référence à résoudre ;
- le budget d'unités (`credit_scale_lines`, `credit_holds`, `credit_movements`) : un
  barème versé par le commerce et ses traces ; une ligne sans connecteur servi ne
  s'applique simplement à aucun appel.

Feuille : aucun import d'`oto_mcp` au niveau du module (`providers` et `db` en tardif),
pour que les lecteurs du coffre, de la sélection et des réglages puissent l'appeler.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable

logger = logging.getLogger(__name__)

# Les (table, nom) déjà journalisés par CE processus. Un nom orphelin se relit à chaque
# chargement de toolbox : sans mémoire, une seule ligne morte inonderait le journal.
_SIGNALES: set[tuple[str, str]] = set()
_VERROU = threading.Lock()

# Un credential remote se définit par sa donnée, pas par le registre (cf. l'en-tête).
_PAS_REMOTE = "COALESCE(meta->>'base_url', '') = ''"

# Les tables qu'un autre module NOMME (le nettoyage, `orphelins_nettoyage`) : le nom de la
# table des instances ne s'écrit que dans un module admis à la connaître (garde L6,
# `tests/test_connector_instances_l6.py`).
CREDENTIALS = "connector_credentials"
INSTANCES = "connector_instances"
KIT = "orgs.default_connectors"

# Les références vivantes à un connecteur : (table, SQL du constat). Chaque requête rend
# `connecteur, lignes` pour les noms absents de `%(catalogue)s`, et ne lit que sa table.
REFERENCES: tuple[tuple[str, str], ...] = (
    ("user_selected_connectors",
     "SELECT connector AS connecteur, count(*) AS lignes FROM user_selected_connectors "
     "WHERE connector <> ALL(%(catalogue)s) GROUP BY connector"),
    ("connector_selection_removed",
     "SELECT connector AS connecteur, count(*) AS lignes FROM connector_selection_removed "
     "WHERE connector <> ALL(%(catalogue)s) GROUP BY connector"),
    ("connector_availability",
     "SELECT connector AS connecteur, count(*) AS lignes FROM connector_availability "
     "WHERE connector <> ALL(%(catalogue)s) GROUP BY connector"),
    (CREDENTIALS,
     "SELECT connector AS connecteur, count(*) AS lignes FROM connector_credentials "
     f"WHERE connector <> ALL(%(catalogue)s) AND {_PAS_REMOTE} GROUP BY connector"),
    ("connector_settings",
     "SELECT connector AS connecteur, count(*) AS lignes FROM connector_settings "
     "WHERE connector <> ALL(%(catalogue)s) GROUP BY connector"),
    (INSTANCES,
     "SELECT i.connector AS connecteur, count(*) AS lignes FROM connector_instances i "
     "WHERE i.revoked_at IS NULL AND i.connector <> ALL(%(catalogue)s) "
     "AND NOT EXISTS (SELECT 1 FROM connector_credentials c "
     "                 WHERE c.entity_type = i.owner_type AND c.entity_id = i.owner_id "
     "                   AND c.connector = i.connector AND c.account = i.account "
     "                   AND COALESCE(c.meta->>'base_url', '') <> '') "
     "GROUP BY i.connector"),
    (KIT,
     "SELECT k AS connecteur, count(*) AS lignes "
     "FROM orgs, unnest(default_connectors) AS k "
     "WHERE k <> ALL(%(catalogue)s) GROUP BY k"),
)


def catalogue() -> frozenset[str]:
    """Les noms du catalogue SERVI par ce processus."""
    from .. import providers

    return frozenset(providers.REGISTRY)


def signaler(table: str, noms: Iterable[str]) -> frozenset[str]:
    """Journalise en erreur chaque nom de `noms` absent du catalogue, une fois par
    `(table, nom)` et par processus. Rend les noms orphelins vus dans cet appel (déjà
    journalisés ou non) ; ne change rien à ce que l'appelant lit."""
    from .. import providers

    # Lu sur des chemins d'appel (la cascade des clés) : le registre tel quel, sans copie.
    orphelins = frozenset(n for n in noms if n not in providers.REGISTRY)
    if not orphelins:
        return orphelins
    with _VERROU:
        neufs = sorted(n for n in orphelins if (table, n) not in _SIGNALES)
        _SIGNALES.update((table, n) for n in neufs)
    for nom in neufs:
        logger.error(
            "connecteur orphelin : la table %s nomme %r, absent du catalogue servi — "
            "la référence ne résout rien et reste en base (rien n'est supprimé). "
            "Constat complet : oto_admin_monitoring op=connecteurs_orphelins (#905)",
            table, nom)
    return orphelins


def constat() -> dict:
    """Les références orphelines de chaque table, par nom — lecture seule, bornée.

    `{"catalogue": n, "tables": [{"table", "connecteurs": [{"connecteur", "lignes"}]}],
    "total": lignes}` ; une table sans orphelin figure avec une liste vide, pour qu'un
    zéro se lise comme « regardé », jamais comme « oublié »."""
    from ..db import lecture_bornee

    connus = sorted(catalogue())
    tables = []
    total = 0
    with lecture_bornee.lecture_d_agregat("constat des connecteurs orphelins",
                                          isolation="REPEATABLE READ") as conn:
        for table, sql in REFERENCES:
            rows = conn.execute(sql, {"catalogue": connus}).fetchall()
            lignes = sorted(({"connecteur": r["connecteur"], "lignes": int(r["lignes"])}
                             for r in rows), key=lambda d: (-d["lignes"], d["connecteur"]))
            total += sum(d["lignes"] for d in lignes)
            tables.append({"table": table, "connecteurs": lignes})
    return {"catalogue": len(connus), "tables": tables, "total": total}
