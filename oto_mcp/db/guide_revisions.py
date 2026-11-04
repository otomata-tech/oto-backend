"""Les versions gardées d'une couche de contexte — guide à charger ou readme injecté
(DDL : `schema/guides.py::GUIDE_REVISIONS`).

Jusqu'au 03/11/2026, une écriture de guide REMPLAÇAIT sur place (`db/guides.py`) : la
garde `expected_rev` empêchait d'écraser à l'aveugle, rien ne rendait ce qui avait été
écrasé en connaissance de cause, ni un guide supprimé. Désormais chaque remplacement
(`set_guide_db`, `patch_guide_db`, `set_init_guide_db`) et chaque suppression
(`delete_guide_db`) garde ici l'état PRÉCÉDENT, dans la transaction même de
l'écriture : l'écriture et sa version passent ou échouent ensemble.

- **Pas de version sans état précédent** : une première écriture (rien avant), un
  readme vide, ou une réécriture au même contenu (même `rev`) n'en laissent pas.
- **L'identité est la clé naturelle** `(owner_type, owner_id, slug, delivery)`, pas
  l'id du nœud : un guide supprimé n'a plus de nœud, et c'est alors que sa dernière
  version compte le plus.
- **Rétention au fil de l'écriture** (`_purger`, même transaction, aucune tâche
  planifiée) : une version reste si elle a moins de `GARDER_JOURS` jours OU si elle
  est parmi les `GARDER_DERNIERES` plus récentes de sa couche — le plus généreux des
  deux.
- **Restaurer** n'est pas écrit ici : c'est une écriture ordinaire du contenu d'une
  version (`capabilities/guides.py`), avec ses droits et sa garde, donc elle-même
  versionnée (`reason='restore'`).

Non aplati dans `db.*` : `lister`/`lire` sont trop communs — `db.guide_revisions.<fn>`.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

# Pourquoi une version a été gardée : le geste qui a remplacé (ou emporté) cet état.
# `write` = un guide à charger réécrit (op=write ou op=patch) ; `init` = un readme
# réécrit ; `delete` = un guide supprimé ou un readme vidé ; `restore` = une version
# restaurée par-dessus. Le CHECK de la table porte la même liste.
RAISONS = ("write", "init", "delete", "restore")

GARDER_JOURS = 30
GARDER_DERNIERES = 50

_COUCHE = "owner_id = %s AND owner_type = %s AND slug = %s AND delivery = %s"


class RevisionInconnue(LookupError):
    """Aucune version gardée de cette couche ne porte ce `rev` — jamais remplacée par
    une autre, jamais devinée : l'appelant relit la liste (`lister`)."""

    def __init__(self, rev: str):
        self.rev = rev
        super().__init__(f"aucune version gardée au rev `{rev}`")


def garder(conn, avant: dict, *, delivery: str, rev: str, reason: str,
           edited_by: Optional[str]) -> None:
    """Garde `avant` (une ligne `db/guides._COLS`, lue sous verrou dans `conn`) comme
    version de sa couche, puis purge ce que la rétention ne garde plus — dans la
    transaction de l'écriture qui le remplace. `rev` = son empreinte, déjà calculée
    par l'appelant (`db.rev_de_couche`)."""
    if reason not in RAISONS:
        raise ValueError(f"raison de version inconnue : {reason!r}")
    cle = (str(avant["owner_id"]), avant["scope"], avant["slug"], delivery)
    conn.execute(
        "INSERT INTO guide_revisions (owner_id, owner_type, slug, delivery, title, "
        "description, body_md, rev, reason, edited_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (*cle, avant.get("title") or "", avant.get("description") or "",
         avant.get("body_md") or "", rev, reason, edited_by))
    _purger(conn, cle)


def _purger(conn, cle: tuple) -> None:
    """Retire les versions de la couche qui ont plus de `GARDER_JOURS` jours ET ne sont
    pas parmi ses `GARDER_DERNIERES` plus récentes. Par l'index de la couche."""
    conn.execute(
        f"DELETE FROM guide_revisions WHERE {_COUCHE} "
        "AND created_at < NOW() - make_interval(days => %s) "
        f"AND id NOT IN (SELECT id FROM guide_revisions WHERE {_COUCHE} "
        "                ORDER BY id DESC LIMIT %s)",
        (*cle, GARDER_JOURS, *cle, GARDER_DERNIERES))


def lister(owner_type: str, owner_id: str, slug: str, delivery: str) -> list[dict]:
    """Les versions gardées d'une couche, la plus récente d'abord — sans les corps :
    `{rev, title, reason, edited_by, created_at, body_md_bytes}`."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT rev, title, reason, edited_by, created_at, "
            "       octet_length(body_md) AS body_md_bytes "
            f"FROM guide_revisions WHERE {_COUCHE} ORDER BY id DESC",
            (str(owner_id), owner_type, slug, delivery)).fetchall()
    return [dict(r) for r in rows]


def lire(owner_type: str, owner_id: str, slug: str, delivery: str, rev: str) -> dict:
    """Le contenu de la version `rev` de la couche (la plus récente qui le porte : deux
    versions au même rev ont le même contenu). Lève `RevisionInconnue`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT rev, title, description, body_md, reason, edited_by, created_at "
            f"FROM guide_revisions WHERE {_COUCHE} AND rev = %s ORDER BY id DESC LIMIT 1",
            (str(owner_id), owner_type, slug, delivery, rev)).fetchone()
    if row is None:
        raise RevisionInconnue(rev)
    return dict(row)
