"""Changer le propriétaire d'un compte (#1499, règle d'oto-core « changer son propriétaire
exige la gestion avant et après »).

Le compte reste le même objet : sa ligne de coffre change d'entité (`entity_type`,
`entity_id`) EN PLACE — le secret chiffré ne sort jamais du coffre, n'est ni lu ni copié —,
son instance garde son identifiant, ses règles et son journal. Dans la même transaction :

- les règles qui n'ont plus de sens tombent (journalisées `removed` par le déclencheur) :
  sur un compte devenu personnel, la règle d'org et celle qui nommerait son nouveau
  propriétaire (`owner_subject`) ;
- le défaut porté par la ligne tombe si le nouveau palier a déjà le sien ;
- le journal des partages note le transfert (`action='transferred'`, le nouveau
  propriétaire en sujet, l'ancien en `owner_before`).

Les DROITS (gérer avant et après, rendre personnel réservé à l'admin d'org) sont jugés par
la capacité (`capabilities/connectors/transfert.py`), pas ici.
"""
from __future__ import annotations

from typing import Optional

from ._conn import _connect

#: Les propriétaires entre lesquels un compte se transfère, dans une même org.
PROPRIETAIRES = ("member", "group", "org")


class TransfertRefuse(Exception):
    """Un transfert que la base refuse, nommé : `code` est le code déclaré du refus."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sujet(owner_type: str, owner_id: str) -> tuple[str, Optional[str]]:
    if owner_type == "member":
        return "user", owner_id.partition(":")[2]
    if owner_type == "group":
        return "group", owner_id
    return "org", None


def _org_du_proprietaire(conn, owner_type: str, owner_id: str) -> int:
    if owner_type == "member":
        return int(owner_id.partition(":")[0])
    if owner_type == "org":
        return int(owner_id)
    return conn.execute("SELECT org_id FROM org_groups WHERE id = %s",
                        (int(owner_id),)).fetchone()["org_id"]


def transferer(instance_id: int, *, vers_type: str, vers_id: str, acteur: str) -> dict:
    """Fait passer le compte `instance_id` à `(vers_type, vers_id)`. Rend
    `{instance_id, owner_before, owner_after, rules_removed, default_dropped}`.
    Lève `TransfertRefuse` (`not_found`, `not_transferable`, `same_owner`,
    `account_name_taken`)."""
    if vers_type not in PROPRIETAIRES:
        raise ValueError(f"unknown owner kind {vers_type!r}")
    vers_id = str(vers_id)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('oto.acteur', %s, true)", (acteur,))
            inst = conn.execute(
                "SELECT id, connector, owner_type, owner_id, account FROM connector_instances "
                "WHERE id = %s AND revoked_at IS NULL FOR UPDATE", (int(instance_id),)
            ).fetchone()
            if inst is None:
                raise TransfertRefuse("not_found", f"no live account #{instance_id}.")
            avant = (inst["owner_type"], inst["owner_id"])
            if avant == (vers_type, vers_id):
                raise TransfertRefuse("same_owner", "the account already belongs there.")
            ligne = conn.execute(
                "SELECT meta FROM connector_credentials WHERE entity_type = %s "
                "AND entity_id = %s AND connector = %s AND account = %s FOR UPDATE",
                (*avant, inst["connector"], inst["account"])).fetchone()
            if ligne is None or avant[0] not in PROPRIETAIRES:
                # Une sous-instance de canal (compte de messagerie hébergée) n'a pas de
                # ligne de coffre : c'est la connexion d'une personne, elle ne se donne pas.
                raise TransfertRefuse(
                    "not_transferable",
                    "this account has no key of its own to hand over (a hosted messaging "
                    "account is its person's login): it cannot change owner.")
            if conn.execute(
                    "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
                    "AND entity_id = %s AND connector = %s AND account = %s",
                    (vers_type, vers_id, inst["connector"], inst["account"])).fetchone():
                raise TransfertRefuse(
                    "account_name_taken",
                    f"the new owner already has a `{inst['connector']}` account named "
                    f"“{inst['account'] or '(default)'}”: rename one of them first.")
            # Les règles qui n'ont plus de sens, avant de bouger l'instance (la garde
            # `owner_subject` refuserait sinon le nouvel état).
            retirees = 0
            if vers_type == "member":
                cur = conn.execute(
                    "DELETE FROM connector_account_rules WHERE instance_id = %s "
                    "AND (subject_org OR subject_user = %s)",
                    (inst["id"], _sujet(vers_type, vers_id)[1]))
                retirees = cur.rowcount or 0
            conn.execute(
                "UPDATE connector_credentials SET entity_type = %s, entity_id = %s "
                "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
                (vers_type, vers_id, *avant, inst["connector"], inst["account"]))
            conn.execute("UPDATE connector_instances SET owner_type = %s, owner_id = %s "
                         "WHERE id = %s", (vers_type, vers_id, inst["id"]))
            defaut_perdu = False
            if (ligne["meta"] or {}).get("is_default") and conn.execute(
                    "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
                    "AND entity_id = %s AND connector = %s AND account <> %s "
                    "AND meta->>'is_default' = 'true'",
                    (vers_type, vers_id, inst["connector"], inst["account"])).fetchone():
                conn.execute(
                    "UPDATE connector_credentials SET meta = (meta - 'default_set_by' "
                    "- 'default_set_at') || '{\"is_default\": false}' WHERE entity_type = %s "
                    "AND entity_id = %s AND connector = %s AND account = %s",
                    (vers_type, vers_id, inst["connector"], inst["account"]))
                defaut_perdu = True
            org = _org_du_proprietaire(conn, vers_type, vers_id)
            kind, sid = _sujet(vers_type, vers_id)
            conn.execute(
                "INSERT INTO connector_account_share_events (org_id, instance_id, action, "
                "subject_kind, subject_id, level, owner_before, actor_sub) "
                "VALUES (%s, %s, 'transferred', %s, %s, 'manage', %s, %s)",
                (org, inst["id"], kind, sid, f"{avant[0]}:{avant[1]}", acteur))
    return {"instance_id": inst["id"], "owner_before": f"{avant[0]}:{avant[1]}",
            "owner_after": f"{vers_type}:{vers_id}", "rules_removed": retirees,
            "default_dropped": defaut_perdu}
